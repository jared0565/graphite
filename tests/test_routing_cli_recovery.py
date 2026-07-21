"""End-to-end CLI recovery boundary and error-sanitization tests."""
from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path
from typing import NoReturn

import pytest

import graphite.cli as cli
import graphite.routing.service as service_module
from graphite.routing.approval import ApprovalAuthority
from graphite.routing.contracts import Effort, ExecutionOutcome, ExecutionReceipt
from graphite.routing.storage import RepositoryStore


_DIGEST = "1" * 64
_PROMPT_HASH = "2" * 64


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("recovery_authority_or_provider_called")


@pytest.fixture(autouse=True)
def _deny_execution_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "execute_claude", _forbidden)
    monkeypatch.setattr(service_module, "execute_codex", _forbidden)
    monkeypatch.setattr(ApprovalAuthority, "issue", _forbidden)
    monkeypatch.setattr(ApprovalAuthority, "consume", _forbidden)


def _attempt(root: Path, state: str) -> tuple[RepositoryStore, ExecutionReceipt]:
    root.mkdir()
    store = RepositoryStore(root)
    store.initialize()
    store.record_task("task-1", "isolated_code", "low", "a" * 64, 1)
    store.record_decision(
        "decision-1", "task-1", "model:cloud", "default", "1", "1", 1
    )
    store.save_approval_record(
        approval_id="approval-1", task_id="task-1", decision_id="decision-1",
        nonce_hash="b" * 64, manifest_hash="c" * 64, expires_at=99,
        reserved_tokens=10,
    )
    store.create_execution_attempt(
        attempt_id="attempt-1", approval_id="approval-1", task_id="task-1",
        decision_id="decision-1", manifest_hash="c" * 64,
        graph_fingerprint="a" * 64, model_id="model:cloud", effort="default",
        reserved_tokens=10, max_input_tokens=8, max_output_tokens=2,
        expected_prompt_hash=_PROMPT_HASH, inventory_digest=_DIGEST, created_at=1,
    )
    store.consume_approval_record(
        approval_id="approval-1", nonce_hash="b" * 64, manifest_hash="c" * 64,
        now=2, token_amount=10, repository_quota=100,
    )
    receipt = ExecutionReceipt(
        "exec-1", "approval-1", "model:cloud", Effort.DEFAULT,
        ExecutionOutcome.SUCCEEDED, 6, 2, 5, _PROMPT_HASH, "d" * 64, None,
    )
    if state == "recoverable":
        store.stage_execution_receipt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest=_DIGEST, staged_at=2,
        )
    elif state == "completed":
        store.finalize_execution_attempt(
            attempt_id="attempt-1", receipt=receipt,
            inventory_digest=_DIGEST, completed_at=2,
        )
    elif state == "legacy":
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE execution_attempts SET status='legacy_unrecoverable', "
                "failure_reason='legacy_attempt_digest_missing' WHERE attempt_id='attempt-1'"
            )
    elif state != "conflict":
        raise AssertionError("test_state_invalid")
    return store, receipt


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_root", "repository_root_invalid"),
        ("malformed", "attempt_id_invalid"),
        ("nonexistent", "execution_attempt_missing"),
        ("legacy", "legacy_attempt_digest_missing"),
        ("conflict", "execution_attempt_conflict"),
        ("unavailable", "storage_corrupt"),
    ],
)
def test_reconcile_expected_failures_are_sanitized_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected: str,
) -> None:
    root = tmp_path / "PRIVATE-ROOT"
    attempt_id = "attempt-1"
    if case == "missing_root":
        pass
    elif case == "malformed":
        root.mkdir()
        attempt_id = "../../PRIVATE-PROMPT"
    elif case == "nonexistent":
        root.mkdir()
    elif case == "unavailable":
        root.mkdir()
        path = root / ".graphite" / "routing" / "events.sqlite3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"PRIVATE RESPONSE: not a database")
    else:
        store, _receipt = _attempt(root, "legacy" if case == "legacy" else "conflict")

    assert cli.main([
        "route", "reconcile", str(root), "--attempt-id", attempt_id, "--json",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": {"code": expected}}
    rendered = captured.err
    assert str(root) not in rendered
    assert "PRIVATE" not in rendered
    assert "Traceback" not in rendered


def test_recoverable_storage_failure_is_fixed_path_free_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "PRIVATE-ROOT"

    assert cli.main(["route", "recoverable", str(missing)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[graphite] route recovery error: repository_root_invalid\n"
    assert str(missing) not in captured.err


@pytest.mark.parametrize("error", [ValueError("programmer_bug"), RuntimeError("bug")])
def test_recovery_error_boundary_does_not_convert_programmer_failures(
    error: Exception,
) -> None:
    with pytest.raises(type(error), match="^" + str(error) + "$"):
        cli._route_recovery_error(error, json_mode=True)  # type: ignore[arg-type]


def test_recovery_error_boundary_sanitizes_known_value_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._route_recovery_error(ValueError("attempt_id_invalid"), json_mode=True) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": {"code": "attempt_id_invalid"}
    }


@pytest.mark.parametrize("error", [ValueError("programmer_bug"), RuntimeError("bug")])
def test_reconcile_handler_propagates_programmer_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    class BrokenService:
        def __init__(self, _path: str) -> None:
            pass

        def reconcile_execution(self, _attempt_id: str) -> NoReturn:
            raise error

    monkeypatch.setattr(cli, "RoutingService", BrokenService)
    args = Namespace(path=".", attempt_id="attempt-1", json=True)

    with pytest.raises(type(error), match="^" + str(error) + "$"):
        cli.cmd_route_reconcile(args)


def test_real_recovery_success_is_idempotent_and_authority_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    _store, receipt = _attempt(root, "recoverable")

    assert cli.main(["route", "recoverable", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "attempts": [{"attempt_id": "attempt-1", "status": "recoverable"}],
        "has_more": False,
        "next_cursor": None,
    }
    command = [
        "route", "reconcile", str(root), "--attempt-id", "attempt-1", "--json",
    ]
    assert cli.main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second == receipt.to_dict()
    assert "PRIVATE" not in json.dumps(first)
    assert "text" not in first


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["--limit", "101"], "recovery_limit_invalid"),
        (["--after", "../PRIVATE-CURSOR"], "recovery_cursor_invalid"),
    ],
)
def test_recoverable_rejects_invalid_pagination_without_reflection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()

    assert cli.main([
        "route", "recoverable", str(root), *arguments, "--json",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": {"code": code}}
    assert "PRIVATE" not in captured.err


def test_recoverable_cli_pages_deterministically_without_silent_truncation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    store, _receipt = _attempt(root, "recoverable")
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for index in (2, 3):
            connection.execute(
                "INSERT INTO tasks VALUES (?, 'isolated_code', 'low', ?, 1)",
                (f"task-{index}", "a" * 64),
            )
            connection.execute(
                "INSERT INTO decisions VALUES (?, ?, 'model:cloud', 'default', '1', '1', 1)",
                (f"decision-{index}", f"task-{index}"),
            )
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, 'consumed', 99, 10)",
                (f"approval-{index}", f"task-{index}", f"decision-{index}",
                 f"{index:064x}", f"{index + 10:064x}"),
            )
            connection.execute(
                "INSERT INTO execution_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "'persistence_failed','execution_persistence_failed',NULL,1,1)",
                (f"attempt-{index}", f"approval-{index}", f"task-{index}",
                 f"decision-{index}", f"{index + 10:064x}", "a" * 64,
                 "model:cloud", "default", 10, 8, 2, _PROMPT_HASH, _DIGEST),
            )
            connection.execute(
                "INSERT INTO staged_execution_receipts VALUES (?,?,?,?,?,'succeeded',"
                "6,2,5,?,?,?,NULL,1)",
                (f"attempt-{index}", f"exec-{index}", f"approval-{index}",
                 "model:cloud", "default", _PROMPT_HASH, "d" * 64, _DIGEST),
            )

    base = ["route", "recoverable", str(root), "--limit", "2", "--json"]
    assert cli.main(base) == 0
    first_output = capsys.readouterr().out
    first = json.loads(first_output)
    assert first == {
        "attempts": [
            {"attempt_id": "attempt-1", "status": "recoverable"},
            {"attempt_id": "attempt-2", "status": "recoverable"},
        ],
        "has_more": True,
        "next_cursor": "attempt-2",
    }
    assert len(first_output.encode("utf-8")) < 512
    assert cli.main([*base[:-1], "--after", first["next_cursor"], "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == {
        "attempts": [{"attempt_id": "attempt-3", "status": "recoverable"}],
        "has_more": False,
        "next_cursor": None,
    }
