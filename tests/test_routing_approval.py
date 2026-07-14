"""Default-No, signed, single-use routing approval tests."""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from graphite.routing.approval import (
    ApprovalAuthority,
    ApprovalError,
    approval_prompt,
)
from graphite.routing.contracts import ApprovalManifest, Effort
from graphite.routing.storage import RepositoryStore


def _manifest(**changes) -> ApprovalManifest:
    values = {
        "approval_id": "approval-1",
        "task_id": "task-1",
        "decision_id": "decision-1",
        "graph_fingerprint": "a" * 64,
        "context_manifest_hash": "b" * 64,
        "inventory_digest": "d" * 64,
        "model_id": "kimi-k2.7-code:cloud",
        "effort": Effort.DEFAULT,
        "max_input_tokens": 8_000,
        "max_output_tokens": 2_000,
        "policy_version": "1",
        "issued_at": 100,
        "expires_at": 200,
        "nonce": "nonce-1",
    }
    values.update(changes)
    return ApprovalManifest(**values)


@pytest.mark.parametrize(
    ("answer", "approved"),
    [
        ("y\n", True),
        ("YES\n", True),
        ("\n", False),
        ("no\n", False),
        ("1\n", False),
        ("approve\n", False),
    ],
)
def test_prompt_defaults_no_and_accepts_only_explicit_yes(answer: str, approved: bool) -> None:
    output = io.StringIO()

    result = approval_prompt(
        stdin=io.StringIO(answer),
        stdout=output,
        stdin_is_tty=True,
        stdout_is_tty=True,
        json_mode=False,
        assume_yes=False,
        ci=False,
    )

    assert result is approved
    assert output.getvalue() == "Approve this Ollama model call? [y/N] "


@pytest.mark.parametrize(
    "changes",
    [
        {"stdin_is_tty": False},
        {"stdout_is_tty": False},
        {"json_mode": True},
        {"assume_yes": True},
        {"ci": True},
    ],
)
def test_noninteractive_json_ci_and_yes_never_grant_consent(changes: dict[str, bool]) -> None:
    values = {
        "stdin": io.StringIO("yes\n"),
        "stdout": io.StringIO(),
        "stdin_is_tty": True,
        "stdout_is_tty": True,
        "json_mode": False,
        "assume_yes": False,
        "ci": False,
    }
    values.update(changes)

    assert approval_prompt(**values) is False


def _authority(tmp_path: Path, now=lambda: 150) -> tuple[ApprovalAuthority, RepositoryStore]:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    store = RepositoryStore(root)
    store.initialize()
    authority = ApprovalAuthority(
        store,
        key_path=tmp_path / "machine" / "approval.key",
        quota_path=tmp_path / "machine" / "quota.sqlite3",
        now=now,
    )
    return authority, store


def test_signed_approval_is_bound_to_every_manifest_field(tmp_path: Path) -> None:
    authority, _ = _authority(tmp_path)
    signed = authority.issue(_manifest())

    authority.verify(signed, _manifest())
    for changed in (
        replace(_manifest(), graph_fingerprint="c" * 64),
        replace(_manifest(), context_manifest_hash="c" * 64),
        replace(_manifest(), inventory_digest="e" * 64),
        replace(_manifest(), model_id="kimi-k2.6:cloud"),
        replace(_manifest(), effort=Effort.LOW),
        replace(_manifest(), max_output_tokens=3_000),
        replace(_manifest(), policy_version="2"),
        replace(_manifest(), expires_at=201),
    ):
        with pytest.raises(ApprovalError, match="approval_manifest_changed"):
            authority.verify(signed, changed)


@pytest.mark.parametrize("digest", (None, "", "A" * 64, "a" * 63, "g" * 64))
def test_approval_manifest_rejects_missing_or_malformed_inventory_digest(
    digest: object,
) -> None:
    values = dict(_manifest().to_dict())
    values["inventory_digest"] = digest
    with pytest.raises((TypeError, ValueError), match="inventory_digest_invalid"):
        ApprovalManifest(**values)


def test_approval_is_single_use_and_reserves_both_quotas(tmp_path: Path) -> None:
    authority, store = _authority(tmp_path)
    signed = authority.issue(_manifest())

    authority.consume(
        signed,
        _manifest(),
        repository_quota_tokens=20_000,
        machine_quota_tokens=30_000,
    )

    assert store.approval_status("approval-1") == "consumed"
    assert store.reserved_token_total() == 10_000
    assert authority.machine_reserved_token_total() == 10_000
    with pytest.raises(ApprovalError, match="approval_reused"):
        authority.consume(
            signed,
            _manifest(),
            repository_quota_tokens=20_000,
            machine_quota_tokens=30_000,
        )


def test_expired_or_exhausted_approval_fails_without_consuming(tmp_path: Path) -> None:
    authority, store = _authority(tmp_path, now=lambda: 201)
    signed = authority.issue(_manifest())

    with pytest.raises(ApprovalError, match="approval_expired"):
        authority.consume(
            signed,
            _manifest(),
            repository_quota_tokens=20_000,
            machine_quota_tokens=30_000,
        )
    assert store.approval_status("approval-1") == "issued"
    assert authority.machine_reserved_token_total() == 0

    authority, store = _authority(tmp_path / "other")
    signed = authority.issue(_manifest())
    with pytest.raises(ApprovalError, match="budget_exhausted"):
        authority.consume(
            signed,
            _manifest(),
            repository_quota_tokens=9_999,
            machine_quota_tokens=30_000,
        )
    assert store.approval_status("approval-1") == "issued"
    assert authority.machine_reserved_token_total() == 0


def test_concurrent_consumers_cannot_reuse_nonce(tmp_path: Path) -> None:
    authority, store = _authority(tmp_path)
    signed = authority.issue(_manifest())

    def consume() -> str:
        try:
            authority.consume(
                signed,
                _manifest(),
                repository_quota_tokens=20_000,
                machine_quota_tokens=30_000,
            )
            return "consumed"
        except ApprovalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: consume(), range(2)))

    assert results == ["approval_reused", "consumed"]
    assert store.reserved_token_total() == 10_000
    assert authority.machine_reserved_token_total() == 10_000


def test_public_signed_approval_contains_no_key_source_or_absolute_path(tmp_path: Path) -> None:
    authority, _ = _authority(tmp_path)

    public = authority.issue(_manifest()).to_dict()
    serialized = str(public)

    assert set(public) == {"manifest", "signature"}
    assert "approval.key" not in serialized
    assert str(tmp_path) not in serialized
    assert "source" not in serialized.casefold()
    assert "response" not in serialized.casefold()
