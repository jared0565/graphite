"""Lightweight local watcher for keeping Graphite graphs current."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable

from .config import Config
from .ingest import collect_files

Snapshot = dict[str, str]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class WatchChange:
    """File-hash changes detected between two Graphite scans."""

    added: tuple[str, ...] = field(default_factory=tuple)
    changed: tuple[str, ...] = field(default_factory=tuple)
    removed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added).union(self.changed).union(self.removed)))

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "added": list(self.added),
            "changed": list(self.changed),
            "removed": list(self.removed),
        }


@dataclass(frozen=True)
class WatchOptions:
    interval_seconds: float = 1.5
    debounce_seconds: float = 0.75
    max_cycles: int | None = None
    build_now: bool = True
    once: bool = False

    def validate(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("watch interval must be greater than zero")
        if self.debounce_seconds < 0:
            raise ValueError("watch debounce must be zero or greater")
        if self.max_cycles is not None and self.max_cycles <= 0:
            raise ValueError("watch max cycles must be greater than zero")


def snapshot(root: Path, cfg: Config) -> Snapshot:
    """Return a stable content-hash snapshot for files Graphite would ingest."""
    return {entry.rel_path: entry.content_hash for entry in collect_files(root, cfg)}


def diff_snapshots(previous: Snapshot, current: Snapshot) -> WatchChange:
    """Compute deterministic file additions, content changes, and removals."""
    previous_paths = set(previous)
    current_paths = set(current)
    added = tuple(sorted(current_paths - previous_paths))
    removed = tuple(sorted(previous_paths - current_paths))
    changed = tuple(sorted(path for path in previous_paths & current_paths if previous[path] != current[path]))
    return WatchChange(added=added, changed=changed, removed=removed)


def wait_for_stable_snapshot(
    root: Path,
    cfg: Config,
    first: Snapshot,
    debounce_seconds: float,
    *,
    sleep: SleepFn = time.sleep,
    max_rounds: int = 5,
) -> Snapshot:
    """Wait until the ingest snapshot stops changing across the debounce window."""
    if debounce_seconds <= 0:
        return first

    candidate = first
    for _ in range(max_rounds):
        sleep(debounce_seconds)
        next_snapshot = snapshot(root, cfg)
        if next_snapshot == candidate:
            return next_snapshot
        candidate = next_snapshot
    return candidate


def watch_loop(
    root: Path,
    cfg: Config,
    on_change: Callable[[WatchChange], bool],
    options: WatchOptions,
    *,
    stop_event: Event | None = None,
    sleep: SleepFn = time.sleep,
    on_error: Callable[[Exception], None] | None = None,
) -> int:
    """Poll for content changes and call `on_change` after debounced stable updates.

    `on_change` returns True when downstream processing succeeded. Failed callbacks leave the
    previous snapshot unchanged so the same change can be retried on the next cycle.
    """
    options.validate()
    root = root.resolve()
    if not root.exists():
        raise FileNotFoundError(root)

    previous = snapshot(root, cfg)
    processed = 0

    if options.build_now:
        initial = WatchChange(added=tuple(sorted(previous)))
        if on_change(initial):
            processed += 1
        elif options.once:
            return processed

    cycles = 0
    while True:
        if stop_event and stop_event.is_set():
            return processed
        if options.once and cycles > 0:
            return processed
        if options.max_cycles is not None and cycles >= options.max_cycles:
            return processed

        sleep(options.interval_seconds)
        cycles += 1

        try:
            current = snapshot(root, cfg)
            change = diff_snapshots(previous, current)
            if not change.has_changes:
                continue

            stable = wait_for_stable_snapshot(
                root,
                cfg,
                current,
                options.debounce_seconds,
                sleep=sleep,
            )
            change = diff_snapshots(previous, stable)
            if not change.has_changes:
                previous = stable
                continue

            if on_change(change):
                previous = stable
                processed += 1
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                raise

    return processed
