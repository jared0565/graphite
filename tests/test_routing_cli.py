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

    def execute_approved(self, recommendation):
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


@pytest.fixture(autouse=True)
def _service(monkeypatch: pytest.MonkeyPatch):
    _Service.calls = []
    monkeypatch.setattr(cli, "RoutingService", _Service)


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
    assert _Service.calls == ["recommend"]


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
    assert cli.main(["route", "run", ".", "--objective", "review"]) == 0
    assert _Service.calls == ["recommend", "execute"]
    output = stdout.getvalue()
    assert output.count("Approve this Ollama model call?") == 1
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
    assert _Service.calls == ["recommend"]
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
