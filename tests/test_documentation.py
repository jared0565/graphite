from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_contributing_guide_has_required_sections() -> None:
    contributing = read_document("CONTRIBUTING.md")

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
        assert heading in contributing


def test_readme_links_to_contributor_guides() -> None:
    readme = read_document("README.md")

    assert "[Contributor guide](CONTRIBUTING.md)" in readme
    assert "[Architecture guide](ARCHITECTURE.md)" in readme
    assert "[Release guide](RELEASING.md)" in readme
