"""Claude Code agent-hook handlers enforcing graphite-first on onboarded repos.

Every handler is fail-open by contract: any parse, IO, graph, or timing problem
yields ``None`` (no hook output) rather than an exception, so a hook bug can
never break a tool call or a session. The CLI wrapper adds a second catch-all.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from . import savings as savings_model
from . import usage_ledger
from .activation import mark_active
from .config import Config
from .freshness import check_graph_freshness
from .graph_io import load_validated_graph_bundle
from .health import _edge_language, is_placeholder, persisted_resolution
from .incident_ledger import record_incident, repo_ledger_dir
from .ingest import enclosing_git_root, repository_root

SESSION_CONTRACT = (
    "graphite-first: this repo uses a shared code graph. For cross-file questions "
    "(who-calls / who-reads / where-defined / data-flow / blast-radius / structure), run "
    'python -m graphite context <file> | impact <file> | query "..." | search "..." BEFORE '
    "grep/glob exploration; manual search is for literal text and filename lookups. Fall "
    "back only when a graph answer proved insufficient, and say so. See GRAPHITE.md."
)

_FRESHNESS_BUDGET_SECONDS = 2.0


def _payload_cwd(payload: dict[str, Any]) -> Path:
    """The directory the session is in. It follows a `cd` in the shell."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return Path.cwd()


def _repo_root(cwd: Path) -> Path:
    """The repository a session in `cwd` belongs to: where its graph is.

    Taking the session's directory for the root made the graph "missing" one
    `cd` below it, and switched the gate off there (#80). The rule is
    `ingest.repository_root`, which the CLI uses too (#81): one definition,
    so a hook and the command it tells the agent to run agree on the root.
    """
    return repository_root(cwd)


def _where_to_run(root: Path, cwd: Path) -> str:
    """One sentence for a session below the repository root, else nothing.

    The commands these hooks print are run where the agent is. The reads
    (`query`, `search`, `context`, `impact`) find the repository's graph from
    below its root (#81). `build .` and `check .` name the directory they are
    typed in and are refused there, so a status that says "rebuild" and names
    no directory sends the agent to a command that fails.
    """
    if cwd == root:
        return ""
    return (
        " This session is below the repository root: queries work from here;"
        f" to build or check the graph, run graphite from `{root}`."
    )


def _activate(root: Path) -> None:
    """Register this repo as open, so the daemon supervises it and leaves the
    rest of the machine alone.

    A directory below a checkout's root is never registered. No build can run
    there (`ingest` refuses it), so the daemon would fail on it at every
    discovery cycle for as long as the marker lived.

    Guarded separately from the caller's own try/except: activation is
    bookkeeping, and this module is fail-open by contract -- a broken registry
    must never cost the agent its session context or its graph-first reminder.
    """
    try:
        if enclosing_git_root(root) not in (None, root):
            return
        mark_active(root, "claude")
    except Exception:
        return


def _freshness_within_budget(root: Path) -> tuple[str, str | None]:
    """Return ``(state, reason)`` within budget, never raising.

    ``state`` is 'fresh' | 'stale' | 'timeout' | 'unknown'.

    'timeout' and 'unknown' are kept apart deliberately (#24). Both used to
    collapse into a single "unknown" message, but they mean different things
    and the timeout case is the one most likely to be hiding a real STALE --
    a check that runs long suggests a large, cold, or unbuilt repo, which is
    exactly the population that tends to be stale. Reporting them identically
    made the reassuring reading the default in the least reassuring case.

    ``reason`` carries ``check_graph_freshness``'s own explanation
    (``engine_changed``, ``missing manifest``, ...) so a STALE message can say
    why without the agent running ``graphite check .`` a second time to find
    out. It is advisory: absent or malformed, the state still stands.
    """
    outcome: list[tuple[str, str | None]] = [("unknown", None)]

    def _worker() -> None:
        try:
            cfg = Config(output_dir=root / "graph-out", cache_dir=root / ".cache" / "graphite")
            status = check_graph_freshness(root, cfg)
            raw = status.get("reason")
            reason = raw if isinstance(raw, str) and raw else None
            outcome[0] = ("stale", reason) if status.get("stale") else ("fresh", reason)
        except Exception:
            outcome[0] = ("unknown", None)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(_FRESHNESS_BUDGET_SECONDS)
    # The thread is a daemon and is never joined again, so a slow check is
    # abandoned rather than waited on. is_alive() is what separates "still
    # running" from "ran and gave up" -- outcome[0] alone cannot.
    if worker.is_alive():
        return ("timeout", None)
    return outcome[0]


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        cwd = _payload_cwd(payload)
        root = _repo_root(cwd)
        _activate(root)
        _open_session_books(root, str(payload.get("session_id") or ""), str(payload.get("source") or ""))
        if (root / "graph-out" / "graph.json").is_file():
            state, reason = _freshness_within_budget(root)
            if state == "stale":
                cause = f" ({reason})" if reason else ""
                status = f"Graph status: STALE{cause} - run `python -m graphite build .` before relying on graph answers."
            elif state == "fresh":
                status = "Graph status: fresh."
            elif state == "timeout":
                status = (
                    f"Graph status: unknown (freshness check did not finish in {_FRESHNESS_BUDGET_SECONDS}s) "
                    "- verify with `python -m graphite check .`."
                )
            else:
                status = (
                    "Graph status: unknown (check ran, could not determine) "
                    "- verify with `python -m graphite check .`."
                )
        else:
            status = "Graph status: missing - run `python -m graphite build .` to create it."
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"{SESSION_CONTRACT}\n{status}{_where_to_run(root, cwd)}",
            }
        }
    except Exception:
        return None


PRE_TOOL_REMINDER = (
    "graph-first: this repo's graphite graph answers relationship questions. For who-calls / "
    "who-reads / data-flow / blast-radius / where-defined questions, use python -m graphite "
    'context <file> | impact <file> | query "..." instead of grepping or globbing across '
    "files. Literal text and filename searches are fine. Falling back after an insufficient "
    "graph answer is allowed - say so when you do."
)

STRICT_SUSPENSION_NOTE = (
    " (strict denial suspended: graph resolution health is low or unknown — "
    "grep fallback allowed.)"
)

MAX_HOOK_GRAPH_BYTES = 32 * 1024 * 1024
_MAX_TOKENS_CHECKED = 5
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


_IDENTIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
# What follows a file's stem: a dot, escaped or not, an extension, and no more
# of a word. The extensions are the ones a name before them is a file's stem
# for; `self\.run` and `graph\.jsonify` end in neither.
_FILE_EXTENSION_RE = re.compile(r"\\?\.([A-Za-z0-9]{1,5})(?![A-Za-z0-9_])")
_FILE_EXTENSIONS = frozenset({
    "py", "pyi", "md", "txt", "json", "jsonl", "toml", "yaml", "yml", "cfg", "ini", "lock", "log",
    "html", "css", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs",
    "sh", "ps1", "cmd", "bat", "xml", "csv", "sql",
})


def _is_literal_fragment(pattern: str, start: int, end: int) -> bool:
    """The identifier at `pattern[start:end]` is one piece of a longer literal.

    `graph-out` is a directory, `src/graphite/query` a path and `graph\\.json`
    a file. Judged piece by piece, each was refused for a symbol it merely
    contains (#72). A piece is skipped only where the text the search matches
    must carry the joining character right beside it, so the search cannot be
    finding the bare name:

    - a hyphen directly before it, or directly after it and followed by more
      of a word: `--no-verify`, `pre-tool-use`. Not `name-?` or `name->`.
    - a slash on either side: `tests/`, `src/pkg`.
    - a file extension after it: `query\\.py`, `uv.lock`.

    The extension itself is still judged, as is every piece of a dotted name
    that is not a file's: `self\\.run` is a search for callers of `run`.
    """
    before = pattern[start - 1] if start else ""
    after = pattern[end:end + 2]
    if before == "-" or (after[:1] == "-" and after[1:] and after[1] in _IDENTIFIER_CHARS):
        return True
    if before == "/" or after[:1] == "/" or after == "\\/":
        return True
    extension = _FILE_EXTENSION_RE.match(pattern, end)
    return extension is not None and extension.group(1).lower() in _FILE_EXTENSIONS


# A regex escape fused to the name after it: `\bname`, `\sname`. Read as one
# identifier, `\bdependents` was `bdependents`, which is no symbol, and the
# search passed (#75). A pair of backslashes is matched first and kept, so
# that `\\bname`, a literal backslash before `bname`, stays whole. A letter
# that is no escape (`\q`) is left alone as well.
_FUSED_ESCAPE_RE = re.compile(r"\\\\|\\[bBdDsSwWAZznrtfv](?=[A-Za-z_])")


def _without_fused_escapes(pattern: str) -> str:
    """`pattern` with each fused escape blanked. The length does not change."""
    return _FUSED_ESCAPE_RE.sub(lambda found: found.group() if found.group() == "\\\\" else "  ", pattern)


def _pattern_tokens(pattern: str, fold_case: bool) -> list[str]:
    """Identifier-like tokens from a grep pattern, deduplicated, capped.

    Two spellings of one word are one token only when the search folds case.
    Otherwise `Done|done` would keep `Done` alone and never check `done`.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    pattern = _without_fused_escapes(pattern)
    for found in _IDENTIFIER_RE.finditer(pattern):
        if _is_literal_fragment(pattern, found.start(), found.end()):
            continue
        token = found.group()
        key = token.lower() if fold_case else token
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
        if len(tokens) >= _MAX_TOKENS_CHECKED:
            break
    return tokens


def _hook_graph(root: Path) -> Any | None:
    """The repo's graph, or None when it is absent or too large to load in a hook."""
    graph_path = root / "graph-out" / "graph.json"
    if not graph_path.is_file() or graph_path.stat().st_size > MAX_HOOK_GRAPH_BYTES:
        return None
    _, graph = load_validated_graph_bundle(graph_path, root=root, max_bytes=MAX_HOOK_GRAPH_BYTES)
    return graph


def _graph_symbol(graph: Any, tokens: list[str], fold_case: bool) -> tuple[str, str] | None:
    """First token that IS something this repo defines (exact id or name), else None.

    A placeholder does not count. `json` is in the graph because a file imports
    it, not because the repo defines it, so there is nothing to query. About a
    quarter of the nodes in graphite's own graph are placeholders, and each of
    their names used to refuse a plain-word search.

    Case is compared the way the search compares it. A case-exact search for
    `DONE` cannot find a function `done`, so it is not a search for one.
    """
    wanted = {(token.lower() if fold_case else token): token for token in tokens}
    for node_id, data in graph.nodes(data=True):
        if is_placeholder(data):
            continue
        for candidate in (node_id, str(data.get("name", ""))):
            key = candidate.lower() if fold_case else candidate
            if key and key in wanted:
                return node_id, wanted[key]
    return None


# Search binaries reachable from the Bash tool. The PreToolUse matcher was
# "Grep|Glob" -- tool NAMES -- so `grep` run through Bash bypassed this hook
# entirely, and consumer agents reported using exactly that route for
# cross-file work. Enforcing on the Grep tool alone enforces a naming
# convention, not a rule.
_SEARCH_COMMANDS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr",
    # PowerShell's grep, plus its alias. The PowerShell tool is a separate
    # tool name from Bash, so it needs its own matcher entry as well.
    "select-string", "sls",
})
# `find -name` is deliberately absent: a filename lookup is not a cross-file
# content search, and the graph-first contract explicitly permits it.
_SHELL_OPERATORS = frozenset({"|", "||", "&&", "&", ";", ";;", "|&"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Flags whose VALUE is the pattern. Without these, `grep -e PAT path` and
# PowerShell's `-Path . -Pattern PAT` put a non-pattern token first and the
# positional guess silently reads the wrong one -- which fails open, i.e. the
# search is allowed through. Honouring them explicitly is what closes that.
_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-pattern", "--pattern"})
# Matched case-SENSITIVELY for the POSIX short forms. The old lookup lowercased
# every token, so `-E` (extended-regex mode, takes no value) collided with `-e`
# (whose value IS the pattern) and `grep -E -i PAT file` read `-i` as the
# pattern. That direction fails open, so it denied nothing -- but it also
# matched nothing it should have.
_PATTERN_FLAGS_EXACT = frozenset({"-e", "--regexp"})
_PATTERN_FLAGS_FOLDED = frozenset({"-pattern", "--pattern"})

# Redirection operators as `shlex(punctuation_chars=True)` emits them. It splits
# `2>/dev/null` into `2`, `>` and `/dev/null`, all three of which then looked
# like path ARGUMENTS to the search.
_REDIRECTION_TOKENS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", ">|", "&>", "&>>"})

# Flags whose value is a NUMBER, not a path. `grep -A 5 file` left `5` in the
# path list. Case-sensitive on purpose: lowercasing `-A` yields `-a`, which is a
# real grep flag that takes no value at all.
_VALUE_FLAGS = frozenset({
    "-A", "-B", "-C", "-m", "-d", "-j",
    "--after-context", "--before-context", "--context",
    "--max-count", "--max-depth", "--threads",
})


def _strip_redirections(argv: list[str]) -> list[str]:
    """Drop redirection operators, their targets, and any file-descriptor digit.

    Without this a single `2>/dev/null` contributed `2`, `>` and `/dev/null` to
    the path list, and `_is_single_file_scope` requires EVERY named path to be an
    existing file -- so one redirection turned a legitimate single-file search
    into a denial whose own message promised that such searches are allowed.
    """
    kept: list[str] = []
    skip_target = False
    for token in argv:
        if skip_target:
            skip_target = False
            continue
        if token in _REDIRECTION_TOKENS:
            # `2>file`: the descriptor was lexed as its own token just before.
            if kept and kept[-1].isdigit():
                kept.pop()
            skip_target = True
            continue
        kept.append(token)
    return kept


def _strip_flag_values(rest: list[str]) -> list[str]:
    """Drop the VALUE that follows a numeric-valued flag, so it is not a path."""
    kept: list[str] = []
    skip_value = False
    for token in rest:
        if skip_value:
            skip_value = False
            continue
        if token in _VALUE_FLAGS:
            skip_value = True
            kept.append(token)
            continue
        kept.append(token)
    return kept


def _repo_scope(root_resolved: Path, target: Path) -> str | None:
    """Where `target` sits against the repository.

    Its path from the root when it is inside, `.` for the root itself and for
    any directory the repository lies under, None when the two are apart.

    A directory above the repository holds all of it, so a search rooted there
    is a whole-repo search. Read as "outside", `..` let a symbol search through.

    Compared case-folded on every platform. A case-insensitive filesystem
    opens `SRC/` as `src/`, and folding can only place more paths inside the
    repository, never fewer, so it cannot create an exemption.
    """
    repo = root_resolved.as_posix().casefold().rstrip("/")
    there = target.resolve().as_posix().casefold().rstrip("/")
    if there == repo or repo.startswith(there + "/"):
        return "."
    if there.startswith(repo + "/"):
        return there[len(repo) + 1:]
    return None


def _is_outside_repository(root: Path, paths: list[str]) -> bool:
    """Every named path lies apart from this repository.

    The graph describes THIS repo. A search rooted anywhere else is not a
    question it can answer, so denying it points the reader at a tool with
    nothing to say -- and names symbols from a repository the search never
    touched. Observed exactly that way: a scratchpad file was refused because
    the PATTERN happened to contain `START`, matching `daemon_start` here.
    """
    if not paths:
        return False
    try:
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover - a repo root that cannot resolve
        return False
    for target in paths:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            if _repo_scope(root_resolved, candidate) is None:
                continue  # apart, keep looking
        except OSError:
            continue
        return False  # at least one path is inside the repo, or holds it
    return True


class _Head(NamedTuple):
    """One command of a command line, with the operators on either side of it."""

    before: str
    argv: list[str]
    after: str


# What a subshell, a command substitution or a here-string leaves behind in the
# line it stood in: `${_3}` for the fourth one found. It holds no name to check
# and, read as a path, it is not there, so it earns no exemption.
_NESTED_RE = re.compile(r"\$\{_(\d+)\}")
# A line that ends in one of these runs on into the next. `|` ends `||` too.
_LINE_CONTINUES_AFTER = ("|", "&&", "|&")
_HEREDOC_WORD_END = frozenset(" \t\r\n;&|()<>")


class _Unreadable(Exception):
    """A quote that never closes. The command is not read, and so not refused."""


def _command_lines(command: str, powershell: bool = False) -> tuple[list[str], list[list[str]]]:
    """The command lines `command` holds, split where its shell splits them,
    and the lines of each subshell or substitution among them, by number.

    The lexer below reads a newline as a space. A multi-line command was one
    command to it, with the first word of the first line as its head, and
    nothing after that word was examined (#75). So the lines are found first:

    - a newline outside quotes ends a line, unless the line ends in `|`, `&&`
      or `||`, or the newline is escaped;
    - the body of a heredoc (`<<EOF`) and a PowerShell here-string (`@'`) are
      text, not commands. A commit message may quote the very search this
      gate refuses;
    - a subshell `( ... )`, a substitution `$( ... )` and, in a POSIX shell,
      a backtick pair hold commands of their own. Those are kept apart, and
      `${_<number>}` stands where they were. They run in a shell of their own
      that starts where the command holding them is, so they are read there
      (`_bash_searches`) and not as lines of this shell;
    - a comment runs to the end of its line.

    Quoted text stays in its line, newlines included. A quote that never
    closes gives no lines at all: this module fails open.
    """
    nested: list[list[str]] = []
    try:
        lines, _ = _scan_command_lines(command, 0, "", powershell, nested)
    except _Unreadable:
        return [], []
    return _stripped(lines), nested


def _stripped(lines: list[str]) -> list[str]:
    return [line for line in (text.strip() for text in lines) if line]


def _heredoc_start(text: str, start: int) -> tuple[str, bool, int] | None:
    """For a `<<` at `start`: its delimiter, whether tabs are stripped, and
    where the word ends. None when it is not `<<` or no word follows it."""
    if not text.startswith("<<", start):
        return None
    index = start + 2
    strip_tabs = text.startswith("-", index)
    if strip_tabs:
        index += 1
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text):
        return None
    if text[index] in "'\"":
        end = text.find(text[index], index + 1)
        return None if end == -1 else (text[index + 1:end], strip_tabs, end + 1)
    word = index + 1 if text[index] == "\\" else index
    end = word
    while end < len(text) and text[end] not in _HEREDOC_WORD_END:
        end += 1
    return None if end == word else (text[word:end], strip_tabs, end)


def _after_heredoc_bodies(text: str, index: int, pending: list[tuple[str, bool]]) -> int:
    """`index` is the start of a line. Where the last pending body ends.

    A body that never closes runs to the end of the text, as it does in the
    shell, so nothing after it is a command.
    """
    for delimiter, strip_tabs in pending:
        while True:
            if index >= len(text):
                return len(text)
            end = text.find("\n", index)
            end = len(text) if end == -1 else end
            line = text[index:end].rstrip("\r")
            index = min(end + 1, len(text))
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return index


def _here_string_end(text: str, index: int) -> int | None:
    """For a PowerShell `@'` or `@"` at `index` that opens a here-string: the
    index just past its closing `'@`. None when it is not one."""
    quote = text[index + 1:index + 2]
    if quote not in ("'", '"'):
        return None
    header_end = text.find("\n", index)
    if header_end == -1 or text[index + 2:header_end].strip(" \t\r"):
        return None
    close = text.find("\n" + quote + "@", header_end)
    if close == -1:
        raise _Unreadable
    return close + 3


def _scan_command_lines(
    text: str, index: int, closer: str, powershell: bool, nested: list[list[str]]
) -> tuple[list[str], int]:
    """The lines of `text[index:]` up to an unquoted `closer`, and where it ended.

    The lines of each subshell and substitution met on the way are added to
    `nested`. One inside another is met first, so it has the lower number.
    """
    lines: list[str] = []
    line: list[str] = []
    pending: list[tuple[str, bool]] = []
    escape = "`" if powershell else "\\"
    length = len(text)

    def set_apart(inner: list[str]) -> None:
        nested.append(_stripped(inner))
        line.append(f"${{_{len(nested) - 1}}}")

    def subshell(start: int, end_char: str) -> int:
        inner, after = _scan_command_lines(text, start, end_char, powershell, nested)
        set_apart(inner)
        return after

    def backticks_open(at: int) -> bool:
        # In PowerShell the backtick is the escape, taken before this is asked.
        return closer != "`" and text.find("`", at + 1) != -1

    while index < length:
        char = text[index]
        if closer and char == closer:
            lines.append("".join(line))
            return lines, index + 1
        if char == escape and index + 1 < length:
            if text[index + 1] == "\n":
                index += 2  # the line goes on
            elif text.startswith("\r\n", index + 1):
                index += 3
            else:
                line.append(text[index:index + 2])
                index += 2
        elif char == "'":
            end = text.find("'", index + 1)
            if end == -1:
                raise _Unreadable
            line.append(text[index:end + 1])
            index = end + 1
        elif char == '"':
            line.append(char)
            index += 1
            while True:
                if index >= length:
                    raise _Unreadable
                inner = text[index]
                if inner == '"':
                    line.append(inner)
                    index += 1
                    break
                if inner == escape and index + 1 < length:
                    line.append(text[index:index + 2])
                    index += 2
                elif text.startswith("$(", index):
                    index = subshell(index + 2, ")")
                elif inner == "`" and backticks_open(index):
                    index = subshell(index + 1, "`")
                else:
                    line.append(inner)
                    index += 1
        elif char == "#" and (not line or line[-1][-1:] in " \t"):
            end = text.find("\n", index)
            index = length if end == -1 else end
        elif powershell and text.startswith("<#", index):
            end = text.find("#>", index + 2)  # a block comment; one never closed runs to the end
            index = length if end == -1 else end + 2
        elif powershell and char == "@" and (after := _here_string_end(text, index)) is not None:
            set_apart([])  # text, and no commands
            index = after
        elif text.startswith("$(", index):
            index = subshell(index + 2, ")")
        elif char == "(":
            index = subshell(index + 1, ")")
        elif char == ")":
            # A `case` arm, or a parenthesis nothing opened. A new command either way.
            lines.append("".join(line))
            line.clear()
            index += 1
        elif char == "`" and backticks_open(index):
            index = subshell(index + 1, "`")
        elif text.startswith("<<<", index):
            line.append("<<<")  # a here-string: its word is on this line, and no body follows
            index += 3
        elif char == "<" and not powershell and (heredoc := _heredoc_start(text, index)) is not None:
            delimiter, strip_tabs, after = heredoc
            pending.append((delimiter, strip_tabs))
            line.append(text[index:after])
            index = after
        elif char == "\n":
            index += 1
            if pending:
                index = _after_heredoc_bodies(text, index, pending)
                pending.clear()
            if "".join(line).rstrip().endswith(_LINE_CONTINUES_AFTER):
                line.append(" ")
            else:
                lines.append("".join(line))
                line.clear()
        else:
            line.append(char)
            index += 1
    lines.append("".join(line))
    return lines, length


def _command_name(word: str) -> str:
    """The program a command's first word names: no directory, no `.exe`, lowercase."""
    name = PurePosixPath(word.replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _bash_command_heads(lines: list[str], powershell: bool = False) -> list[_Head]:
    """Each command in `lines`, the lines of one shell (`_command_lines`).

    `&&`, `||`, `;`, `&` and a pipe each start a new command. The first command
    of a later line carries `"\\n"` as what came before it: like `;`, a newline
    runs the next command whatever the last one did.

    Raises ValueError for a line the lexer cannot read.
    """
    heads: list[_Head] = []
    for number, line in enumerate(lines):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        # The comments are gone already. The lexer's own rule takes any `#` for
        # one, and `echo a#b; ...` and `[ $# -gt 0 ] && ...` lost all that followed.
        lexer.commenters = ""
        if powershell:
            # A backslash is part of a path there, and the backtick escapes.
            # Read as an escape, `C:\repo\src\a.py` lost every backslash and
            # named no file, so a search of that one file was refused.
            lexer.escape = "`"
        current: list[str] = []
        before = "\n" if number else ""
        for token in [*lexer, None]:  # None ends the line
            if token is not None and token not in _SHELL_OPERATORS:
                current.append(token)
                continue
            if current:
                heads.append(_Head(before, current, token or ""))
            before = token or ""
            current = []
    return heads


_CD_COMMANDS = frozenset({"cd", "pushd"})
# What a shell expands before `cd` runs. The hook cannot expand it, so a
# directory spelled with any of these is not followed.
_SHELL_EXPANSION_CHARS = frozenset("$`~*?%")
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")


def _shell_path(text: str, msys: bool) -> Path:
    """`text` as the shell that runs the command will open it.

    Git Bash on Windows spells a drive as a top-level directory: `/c/work` is
    `work` on drive C. Python on Windows reads that as a path on the current
    drive, so a scratch directory was not found at all, and a path into the
    repository read as one outside it.
    """
    if msys:
        drive = _MSYS_DRIVE_RE.match(text)
        if drive:
            text = f"{drive.group(1).upper()}:{text[drive.end():] or '/'}"
    return Path(text)


def _cd_target(start: Path | None, arguments: list[str], msys: bool) -> Path | None:
    """The directory `cd <arguments>` leaves the shell in, or None when unknown.

    `start` is None when the hook does not know where the shell is. From there
    only an absolute path leads somewhere known.
    """
    if len(arguments) != 1 or os.environ.get("CDPATH"):
        return None  # with `CDPATH` set, `cd docs` may land somewhere other than `./docs`
    text = arguments[0]
    if text.startswith("-") or _SHELL_EXPANSION_CHARS.intersection(text):
        return None
    target = _shell_path(text, msys)
    if start is not None:
        target = start / target  # an absolute path replaces `start`
    elif not target.is_absolute():
        return None
    try:
        return target if target.is_dir() else None
    except OSError:  # pragma: no cover - a path the platform cannot stat
        return None


def _located(paths: list[str], base: Path | None, msys: bool) -> list[str]:
    """Each path as the shell will open it.

    After a `cd` the hook followed, a relative path is relative to that
    directory, and a search that names no path searches it.
    """
    if base is None:
        return [str(_shell_path(text, msys)) for text in paths]
    if not paths:
        return [str(base)]
    return [str(base / _shell_path(text, msys)) for text in paths]  # absolute replaces `base`


class _Search(NamedTuple):
    """A search found in a shell command: what it looks for, where, and how."""

    pattern: str
    paths: list[str]
    fold_case: bool


# The only tools modelled as matching case exactly. Every other one is treated
# as folding, which is how this check read all of them before case was
# considered: `Select-String` folds unless told otherwise, and `ag` folds an
# all-lowercase pattern.
#
# Each is listed with the environment variable that hands it options the
# command line does not show. While that variable is set the tool is not known
# to match case exactly either. What the hook sees of the environment is
# partial: the shell that runs the command loads the user's profile, and the
# hook does not.
_CASE_EXACT_TOOLS = {
    "grep": "GREP_OPTIONS",
    "egrep": "GREP_OPTIONS",
    "fgrep": "GREP_OPTIONS",
    "rg": "RIPGREP_CONFIG_PATH",
}
# An inline flag group that turns folding on: `(?i)`, `(?si)`, `(?i:...)`.
_INLINE_FOLD_RE = re.compile(r"\(\?[A-Za-z]*i")


def _search_folds_case(name: str, rest: list[str]) -> bool:
    """Whether the command's own options make the search ignore case.

    Unsure means True. Folding refuses more, never less, so a flag read as
    folding by mistake costs a refusal the search had before, and nothing else.
    That is why a short-flag cluster is searched for its letters without
    working out which of them take a value.

    A flag inside the pattern is read in `_denial_for`, for every route.
    """
    configured_by = _CASE_EXACT_TOOLS.get(name)
    if configured_by is None or os.environ.get(configured_by):
        return True
    for flag in rest:
        if flag.startswith("--"):
            option = flag.split("=", 1)[0]
            # GNU grep accepts any unambiguous prefix of a long option.
            if option == "--smart-case" or (len(option) > 3 and "--ignore-case".startswith(option)):
                return True
        elif flag.startswith("-") and any(letter in flag for letter in "iyS"):
            return True
    return False


def _search_in(name: str, rest: list[str], base: Path | None, msys: bool) -> _Search | None:
    """The search one command runs, else None.

    Deliberately conservative: the first non-flag argument is taken as the
    pattern, which is the positional order of every tool in
    `_SEARCH_COMMANDS`. A flag that takes a separate value (`grep -e X`,
    `rg --glob X`) can mis-identify the pattern, and that is the safe
    direction -- a mis-read pattern simply fails to match a graph symbol and
    nothing is denied.
    """
    rest = _strip_flag_values(rest)
    fold_case = _search_folds_case(name, rest)
    for index, token in enumerate(rest):
        if (
            token in _PATTERN_FLAGS_EXACT or token.lower() in _PATTERN_FLAGS_FOLDED
        ) and index + 1 < len(rest):
            paths = [
                value
                for position, value in enumerate(rest)
                if position not in (index, index + 1) and not value.startswith("-")
            ]
            return _Search(rest[index + 1], _located(paths, base, msys), fold_case)
    positional = [token for token in rest if not token.startswith("-")]
    if not positional:
        return None
    return _Search(positional[0], _located(positional[1:], base, msys), fold_case)


def _unplaced(search: _Search, cwd: Path) -> _Search:
    """`search` when the hook does not know which directory it runs in.

    An absolute path says where it is. A relative one that names a file in the
    session's directory is still one file's text, as it is at the root:
    `ls && grep x notes.md`. A directory, a path that is not there, and a
    search that names no path could be anywhere in the repository. The search
    is then one of all of it, and no path is left to earn an exemption.
    """
    placed: list[str] = []
    for text in search.paths:
        path = Path(text)
        if not path.is_absolute():
            path = cwd / path
            if not path.is_file():
                return search._replace(paths=[])
        placed.append(str(path))
    return search._replace(paths=placed)


# Words that run the command after them, so that a search behind one is still a
# search: `sudo grep`, `env LC_ALL=C grep`, `if grep`. `xargs` belongs here too,
# and is the one of them that is read after a pipe (`_bash_command_heads`).
_PREFIX_WORDS = frozenset({
    "sudo", "doas", "env", "time", "command", "exec", "nice", "ionice", "nohup",
    "stdbuf", "timeout", "xargs",
    "if", "elif", "then", "else", "while", "until", "do", "!", "{",
})
# What may stand between such a word and the command it runs: its own flags,
# `NAME=value`, a number or a duration (`nice -n 5`, `timeout 10s`), and the
# `{}` of `xargs -I {}`. A flag that takes a word (`sudo -u someone`) is not
# read: the command behind it is then not seen, which fails open.
_PREFIX_ARGUMENT_RE = re.compile(r"-.*|[A-Za-z_][A-Za-z0-9_]*=.*|\d+(?:\.\d+)?[smhd]?|\{\}", re.DOTALL)


def _unwrapped(argv: list[str]) -> list[str]:
    """`argv` without the words that only run what follows them."""
    while argv and _command_name(argv[0]) in _PREFIX_WORDS:
        argv = argv[1:]
        while argv and _PREFIX_ARGUMENT_RE.fullmatch(argv[0]):
            argv = argv[1:]
    return argv


_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _find_exec_commands(rest: list[str]) -> list[list[str]]:
    """The commands a `find` runs on what it finds.

    `{}` is a file `find` found under one of its starting points, so those
    stand in for it: `find docs -exec grep word {} +` searches `docs`. With no
    starting point `find` starts where the shell is.
    """
    roots: list[str] = []
    for token in rest:
        if token.startswith("-") or token in ("(", "!"):
            break
        roots.append(token)
    commands: list[list[str]] = []
    index = 0
    while index < len(rest):
        if rest[index] in _FIND_EXEC_FLAGS:
            end = index + 1
            while end < len(rest) and rest[end] not in ("+", ";"):
                end += 1
            ran = [word for word in rest[index + 1:end] if word != "{}"]
            if ran:
                commands.append(ran + roots)
            index = end
        index += 1
    return commands


_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_POWERSHELL_VARIABLE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_VARIABLE_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _assignments(argv: list[str], powershell: bool) -> dict[str, str] | None:
    """What `argv` assigns, when assigning is all it does. Else None.

    `P=x`, `P=x Q=y` and `export P=x`; in PowerShell `$p = 'x'` and `$p='x'`.
    A prefix (`P=x grep $P`) is not one of these. The shell expands `$P`
    before the prefix takes effect, so it tells nothing about the pattern.
    """
    words = argv[1:] if argv[:1] == ["export"] else argv
    if not words:
        return None
    if powershell:
        if len(words) == 3 and words[1] == "=" and _POWERSHELL_VARIABLE_RE.fullmatch(words[0]):
            return {words[0][1:]: words[2]}
        words = [word[1:] if word.startswith("$") else word for word in words]
    found: dict[str, str] = {}
    for word in words:
        assignment = _ASSIGNMENT_RE.fullmatch(word)
        if assignment is None:
            return None
        found[assignment.group(1)] = assignment.group(2)
    return found


def _expanded(token: str, assigned: dict[str, str], powershell: bool) -> str:
    """`token` with each variable the command itself assigned replaced by its text.

    The hook sees `$P` where the shell will see what `P` holds, and `$P` holds
    no name to check (#75). A variable set anywhere else stays as written: in
    the user's profile, by an earlier command, or from a command's output.
    """
    if "$" not in token or not assigned:
        return token

    def text(found: re.Match[str]) -> str:
        name = found.group(1) or found.group(2)
        return assigned.get(name.lower() if powershell else name, found.group())

    return _VARIABLE_RE.sub(text, token)


# Commands that run text the hook does not see. Anything may be set by it.
# `.` is one more, and is matched as written: it has no file name to reduce to.
_RUNS_UNSEEN_TEXT = frozenset({"source", "eval", "invoke-expression", "iex"})
_LEADING_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _forget_what_may_be_set_again(assigned: dict[str, str], argv: list[str], powershell: bool) -> None:
    """Stop trusting each variable a command the hook does not model may set.

    A value the hook read can earn a search an exemption (`D=docs; grep word
    $D`), so it has to be the value the shell will use. `for D in src`,
    `read D` and `D+=/x` each start a word with the name and each change what
    it holds. Naming it is enough: a variable forgotten for no reason costs an
    exemption, and one trusted for no reason gives one away. Once forgotten it
    is read as written, like a variable the command never set.
    """
    if argv and (argv[0] == "." or _command_name(argv[0]) in _RUNS_UNSEEN_TEXT):
        assigned.clear()
        return
    for word in argv:
        named = _LEADING_NAME_RE.match(word[1:] if powershell and word.startswith("$") else word)
        if named is not None:
            assigned.pop(named.group().lower() if powershell else named.group(), None)


# PowerShell commands whose output is the files (or folders) of a path, so that a
# `Select-String` after a pipe searches what they hand it (#84).
_FILE_LISTINGS = frozenset({"get-childitem", "gci", "ls", "dir"})
# The searches that take files from the pipeline. `grep` does not: it reads the
# lines of what is piped, so `ls | grep name` filters a list of names.
_POWERSHELL_SEARCHES = frozenset({"select-string", "sls"})
# What may stand between such a listing and the search and still hand the search
# files: a stage that only keeps or orders them. Any other stage (`ForEach-Object`,
# `Out-String`, `Select-Object -ExpandProperty`) may make text, and ends the chain.
_FILE_KEEPING_STAGES = frozenset({
    "where-object", "where", "?", "select-object", "select", "sort-object", "sort",
})
# Listing flags that take a word, so the word is not a path. `-Path` and
# `-LiteralPath` are the exceptions: their word IS one.
_LISTING_VALUE_FLAGS = frozenset({
    "-filter", "-include", "-exclude", "-depth", "-attributes",
})
_LISTING_PATH_FLAGS = frozenset({"-path", "-literalpath"})


def _listed_paths(name: str, rest: list[str]) -> list[str] | None:
    """The paths a file listing names, else None where `name` lists no files.

    An empty list is a listing of the current directory. `-Name` makes the
    listing text, and what is piped on is then a filter of it.
    """
    if name not in _FILE_LISTINGS:
        return None
    paths: list[str] = []
    expecting = False
    skipping = False
    for token in rest:
        lowered = token.lower()
        if expecting or skipping:
            if expecting:
                paths.append(token)
            expecting = skipping = False
        elif lowered == "-name" or lowered == "-n":
            return None
        elif lowered in _LISTING_PATH_FLAGS:
            expecting = True
        elif lowered in _LISTING_VALUE_FLAGS:
            skipping = True
        elif not token.startswith("-"):
            paths.append(token)
    return paths


def _bash_searches(
    command: str, root: Path, msys: bool = False, cwd: Path | None = None, powershell: bool = False
) -> list[_Search]:
    """Every search `command` runs, a filter after a pipe apart.

    All of them, not the first: `grep x notes.md ; grep -rn <symbol> src/` is
    two searches, and judging one let the other through behind it.

    A search after a pipe filters the output of the command before it and does
    not search the repository -- `graphite query ... | grep name` is the
    obvious case, and denying it would break the very commands the denial
    message recommends. One command after a pipe is read: `xargs`. What it
    reads from the pipe is a list of file names, and the command it runs
    searches those files.

    A search is looked for behind what used to hide one (#75): on a later line
    (`_command_lines`), behind a word that runs the next command
    (`_unwrapped`), in what `find -exec` and `xargs` run, in a variable the
    command itself assigns (`_expanded`), and inside a subshell or a
    substitution. Those two run in a shell of their own. It starts where the
    command that holds them is, with that command's variables, and nothing it
    does reaches the shell outside it.

    A `cd` is followed only where the search cannot run without it having
    worked: `cd <dir> && ...`, with every command up to the search joined by
    `&&`. After `;`, `||` or `&` the search runs in the old directory if the
    `cd` failed, and a `cd` in a pipeline moves a subshell. The directory then
    holds only through another `cd` or a search. Any other command may be a
    builtin, an alias or a function that moves the shell again, so
    `cd docs && ls && grep ...` is not followed. In every such case, and when
    the directory is not one the hook can find, paths resolve against the
    repository root as they did before `cd` was read at all.

    `cwd` is the directory the shell starts in when that is below the root
    (#80). The line's first command runs there, and so does a search reached
    through searches alone. Once a `cd` or a command the hook does not model
    has run, the shell may be anywhere. At the root that costs nothing, since
    the root holds every directory it could be in. Below the root the session's
    directory holds only itself: from a `docs/` with no code in it,
    `cd ../src ; grep` would pass as a search of `docs/`. There the search
    keeps only the paths that do not depend on where the shell is (`_unplaced`).
    """
    lines, nested = _command_lines(command, powershell)
    searches: list[_Search] = []

    def read(lines: list[str], holds: int, start: Path | None, start_placed: bool, assigned: dict[str, str]) -> None:
        """Add the searches one shell runs.

        The shell starts in `start` (None is the repository root), or the hook
        does not know where (`start_placed`), with the variables `assigned`:
        each as the text it was given. Its lines can hold the first `holds` of
        `nested`.
        """
        base, placed = start, start_placed  # `base` is where the shell is
        moved = False  # a `cd`, or a command the hook does not model, has run in this shell
        pipeline_base, pipeline_placed = base, placed  # where the pipeline being read started
        listed: list[str] | None = None  # the paths of the file listing whose output the stage before gives
        for head in _bash_command_heads(lines, powershell):
            piped = head.before in ("|", "|&")  # `||` is or-else, not a pipe
            if not piped:
                listed = None
                if head.before != "&&":
                    base, placed = (None, False) if moved else (start, start_placed)
                pipeline_base, pipeline_placed = base, placed
            # Every stage of a pipeline starts where the pipeline did. A stage
            # runs in a shell of its own and cannot move the one after it.
            here, here_placed = pipeline_base, pipeline_placed
            for token in head.argv:
                for number in map(int, _NESTED_RE.findall(token)):
                    if number < holds:  # found before these lines were: nothing holds itself
                        read(nested[number], number, here, here_placed, dict(assigned))
            if len(head.argv) == 1 and _NESTED_RE.fullmatch(head.argv[0]):
                continue  # a subshell standing alone
            if piped and _command_name(head.argv[0]) != "xargs":
                stage = _command_name(head.argv[0])
                if listed is not None and stage in _POWERSHELL_SEARCHES:
                    # `Get-ChildItem -Recurse src | Select-String name` searches
                    # the files it is handed (#84), as `xargs grep` does.
                    found = _search_in(stage, [_expanded(t, assigned, powershell) for t in head.argv[1:]], here, msys)
                    if found is not None:
                        found = found._replace(paths=_located(listed, here, msys))
                        searches.append(found if here_placed or cwd is None else _unplaced(found, cwd))
                if stage not in _FILE_KEEPING_STAGES:
                    listed = None
                continue  # a filter: it reads what the command before it wrote
            assignments = _assignments(head.argv, powershell)
            if assignments is not None:
                for variable, text in assignments.items():
                    variable = variable.lower() if powershell else variable
                    if head.before in ("&&", "||"):
                        # It runs or it does not, and the value is one of two.
                        assigned.pop(variable, None)
                        continue
                    # `D="$S/notes"` reads through `S`. An expansion the hook cannot
                    # read stays in the text, and is judged where the variable is
                    # used as it would be if it were written there.
                    assigned[variable] = _expanded(text, assigned, powershell)
                continue
            argv = head.argv
            while argv and _ENV_ASSIGNMENT_RE.match(argv[0]):
                argv = argv[1:]
            written = _strip_redirections(argv)
            argv = _unwrapped(written)
            if not argv:
                continue
            name = _command_name(argv[0])
            rest = [_expanded(token, assigned, powershell) for token in argv[1:]]
            listed = _listed_paths(name, rest) if len(argv) == len(written) else None
            # A `cd` behind a wrapper is not the shell's: `env cd docs` moves a
            # program that then exits, and `! cd docs && ...` goes on only when
            # the `cd` failed. It is read below, as a command that may move.
            if name in _CD_COMMANDS and len(argv) == len(written):
                moved = True
                # At the root an unplaced shell is read as being at the root. Below
                # it, only an absolute path leads somewhere known.
                origin = base or (root if cwd is None else None)
                base = _cd_target(origin, rest, msys) if head.after == "&&" else None
                placed = base is not None
                continue
            if name == "git" and rest and rest[0] == "grep":
                name, rest = "grep", rest[1:]
            ran = [(name, rest)]
            if name == "find":
                ran = [
                    (_command_name(inner[0]), inner[1:])
                    for inner in map(_unwrapped, _find_exec_commands(rest))
                    if inner
                ]
            for tool, arguments in ran:
                if tool not in _SEARCH_COMMANDS:
                    continue
                found = _search_in(tool, arguments, here, msys)
                if found is not None:
                    searches.append(found if here_placed or cwd is None else _unplaced(found, cwd))
            if name not in _SEARCH_COMMANDS:
                # `popd`, `builtin cd ..`, a function: the shell may have moved
                base, placed, moved = None, False, True
                _forget_what_may_be_set_again(assigned, head.argv, powershell)

    try:
        read(lines, len(nested), cwd, True, {})
    except ValueError:
        return []  # a line the lexer cannot read: fail open, never break the user's shell
    return searches


def _is_single_file_scope(root: Path, paths: list[str]) -> bool:
    """Every named path exists and is a file. Literal-text searches scoped to
    one file are always allowed -- the graph answers relationships, not what a
    specific file literally contains."""
    if not paths:
        return False
    for target in paths:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.is_file():
            return False
    return True


def _holds_no_code(root: Path, paths: list[str], graph: Any) -> bool:
    """No directory the search names holds a source file the graph models.

    The graph answers questions about code. Under a `docs/` of Markdown and
    JSON there is no definition and no call site, so a word found there is
    text, whatever it names elsewhere in the repo.

    Every named path must exist. A glob or a shell variable reaches the hook
    unexpanded, so what it will match is unknown, and "no code under a path
    that is not there" must not read as "no code": that would let `src/*.py`
    through.

    Which files hold code is read from the extension. A document has a file
    node and nothing else, so any other node counts as code whatever its file
    is called.
    """
    if not paths:
        return False
    try:
        root_resolved = root.resolve()
    except OSError:  # pragma: no cover - a repo root that cannot resolve
        return False
    scopes: list[str] = []
    for target in paths:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            continue  # a named file is literal text, as in `_is_single_file_scope`
        if not candidate.is_dir():
            return False
        try:
            scope = _repo_scope(root_resolved, candidate)
        except OSError:  # pragma: no cover - a directory that cannot resolve
            return False
        if scope is None:
            continue  # a directory apart from the repo holds none of its code
        scopes.append(scope)
    for _node_id, data in graph.nodes(data=True):
        source_file = data.get("source_file")
        if not isinstance(source_file, str):
            continue
        if _edge_language(source_file) == "other" and data.get("kind") == "file":
            continue  # a document
        modelled = source_file.replace("\\", "/").casefold()
        for scope in scopes:
            if scope == "." or modelled.startswith(scope + "/"):
                return False
    return True


def _denial_for(root: Path, searches: list[_Search]) -> str | None:
    """Shared by the Grep and Bash routes so both enforce the same rule.

    One refused search refuses the command. The graph is loaded at most once,
    and only if some search gets as far as needing it.
    """
    graph: Any | None = None
    for pattern, paths, fold_case in searches:
        if not pattern:
            continue
        if _is_single_file_scope(root, paths):
            continue
        if _is_outside_repository(root, paths):
            continue
        # `(?i)` folds whatever the tool's own flags say, on the Grep tool and in a shell.
        fold_case = fold_case or _INLINE_FOLD_RE.search(pattern) is not None
        tokens = _pattern_tokens(pattern, fold_case)
        if not tokens:
            continue
        if graph is None:
            graph = _hook_graph(root)
            if graph is None:
                return None
        match = _graph_symbol(graph, tokens, fold_case)
        if match is None:
            continue
        if _holds_no_code(root, paths, graph):
            continue
        node_id, token = match
        return (
            f"graphite-first (strict): '{token}' is a symbol in this repo's code graph "
            f"({node_id}). Use the graph instead of cross-file grep: "
            f'python -m graphite query "callers {token}" | python -m graphite query "calls {token}" | '
            f'python -m graphite search "{token}" | python -m graphite context <file>. '
            "Literal-text searches scoped to a single file path are always allowed."
        )
    return None


def _strict_denial(payload: dict[str, Any], root: Path, cwd: Path | None = None) -> str | None:
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return None
        target = tool_input.get("path")
        paths = [target] if isinstance(target, str) and target else []
        if cwd is not None:
            # Below the root: with no `path` the tool searches the session's
            # directory, and a relative one starts there.
            paths = _located(paths, cwd, False)
        # The Grep tool matches case exactly unless `-i` is set. A value that
        # is not plainly "off" is read as on: unsure folds.
        # The tool is built on ripgrep, so it is read as `rg` with no flags of
        # its own. Whether it reads ripgrep's config file is not known here,
        # and a set `RIPGREP_CONFIG_PATH` therefore folds.
        fold_case = tool_input.get("-i") not in (None, False) or _search_folds_case("rg", [])
        return _denial_for(root, [_Search(pattern, paths, fold_case)])
    except Exception:
        return None


MAX_CURSOR_SESSIONS = 20


def _keep_newest_sessions(sessions: dict[str, Any]) -> None:
    while len(sessions) > MAX_CURSOR_SESSIONS:
        sessions.pop(next(iter(sessions)))


def _open_session_books(root: Path, session_id: str, source: str) -> None:
    """Start a new session's savings books where the usage ledger ends now.

    The ledger holds every session's entries, so a session that counted from
    byte 0 took all of them as its own (#108). A session the cursor already
    knows keeps its totals: SessionStart fires again on a resume or a compact.
    On a resume its books move to the ledger's end as well: what the ledger
    gained while it was away is other sessions' use, and a turn of its own
    whose Stop never fired is not counted. A compact falls inside a turn, so
    the books stay where they are.
    """
    try:
        if not session_id:
            return
        sessions = usage_ledger.read_cursor(root).get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        known = sessions.pop(session_id, None)
        if isinstance(known, dict) and source != "resume":
            return
        ledger = usage_ledger.ledger_path(root)
        ledger_stat = ledger.stat() if ledger.is_file() else None
        books = known if isinstance(known, dict) else {"tokens": 0, "seconds": 0.0}
        books["offset"] = ledger_stat.st_size if ledger_stat is not None else 0
        books["ino"] = ledger_stat.st_ino if ledger_stat is not None else None
        sessions[session_id] = books
        _keep_newest_sessions(sessions)
        usage_ledger.write_cursor(root, {"sessions": sessions})
    except Exception:
        return


def _entries_between(path: Path, start: int, end: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        handle.seek(start)
        blob = handle.read(max(0, end - start))
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _entry_is_this_session(entry: dict[str, Any], session_id: str) -> bool:
    """Whether a ledger entry is this session's own use.

    An entry that names no session is counted, as it was: a person running
    graphite in a terminal in the same repository is real use, and dropping
    it would understate what the graph saved. An entry that names another
    session is that session's, and where two sessions are open at once each
    one's Stop would otherwise claim the other's use as its own (#110).
    """
    tag = entry.get("session")
    if not isinstance(tag, str) or not tag:
        return True
    return tag == session_id


def _cursor_number(value: Any, cast: type) -> Any:
    """Coerce a possibly-corrupted cursor field; non-numeric (or bool) -> 0.

    A raw ``int(...)``/``float(...)`` on a corrupted (e.g. hand-edited or
    concurrently-truncated) cursor field would raise before ``write_cursor``
    runs, permanently stalling the session (offset never advances, same
    failure repeats every turn). Guarding here lets the cursor self-heal.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return cast(0)
    return cast(value)


def handle_stop(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Turn-end savings summary; silent unless graphite was used this turn."""
    try:
        root = _repo_root(_payload_cwd(payload))
        # Stop fires once per assistant turn, which makes it the heartbeat that
        # keeps a working session inside the activation TTL. It must run before
        # the session_id guard below, which returns early on most turns.
        _activate(root)
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        ledger = usage_ledger.ledger_path(root)
        ledger_stat = ledger.stat() if ledger.is_file() else None
        size = ledger_stat.st_size if ledger_stat is not None else 0
        # Rotation (usage_ledger.record_usage) replaces the file at the same path with a
        # brand-new one; the new generation can coincidentally match the old byte offset
        # (e.g. identical repeated entries), so size alone can't detect it. st_ino changes
        # across the replace even when size doesn't, so it's used as the rotation signal.
        current_ino = ledger_stat.st_ino if ledger_stat is not None else None
        cursor = usage_ledger.read_cursor(root)
        sessions = cursor.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        state = sessions.pop(session_id, None)
        if not isinstance(state, dict):
            # No books were opened for this session: its SessionStart was not
            # seen, or the session was pruned from the cursor. What the ledger
            # holds now is other sessions' history (#108), so the books start
            # at its end. This turn's own entries are lost; none are invented.
            state = {"offset": size, "tokens": 0, "seconds": 0.0, "ino": current_ino}
        offset = state.get("offset", 0)
        stored_ino = state.get("ino")
        # A stored `ino` that isn't an int (missing key on the older 3-key cursor
        # schema, or a corrupted value) never counts as rotation, nor does a current
        # ino of 0/None (filesystem doesn't report inodes) -- both fall back to the
        # offset-only heuristic below. That's fail-safe: no false resync/overcount
        # from an absent or unsupported inode, at the cost of not detecting a
        # same-size rotation on such filesystems (same limitation the size-only
        # check always had).
        rotated = (
            isinstance(stored_ino, int)
            and not isinstance(stored_ino, bool)
            and bool(current_ino)
            and stored_ino != current_ino
        )
        if not isinstance(offset, int) or offset > size or rotated:
            offset = 0  # ledger rotated or cursor damaged; resync
        new_entries = _entries_between(ledger, offset, size) if size > offset else []
        # The ledger holds every session's entries, and where two sessions are
        # open at once each one's Stop must claim only its own use (#110).
        own_entries = [entry for entry in new_entries if _entry_is_this_session(entry, session_id)]
        turn_tokens = 0
        turn_seconds = 0.0
        for entry in own_entries:
            est = savings_model.estimate_entry(entry)
            turn_tokens += est["tokens_saved"]
            turn_seconds += est["seconds_saved"]
        state["offset"] = size
        state["ino"] = current_ino
        state["tokens"] = _cursor_number(state.get("tokens", 0), int) + turn_tokens
        state["seconds"] = _cursor_number(state.get("seconds", 0.0), float) + turn_seconds
        sessions[session_id] = state  # re-insert -> newest position
        _keep_newest_sessions(sessions)
        usage_ledger.write_cursor(root, {"sessions": sessions})
        if not own_entries or not usage_ledger.savings_display_enabled(root):
            return None
        turn_text = savings_model.format_compact(turn_tokens, turn_seconds)
        session_text = savings_model.format_compact(state["tokens"], state["seconds"])
        return {
            "systemMessage": (
                f"graphite: est. {turn_text} saved this turn "
                f"(session: {session_text}) [estimates]"
            )
        }
    except Exception:
        return None


def handle_pre_tool_use(payload: dict[str, Any], mode: str) -> dict[str, Any] | None:
    try:
        tool_name = payload.get("tool_name")
        if tool_name not in ("Grep", "Glob", "Bash", "PowerShell"):
            return None
        cwd = _payload_cwd(payload)
        root = _repo_root(cwd)
        if not (root / "graph-out" / "graph.json").is_file():
            return None
        # Two directories, not one: the graph is at `root`, and a search's
        # relative paths start where the shell is.
        below = None if cwd == root else cwd
        where = _where_to_run(root, cwd)
        denial: str | None = None
        if tool_name in ("Bash", "PowerShell"):
            # The matcher now routes EVERY Bash call here, so anything that is
            # not a repo-wide search must leave silently -- not even a
            # reminder. Nagging `git status` is how a real warning gets
            # ignored, which is the same rule check_hooks applies to repos
            # that never onboarded.
            tool_input = payload.get("tool_input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            # On Windows the Bash tool is Git Bash, which spells drives `/c/...`.
            msys = tool_name == "Bash" and os.name == "nt"
            searches = (
                _bash_searches(command, root, msys, below, tool_name == "PowerShell")
                if isinstance(command, str)
                else []
            )
            if not searches:
                return None
            if mode == "strict":
                denial = _denial_for(root, searches)
        elif mode == "strict" and tool_name == "Grep":
            denial = _strict_denial(payload, root, below)
        if denial is not None:
            health = persisted_resolution(
                root,
                on_error=lambda exc: record_incident(
                    repo_ledger_dir(root),
                    klass="build",
                    code="artifact_malformed",
                    subject=".graphite_analysis.json",
                    detail=str(exc),
                ),
            )
            if isinstance(health, dict) and health.get("healthy") is True:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial + where,
                    }
                }
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": PRE_TOOL_REMINDER + STRICT_SUSPENSION_NOTE + where,
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": PRE_TOOL_REMINDER + where,
            }
        }
    except Exception:
        return None
