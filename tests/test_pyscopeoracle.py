"""Known-answer tests for `scripts/pyscopeoracle.py` (#70, #71).

The oracle's figures gate the phase 1 release (spec §5: zero wrong-target
bare-name calls on graphite and Django) and go into its release notes, so the
instrument and its known answers live here together. Its truth side is checked
against hand-derived CPython answers; its pairing side against today's engine,
which must show the planted defects (the CONTROL: an oracle that reads 0 on an
engine known to be wrong sees nothing).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ORACLE_PATH = Path(__file__).parents[1] / "scripts" / "pyscopeoracle.py"


def _load_oracle():
    """Import the oracle from its path; `scripts/` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("_pyscopeoracle", ORACLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def oracle():
    return _load_oracle()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


#: The #70 / #71 shapes in one file. Line numbers are load-bearing.
SCOPE_FIXTURE = (
    "def test_one():\n"  # 1
    "    def fake_build():\n"  # 2
    "        return 1\n"  # 3
    "    return fake_build()\n"  # 4
    "\n"  # 5
    "\n"  # 6
    "def test_two():\n"  # 7
    "    def fake_build():\n"  # 8
    "        return 2\n"  # 9
    "    return fake_build()\n"  # 10
    "\n"  # 11
    "\n"  # 12
    "def generate():\n"  # 13
    "    return 0\n"  # 14
    "\n"  # 15
    "\n"  # 16
    "def route_c(generate):\n"  # 17
    "    return generate()\n"  # 18
    "\n"  # 19
    "\n"  # 20
    "class K:\n"  # 21
    "    def run(self):\n"  # 22
    "        return 1\n"  # 23
    "\n"  # 24
    "    def go(self):\n"  # 25
    "        return run()\n"  # 26
)


def _truths(oracle, tmp_path: Path, files: dict[str, str]) -> dict:
    for rel, text in files.items():
        _write(tmp_path / rel, text)
    model = oracle.TruthModel(oracle.load(tmp_path))
    return {rel: dict(model.sites(rel)) for rel in files}


def test_cpython_truth_for_the_scope_fixture(oracle, tmp_path):
    sites = _truths(oracle, tmp_path, {"m.py": SCOPE_FIXTURE})["m.py"]
    assert sites[(4, "fake_build")] == [("def", "m.py", 2)]
    assert sites[(10, "fake_build")] == [("def", "m.py", 8)]
    assert sites[(18, "generate")] == [("value",)]
    assert sites[(26, "run")] == [("none",)]


def test_cpython_truth_for_comprehensions_and_class_bodies(oracle, tmp_path):
    source = (
        "def g(): pass\n"  # 1
        "def f(xs):\n"  # 2
        "    [g() for g in xs]\n"  # 3
        "    return g()\n"  # 4
        "class K:\n"  # 5
        "    def m(): return [1]\n"  # 6
        "    a = m()\n"  # 7
        "    b = [m() for _ in range(3)]\n"  # 8
        "    c = [y for y in m()]\n"  # 9
    )
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(3, "g")] == [("value",)]
    assert sites[(4, "g")] == [("def", "m.py", 1)]
    assert sites[(7, "m")] == [("def", "m.py", 6)]
    assert sites[(8, "m")] == [("none",)]
    assert sites[(9, "m")] == [("def", "m.py", 6)]


def test_a_rebound_global_is_excluded_not_scored(oracle, tmp_path):
    source = "def handler(): pass\ndef setup():\n    global handler\n    handler = make()\nhandler()\n"
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(5, "handler")] == [("rebound",)]


def test_a_name_an_enclosing_function_rebinds_nonlocal_is_excluded_not_scored(oracle, tmp_path):
    """Django's `_deferredSkip`: the MIDDLE function declares `nonlocal` and then defines it."""
    source = (
        "def outer(condition):\n"  # 1
        "    def decorator():\n"  # 2
        "        nonlocal condition\n"  # 3
        "        def wrapper():\n"  # 4
        "            return condition()\n"  # 5
        "        def condition():\n"  # 6
        "            return 1\n"  # 7
        "        return wrapper\n"  # 8
        "    return decorator\n"  # 9
    )
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(5, "condition")] == [("rebound",)]


def test_a_nonlocal_declaration_alone_looks_further_out(oracle, tmp_path):
    source = (
        "def outer(condition):\n"  # 1
        "    def middle():\n"  # 2
        "        nonlocal condition\n"  # 3
        "        def inner():\n"  # 4
        "            return condition()\n"  # 5
        "        return inner\n"  # 6
        "    return middle\n"  # 7
    )
    sites = _truths(oracle, tmp_path, {"m.py": source})["m.py"]
    assert sites[(5, "condition")] == [("value",)]


def test_truth_follows_one_import_hop_and_marks_a_reexport(oracle, tmp_path):
    files = {
        "pkg/__init__.py": "from pkg.impl import f\n",
        "pkg/impl.py": "def f():\n    return 1\n",
        "direct.py": "from pkg.impl import f\nf()\n",
        "via_init.py": "from pkg import f\nf()\n",
        "ext.py": "from json import loads\nloads()\n",
    }
    truths = _truths(oracle, tmp_path, files)
    assert truths["direct.py"][(2, "f")] == [("def", "pkg/impl.py", 1)]
    assert truths["via_init.py"][(2, "f")] == [("def", "pkg/impl.py", 1, "reexport")]
    assert truths["ext.py"][(2, "loads")] == [("external",)]


@pytest.mark.parametrize(
    ("truth", "bound", "category"),
    [
        (("def", "m.py", 2), "m.py:2", "correct"),
        (("def", "m.py", 8), "m.py:2", "WRONG-def->other-def"),
        (("def", "m.py", 8), None, "unbound-but-def"),
        (("def", "p.py", 1, "reexport"), None, "unbound-but-def(reexport)"),
        (("value",), None, "correct-unbound"),
        (("value",), "m.py:13", "WRONG-value-bound"),
        (("none",), "m.py:22", "WRONG-none-bound"),
        (("external",), "m.py:3", "WRONG-external-bound"),
        (("ambiguous",), "m.py:3", "truth-ambiguous"),
        (("rebound",), None, "truth-rebound"),
    ],
)
def test_classify(oracle, truth, bound, category):
    assert oracle.classify(truth, bound) == category


def test_todays_engine_mis_binds_the_three_planted_sites(oracle, tmp_path):
    """CONTROL. Today's engine binds #70's second call, and both #71 shapes, wrongly."""
    _write(tmp_path / "m.py", SCOPE_FIXTURE)
    report = oracle.measure_sites(oracle.load(tmp_path))
    assert report["stats"] == {
        "sites": 4,
        "correct": 1,
        "WRONG-def->other-def": 1,
        "WRONG-value-bound": 1,
        "WRONG-none-bound": 1,
    }
    assert report["wrong"] == 3
    assert report["selfcheck"]["call_nodes"] == report["selfcheck"]["distinct_call_ids"] == 4
    assert report["selfcheck"]["symtable_disagreements"] == 0


def test_a_report_names_the_oracle_that_wrote_it(oracle, tmp_path):
    """Two reports differ for an engine reason only if they came from the same oracle."""
    import hashlib
    import json

    _write(tmp_path / "corpus" / "m.py", "def f(): pass\nf()\n")
    out = tmp_path / "sites.json"
    assert oracle.main(["sites", str(tmp_path / "corpus"), "--out", str(out)]) == 0
    engine = json.loads(out.read_text(encoding="utf-8"))["engine"]
    assert engine["oracle_sha256"] == hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()


def test_the_oracle_refuses_an_engine_it_cannot_pair(oracle, tmp_path, monkeypatch):
    import graphite.extract.ast as A

    monkeypatch.delattr(A, "_python_call_target")
    _write(tmp_path / "m.py", "def f(): pass\nf()\n")
    with pytest.raises(SystemExit, match="_python_call_target"):
        oracle.extract(oracle.load(tmp_path))


def test_extraction_restores_the_engine(oracle, tmp_path):
    import graphite.extract.ast as A

    before = (A._python_call_target, A._edge)
    _write(tmp_path / "m.py", "def f(): pass\nf()\n")
    oracle.extract(oracle.load(tmp_path))
    assert (A._python_call_target, A._edge) == before


def test_dump_records_what_dispatch_did(oracle, tmp_path):
    _write(
        tmp_path / "svc.py",
        "class Svc:\n    def helper(self):\n        return 1\n    def run(self):\n        return self.helper()\n",
    )
    report = oracle.dump(oracle.load(tmp_path))
    (record,) = [r for r in report["records"].values() if r["kind"] == "member"]
    assert record["dispatch"] == "repointed"
    assert record["targets"] == ["svc.py:2"]
    assert record["caller"] == "svc.py:4"


def test_compare_sorts_changes_into_pre_registered_categories(oracle):
    def rec(**changes):
        base = {
            "kind": "bare", "name": "f", "caller": "m.py:1", "targets": ["m.py:5"],
            "confidence": "LOCAL_CALL", "dispatch": "n/a", "member_candidates": None,
        }
        return {**base, **changes}

    member = {"kind": "member", "name": None}
    old = {"records": {
        "m.py:2:0": rec(),
        "m.py:3:0": rec(),
        "m.py:4:0": rec(),
        "m.py:6:0": rec(**member, targets=["unbound"], confidence="EXTERNAL_CALL", dispatch="kept"),
    }}
    new = {"records": {
        "m.py:2:0": rec(),
        "m.py:3:0": rec(targets=["unbound"]),
        "m.py:4:0": rec(caller="m.py:9"),
        "m.py:6:0": rec(**member, targets=["k.py:3"], confidence="LOCAL_CALL", dispatch="repointed"),
        "m.py:7:0": rec(),
    }}
    assert oracle.compare(old, new)["stats"] == {
        "only-old": 0,
        "only-new": 1,
        "same": 1,
        "bare-target-changed": 1,
        "caller-reattributed": 1,
        "dispatch-changed": 1,
        "confidence-EXTERNAL_CALL->LOCAL_CALL": 1,
        "confidence-flip-then-dispatched": 1,
    }
