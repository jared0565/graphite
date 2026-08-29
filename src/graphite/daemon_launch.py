"""The argument vector every daemon launcher shares.

Windows (Scheduled Task, Startup launcher), Linux (systemd user unit) and
macOS (launchd agent) all start the same process; only the supervisor differs.
Building the argv in one place keeps `-P` -- the flag that keeps the working
directory off `sys.path[0]` -- in every launcher by construction rather than
by each generator remembering it. See `windows_task.resolve_launcher_interpreter`
for why the launcher runs the interpreter itself and never a console script.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .windows_task import resolve_launcher_interpreter


@dataclass(frozen=True)
class DaemonLaunch:
    interpreter: Path
    arguments: tuple[str, ...]
    working_dir: Path

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.interpreter), *self.arguments)


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def daemon_launch(
    base_path: Path,
    *,
    interpreter: str | None = None,
    scan_interval: float = 15.0,
    discover_interval: float = 90.0,
    max_projects: int = 128,
    max_depth: int = 6,
    max_builds_per_cycle: int = 1,
    build_timeout: float = 240.0,
    debounce: float = 1.0,
) -> DaemonLaunch:
    base = base_path.resolve()
    # `-P` is the whole point: it keeps the working directory off `sys.path[0]`.
    # `-B` is deliberately NOT included -- it only suppresses bytecode and does
    # nothing to `sys.path`, and pairing them here would re-suggest the reading
    # that caused the original defect.
    arguments = (
        "-P",
        "-m",
        "graphite",
        "daemon",
        str(base),
        "--scan-interval",
        _fmt_number(scan_interval),
        "--discover-interval",
        _fmt_number(discover_interval),
        "--max-projects",
        str(max_projects),
        "--max-depth",
        str(max_depth),
        "--max-builds-per-cycle",
        str(max_builds_per_cycle),
        "--build-timeout",
        _fmt_number(build_timeout),
        "--debounce",
        _fmt_number(debounce),
    )
    return DaemonLaunch(
        interpreter=resolve_launcher_interpreter(interpreter),
        arguments=arguments,
        working_dir=base,
    )
