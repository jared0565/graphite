from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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

    architecture_lower = architecture.lower()
    assert "repository input" in architecture_lower
    assert "model provider" in architecture_lower
