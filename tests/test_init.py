"""Tests for Graphite AI platform initialization."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphite.cli import main
from graphite.init import init_project, resolve_platform_selection
from graphite.typescript_activation import ActivationOutcome, ActivationResult


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_init_project_creates_shared_graphite_and_platform_files(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "/*\n!/.gitignore\n")

    first = init_project(tmp_path, platforms=["codex", "claude", "visual-studio"]).to_dict()
    second = init_project(tmp_path, platforms=["codex", "claude", "visual-studio"]).to_dict()

    assert first["graphite_doc"]["changed"] is True
    assert second["graphite_doc"]["changed"] is False
    graphite_doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    assert "## Canonical Graph Isolation" in graphite_doc
    assert "do not read" in graphite_doc
    assert "provider credentials" in graphite_doc
    assert "GRAPHITE_LLM_API_KEY" not in graphite_doc
    assert "Follow `GRAPHITE.md`" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Follow `GRAPHITE.md`" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Follow `GRAPHITE.md`" in (tmp_path / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "!/GRAPHITE.md" in gitignore
    assert "!/AGENTS.md" in gitignore
    assert "!/CLAUDE.md" in gitignore
    assert "!/.github/" in gitignore
    assert "!/.github/copilot-instructions.md" in gitignore


def test_init_project_preserves_existing_instruction_content(tmp_path: Path) -> None:
    _write(tmp_path / "CLAUDE.md", "# Existing Claude Notes\n\nKeep this line.\n")

    result = init_project(tmp_path, platforms=["claude"]).to_dict()
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert result["platform_files"][0]["changed"] is True
    assert "Keep this line." in text
    assert "Follow `GRAPHITE.md`" in text


def test_resolve_platform_selection_accepts_aliases_numbers_and_all() -> None:
    assert resolve_platform_selection(["1, claude-code, copilot"]) == ("codex", "claude", "visual-studio")
    assert "windsurf" in resolve_platform_selection(["all"])
    assert resolve_platform_selection(["gemini-cli"]) == ("gemini",)


def test_init_gemini_platform_writes_gemini_md(tmp_path: Path) -> None:
    result = init_project(tmp_path, platforms=["gemini"]).to_dict()

    text = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert result["platforms"] == ["gemini"]
    assert "Follow `GRAPHITE.md`" in text


def test_graphite_doc_uses_shell_agnostic_invocation(tmp_path: Path) -> None:
    init_project(tmp_path, platforms=["claude"])
    doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")

    assert "python -m graphite check ." in doc
    assert "python -m graphite context <target-file>" in doc
    # No machine-specific install path may leak into project files.
    assert "_tools" not in doc
    assert "F:\\Projects" not in doc


def test_init_writes_versioned_managed_markers_and_is_idempotent(tmp_path: Path) -> None:
    from graphite.init import DOC_VERSION

    first = init_project(tmp_path, platforms=["claude"]).to_dict()
    second = init_project(tmp_path, platforms=["claude"]).to_dict()

    begin = f"<!-- graphite:managed version={DOC_VERSION} -->"
    for rel in ("GRAPHITE.md", "CLAUDE.md"):
        text = (tmp_path / rel).read_text(encoding="utf-8")
        assert begin in text
        assert "<!-- graphite:managed-end -->" in text
    assert first["graphite_doc"]["changed"] is True
    assert second["graphite_doc"]["changed"] is False
    assert second["graphite_doc"]["action"] == "already current"
    assert second["platform_files"][0]["action"] == "already current"


def test_init_refreshes_outdated_managed_region_preserving_surrounding_content(tmp_path: Path) -> None:
    from graphite.init import DOC_VERSION

    _write(
        tmp_path / "GRAPHITE.md",
        "user preamble\n\n"
        "<!-- graphite:managed version=1 -->\n"
        "OLD MANAGED BODY\n"
        "<!-- graphite:managed-end -->\n"
        "\nuser suffix\n",
    )
    result = init_project(tmp_path, platforms=["claude"]).to_dict()
    text = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")

    assert result["graphite_doc"]["changed"] is True
    assert result["graphite_doc"]["action"] == "refreshed"
    assert "user preamble" in text
    assert "user suffix" in text
    assert "OLD MANAGED BODY" not in text
    assert f"<!-- graphite:managed version={DOC_VERSION} -->" in text
    assert "## Required Workflow" in text


def test_init_leaves_legacy_unversioned_docs_untouched(tmp_path: Path) -> None:
    legacy_doc = (
        "# Graphite Development Context\n\n"
        "## Required Workflow\n\n"
        "Hand-curated legacy instructions.\n"
    )
    legacy_pointer = (
        "# Notes\n\nFollow `GRAPHITE.md`; use `graph-out/graph.json` as the shared graph.\n"
    )
    _write(tmp_path / "GRAPHITE.md", legacy_doc)
    _write(tmp_path / "CLAUDE.md", legacy_pointer)

    result = init_project(tmp_path, platforms=["claude"]).to_dict()

    assert result["graphite_doc"]["changed"] is False
    assert result["graphite_doc"]["action"] == "legacy unversioned"
    assert result["platform_files"][0]["changed"] is False
    assert result["platform_files"][0]["action"] == "legacy unversioned"
    assert (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8") == legacy_doc
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == legacy_pointer


def test_init_never_downgrades_or_touches_damaged_managed_regions(tmp_path: Path) -> None:
    newer = (
        "<!-- graphite:managed version=999999 -->\n"
        "FUTURE BODY\n"
        "<!-- graphite:managed-end -->\n"
    )
    damaged = "<!-- graphite:managed version=1 -->\nNO END MARKER\n"
    _write(tmp_path / "GRAPHITE.md", newer)
    _write(tmp_path / "CLAUDE.md", damaged)

    result = init_project(tmp_path, platforms=["claude"]).to_dict()

    assert result["graphite_doc"]["changed"] is False
    assert result["graphite_doc"]["action"] == "newer than tool"
    assert result["platform_files"][0]["changed"] is False
    assert result["platform_files"][0]["action"] == "managed markers damaged"
    assert (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8") == newer
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == damaged


def test_template_change_requires_doc_version_bump() -> None:
    import hashlib

    from graphite import init as init_module

    digest = hashlib.sha256(
        "\x00".join(
            [init_module.GRAPHITE_DOC, init_module.SHARED_POINTER, init_module.CURSOR_POINTER]
        ).encode("utf-8")
    ).hexdigest()
    assert (init_module.DOC_VERSION, digest) == (
        2,
        "33531a6a9ee54863778adde2ed0e19bc7d9146aaaf4a1ab76a945b11d5a57652",
    ), "template content changed: bump DOC_VERSION and update this pinned digest"


def test_init_cli_json_no_build(tmp_path: Path, capsys) -> None:
    result = main([
        "init",
        str(tmp_path),
        "--platform",
        "codex,claude",
        "--no-build",
        "--no-validate",
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["platforms"] == ["codex", "claude"]
    assert payload["build"]["requested"] is False
    assert payload["validation"]["requested"] is False
    graphite_doc = (tmp_path / "GRAPHITE.md").read_text(encoding="utf-8")
    assert "## Canonical Graph Isolation" in graphite_doc
    assert "GRAPHITE_LLM_API_KEY" not in graphite_doc
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").exists()


def test_init_cli_list_platforms(capsys) -> None:
    result = main(["init", "--list-platforms"])
    out = capsys.readouterr().out

    assert result == 0
    assert "codex" in out
    assert "claude" in out
    assert "visual-studio" in out


def test_init_json_includes_noninteractive_typescript_activation(tmp_path, capsys, monkeypatch) -> None:
    calls = []
    expected = ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive")
    monkeypatch.setattr("graphite.cli.activate_typescript", lambda request: calls.append(request) or expected)

    result = main(["init", str(tmp_path), "--platform", "codex", "--no-build", "--no-validate", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["typescript_activation"] == expected.to_dict()
    assert calls[0].json_mode is True
    assert calls[0].stdin_is_tty is False or calls[0].stdout_is_tty is False


def test_init_fatal_activation_preserves_onboarding_and_returns_one(tmp_path, capsys, monkeypatch) -> None:
    expected = ActivationResult(ActivationOutcome.VALIDATION_FAILED, None, "validator_missing")
    monkeypatch.setattr("graphite.cli.activate_typescript", lambda request: expected)

    result = main(["init", str(tmp_path), "--platform", "codex", "--no-build", "--no-validate", "--json"])
    capsys.readouterr()

    assert result == 1
    assert (tmp_path / "GRAPHITE.md").exists()


@pytest.mark.parametrize(
    ("extra_args", "ci", "stdin_tty", "stdout_tty"),
    [
        (["--json"], False, True, True),
        ([], True, True, True),
        ([], False, False, True),
        ([], False, True, False),
    ],
)
def test_init_noninteractive_boundaries_never_prompt_for_platform_or_activation(
    tmp_path, capsys, monkeypatch, extra_args, ci, stdin_tty, stdout_tty
) -> None:
    calls = []
    if ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr("graphite.cli.sys.stdin.isatty", lambda: stdin_tty)
    monkeypatch.setattr("graphite.cli.sys.stdout.isatty", lambda: stdout_tty)
    monkeypatch.setattr(
        "graphite.init._prompt_for_platforms",
        lambda **kwargs: pytest.fail("platform prompt called"),
    )
    monkeypatch.setattr(
        "graphite.cli.activate_typescript",
        lambda request: calls.append(request)
        or ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive"),
    )

    result = main(
        ["init", str(tmp_path), "--no-build", "--no-validate", *extra_args]
    )
    capsys.readouterr()

    assert result == 0
    assert calls and (not calls[0].stdin_is_tty or not calls[0].stdout_is_tty or calls[0].json_mode)


def test_init_human_guidance_uses_exact_fixed_workflow(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "graphite.cli.activate_typescript",
        lambda request: ActivationResult(ActivationOutcome.GUIDANCE_ONLY, None, "non_interactive"),
    )
    main(["init", str(tmp_path), "--yes", "--no-build", "--no-validate"])
    output = capsys.readouterr().out
    expected = [
        "    1. Set GRAPHITE_PACKAGE_VALIDATOR=<absolute-validator-path>.",
        "    2. Fail closed if GRAPHITE_PACKAGE_VALIDATOR is unset, relative, missing, or not a regular file.",
        "    3. Run: node <absolute-validator-path> typescript",
        "    4. With <project-manager>, add local dev dependency typescript with scripts disabled.",
        "    5. Rerun graphite doctor or onboarding to confirm detection.",
    ]
    assert [line for line in output.splitlines() if line.startswith("    ")] == expected
    assert "global install" not in output.lower()
    assert str(tmp_path) not in "\n".join(expected)
