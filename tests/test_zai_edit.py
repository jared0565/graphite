"""Parser: z.ai plain-text whole-file edit response -> apply payload."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.edit_apply import EDIT_RESULT_MARKER, apply_whole_file_edit
from graphite.routing.zai_edit import (
    EDIT_BEGIN_TEMPLATE,
    EDIT_END_TEMPLATE,
    parse_whole_file_edit_text,
)


def _block(path: str, content: str) -> str:
    return (
        EDIT_BEGIN_TEMPLATE.format(path=path)
        + "\n"
        + content
        + EDIT_END_TEMPLATE.format(path=path)
        + "\n"
    )


def _message(*blocks: str, sentinel: bool = True) -> str:
    body = "".join(blocks)
    return body + (f"{EDIT_RESULT_MARKER}\n" if sentinel else "done\n")


def test_templates_and_sentinel():
    assert EDIT_BEGIN_TEMPLATE.format(path="a/b.py") == "===GRAPHITE BEGIN FILE a/b.py==="
    assert EDIT_END_TEMPLATE.format(path="a/b.py") == "===GRAPHITE END FILE a/b.py==="
    assert EDIT_RESULT_MARKER == "GRAPHITE_EDIT_OK"


def test_parses_two_files_ordered_by_scope():
    msg = _message(_block("y.py", "def y():\n    return 2\n"),
                   _block("x.py", "def x():\n    return 1\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    assert payload["result"] == EDIT_RESULT_MARKER
    assert [f["path"] for f in payload["files"]] == ["x.py", "y.py"]
    assert payload["files"][0]["content"] == "def x():\n    return 1\n"
    assert payload["files"][1]["content"] == "def y():\n    return 2\n"


def test_trailing_newline_preserved_byte_exact():
    msg = _message(_block("a.py", "line1\nline2\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == "line1\nline2\n"


def test_content_with_pseudo_marker_line_not_matching_path():
    # A content line containing '===' but not a real path-qualified marker.
    body = "x = '=== not a marker ==='\n"
    msg = _message(_block("a.py", body))
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == body


def test_missing_sentinel_rejected():
    msg = _message(_block("a.py", "x\n"), sentinel=False)
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_missing_one_block_rejected():
    msg = _message(_block("a.py", "x\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py", "b.py"))
    assert info.value.code == "response_contract_invalid"


def test_extra_out_of_scope_block_rejected():
    msg = _message(_block("a.py", "x\n"), _block("evil.py", "y\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_duplicate_path_rejected():
    msg = _message(_block("a.py", "x\n"), _block("a.py", "y\n"))
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_begin_without_matching_end_rejected():
    msg = (
        EDIT_BEGIN_TEMPLATE.format(path="a.py") + "\nx\n"
        + f"{EDIT_RESULT_MARKER}\n"
    )
    with pytest.raises(AdapterError) as info:
        parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert info.value.code == "response_contract_invalid"


def test_empty_and_non_str_rejected():
    for bad in ("", 123, None):
        with pytest.raises(AdapterError) as info:
            parse_whole_file_edit_text(bad, edit_scope=("a.py",))  # type: ignore[arg-type]
        assert info.value.code == "response_contract_invalid"


def test_parsed_payload_applies_via_apply_engine(tmp_path: Path):
    (tmp_path / "x.py").write_text("old x\n", encoding="utf-8")
    (tmp_path / "y.py").write_text("old y\n", encoding="utf-8")
    msg = _message(_block("x.py", "new x\n"), _block("y.py", "new y\n"))
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    applied = apply_whole_file_edit(
        workspace=tmp_path, payload=payload, edit_scope=("x.py", "y.py"),
        max_total_bytes=4096,
    )
    assert set(applied) == {"x.py", "y.py"}
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "new x\n"
    assert (tmp_path / "y.py").read_text(encoding="utf-8") == "new y\n"


def test_preamble_and_interstitial_prose_tolerated():
    # The live smoke relies on this: models emit prose despite instructions.
    msg = (
        "Here are the edited files:\n"
        + _block("x.py", "new x\n")
        + "\nAnd the second one:\n\n"
        + _block("y.py", "new y\n")
        + f"{EDIT_RESULT_MARKER}\n"
    )
    payload = parse_whole_file_edit_text(msg, edit_scope=("x.py", "y.py"))
    assert [f["path"] for f in payload["files"]] == ["x.py", "y.py"]
    assert payload["files"][0]["content"] == "new x\n"
    assert payload["files"][1]["content"] == "new y\n"


def test_trailing_whitespace_on_marker_lines_tolerated():
    msg = (
        "===GRAPHITE BEGIN FILE a.py===  \n"
        "body\n"
        "===GRAPHITE END FILE a.py===\t\n"
        f"{EDIT_RESULT_MARKER}\n"
    )
    payload = parse_whole_file_edit_text(msg, edit_scope=("a.py",))
    assert payload["files"][0]["content"] == "body\n"
