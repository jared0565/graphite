from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.config import Config
from graphite.daemon import _build_command


def _write_fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def answer() -> int:\n    return 42\n",
        encoding="utf-8",
    )


class _CredentialTrap(dict[str, str]):
    def __getitem__(self, key: str) -> str:
        if key.endswith("API_KEY") or key == "OLLAMA_HOST":
            raise AssertionError("canonical config read a provider credential")
        return super().__getitem__(key)


def test_canonical_config_does_not_read_llm_environment_values(monkeypatch) -> None:
    environment = _CredentialTrap(
        {
            "GRAPHITE_OUTPUT_DIR": "safe-out",
            "GRAPHITE_LLM": "cloud",
            "GRAPHITE_LLM_PROVIDER": "openrouter",
            "GRAPHITE_LLM_API_KEY": "must-not-be-read",
        }
    )
    monkeypatch.setattr("graphite.config.os.environ", environment)

    cfg = Config.from_env(include_llm=False)

    assert cfg.output_dir == Path("safe-out")
    assert cfg.llm_mode == "none"
    assert cfg.llm_provider == "none"
    assert cfg.llm_api_key is None


@pytest.mark.parametrize(
    "legacy_args",
    [
        ["--llm", "cloud"],
        ["--llm", "auto"],
        ["--llm-provider", "openrouter"],
        ["--llm-model", "vendor/model"],
        ["--llm-base-url", "https://example.invalid/v1"],
        ["--llm-api-key", "must-not-enter-canonical-config"],
        ["--llm-max-output-tokens", "64"],
    ],
)
def test_canonical_commands_reject_legacy_enrichment_options(
    tmp_path: Path, capsys, legacy_args: list[str]
) -> None:
    _write_fixture(tmp_path)

    result = main([*legacy_args, "build", str(tmp_path)])

    assert result == 2
    error = capsys.readouterr().err
    assert error == (
        "[graphite] canonical graph commands do not accept LLM enrichment; "
        "use 'graphite overlay build' after building the canonical graph\n"
    )
    assert "must-not-enter-canonical-config" not in error


@pytest.mark.parametrize(
    "command",
    [
        ["scan", "missing"],
        ["build", "missing"],
        ["report", "missing"],
        ["check", "missing"],
        ["validate"],
        ["query", "stats"],
        ["context", "src/app.py"],
        ["impact", "src/app.py"],
        ["watch", "missing"],
        ["daemon", "missing"],
    ],
)
def test_every_canonical_command_rejects_non_none_llm_before_work(
    capsys, command: list[str]
) -> None:
    assert main(["--llm", "cloud", *command]) == 2
    assert "graphite overlay build" in capsys.readouterr().err


def test_explicit_llm_none_remains_a_compatible_canonical_noop(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    result = main(
        [
            "--output-dir",
            str(tmp_path / "graph-out"),
            "--llm",
            "none",
            "build",
            str(tmp_path),
        ]
    )

    assert result == 0
    manifest = json.loads(
        (tmp_path / "graph-out" / ".graphite_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert not any(key.startswith("llm_") for key in manifest)


def test_daemon_child_build_has_no_provider_argv_or_environment(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "provider-secret-must-not-propagate"
    environment = _CredentialTrap(
        {
            "PATH": "safe-bin",
            "GRAPHITE_LLM": "cloud",
            "GRAPHITE_LLM_API_KEY": secret,
            "OPENAI_API_KEY": secret,
            "OPENROUTER_API_KEY": secret,
            "ANTHROPIC_API_KEY": secret,
            "OLLAMA_HOST": "http://provider.invalid",
        }
    )
    monkeypatch.setattr("graphite.daemon.os.environ", environment)
    cfg = Config(
        llm_mode="cloud",
        llm_provider="openrouter",
        llm_model="vendor/model",
        llm_base_url="https://provider.invalid/v1",
        llm_api_key=secret,
    )

    argv, environment = _build_command(cfg, tmp_path)
    serialized = " ".join(argv)

    assert argv[-3:] == ["none", "build", str(tmp_path)]
    assert argv[argv.index("--llm") + 1] == "none"
    for forbidden in ("--llm-provider", "--llm-model", "--llm-base-url", secret):
        assert forbidden not in serialized
    for name in (
        "GRAPHITE_LLM",
        "GRAPHITE_LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OLLAMA_HOST",
    ):
        assert name not in environment


def test_canonical_artifacts_and_read_results_ignore_every_provider_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    _write_fixture(project)
    monkeypatch.chdir(project)

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("canonical graph build instantiated an LLM provider")

    monkeypatch.setattr("graphite.llm.make_provider", forbidden_provider)
    scenarios = (
        ("healthy", "cloud", "openrouter", "vendor/healthy"),
        ("unavailable", "local", "ollama", "missing:latest"),
        ("incompatible", "cloud", "openai-compatible", "vendor/incompatible"),
        ("drifted", "auto", "openrouter", "vendor/drifted"),
        ("absent", "cloud", "imaginary-vendor", "vendor/absent"),
    )
    artifact_digests: list[dict[str, str]] = []
    read_results: list[dict[str, object]] = []
    artifact_names = (
        ".graphite_analysis.json",
        ".graphite_clusters.json",
        ".graphite_graph.json",
        ".graphite_manifest.json",
        ".graphite_validation.json",
        "GRAPH_REPORT.md",
        "graph.html",
        "graph.json",
    )

    for state, mode, provider, model in scenarios:
        monkeypatch.setenv("GRAPHITE_TEST_PROVIDER_STATE", state)
        monkeypatch.setenv("GRAPHITE_LLM", mode)
        monkeypatch.setenv("GRAPHITE_LLM_PROVIDER", provider)
        monkeypatch.setenv("GRAPHITE_LLM_MODEL", model)
        monkeypatch.setenv("GRAPHITE_LLM_API_KEY", f"secret-{state}")

        assert main(["build", str(project)]) == 0
        capsys.readouterr()
        out = project / "graph-out"
        artifact_digests.append(
            {
                name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in artifact_names
            }
        )

        outputs: dict[str, object] = {}
        for name, argv in (
            ("check", ["check", str(project), "--json"]),
            ("validate", ["validate", "--json"]),
            ("query", ["query", "stats"]),
            ("context", ["context", "src/app.py", "--json"]),
            ("impact", ["impact", "src/app.py", "--json"]),
        ):
            assert main(argv) == 0
            outputs[name] = json.loads(capsys.readouterr().out)
        read_results.append(outputs)

    assert all(value == artifact_digests[0] for value in artifact_digests[1:])
    assert all(value == read_results[0] for value in read_results[1:])
