"""Deterministic synthetic repository for build benchmarks.

Five languages in fixed proportions (py 40 %, ts 25 %, js 15 %, go 10 %,
rs 10 %). Every file defines `_FUNCS_PER_FILE` functions and one class, and
imports and calls into the previous file of its language, so the resolver
has real cross-file work to do rather than a pile of unrelated leaves.

    from benchmarks.synthetic_repo import generate
    counts = generate(Path("repo"), files=3000)

Same `seed` and `files` -> byte-identical tree, so two measurements differ
only by the machine and the engine.
"""
from __future__ import annotations

import random
from pathlib import Path

_SHARES: tuple[tuple[str, float], ...] = (("py", 0.40), ("ts", 0.25), ("js", 0.15), ("go", 0.10), ("rs", 0.10))
_FUNCS_PER_FILE = 12


def _py(prev: str | None) -> str:
    head = f"from .mod_{prev} import fn_0 as prev_fn\n\n\n" if prev else ""
    body = "".join(
        f"def fn_{k}(x: int) -> int:\n    return {'prev_fn(x)' if prev and k == 0 else 'x'} + {k}\n\n\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + "class Widget:\n    def run(self) -> int:\n        return fn_1(1)\n"


def _ts(prev: str | None, *, typed: bool) -> str:
    param = "x: number" if typed else "x"
    ret = ": number" if typed else ""
    head = f"import {{ fn0 }} from './mod_{prev}';\n\n" if prev else ""
    body = "".join(
        f"export function fn{k}({param}){ret} {{ return {'fn0(x)' if prev and k == 0 else 'x'} + {k}; }}\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + f"export class Widget {{ run(){ret} {{ return fn1(1); }} }}\n"


def _go(prev: str | None) -> str:
    body = "".join(
        f"func Fn{k}(x int) int {{ return {'Fn0(x)' if prev and k == 1 else 'x'} + {k} }}\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return "package synth\n\n" + body + "func Run() int { return Fn1(1) }\n"


def _rs(prev: str | None) -> str:
    head = f"use crate::mod_{prev}::fn_0 as prev_fn;\n\n" if prev else ""
    body = "".join(
        f"pub fn fn_{k}(x: i64) -> i64 {{ {'prev_fn(x)' if prev and k == 0 else 'x'} + {k} }}\n"
        for k in range(_FUNCS_PER_FILE)
    )
    return head + body + "pub fn run() -> i64 { fn_1(1) }\n"


def generate(root: Path, *, files: int, seed: int = 7) -> dict[str, int]:
    """Write `files` source files under `root`; return the per-language counts."""
    if files < 1:
        raise ValueError("files must be positive")
    rng = random.Random(seed)
    counts = {lang: int(files * share) for lang, share in _SHARES}
    counts["py"] += files - sum(counts.values())
    root.mkdir(parents=True, exist_ok=True)
    for lang, count in counts.items():
        directory = root / lang
        directory.mkdir(parents=True, exist_ok=True)
        prev: str | None = None
        for index in range(count):
            name = f"{index:05d}_{rng.randrange(1000):03d}"
            if lang == "py":
                (directory / f"mod_{name}.py").write_text(_py(prev), encoding="utf-8")
            elif lang == "ts":
                (directory / f"mod_{name}.ts").write_text(_ts(prev, typed=True), encoding="utf-8")
            elif lang == "js":
                (directory / f"mod_{name}.js").write_text(_ts(prev, typed=False), encoding="utf-8")
            elif lang == "go":
                (directory / f"mod_{name}.go").write_text(_go(prev), encoding="utf-8")
            else:
                (directory / f"mod_{name}.rs").write_text(_rs(prev), encoding="utf-8")
            prev = name
    if counts["py"]:
        (root / "py" / "__init__.py").write_text("", encoding="utf-8")
    if counts["rs"]:
        mods = sorted(p.stem for p in (root / "rs").glob("mod_*.rs"))
        (root / "rs" / "lib.rs").write_text("".join(f"pub mod {m};\n" for m in mods), encoding="utf-8")
    (root / "package.json").write_text('{"name": "synth", "private": true}\n', encoding="utf-8")
    (root / "go.mod").write_text("module synth\n\ngo 1.22\n", encoding="utf-8")
    return counts
