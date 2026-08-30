"""Build a synthetic repository and record how graphite scales.

    python benchmarks/build_benchmark.py --files 3000 --out metrics.json

Writes one JSON document: file count, wall time, ms/file, peak RSS of the
build process (POSIX only; -1 on Windows), node and edge counts, graph size,
platform. It is a RECORD, not a promise -- `docs/benchmarks.md` collects the
runs that back `capabilities`' `supported_repo_files`.

In CI it is a catastrophe detector: with `--wall-budget-s` the exit status
is non-zero only when the build blows past the budget or the graph exceeds
`max_graph_bytes`. Nothing here compares one run against another.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def _load_generate() -> Callable[..., dict[str, int]]:
    """Import the sibling generator by file location, not by path search.

    Prepending the repository root to ``sys.path`` would put a repo-local
    ``graphite.py`` ahead of the installed package for this interpreter --
    the exact shape the launch contract forbids (``-P`` exists to close it).
    Loading ``synthetic_repo.py`` by its location touches ``sys.path`` not at
    all, and CI runs this script under ``-P`` so the script directory is not
    prepended either.
    """
    location = Path(__file__).resolve().with_name("synthetic_repo.py")
    spec = importlib.util.spec_from_file_location("graphite_benchmark_synthetic_repo", location)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {location}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate


generate = _load_generate()

MAX_GRAPH_BYTES = 128 * 1024 * 1024
EXIT_BUILD_FAILED = 1
EXIT_BUDGET_EXCEEDED = 2
EXIT_GRAPH_TOO_LARGE = 3


def _peak_child_rss_mb() -> float:
    """Peak RSS of waited-for children. POSIX only: ru_maxrss is KiB on Linux
    and bytes on macOS; Windows has no `resource` module and reports -1."""
    if sys.platform == "win32":
        return -1.0
    import resource

    raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def run(files: int, out: Path, *, wall_budget_s: float | None, seed: int) -> int:
    # ignore_cleanup_errors: on Windows the build's worker processes can hold a
    # handle for a moment after `build` returns, and a cleanup race must not
    # turn a recorded measurement into a failed run (seen: WinError 145).
    with tempfile.TemporaryDirectory(prefix="graphite-bench-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "repo"
        output = Path(tmp) / "out"
        cache = Path(tmp) / "cache"
        counts = generate(root, files=files, seed=seed)
        argv = [
            sys.executable, "-P", "-m", "graphite",
            "--llm", "none", "--output-dir", str(output), "--cache-dir", str(cache),
            "build", str(root),
        ]
        start = time.perf_counter()
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=tmp)
        wall = time.perf_counter() - start
        graph = output / "graph.json"
        graph_bytes = graph.stat().st_size if graph.is_file() else -1
        nodes = edges = -1
        if graph.is_file():
            data = json.loads(graph.read_text(encoding="utf-8"))
            nodes, edges = len(data.get("nodes", [])), len(data.get("edges", []))
        metrics = {
            "files": files,
            "seed": seed,
            "counts": counts,
            "returncode": proc.returncode,
            "wall_s": round(wall, 2),
            "ms_per_file": round(wall * 1000 / files, 2),
            "peak_rss_mb": round(_peak_child_rss_mb(), 1),
            "nodes": nodes,
            "edges": edges,
            "graph_bytes": graph_bytes,
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(metrics, indent=2))
        if proc.returncode != 0:
            print(proc.stdout[-2000:], file=sys.stderr)
            print(proc.stderr[-2000:], file=sys.stderr)
            return EXIT_BUILD_FAILED
        if wall_budget_s is not None and wall > wall_budget_s:
            print(f"BUDGET EXCEEDED: {wall:.1f}s > {wall_budget_s}s", file=sys.stderr)
            return EXIT_BUDGET_EXCEEDED
        if graph_bytes > MAX_GRAPH_BYTES:
            print(f"GRAPH TOO LARGE: {graph_bytes} bytes > {MAX_GRAPH_BYTES}", file=sys.stderr)
            return EXIT_GRAPH_TOO_LARGE
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files", type=int, default=3000, help="source files to generate (default 3000)")
    parser.add_argument("--seed", type=int, default=7, help="generator seed (default 7)")
    parser.add_argument("--out", type=Path, default=Path("benchmark-metrics.json"), help="where to write the metrics JSON")
    parser.add_argument("--wall-budget-s", type=float, default=None, help="fail (exit 2) when the build takes longer than this")
    arguments = parser.parse_args(argv)
    return run(arguments.files, arguments.out, wall_budget_s=arguments.wall_budget_s, seed=arguments.seed)


if __name__ == "__main__":
    raise SystemExit(main())
