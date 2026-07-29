"""Machine-local incident ledger: durable capture of graphite failures.

Append-only JSONL, one file per repo (``.graphite/local/incidents.jsonl``,
the usage-ledger idiom) plus one for the daemon's own state dir. Dedup is a
READ-time concern: occurrences append freely and ``fold_incidents`` groups
them by fingerprint. Triage is event-sourced: ``ack``/``resolve`` are
appended entries, never mutations; a new occurrence strictly newer than a
resolve reopens the incident. Recording is best-effort by contract — a
ledger write failure must never break the operation being recorded.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LEDGER_BYTES = 5 * 1024 * 1024
MAX_SUBJECT_CHARS = 512
MAX_DETAIL_CHARS = 2048
MAX_LINE_BYTES = 8192
# "doctor" covers deep-probe failures (#29). It is a class of its own rather
# than folded into "build" because doctor incidents must not inflate build
# failure counts -- daemon_health reads those, and a probe that could not reach
# an MCP server says nothing about whether the graph builds.
_CLASSES = frozenset({"build", "query", "daemon", "doctor"})
_LIFECYCLE_KINDS = frozenset({"ack", "resolve"})
_KINDS = frozenset({"occurrence"}) | _LIFECYCLE_KINDS
_STATE_ORDER = {"open": 0, "acked": 1, "resolved": 2}


def repo_ledger_dir(root: Path) -> Path:
    from .usage_ledger import local_dir

    return local_dir(root)


def ledger_path(ledger_dir: Path) -> Path:
    return ledger_dir / "incidents.jsonl"


def rotated_ledger_path(ledger_dir: Path) -> Path:
    return ledger_dir / "incidents.jsonl.1"


def incident_fingerprint(klass: str, code: str, subject: str) -> str:
    digest = hashlib.sha256(f"{klass}|{code}|{subject}".encode("utf-8")).hexdigest()
    return digest[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append(ledger_dir: Path, entry: dict[str, Any]) -> None:
    path = ledger_path(ledger_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LEDGER_BYTES:
        os.replace(path, rotated_ledger_path(ledger_dir))
    line = json.dumps(entry, ensure_ascii=False)
    overshoot = len(line.encode("utf-8")) - MAX_LINE_BYTES
    if overshoot > 0:
        for key in ("detail", "note"):
            if key in entry:
                text = str(entry[key])
                entry = {**entry, key: text[: max(0, len(text) - overshoot)]}
                line = json.dumps(entry, ensure_ascii=False)
                break
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def record_incident(ledger_dir: Path, *, klass: str, code: str, subject: str, detail: str) -> None:
    """Append one occurrence; never raise."""
    try:
        if klass not in _CLASSES:
            return
        subject = str(subject)[:MAX_SUBJECT_CHARS]
        _append(
            ledger_dir,
            {
                "schema": 1,
                "kind": "occurrence",
                "fingerprint": incident_fingerprint(klass, str(code), subject),
                "ts": _now(),
                "class": klass,
                "code": str(code),
                "subject": subject,
                "detail": str(detail)[:MAX_DETAIL_CHARS],
            },
        )
    except Exception:
        return


def append_lifecycle(ledger_dir: Path, fingerprint: str, kind: str, note: str | None = None) -> bool:
    """Append ack/resolve for a known fingerprint; False (no write) if unknown."""
    if kind not in _LIFECYCLE_KINDS:
        raise ValueError("kind must be 'ack' or 'resolve'")
    entries, _skipped = read_incident_entries(ledger_dir)
    known = any(e.get("kind") == "occurrence" and e.get("fingerprint") == fingerprint for e in entries)
    if not known:
        return False
    entry: dict[str, Any] = {"schema": 1, "kind": kind, "fingerprint": fingerprint, "ts": _now()}
    if note:
        entry["note"] = str(note)[:MAX_DETAIL_CHARS]
    _append(ledger_dir, entry)
    return True


def read_incident_entries(ledger_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """(entries, skipped): rotated generation first; corrupt lines counted; never raises.

    Each generation file's read is individually guarded: if a file exists
    but cannot be read or decoded (permissions, path is a directory, etc.),
    that generation contributes >=1 to ``skipped`` and reading continues
    with the next generation, instead of the whole read aborting and
    silently presenting as "no incidents".
    """
    entries: list[dict[str, Any]] = []
    skipped = 0
    try:
        for path in (rotated_ledger_path(ledger_dir), ledger_path(ledger_dir)):
            try:
                if not path.exists():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                skipped += 1
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                    skipped += 1
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("fingerprint"), str)
                    and entry.get("kind") in _KINDS
                ):
                    entries.append(entry)
                else:
                    skipped += 1
    except Exception:
        return entries, skipped
    return entries, skipped


@dataclass(frozen=True)
class IncidentView:
    fingerprint: str
    klass: str
    code: str
    subject: str
    state: str
    first_seen: str
    last_seen: str
    count: int
    last_detail: str
    last_note: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "class": self.klass,
            "code": self.code,
            "subject": self.subject,
            "state": self.state,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "last_detail": self.last_detail,
            "last_note": self.last_note,
        }


def fold_incidents(entries: list[dict[str, Any]]) -> list[IncidentView]:
    """Group chronologically-ordered entries into per-fingerprint views."""
    slots: dict[str, dict[str, Any]] = {}
    for e in entries:
        fp = str(e["fingerprint"])
        slot = slots.setdefault(
            fp,
            {
                "first": None,
                "last": "",
                "count": 0,
                "klass": "",
                "code": "",
                "subject": "",
                "detail": "",
                "life": None,
                "life_ts": "",
                "note": None,
            },
        )
        ts = str(e.get("ts", ""))
        if e.get("kind") == "occurrence":
            slot["count"] += 1
            slot["klass"] = str(e.get("class", ""))
            slot["code"] = str(e.get("code", ""))
            slot["subject"] = str(e.get("subject", ""))
            slot["detail"] = str(e.get("detail", ""))
            if slot["first"] is None:
                slot["first"] = ts
            slot["last"] = ts
        else:
            slot["life"] = e["kind"]
            slot["life_ts"] = ts
            if e.get("note") is not None:
                slot["note"] = str(e["note"])
    views: list[IncidentView] = []
    for fp, s in slots.items():
        if s["count"] == 0:
            continue
        if s["life"] is None:
            state = "open"
        elif s["life"] == "resolve":
            state = "open" if s["last"] > s["life_ts"] else "resolved"
        else:
            state = "acked"
        views.append(
            IncidentView(
                fingerprint=fp,
                klass=s["klass"],
                code=s["code"],
                subject=s["subject"],
                state=state,
                first_seen=s["first"] or "",
                last_seen=s["last"],
                count=s["count"],
                last_detail=s["detail"],
                last_note=s["note"],
            )
        )
    views.sort(key=lambda v: v.last_seen, reverse=True)
    views.sort(key=lambda v: _STATE_ORDER[v.state])
    return views
