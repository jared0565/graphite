"""CLI grammar and execution-authority boundary tests."""
from __future__ import annotations

import io
import json

import pytest

import graphite.cli as cli


class _Service:
    calls: list[str] = []

    def __init__(self, path):
        self.path = path

    def recommend(self, *, objective, targets):
        self.calls.append("recommend")
        return type("Recommendation", (), {
            "manual_handoff": False,
            "to_dict": lambda self: {
                "task_id": "task-1", "model_id": "kimi-k2.7-code:cloud",
                "effort": "default", "risk": "low", "estimated_tokens": 100,
                "outbound_manifest": {"items": [], "total_bytes": 0},
                "reasons": ["eligible_ranked"], "recommended_channels": [],
            },
        })()

    def prepare(self, recommendation):
        self.calls.append("prepare")
        return type("Prepared", (), {
            "task_id": "task-1",
            "to_dict": lambda self: {
                "task_id": "task-1", "worktree_id": "worktree-1",
                "provider": "codex", "requested_model": "gpt-5.6-codex",
                "effective_model": "gpt-5.6-codex", "effort": "xhigh",
                "approval_required": True,
            },
        })()

    def decline(self, prepared):
        self.calls.append("decline")
        return {"status": "quarantined"}

    def run_approved(self, prepared, *, approval_granted):
        assert approval_granted is True
        self.calls.append("execute")
        return type("ApprovedExecution", (), {
            "text": "bounded suggestion",
            "to_public_dict": lambda self: {
                "outcome": "succeeded", "execution_id": "exec-1",
            },
        })()

    def status(self):
        self.calls.append("status")
        return {"routing": "ready", "authority": "approval_required"}

    def recoverable_attempts(self, *, limit, after):
        self.calls.append(f"recoverable:{limit}:{after}")
        return type("Page", (), {
            "to_dict": lambda self: {
                "attempts": [{"attempt_id": "attempt-1", "status": "recoverable"}],
                "has_more": False,
                "next_cursor": None,
            },
        })()

    def reconcile_execution(self, attempt_id):
        self.calls.append(f"reconcile:{attempt_id}")
        return {"execution_id": "exec-1", "approval_id": "approval-1", "outcome": "succeeded"}

    def record_outcome(self, **kwargs):
        self.calls.append("record")
        return {"recorded": True}

    def policy(self, **kwargs):
        self.calls.append("policy")
        return {"policy_version": "1", "execution_authority": "approval_required"}

    def accept(self, task_id, *, authority_granted):
        self.calls.append("accept")
        return {"task_id": task_id, "status": "accepted", "commit_id": "a" * 40}

    def reject(self, task_id, *, authority_granted):
        self.calls.append("reject")
        return {"task_id": task_id, "status": "rejected"}

    def cleanup(self, task_id, *, authority_granted):
        self.calls.append("cleanup")
        return {"task_id": task_id, "status": "cleaned"}

    def prepare_review(self, task_id):
        self.calls.append("prepare_review")
        return type("PreparedReview", (), {
            "task_id": task_id,
            "to_dict": lambda self: {
                "task_id": task_id,
                "worktree_id": "review-worktree-1",
                "provider": "claude-code",
                "permission_mode": "read-only",
                "approval_required": True,
            },
        })()

    def run_review_approved(self, prepared, *, approval_granted):
        assert approval_granted is True
        self.calls.append("review")
        return type("ReviewResult", (), {
            "text": "bounded review",
            "to_public_dict": lambda self: {
                "outcome": "succeeded", "execution_id": "review-exec-1",
            },
        })()


class _LifecycleOperator:
    calls: list[str] = []

    def __init__(self, path):
        self.path = path

    def status(self, boundary_digest):
        self.calls.append(f"status:{boundary_digest}")
        return {
            "schema_version": 1,
            "storage_integrity": "ok",
            "observation": {
                "boundary_digest": boundary_digest,
                "provider": "claude-code",
                "runtime_kind": "local-cli",
                "state": "verification_required",
                "lifecycle_identity_digest": "a" * 64,
                "policy_version": "1.0.0",
                "updated_at": 100,
            },
        }

    def list_observations(self, *, limit):
        self.calls.append(f"list:{limit}")
        return {"schema_version": 1, "count": 0, "observations": []}

    def history(self, boundary_digest, *, limit):
        self.calls.append(f"history:{boundary_digest}:{limit}")
        return {"schema_version": 1, "count": 0, "events": []}

    def inspect_policy(self, boundary_digest):
        self.calls.append(f"policy-inspect:{boundary_digest}")
        return {
            "schema_version": 1,
            "provider": "claude-code",
            "runtime_kind": "local-cli",
            "policy_version": "1.0.0",
            "automatic_activation": False,
        }

    def prepare_policy_promotion(self, **kwargs):
        self.calls.append("policy-prepare")
        return {
            "schema_version": 1,
            "candidate_digest": "c" * 64,
            "promotion_requires_separate_human_authority": True,
            "automatic_activation": False,
        }

    def prepare_verification_manifest(self, **kwargs):
        self.calls.append("verification-prepare")
        return {
            "schema_version": 1,
            "manifest_digest": "d" * 64,
            "manifest": {
                "lifecycle_identity_digest": kwargs["lifecycle_identity_digest"],
                "max_attempts": 1,
                "fallback_enabled": False,
            },
            "execution_performed": False,
            "activation_performed": False,
        }


@pytest.fixture(autouse=True)
def _service(monkeypatch: pytest.MonkeyPatch):
    _Service.calls = []
    _LifecycleOperator.calls = []
    monkeypatch.setattr(cli, "RoutingService", _Service)
    monkeypatch.setattr(cli, "LifecycleOperator", _LifecycleOperator)


def test_recommend_is_read_only_and_prints_public_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["route", "recommend", ".", "--objective", "review", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "kimi-k2.7-code:cloud"
    assert _Service.calls == ["recommend"]
    assert "source" not in str(payload).casefold()


@pytest.mark.parametrize("extra", [["--json"], ["--yes"]])
def test_json_and_yes_never_grant_execution_consent(
    extra: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    assert cli.main(["route", "run", ".", "--objective", "review", *extra]) == 2
    assert _Service.calls == ["recommend", "prepare", "decline"]


def test_interactive_run_displays_budget_then_prompts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    stdin = _TTY("yes\n")
    stdout = _TTY()
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.delenv("CI", raising=False)
    assert cli.main(["route", "run", ".", "--objective", "review"]) == 0
    assert _Service.calls == ["recommend", "prepare", "execute"]
    output = stdout.getvalue()
    assert output.count("Approve this development model call?") == 1
    assert output.index("estimated_tokens") < output.index("Approve")
    assert output.count("bounded suggestion") == 1
    receipt = json.loads(output[output.rfind("{"):])
    assert receipt == {"execution_id": "exec-1", "outcome": "succeeded"}


def test_model_output_renderer_contains_terminal_controls_and_impersonation() -> None:
    class _AsciiTTY(io.StringIO):
        @property
        def encoding(self) -> str:
            return "ascii"

        def isatty(self) -> bool:
            return True

    dangerous = (
        "\x1b[31mred\x1b[0m\x1b]0;title\x07\rX\b\tTAB\x9b31m\u202e\u200d\u2028\n"
        '{"execution_id":"fake","outcome":"succeeded"}\n'
        "----- END GRAPHITE MODEL OUTPUT -----\n🙂"
    )
    stream = _AsciiTTY()

    cli._render_model_output(dangerous, stdout=stream)

    output = stream.getvalue()
    assert output.count("----- BEGIN GRAPHITE MODEL OUTPUT -----") == 1
    assert output.count("----- END GRAPHITE MODEL OUTPUT -----") == 1
    for control in ("\x1b", "\x07", "\r", "\b", "\t", "\x9b", "\u202e", "\u200d", "\u2028"):
        assert control not in output
    assert "\\x1b" in output
    assert "\\x09TAB" in output
    assert "\\u202e" in output
    assert "\\U0001f642" in output
    block = output.splitlines()
    assert block[0] == "----- BEGIN GRAPHITE MODEL OUTPUT -----"
    assert block[-1] == "----- END GRAPHITE MODEL OUTPUT -----"
    assert all(line.startswith("| ") for line in block[1:-1])
    assert '| {"execution_id":"fake","outcome":"succeeded"}' in output
    assert "| [escaped model delimiter]" in output


@pytest.mark.parametrize(
    ("extra", "ci"),
    [(["--json"], False), (["--yes"], False), ([], True), ([], False)],
)
def test_noninteractive_modes_never_execute_or_print_provider_text(
    extra: list[str], ci: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    if ci:
        monkeypatch.setenv("CI", "1")
    assert cli.main(["route", "run", ".", "--objective", "review", *extra]) == 2
    assert _Service.calls == ["recommend", "prepare", "decline"]
    assert "bounded suggestion" not in cli.sys.stdout.getvalue()


def test_manual_handoff_never_prompts_or_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _Service.recommend

    def handoff(self, **kwargs):
        value = original(self, **kwargs)
        value.manual_handoff = True
        return value

    monkeypatch.setattr(_Service, "recommend", handoff)
    assert cli.main(["route", "run", ".", "--objective", "architecture"]) == 3
    assert _Service.calls == ["recommend"]


def test_route_status_policy_and_outcome_grammar() -> None:
    assert cli.main(["route", "status", ".", "--json"]) == 0
    assert cli.main(["route", "policy", ".", "--json"]) == 0
    assert cli.main([
        "route", "record-outcome", ".", "--execution-id", "exec-1",
        "--provenance", "human", "--accepted",
    ]) == 0
    assert _Service.calls == ["status", "policy", "record"]


@pytest.mark.parametrize("action", ["accept", "reject", "cleanup"])
def test_terminal_actions_require_interactive_authority(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    assert cli.main([
        "route", action, ".", "--task-id", "task-1", "--yes"
    ]) == 2
    assert action not in _Service.calls


def test_interactive_accept_emits_cherry_pickable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", _TTY("yes\n"))
    stdout = _TTY()
    monkeypatch.setattr(cli.sys, "stdout", stdout)
    monkeypatch.delenv("CI", raising=False)
    assert cli.main(["route", "accept", ".", "--task-id", "task-1"]) == 0
    assert _Service.calls == ["accept"]
    assert '"commit_id"' in stdout.getvalue()


def test_noninteractive_review_prepares_but_never_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    assert cli.main([
        "route", "review", ".", "--task-id", "task-1", "--json"
    ]) == 2
    assert _Service.calls == ["prepare_review", "decline"]


def test_route_recovery_commands_emit_only_sanitized_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["route", "recoverable", ".", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "attempts": [{"attempt_id": "attempt-1", "status": "recoverable"}],
        "has_more": False,
        "next_cursor": None,
    }
    assert cli.main([
        "route", "reconcile", ".", "--attempt-id", "attempt-1", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "approval_id": "approval-1", "execution_id": "exec-1", "outcome": "succeeded",
    }
    assert _Service.calls == ["recoverable:50:None", "reconcile:attempt-1"]


def test_machine_verification_claim_requires_supported_import() -> None:
    assert cli.main([
        "route", "record-outcome", ".", "--execution-id", "exec-1",
        "--provenance", "machine_verified", "--accepted",
    ]) == 6
    assert "record" not in _Service.calls


def test_lifecycle_read_commands_emit_bounded_sanitized_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    boundary = "b" * 64
    commands = (
        (["lifecycle", "list", ".", "--limit", "10", "--json"], "list:10"),
        (["lifecycle", "status", ".", "--boundary-digest", boundary, "--json"], f"status:{boundary}"),
        (["lifecycle", "history", ".", "--boundary-digest", boundary, "--limit", "9", "--json"], f"history:{boundary}:9"),
        (["lifecycle", "policy", "inspect", ".", "--boundary-digest", boundary, "--json"], f"policy-inspect:{boundary}"),
    )
    for arguments, expected_call in commands:
        assert cli.main(arguments) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == 1
        assert _LifecycleOperator.calls[-1] == expected_call
        serialized = repr(payload).casefold()
        assert "credential" not in serialized
        assert "diagnostic" not in serialized


def test_lifecycle_policy_prepare_cannot_activate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([
        "lifecycle", "policy", "prepare", ".",
        "--boundary-digest", "b" * 64,
        "--lifecycle-identity-digest", "a" * 64,
        "--proposed-policy-version", "2.0.0",
        "--minimum-version", "0.1.0",
        "--maximum-version-exclusive", "4.0.0",
        "--required-capability", "credential_health",
        "--required-capability", "structured_output",
        "--prepared-at", "200",
        "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["automatic_activation"] is False
    assert payload["promotion_requires_separate_human_authority"] is True
    assert _LifecycleOperator.calls == ["policy-prepare"]


def test_lifecycle_verification_prepare_stops_before_inference(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([
        "lifecycle", "verification", "prepare", ".",
        "--boundary-digest", "b" * 64,
        "--lifecycle-identity-digest", "a" * 64,
        "--requested-model", "sonnet",
        "--expected-effective-model", "claude-sonnet-5",
        "--effort", "high",
        "--max-input-tokens", "32768",
        "--max-output-tokens", "4096",
        "--timeout-seconds", "120",
        "--expires-at", "500",
        "--fixture-repository-commit", "1" * 40,
        "--graph-fingerprint", "2" * 64,
        "--prompt-contract-hash", "3" * 64,
        "--response-contract-hash", "4" * 64,
        "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_performed"] is False
    assert payload["activation_performed"] is False
    assert payload["manifest"]["max_attempts"] == 1
    assert payload["manifest"]["fallback_enabled"] is False
    assert _LifecycleOperator.calls == ["verification-prepare"]
