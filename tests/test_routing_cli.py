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


def test_machine_verification_claim_requires_supported_import() -> None:
    assert cli.main([
        "route", "record-outcome", ".", "--execution-id", "exec-1",
        "--provenance", "machine_verified", "--accepted",
    ]) == 6
    assert "record" not in _Service.calls
