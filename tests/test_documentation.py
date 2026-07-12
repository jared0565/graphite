import re
from collections.abc import Iterator
from pathlib import Path
from string import punctuation
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
DANGEROUS_URI_SCHEMES = frozenset(("data", "javascript", "vbscript"))
DOCUMENTS = ("README.md", "CONTRIBUTING.md", "ARCHITECTURE.md", "RELEASING.md")


def read_document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _fence_run(line: str) -> tuple[str, int, int] | None:
    stripped = line.lstrip(" ")
    indentation = len(line) - len(stripped)
    if indentation > 3 or not stripped:
        return None
    marker = stripped[0]
    if marker not in ("`", "~"):
        return None
    length = len(stripped) - len(stripped.lstrip(marker))
    return (marker, length, indentation + length) if length >= 3 else None


def _blockquote_content(line: str) -> tuple[int, str]:
    index = 0
    while index < min(3, len(line)) and line[index] == " ":
        index += 1
    if index == len(line) or line[index] != ">":
        return 0, line

    depth = 0
    while index < len(line) and line[index] == ">":
        depth += 1
        index += 1
        if index < len(line) and line[index] in (" ", "\t"):
            index += 1
    return depth, line[index:]


def _mask_inline_code_and_comments(
    line: str, in_html_comment: bool
) -> tuple[str, bool]:
    visible = list(line)
    index = 0
    while index < len(line):
        if in_html_comment:
            comment_end = line.find("-->", index)
            if comment_end == -1:
                visible[index:] = " " * (len(line) - index)
                return "".join(visible), True
            comment_end += 3
            visible[index:comment_end] = " " * (comment_end - index)
            index = comment_end
            in_html_comment = False
            continue

        if line.startswith("<!--", index):
            in_html_comment = True
            continue

        if line[index] != "`":
            index += 1
            continue

        run_length = len(line[index:]) - len(line[index:].lstrip("`"))
        search_index = index + run_length
        closing_end: int | None = None
        while search_index < len(line):
            closing_start = line.find("`", search_index)
            if closing_start == -1:
                break
            closing_length = len(line[closing_start:]) - len(
                line[closing_start:].lstrip("`")
            )
            if closing_length == run_length:
                closing_end = closing_start + closing_length
                break
            search_index = closing_start + closing_length
        if closing_end is None:
            index += run_length
            continue
        visible[index:closing_end] = " " * (closing_end - index)
        index = closing_end

    return "".join(visible), in_html_comment


def _parse_title(text: str, start: int, line_number: int) -> int:
    opener = text[start]
    if opener not in ('"', "'", "("):
        raise ValueError(f"line {line_number}: unsupported Markdown link title")
    closer = ")" if opener == "(" else opener
    index = start + 1
    while index < len(text):
        if text[index] == closer and not _is_escaped(text, index):
            return index + 1
        index += 1
    raise ValueError(f"line {line_number}: unterminated Markdown link title")


def _parse_destination(
    text: str, start: int, line_number: int, *, inline: bool
) -> tuple[str, int]:
    """Parse CommonMark-like inline/reference destinations used by root docs."""
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index == len(text):
        raise ValueError(
            f"line {line_number}: multiline Markdown links are unsupported"
        )
    if inline and text[index] == ")":
        raise ValueError(f"line {line_number}: empty Markdown link destination")

    if text[index] == "<":
        destination_start = index + 1
        index = destination_start
        while index < len(text):
            if text[index] == ">" and not _is_escaped(text, index):
                target = text[destination_start:index]
                index += 1
                break
            index += 1
        else:
            raise ValueError(f"line {line_number}: unterminated angle link destination")
    else:
        destination_start = index
        depth = 0
        while index < len(text):
            character = text[index]
            if character == "\\" and index + 1 < len(text):
                index += 2
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if inline and depth == 0:
                    break
                if depth == 0:
                    raise ValueError(
                        f"line {line_number}: unbalanced Markdown link destination"
                    )
                depth -= 1
            elif character.isspace() and depth == 0:
                break
            index += 1
        if depth:
            raise ValueError(f"line {line_number}: unbalanced Markdown link destination")
        target = text[destination_start:index]

    had_separator = index < len(text) and text[index].isspace()
    while index < len(text) and text[index].isspace():
        index += 1
    if inline and index < len(text) and text[index] == ")":
        return target, index + 1
    if not inline and index == len(text):
        return target, index
    if not had_separator or index == len(text):
        raise ValueError(f"line {line_number}: malformed Markdown link destination")

    index = _parse_title(text, index, line_number)
    while index < len(text) and text[index].isspace():
        index += 1
    if inline:
        if index == len(text) or text[index] != ")":
            raise ValueError(f"line {line_number}: malformed Markdown link title")
        return target, index + 1
    if index != len(text):
        raise ValueError(f"line {line_number}: trailing reference-link content")
    return target, index


def _reference_destination_start(line: str) -> int | None:
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if indent > 3 or not stripped.startswith("["):
        return None
    index = indent + 1
    while index < len(line):
        if line[index] == "]" and not _is_escaped(line, index):
            return index + 2 if line[index + 1 : index + 2] == ":" else None
        index += 1
    return None


def scan_markdown_links(markdown: str) -> Iterator[tuple[int, str]]:
    """Yield line/target pairs for inline, image, and reference-definition links.

    The scanner ignores fenced/indented/inline code and HTML comments. It handles
    nested/empty labels, balanced or escaped destination parentheses, angle
    destinations, blockquote-contained fences, and optional link titles.
    Malformed supported link forms raise ValueError instead of disappearing;
    multiline links and reference definitions are intentionally unsupported.
    """
    fence: tuple[str, int, int] | None = None
    in_html_comment = False
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        quote_depth, content = _blockquote_content(line)
        run = _fence_run(content)
        if fence:
            if (
                run
                and run[0] == fence[0]
                and run[1] >= fence[1]
                and quote_depth == fence[2]
                and not content[run[2] :].strip()
            ):
                fence = None
            continue

        if not in_html_comment and (
            content.startswith("\t") or content.startswith("    ")
        ):
            continue

        visible, in_html_comment = _mask_inline_code_and_comments(
            content, in_html_comment
        )
        run = _fence_run(visible)
        if run:
            fence = (run[0], run[1], quote_depth)
            continue

        reference_start = _reference_destination_start(visible)
        if reference_start is not None:
            target, _ = _parse_destination(
                visible, reference_start, line_number, inline=False
            )
            yield line_number, target
            continue

        index = 0
        label_depth = 0
        while index < len(visible):
            if visible[index] == "[" and not _is_escaped(visible, index):
                label_depth += 1
            elif visible[index] == "]" and not _is_escaped(visible, index):
                if (
                    label_depth
                    and index + 1 < len(visible)
                    and visible[index + 1] == "("
                ):
                    target, index = _parse_destination(
                        visible, index + 2, line_number, inline=True
                    )
                    yield line_number, target
                    label_depth = 0
                    continue
                label_depth = max(0, label_depth - 1)
            index += 1
    if fence:
        raise ValueError("unterminated Markdown fence")


def _markdown_path(raw_target: str) -> str:
    index = 0
    while index < len(raw_target):
        if raw_target[index] == "\\" and index + 1 < len(raw_target):
            index += 2
            continue
        if raw_target[index] in ("?", "#"):
            return raw_target[:index]
        index += 1
    return raw_target


def _unescape_markdown(text: str) -> str:
    characters: list[str] = []
    index = 0
    while index < len(text):
        if (
            text[index] == "\\"
            and index + 1 < len(text)
            and text[index + 1] in punctuation
        ):
            index += 1
        characters.append(text[index])
        index += 1
    return "".join(characters)


def link_target_diagnostic(document_path: Path, raw_target: str) -> str | None:
    markdown_target = _unescape_markdown(raw_target)
    if not markdown_target or markdown_target.startswith("#"):
        return None

    encoded_path = _markdown_path(markdown_target)
    if URI_SCHEME.match(encoded_path) and not WINDOWS_DRIVE.match(encoded_path):
        scheme = encoded_path.partition(":")[0].casefold()
        if scheme in DANGEROUS_URI_SCHEMES:
            return f"unsafe URI scheme is not allowed: {scheme}"
        return None
    if not encoded_path:
        return None

    decoded_path = unquote(encoded_path)
    if WINDOWS_DRIVE.match(decoded_path):
        return "Windows drive target is not repository-local"
    if decoded_path.startswith(("//", "\\\\")):
        return "UNC or protocol-relative target is not repository-local"
    repository_root = ROOT.resolve()
    portable_path = decoded_path.replace("\\", "/")
    resolved_target = (document_path.parent / portable_path).resolve()
    if not resolved_target.is_relative_to(repository_root):
        return "target escapes repository"
    if not resolved_target.exists():
        return "target does not exist"
    return None


def document_link_diagnostics(document_path: Path, markdown: str) -> list[str]:
    diagnostics: list[str] = []
    for line_number, raw_target in scan_markdown_links(markdown):
        diagnostic = link_target_diagnostic(document_path, raw_target)
        if diagnostic:
            diagnostics.append(
                f"{document_path.name}:{line_number}: {diagnostic}: {raw_target!r}"
            )
    return diagnostics


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


def test_markdown_link_scanner_handles_supported_forms_and_fences() -> None:
    markdown = "\n".join(
        (
            "````python",
            "[ignored](missing.md)",
            "```",
            "````",
            "~~~",
            "![also ignored](missing.png)",
            "~~~",
            "[simple](README.md)",
            "[](empty.md)",
            "[outer [inner]](nested.md)",
            "![image](image.png)",
            "[balanced](docs/a(b).md)",
            r"[escaped](docs/a\(b\).md)",
            '[angle](<docs/a b.md> "Title")',
            "[query](README.md?download=1#usage)",
            "[parenthesized title](README.md (Root document))",
            "[guide]: CONTRIBUTING.md 'Contributor guide'",
        )
    )

    assert list(scan_markdown_links(markdown)) == [
        (8, "README.md"),
        (9, "empty.md"),
        (10, "nested.md"),
        (11, "image.png"),
        (12, "docs/a(b).md"),
        (13, r"docs/a\(b\).md"),
        (14, "docs/a b.md"),
        (15, "README.md?download=1#usage"),
        (16, "README.md"),
        (17, "CONTRIBUTING.md"),
    ]


def test_markdown_link_scanner_ignores_code_comments_and_quoted_fences() -> None:
    markdown = "\n".join(
        (
            "`[inline](missing.md)` [first](README.md)",
            "``code ` [long span](missing.md)`` [second](CONTRIBUTING.md)",
            "    [indented](missing.md)",
            "\t[tab indented](missing.md)",
            "<!-- [single comment](missing.md) --> [third](ARCHITECTURE.md)",
            "<!--",
            "[multiline comment](missing.md)",
            "--> [fourth](RELEASING.md)",
            "> ```python",
            "> [quoted fence](missing.md)",
            "> ```",
            "> > ~~~~",
            "> > [nested quoted fence](missing.md)",
            "> > ~~~~",
            "> [fifth](README.md)",
        )
    )

    assert list(scan_markdown_links(markdown)) == [
        (1, "README.md"),
        (2, "CONTRIBUTING.md"),
        (5, "ARCHITECTURE.md"),
        (8, "RELEASING.md"),
        (15, "README.md"),
    ]


def test_markdown_link_scanner_rejects_malformed_single_line_forms() -> None:
    malformed = (
        "[missing close](README.md",
        "[unterminated angle](<README.md)",
        "[unsupported title](README.md title)",
        '[unterminated title](README.md "title)',
        "[trailing title content](README.md 'title' trailing)",
        "[empty]()",
        "[empty reference]:",
    )

    for markdown in malformed:
        with pytest.raises(ValueError, match=r"line 1:"):
            list(scan_markdown_links(markdown))


def test_markdown_link_scanner_reports_multiline_forms_as_unsupported() -> None:
    for markdown in ("[link](\nREADME.md)", "[reference]:\nREADME.md"):
        with pytest.raises(ValueError, match="multiline Markdown links are unsupported"):
            list(scan_markdown_links(markdown))


def test_link_target_diagnostic_normalizes_before_security_checks() -> None:
    document_path = ROOT / "README.md"
    unsafe_targets = (
        ("C:outside.md", "Windows drive target is not repository-local"),
        ("C:/outside.md", "Windows drive target is not repository-local"),
        (r"C:\outside.md", "Windows drive target is not repository-local"),
        ("%43%3Aoutside.md", "Windows drive target is not repository-local"),
        ("%43%3A%2Foutside.md", "Windows drive target is not repository-local"),
        ("//server/share.md", "UNC or protocol-relative target is not repository-local"),
        (r"\\\\server\share.md", "UNC or protocol-relative target is not repository-local"),
        ("%2F%2Fserver%2Fshare.md", "UNC or protocol-relative target is not repository-local"),
        ("%5C%5Cserver%5Cshare.md", "UNC or protocol-relative target is not repository-local"),
        ("../outside.md", "target escapes repository"),
        ("%2E%2E%2Foutside.md", "target escapes repository"),
        ("%2e%2e%5coutside.md", "target escapes repository"),
    )

    for target, expected in unsafe_targets:
        assert link_target_diagnostic(document_path, target) == expected


def test_link_target_diagnostic_skips_non_local_targets_and_queries() -> None:
    document_path = ROOT / "README.md"

    for target in (
        "https://example.com/docs",
        "http://example.com/docs",
        "mailto:docs@example.com",
        "ftp://example.com/docs",
        "#usage",
    ):
        assert link_target_diagnostic(document_path, target) is None

    assert link_target_diagnostic(document_path, "README.md?download=1#usage") is None


def test_link_target_diagnostic_classifies_uri_before_percent_decoding() -> None:
    document_path = ROOT / "README.md"

    for target in (
        "https%3A%2F%2Fexample.com%2Fdocs",
        "javascript%3Aalert%281%29",
        "data%3Atext%2Fplain%2Chello",
        "vbscript%3AMsgBox%281%29",
    ):
        assert link_target_diagnostic(document_path, target) == "target does not exist"


def test_link_target_diagnostic_rejects_dangerous_uri_schemes() -> None:
    document_path = ROOT / "README.md"
    dangerous_targets = (
        ("javascript:alert(1)", "javascript"),
        ("JaVaScRiPt:alert(1)", "javascript"),
        ("data:text/html,<script>alert(1)</script>", "data"),
        ("DaTa:text/plain,hello", "data"),
        ("vbscript:MsgBox(1)", "vbscript"),
        ("VbScRiPt:MsgBox(1)", "vbscript"),
    )

    for target, scheme in dangerous_targets:
        assert link_target_diagnostic(document_path, target) == (
            f"unsafe URI scheme is not allowed: {scheme}"
        )


def test_link_target_diagnostic_splits_after_markdown_unescaping() -> None:
    document_path = ROOT / "README.md"

    assert link_target_diagnostic(document_path, r"README.md\#usage") is None
    assert link_target_diagnostic(document_path, r"README.md\?download=1") is None


def test_document_link_diagnostics_reports_unsafe_and_missing_targets() -> None:
    markdown = "\n".join(
        (
            "[safe](README.md)",
            "[external](https://example.com/docs)",
            "[fragment](#usage)",
            "[drive](%43%3Aoutside.md)",
            "[missing](missing-document.md)",
            "[script](JaVaScRiPt:alert(1))",
            "[embedded](data:text/html,unsafe)",
            "[legacy](VBSCRIPT:MsgBox(1))",
        )
    )

    assert document_link_diagnostics(ROOT / "README.md", markdown) == [
        "README.md:4: Windows drive target is not repository-local: "
        "'%43%3Aoutside.md'",
        "README.md:5: target does not exist: 'missing-document.md'",
        "README.md:6: unsafe URI scheme is not allowed: javascript: "
        "'JaVaScRiPt:alert(1)'",
        "README.md:7: unsafe URI scheme is not allowed: data: "
        "'data:text/html,unsafe'",
        "README.md:8: unsafe URI scheme is not allowed: vbscript: "
        "'VBSCRIPT:MsgBox(1)'",
    ]


def test_relative_markdown_links_resolve() -> None:
    failures: list[str] = []

    for document_name in DOCUMENTS:
        document_path = ROOT / document_name
        failures.extend(
            document_link_diagnostics(document_path, read_document(document_name))
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
