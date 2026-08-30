# Build benchmarks

Recorded, not promised: each row is one run on the machine named, with the
engine at the commit named. Re-run `python -P benchmarks/build_benchmark.py
--files N` (synthetic) or time `graphite build` on your own repository to
measure yours. The CI `benchmark` job records a 3 000-file synthetic build
on every push as an artifact; it fails only on a catastrophic budget.

## What limits repository size

Not build time — graph size. `graphite query` refuses to load a
`graph.json` larger than `max_graph_bytes` (128 MiB, see `capabilities`),
and graph bytes per file depend on how dense the code is. The synthetic
corpus (12 functions and a class per file) yields about 7.5 KB of graph per
file and reaches the cap near 18 000 files; Django 5.2 yields about 18 KB
per file and reaches it near 7 400. `capabilities` therefore declares
`supported_repo_files: 7000`, following the densest real repository
measured, with margin.

The other limit that mattered was found by this page's real-repository row
and fixed before 1.0.0: the cycle report enumerated every simple cycle of
the graph, which is exponential on a repository whose test tree and package
import each other densely (Django: 13 GB and unfinished after 30 minutes on
2 930 source files; issue #64). The search is now bounded and says so in
`analysis.cycle_search`.

## Measurements (2026-08-29)

Machine: Windows 11, CPython 3.14.5, the maintainer's workstation, quiet
CPU unless noted. Engine at `c772982` (the #64 fix) for the Django row;
the synthetic rows are unaffected by that fix (their cycle counts are tiny).
`benchmarks/build_benchmark.py` reports peak RSS on POSIX only (`resource`);
the Django figure was taken on Windows by polling the build process itself
(the venv's `python.exe` is a launcher; its child does the work), and the CI
`benchmark` job on ubuntu records peak RSS for the synthetic build.

| Date | Repository | Source files | Wall (s) | ms/file | Peak RSS (MB) | Nodes | Edges | graph.json (MB) | Under the cap? |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-29 | synthetic (seed 7) | 300 | 4.5 | 15.0 | not measured | 4 138 | 4 641 | 2.3 | yes |
| 2026-08-29 | synthetic (seed 7) | 10 000 | 88.9 | 8.9 | not measured | 136 058 | 153 051 | 74.5 | yes |
| 2026-08-29 | synthetic (seed 7) | 15 000 | 119.4 | 8.0 | not measured | 204 058 | 229 551 | 111.9 | yes |
| 2026-08-29 | synthetic (seed 7) | 20 000 | 201.3 | 10.1 | not measured | 272 058 | 306 051 | 149.1 | **no** (149 MB > 128 MiB) |
| 2026-08-29 | django/django @ 9e7cc2b (5.2) | 2 930 (2 818 py, 112 js) | 66.9 | 22.8 | 520 | 45 620 | 109 373 | 52.8 | yes |
| 2026-08-29 | graphite (this repository) | 345 | 10.6 | 30.7 | not measured | 6 881 | 19 712 | 9.0 | yes |

The 20 000-file row ran while a test gate shared the CPU, so its wall time
is pessimistic; its node, edge and byte counts are exact (the corpus is
deterministic — the 10 000 and 15 000 rows measured twice, under load and
quiet, reported identical counts). Django's slices, for the record of #64:
`django/` alone 33 s, `tests/` alone 41 s; both halves together 82 s under
load and 66.9 s quiet after the fix, and unfinished at 13 GB before it.
