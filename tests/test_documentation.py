import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from string import punctuation
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
DANGEROUS_URI_SCHEMES = frozenset(("data", "javascript", "vbscript"))
DOCUMENTS = ("README.md", "CONTRIBUTING.md", "ARCHITECTURE.md", "RELEASING.md")
SECRET_VALUE = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{12,}|ghp_[a-z0-9]{20,}|"
    r"github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{12,}|"
    r"AIza[a-z0-9_-]{20,})\b"
)
API_KEY_ASSIGNMENT = re.compile(
    r'''(?i)\bGRAPHITE_LLM_API_KEY\s*=\s*(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s`]+)'''
)
AUTHORIZATION_BEARER = re.compile(
    r'''(?i)\bAuthorization\s*:\s*Bearer\s+(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s`]+)'''
)
PURE_ENV_REFERENCE = re.compile(
    r"(?i)(?:\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|"
    r"\$env:[A-Z_][A-Z0-9_]*)"
)


def read_document(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def document_section(document: str, heading: str) -> str:
    """Return one Markdown section without coupling tests to the whole document."""
    start = document.index(heading)
    body_start = start + len(heading)
    next_heading = document.find("\n## ", body_start)
    return document[start : next_heading if next_heading != -1 else len(document)]


def credential_example_linter_has_violation(text: str) -> bool:
    """Lint documentation for credential examples that are not pure placeholders."""
    if SECRET_VALUE.search(text):
        return True
    placeholders = {"...", "redacted", "placeholder", "your-api-key"}
    for pattern in (API_KEY_ASSIGNMENT, AUTHORIZATION_BEARER):
        for match in pattern.finditer(text):
            value = match.group("value").strip("\"'")
            value_folded = value.casefold()
            if (
                value_folded in placeholders
                or (value.startswith("<") and value.endswith(">"))
                or PURE_ENV_REFERENCE.fullmatch(value)
            ):
                continue
            if len(value) >= 12:
                return True
    return False


def validator_path_is_fully_qualified(value: str, shell: str) -> bool:
    """Model the documented shell-specific fully qualified validator-path contract."""
    if not value:
        return False
    path_type = PureWindowsPath if shell == "powershell" else PurePosixPath
    return path_type(value).is_absolute()


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


def test_readme_documents_system_readiness_and_optional_activation() -> None:
    readme = read_document("README.md")
    lines = set(readme.splitlines())

    assert "## System readiness and optional integrations" in lines
    for command in (
        "python -m graphite doctor .",
        "python -m graphite doctor . --deep",
        "python -m graphite doctor . --deep --include-llm",
    ):
        assert command in lines

    required_phrases = (
        "ready",
        "optional",
        "degraded",
        "blocked",
        "exit code",
        "external private temporary workspace",
        "selected repository remains read-only",
        "no-follow lease",
        "native Job Object",
        "POSIX process group",
        "same-user process namespace",
        "best-effort trust boundary",
        'python -m pip install -e ".[mcp]"',
        "guarded distribution-record import manifest",
        "current working directory, user-site, and attacker-controlled selected-root shadows",
        "validate-packages.cjs typescript",
        "project-local TypeScript",
        "never executes or transpiles untrusted project JavaScript",
        "local Ollama",
        "newly rotated, session-scoped",
        "GRAPHITE_LLM_API_KEY",
        "provider dashboard",
        "synthetic content only",
        "no repository data",
        "no redirects or retries",
        "GRAPHITE_LLM_MAX_OUTPUT_TOKENS",
        "16-token cap",
    )
    readme_folded = readme.casefold()
    for phrase in required_phrases:
        assert phrase.casefold() in readme_folded


def test_docs_define_consent_gated_typescript_activation() -> None:
    combined = "\n".join(
        read_document(name) for name in ("README.md", "CONTRIBUTING.md", "ARCHITECTURE.md")
    ).casefold()

    for phrase in (
        "project-local typescript",
        "defaults to no",
        "graphite_package_validator",
        "non-interactive",
        "lifecycle scripts",
        "private registries",
        "guidance_only",
        "global typescript",
    ):
        assert phrase in combined
    assert "yarn" in combined
    assert "does not install global typescript" in combined


def test_readme_pins_typescript_consent_and_onboarding_lifecycle() -> None:
    section = document_section(
        read_document("README.md"),
        "## Consent-gated project-local TypeScript activation",
    )
    folded = section.casefold()

    for phrase in (
        "graphite init",
        "graphite bootstrap",
        "project-local typescript is missing. install it with <manager> as a development dependency? [y/n]",
        "prompts exactly once",
        "defaults to no",
        "explicit `y` or `yes`",
        "empty input, eof, malformed input",
        "no remembered consent",
        "json, ci, redirected stdin, redirected stdout, and `--yes`",
        "never prompt, validate, or install",
        "onboarding files are written before activation",
        "validation_failed`, `installation_failed`, and `verification_failed",
        "exit code 1",
        "preserved",
    ):
        assert phrase in folded


def test_contributing_pins_typescript_eligibility_and_manual_workflow() -> None:
    section = document_section(
        read_document("CONTRIBUTING.md"),
        "## TypeScript activation maintenance contract",
    )
    folded = section.casefold()

    for phrase in (
        "npm 8–11",
        "pnpm 11",
        "bun 1",
        "package-lock.json",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "package.json#packagemanager",
        "unsafe manager configuration",
        "https://registry.npmjs.org/",
        "private registries",
        "lifecycle scripts disabled",
        "does not infer `@types/*`",
    ):
        assert phrase in folded
    assert "yarn is" in folded
    assert "guidance_only" in folded

    workflow_steps = (
        "1. set `graphite_package_validator`",
        "2. fail closed",
        "3. run the validator for the exact package name `typescript`",
        "4. only after successful validation",
        "5. rerun `graphite doctor` or onboarding",
    )
    positions = [folded.index(step) for step in workflow_steps]
    assert positions == sorted(positions)


def test_architecture_pins_typescript_activation_security_boundary() -> None:
    section = document_section(
        read_document("ARCHITECTURE.md"),
        "## Consent-gated TypeScript activation boundary",
    )
    folded = section.casefold()

    for phrase in (
        "typescript_activation.py",
        "dependency_install.py",
        "build, report, check, doctor, daemon, watch, and mcp",
        "no installation authority",
        "shell=false",
        "bounded output",
        "shared deadline",
        "ambient registry tokens",
        "no automatic rollback",
        "process-local",
        "do not make graphite unhackable",
        "empty temporary root",
        "posix",
    ):
        assert phrase in folded


def test_architecture_documents_current_llm_transport_and_token_bounds() -> None:
    architecture = read_document("ARCHITECTURE.md")
    architecture_folded = architecture.casefold()

    for phrase in (
        "hard 64 KiB HTTP response cap",
        "redirects disabled",
        "no retries",
        "configured output-token bounds are normalized to 1–4096 with a default of 512",
        "probe forces 16",
    ):
        assert phrase.casefold() in architecture_folded

    for stale_claim in (
        "not currently capped explicitly",
        "do not currently impose an explicit byte or character cap on a successful provider response",
    ):
        assert stale_claim not in architecture_folded


def test_mcp_docs_distinguish_trusted_source_overlap_from_project_shadows() -> None:
    readme = read_document("README.md")
    architecture = read_document("ARCHITECTURE.md")

    for document in (readme, architecture):
        assert "exact origin-verified trusted Graphite source" in document
        assert "may be inside the selected repository" in document
        assert "attacker-controlled selected-root shadows" in document

    assert "does not import Graphite or MCP from the selected repository" not in readme


def test_authoritative_doctor_design_documents_static_typescript_detection() -> None:
    design = read_document(
        "docs/superpowers/specs/2026-07-12-system-readiness-doctor-design.md"
    )
    historical_plan = read_document(
        "docs/superpowers/plans/2026-07-12-system-readiness-doctor.md"
    )

    for phrase in (
        "static `require.resolve('typescript')` detection",
        "trusted external Node executable",
        "does not load, execute, or transpile project-controlled JavaScript",
        "optional and unverified",
        "no OS network sandbox",
        "compiler-execution design was therefore superseded",
    ):
        assert phrase in design

    for stale_claim in (
        "A TypeScript compiler-backed synthetic resolution probe",
        "Doctor then reports the resolved compiler path/version and deep-probe result",
        "TypeScript compiler probe succeeds",
    ):
        assert stale_claim not in design

    assert "Final implementation deviation" in historical_plan
    assert "static `require.resolve('typescript')` detection" in historical_plan
    assert "superseded" in historical_plan
    assert "no OS network sandbox" in historical_plan


def test_optional_activation_docs_reject_unsafe_examples() -> None:
    readme = read_document("README.md")
    contributing = read_document("CONTRIBUTING.md")
    documents = "\n".join((readme, contributing))
    validator_command = (
        'node "C:\\Users\\fbmac\\atlas\\Codex\\.codex_state\\user_home\\scripts\\'
        'validate-packages.cjs" typescript'
    )

    assert validator_command in readme
    assert validator_command in contributing
    forbidden_global_installs = (
        "npm install -g typescript",
        "npm i -g typescript",
        "pnpm add -g typescript",
        "yarn global add typescript",
    )
    for command in forbidden_global_installs:
        assert command not in documents.casefold()
    assert not credential_example_linter_has_violation(documents)

    assert "defaults to 512" in readme
    assert "1–4096" in readme
    assert "16-token cap" in readme


def test_credential_example_linter_is_precise() -> None:
    prohibited = (
        "GRAPHITE_LLM_API_KEY=" + "live-secret-value-12345",
        'GRAPHITE_LLM_API_KEY="literal secret value"',
        "GRAPHITE_LLM_API_KEY=" + "sk-" + "a" * 20,
        "Authorization: Bearer " + "sk-" + "b" * 20,
        'Authorization: Bearer "literal session credential"',
        "provider credential " + "ghp_" + "c" * 24,
    )
    allowed = (
        "token-based-authentication",
        "key-value_configuration",
        "GRAPHITE_LLM_API_KEY",
        "GRAPHITE_LLM_API_KEY=...",
        "GRAPHITE_LLM_API_KEY=<provider-secret>",
        "GRAPHITE_LLM_API_KEY=${SESSION_SECRET}",
        "GRAPHITE_LLM_API_KEY=$env:SESSION_SECRET",
        "Authorization: Bearer <session-token>",
    )
    disguised_literals = (
        "GRAPHITE_LLM_API_KEY=${SESSION_SECRET:-literal-secret-value}",
        "GRAPHITE_LLM_API_KEY=$env:SESSION_SECRET:literal-secret-value",
        "Authorization: Bearer ${TOKEN:-literal-secret-value}",
    )

    for example in prohibited:
        assert credential_example_linter_has_violation(example)
    for example in disguised_literals:
        assert credential_example_linter_has_violation(example)
    for example in allowed:
        assert not credential_example_linter_has_violation(example)


def test_every_mcp_install_is_gated_by_local_validation() -> None:
    install_pattern = re.compile(r"(?i)\bpip install\b.*\[[^\]]*mcp[^\]]*\]")
    found: list[str] = []

    for document_name in ("README.md", "CONTRIBUTING.md"):
        lines = read_document(document_name).splitlines()
        for index, line in enumerate(lines):
            if not install_pattern.search(line):
                continue
            found.append(f"{document_name}:{index + 1}")
            section_start = max(
                (offset for offset in range(index) if lines[offset].startswith("#")),
                default=-1,
            )
            preceding_window = "\n".join(lines[max(section_start + 1, index - 36) : index])
            assert "package-validation policy" in preceding_window.casefold(), (
                f"{document_name}:{index + 1} installs MCP without preceding policy"
            )
            assert "GRAPHITE_PACKAGE_VALIDATOR" in preceding_window, (
                f"{document_name}:{index + 1} lacks a configurable validator"
            )
            assert re.search(
                r"node\s+(?:\"\$GRAPHITE_PACKAGE_VALIDATOR\"|"
                r"\$env:GRAPHITE_PACKAGE_VALIDATOR)\s+mcp\b",
                preceding_window,
            ), f"{document_name}:{index + 1} does not run the configured validator"
            assert "stop" in preceding_window.casefold(), (
                f"{document_name}:{index + 1} does not fail closed"
            )
            assert (
                "[System.IO.Path]::IsPathFullyQualified("
                "$env:GRAPHITE_PACKAGE_VALIDATOR)"
                in preceding_window
            ), f"{document_name}:{index + 1} lacks the PowerShell qualified-path guard"
            assert 'case "$GRAPHITE_PACKAGE_VALIDATOR" in' in preceding_window, (
                f"{document_name}:{index + 1} lacks the POSIX absolute-path guard"
            )
    assert found, "No MCP activation install instructions were found"


def test_validator_workflow_is_portable_fail_closed_and_scoped() -> None:
    readme = read_document("README.md")
    contributing = read_document("CONTRIBUTING.md")
    combined = "\n".join((readme, contributing))

    for document in (readme, contributing):
        assert "GRAPHITE_PACKAGE_VALIDATOR" in document
        document_folded = document.casefold()
        assert "unset" in document_folded
        assert "relative" in document_folded
        assert "missing" in document_folded
        assert "stop" in document_folded
        assert "environment-specific" in document_folded
        assert "do not download" in document_folded

    contributing_folded = contributing.casefold()
    assert "repository-reviewed declared `.[dev]` extra" in contributing_folded
    assert "does not require per-package validation" in contributing_folded
    assert "optional activation installs" in contributing_folded
    assert "adding or changing external dependencies or package names" in contributing_folded
    assert "validate-packages.cjs typescript" in combined


def test_validator_path_contract_rejects_relative_and_empty_values() -> None:
    for shell in ("powershell", "posix"):
        for value in ("", "validate-packages.cjs", "./validate-packages.cjs"):
            assert not validator_path_is_fully_qualified(value, shell)

    for value in (r"C:validate-packages.cjs", r"\validate-packages.cjs"):
        assert not validator_path_is_fully_qualified(value, "powershell")

    assert validator_path_is_fully_qualified(
        r"C:\<trusted-tools>\validate-packages.cjs", "powershell"
    )
    assert validator_path_is_fully_qualified(
        r"\\trusted-server\tools\validate-packages.cjs", "powershell"
    )
    assert validator_path_is_fully_qualified(
        "/<trusted-tools>/validate-packages.cjs", "posix"
    )
    assert not validator_path_is_fully_qualified(
        r"C:\trusted-tools\validate-packages.cjs", "posix"
    )


def test_validator_snippets_require_shell_specific_absolute_paths() -> None:
    for document_name in ("README.md", "CONTRIBUTING.md"):
        document = read_document(document_name)
        assert (
            "[System.IO.Path]::IsPathFullyQualified("
            "$env:GRAPHITE_PACKAGE_VALIDATOR)"
            in document
        )
        assert "[System.IO.Path]::IsPathRooted(" not in document
        assert 'case "$GRAPHITE_PACKAGE_VALIDATOR" in' in document
        assert "/*) ;;" in document
        assert "never execute a relative repository-local validator" in document.casefold()


def test_workspace_security_docs_match_source_semantics() -> None:
    readme = read_document("README.md")
    architecture = read_document("ARCHITECTURE.md")
    workspace_source = (ROOT / "src/graphite/probe_workspace.py").read_text(
        encoding="utf-8"
    )
    probes_source = (ROOT / "src/graphite/doctor_probes.py").read_text(
        encoding="utf-8"
    )

    assert 'sddl = f"D:P(A;OICI;FA;;;' in workspace_source
    assert "_CORE_PROBE_SLOT_ACTIVE" in probes_source
    assert "_CORE_PROBE_SLOT_LOCK" in probes_source
    for document in (readme, architecture):
        document_folded = document.casefold()
        assert "created with a protected, inheritable current-user dacl" in document_folded
        assert "pinned" in document_folded
        assert "reparse" in document_folded
        assert "identity" in document_folded
        assert "same interpreter/process" in document_folded
        assert not re.search(
            r"(?:checks?|validates?|revalidates?).{0,100}d?acl.{0,100}"
            r"before and after (?:each|every) phase",
            document_folded,
        )
        assert not re.search(
            r"d?acl (?:is|are) (?:checked|validated|revalidated)"
            r".{0,60}(?:each|every) phase",
            document_folded,
        )


def test_contributing_documents_doctor_test_and_security_contracts() -> None:
    contributing = read_document("CONTRIBUTING.md")
    contributing_folded = contributing.casefold()

    required_phrases = (
        "stable doctor JSON",
        "redaction",
        "deadlines",
        "process cleanup",
        "missing tools",
        "optional semantics",
        "validate-packages.cjs typescript",
        "no live provider calls in automated tests",
        "fake worker",
        "fake provider",
        "fake process boundary",
        "selected root is hostile",
        "raw outputs, raw errors, secrets, or absolute paths",
        "Windows and POSIX",
    )
    for phrase in required_phrases:
        assert phrase.casefold() in contributing_folded


def test_architecture_documents_doctor_and_deep_probe_boundary() -> None:
    architecture = read_document("ARCHITECTURE.md")
    lines = set(architecture.splitlines())
    architecture_folded = architecture.casefold()

    assert "### Doctor and deep-probe boundary" in lines
    required_phrases = (
        "fast checks are read-only",
        "external private temporary workspace",
        "native process containment",
        "cleanup coordination",
        "guarded distribution-record import manifest",
        "static no-exec",
        "synthetic content only",
        "isolated bounded worker",
        "optional states do not block core",
        "no repository data or model context",
        "same-user process namespace",
        "best effort",
        "bounded parse, schema, and output",
    )
    for phrase in required_phrases:
        assert phrase.casefold() in architecture_folded


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
