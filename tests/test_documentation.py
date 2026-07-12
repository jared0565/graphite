import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
DOCUMENTS = ("README.md", "CONTRIBUTING.md", "ARCHITECTURE.md", "RELEASING.md")


def read_document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_contributing_guide_has_required_sections() -> None:
    contributing = read_document("CONTRIBUTING.md")
    lines = set(contributing.splitlines())

    required_headings = (
        "# Contributing to Graphite",
        "## Development setup",
        "## Engineering workflow",
        "## Testing and quality gates",
        "## Security expectations",
        "## Model-agnostic design",
        "## Contribution conventions",
        "## Pull request checklist",
    )

    for heading in required_headings:
        assert heading in lines


def test_readme_links_to_contributor_guides() -> None:
    readme = read_document("README.md")

    assert "[Contributor guide](CONTRIBUTING.md)" in readme
    assert "[Architecture guide](ARCHITECTURE.md)" in readme
    assert "[Release guide](RELEASING.md)" in readme


def test_windows_drive_relative_link_is_detected() -> None:
    assert WINDOWS_DRIVE.match("C:outside.md")


def test_relative_markdown_links_resolve() -> None:
    repository_root = ROOT.resolve()
    failures: list[str] = []

    for document_name in DOCUMENTS:
        document_path = ROOT / document_name
        for match in MARKDOWN_LINK.finditer(read_document(document_name)):
            raw_target = match.group(1).strip()
            target = raw_target
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()

            if not target or target.startswith("#"):
                continue
            if target.startswith("//") or target.startswith("\\\\"):
                failures.append(
                    f"{document_name}: protocol-relative target is not repository-local: "
                    f"{raw_target!r}"
                )
                continue
            if WINDOWS_DRIVE.match(target):
                failures.append(
                    f"{document_name}: Windows drive target is not repository-local: "
                    f"{raw_target!r}"
                )
                continue
            if URI_SCHEME.match(target):
                continue

            local_target = unquote(target.split("#", 1)[0])
            resolved_target = (document_path.parent / local_target).resolve()
            if not resolved_target.is_relative_to(repository_root):
                failures.append(
                    f"{document_name}: target escapes repository: {raw_target!r}"
                )
            elif not resolved_target.exists():
                failures.append(
                    f"{document_name}: target does not exist: {raw_target!r}"
                )

    assert not failures, "Broken local Markdown links:\n" + "\n".join(failures)


def test_contributor_guides_have_no_draft_markers() -> None:
    markers = ("T" + "ODO", "T" + "BD", "F" + "IXME")
    failures: list[str] = []

    for document_name in DOCUMENTS[1:]:
        document = read_document(document_name)
        for marker in markers:
            if marker in document:
                failures.append(f"{document_name}: contains draft marker {marker!r}")

    assert not failures, "Contributor guides contain draft markers:\n" + "\n".join(
        failures
    )


def test_architecture_guide_has_pipeline_and_boundaries() -> None:
    architecture = read_document("ARCHITECTURE.md")
    lines = set(architecture.splitlines())

    required_headings = (
        "# Graphite architecture",
        "## System context",
        "## Processing pipeline",
        "## Module map",
        "## Trust boundaries",
        "## Artifacts and state",
        "## Failure behavior",
        "## Extension points and invariants",
    )

    for heading in required_headings:
        assert heading in lines

    boundary_labels = (
        "**Repository input.**",
        "**Process boundary.**",
        "**Artifact and browser boundary.**",
        "**Model and network boundary.**",
    )

    for label in boundary_labels:
        assert label in lines

    architecture_lower = architecture.lower()
    assert "repository input" in architecture_lower
    assert "model provider" in architecture_lower


def test_release_guide_has_gates_and_version_sources() -> None:
    releasing = read_document("RELEASING.md")
    lines = set(releasing.splitlines())

    required_headings = (
        "# Releasing Graphite",
        "## Current release model",
        "## Preconditions",
        "## Prepare the version",
        "## Verification gates",
        "## Build and inspect artifacts",
        "## Tag and publish",
        "## Verify and recover",
    )

    for heading in required_headings:
        assert heading in lines

    releasing_lower = releasing.lower()
    assert "pyproject.toml" in releasing_lower
    assert "src/graphite/__init__.py" in releasing_lower
    assert "model" in releasing_lower
    assert "--no-isolation" in releasing
    assert "--no-index" in releasing
    assert "python -m pip check" in releasing
    assert "git diff --cached" in releasing
    assert "git status --short" in releasing
    assert "sha256" in releasing_lower
    assert "--llm none" in releasing
    assert "never recreate or move a public tag" in releasing_lower
    assert "never reuse a released version" in releasing_lower
    assert "sys.argv[1]" in releasing
    assert "normalized forward slashes" in releasing_lower
    assert "shell metacharacters" in releasing_lower
