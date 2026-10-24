"""Doctor probe: hooks configured but not enforced.

Severity rule (from aramid's `probe_enforcement`): a repo with no
`GRAPHITE.md` is deliberately not onboarded and is NOT a finding -- nagging
every un-onboarded repo is how a real warning gets ignored.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from graphite import hookinstall
from graphite.doctor import check_hooks
from graphite.hookinstall import hooks_dir, install_hooks
from graphite.hookshim import TRIGGERS

INTERP = Path("C:/Python314/python.exe")


def _repo(tmp_path: Path, *, onboarded: bool = True) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if onboarded:
        (tmp_path / "GRAPHITE.md").write_text("x", encoding="utf-8")
    return tmp_path


def test_reports_ok_when_hooks_installed_and_hookspath_set(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    check = check_hooks(root)

    assert check.status == "ready"


def test_warns_when_hookspath_set_but_trampoline_missing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    (hooks_dir(root) / "post-commit").unlink()

    check = check_hooks(root)

    assert check.status == "degraded"


def test_warns_when_trampolines_exist_but_hookspath_unset(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)
    subprocess.run(
        ["git", "-C", str(root), "config", "--unset", "core.hooksPath"],
        check=True,
    )

    check = check_hooks(root)

    assert check.status == "degraded"


def test_un_onboarded_repo_is_not_a_finding(tmp_path: Path) -> None:
    root = _repo(tmp_path, onboarded=False)

    check = check_hooks(root)

    assert check.status == "optional"


def test_details_do_not_leak_the_absolute_hooks_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    check = check_hooks(root)

    # NOTE: `str(tmp_path) not in json.dumps(...)` is a vacuous check on
    # Windows -- json.dumps doubles backslashes, so a raw Windows path never
    # matches its own JSON-escaped form. Assert directly over the detail
    # values instead (falsified: reintroducing `"hooks_dir": str(hdir)`
    # makes this fail, while the old `not in json.dumps(...)` form did not).
    assert not any(str(tmp_path) in str(value) for value in check.details.values())
    assert not any(str(hooks_dir(root)) in str(value) for value in check.details.values())
    assert set(check.details) == {
        "onboarded", "custom_hooks_dir", "hookspath_configured", "trampolines_installed", "chain_slots",
    }


def test_details_declare_the_file_each_installed_trampoline_runs_first(tmp_path: Path) -> None:
    """Another tool reads where its hook belongs here instead of parsing shell
    (channel round 324). The name is read out of the installed trampoline's
    own text, so renaming the slot on either side alone fails this."""
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    slots = check_hooks(root).details["chain_slots"]

    for hook in TRIGGERS:
        text = (hooks_dir(root) / hook).read_text(encoding="utf-8")
        assert re.findall(r'CHAINED="\$DIR/([^"]+)"', text) == [slots[hook]["file"]]


def test_a_trampoline_names_one_sibling_of_its_own_hook(tmp_path: Path) -> None:
    """A tool that does read the trampoline needs exactly one `<hook>.<name>`
    in it (channel round 324). Holds on the old trampoline too: it pins the
    property, it does not test the declaration."""
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    for hook in TRIGGERS:
        text = (hooks_dir(root) / hook).read_text(encoding="utf-8")
        assert len(set(re.findall(rf"\b{re.escape(hook)}\.[\w-]+", text))) == 1


def test_an_empty_chain_slot_is_not_a_finding(tmp_path: Path) -> None:
    """Most repositories have no second hook manager."""
    root = _repo(tmp_path)
    install_hooks(root, INTERP)

    check = check_hooks(root)

    assert check.status == "ready"
    assert check.remediation == ()
    assert not any(slot["present"] for slot in check.details["chain_slots"].values())


def test_chain_slots_carry_no_path_when_the_hooks_live_outside_the_repo(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    assert hookinstall._git(root, "config", "core.hooksPath", str(elsewhere)).returncode == 0
    install_hooks(root, INTERP)

    slots = check_hooks(root).details["chain_slots"]

    assert not any(str(elsewhere) in str(slot) for slot in slots.values())
    for hook in TRIGGERS:
        assert slots[hook]["file"] == f"{hook}.local"
