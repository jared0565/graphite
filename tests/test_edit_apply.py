"""The shared, provider-agnostic whole-file apply engine lives in edit_apply."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphite.routing.claude_executor import AdapterError
from graphite.routing.edit_apply import (
    EDIT_RESULT_MARKER,
    MAX_EDIT_FILE_BYTES,
    apply_whole_file_edit,
)


def test_result_marker_value():
    assert EDIT_RESULT_MARKER == "GRAPHITE_EDIT_OK"
    assert MAX_EDIT_FILE_BYTES == 1_048_576


def test_openrouter_reexports_are_the_same_objects():
    from graphite.routing import edit_apply, openrouter_executor

    assert openrouter_executor.apply_whole_file_edit is edit_apply.apply_whole_file_edit
    assert openrouter_executor.EDIT_RESULT_MARKER is edit_apply.EDIT_RESULT_MARKER
    assert openrouter_executor.MAX_EDIT_FILE_BYTES == edit_apply.MAX_EDIT_FILE_BYTES


def test_applies_a_single_file_atomically(tmp_path: Path):
    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    payload = {
        "files": [{"path": "a.py", "content": "new\n"}],
        "result": EDIT_RESULT_MARKER,
    }
    applied = apply_whole_file_edit(
        workspace=tmp_path, payload=payload, edit_scope=("a.py",), max_total_bytes=4096
    )
    assert applied == ("a.py",)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"


def test_rejects_traversal_path(tmp_path: Path):
    payload = {
        "files": [{"path": "../escape.py", "content": "x\n"}],
        "result": EDIT_RESULT_MARKER,
    }
    with pytest.raises(AdapterError) as info:
        apply_whole_file_edit(
            workspace=tmp_path,
            payload=payload,
            edit_scope=("../escape.py",),
            max_total_bytes=4096,
        )
    assert info.value.code == "edit_scope_violation"
