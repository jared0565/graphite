"""Default-No consent, HMAC integrity, and single-use quota coordination."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .contracts import ApprovalManifest
from .storage import RepositoryStore, StorageError

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ApprovalError(RuntimeError):
    """A stable, path-free approval failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SignedApproval:
    manifest: ApprovalManifest
    signature: str

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.to_dict(),
            "signature": self.signature,
        }


def approval_prompt(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
    json_mode: bool,
    assume_yes: bool,
    ci: bool,
) -> bool:
    """Request one explicit interactive approval; every other mode declines."""
    if (
        not stdin_is_tty
        or not stdout_is_tty
        or json_mode
        or assume_yes
        or ci
    ):
        return False
    stdout.write("Approve this Ollama model call? [y/N] ")
    stdout.flush()
    try:
        answer = stdin.readline(16)
    except (EOFError, OSError):
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _canonical_manifest(manifest: ApprovalManifest) -> bytes:
    return json.dumps(
        manifest.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT)


def _secure_parent(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ApprovalError("approval_state_path_invalid")
        if os.name != "nt":
            path.chmod(0o700)
    except ApprovalError:
        raise
    except OSError as exc:
        raise ApprovalError("approval_state_unavailable") from exc


def _load_or_create_key(path: Path, repository_root: Path) -> bytes:
    resolved_parent = path.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ApprovalError("approval_key_path_invalid")
    _secure_parent(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise ApprovalError("approval_key_unavailable") from exc
    if metadata is None:
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                written = os.write(descriptor, key)
                if written != len(key):
                    raise ApprovalError("approval_key_unavailable")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            return _load_or_create_key(path, repository_root)
        except ApprovalError:
            raise
        except OSError as exc:
            raise ApprovalError("approval_key_unavailable") from exc
        if os.name != "nt":
            path.chmod(0o600)
        return key
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ApprovalError("approval_key_path_invalid")
    if metadata.st_size != 32:
        raise ApprovalError("approval_key_invalid")
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise ApprovalError("approval_key_unavailable") from exc
    if len(key) != 32:
        raise ApprovalError("approval_key_invalid")
    return key


class _MachineQuotaStore:
    def __init__(self, path: Path, repository_root: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(repository_root)
        except ValueError:
            pass
        else:
            raise ApprovalError("quota_path_invalid")
        self.path = path
        _secure_parent(path.parent)
        try:
            with sqlite3.connect(path, timeout=2.0) as connection:
                connection.execute("PRAGMA busy_timeout = 2000")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS reservations (
                        nonce_hash TEXT PRIMARY KEY,
                        token_amount INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('reserved', 'released', 'settled')),
                        created_at INTEGER NOT NULL
                    )"""
                )
        except sqlite3.Error as exc:
            raise ApprovalError("quota_unavailable") from exc
        if os.name != "nt":
            path.chmod(0o600)

    def reserve(self, nonce_hash: str, amount: int, quota: int, created_at: int) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout = 2000")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM reservations WHERE nonce_hash = ?",
                (nonce_hash,),
            ).fetchone()
            if existing is not None:
                raise ApprovalError("approval_reused")
            total = int(
                connection.execute(
                    "SELECT COALESCE(SUM(token_amount), 0) FROM reservations WHERE status = 'reserved'"
                ).fetchone()[0]
            )
            if amount > quota or total + amount > quota:
                raise ApprovalError("budget_exhausted")
            connection.execute(
                "INSERT INTO reservations(nonce_hash, token_amount, status, created_at) VALUES (?, ?, 'reserved', ?)",
                (nonce_hash, amount, created_at),
            )
            connection.commit()
        except ApprovalError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise ApprovalError("quota_unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    def release(self, nonce_hash: str) -> None:
        try:
            with sqlite3.connect(self.path, timeout=2.0) as connection:
                connection.execute(
                    "UPDATE reservations SET status = 'released' WHERE nonce_hash = ? AND status = 'reserved'",
                    (nonce_hash,),
                )
        except sqlite3.Error as exc:
            raise ApprovalError("quota_unavailable") from exc

    def reserved_total(self) -> int:
        try:
            with sqlite3.connect(self.path, timeout=2.0) as connection:
                return int(
                    connection.execute(
                        "SELECT COALESCE(SUM(token_amount), 0) FROM reservations WHERE status = 'reserved'"
                    ).fetchone()[0]
                )
        except sqlite3.Error as exc:
            raise ApprovalError("quota_unavailable") from exc


class ApprovalAuthority:
    """Issues and consumes signed approvals.

    HMAC protects cross-file and accidental tampering. A hostile process running
    as the same operating-system user remains outside this trust boundary.
    """

    def __init__(
        self,
        store: RepositoryStore,
        *,
        key_path: Path,
        quota_path: Path,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self._key = _load_or_create_key(key_path, store.root)
        self._quota = _MachineQuotaStore(quota_path, store.root)
        self._now = now or (lambda: int(time.time()))

    def issue(self, manifest: ApprovalManifest) -> SignedApproval:
        payload = _canonical_manifest(manifest)
        manifest_hash = hashlib.sha256(payload).hexdigest()
        nonce_hash = hashlib.sha256(manifest.nonce.encode("utf-8")).hexdigest()
        signature = hmac.new(self._key, payload, hashlib.sha256).hexdigest()
        try:
            self.store.save_approval_record(
                approval_id=manifest.approval_id,
                task_id=None,
                decision_id=None,
                nonce_hash=nonce_hash,
                manifest_hash=manifest_hash,
                expires_at=manifest.expires_at,
                reserved_tokens=manifest.max_input_tokens + manifest.max_output_tokens,
            )
        except StorageError as exc:
            raise ApprovalError(exc.code) from exc
        return SignedApproval(manifest, signature)

    def verify(self, signed: SignedApproval, current_manifest: ApprovalManifest) -> None:
        if not isinstance(signed, SignedApproval):
            raise ApprovalError("approval_invalid")
        signed_payload = _canonical_manifest(signed.manifest)
        current_payload = _canonical_manifest(current_manifest)
        if not hmac.compare_digest(signed_payload, current_payload):
            raise ApprovalError("approval_manifest_changed")
        expected = hmac.new(self._key, signed_payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signed.signature):
            raise ApprovalError("approval_signature_invalid")

    def consume(
        self,
        signed: SignedApproval,
        current_manifest: ApprovalManifest,
        *,
        repository_quota_tokens: int,
        machine_quota_tokens: int,
    ) -> None:
        self.verify(signed, current_manifest)
        now = self._now()
        if now >= current_manifest.expires_at:
            raise ApprovalError("approval_expired")
        amount = current_manifest.max_input_tokens + current_manifest.max_output_tokens
        nonce_hash = hashlib.sha256(current_manifest.nonce.encode("utf-8")).hexdigest()
        manifest_hash = hashlib.sha256(_canonical_manifest(current_manifest)).hexdigest()
        self._quota.reserve(nonce_hash, amount, machine_quota_tokens, now)
        try:
            self.store.consume_approval_record(
                approval_id=current_manifest.approval_id,
                nonce_hash=nonce_hash,
                manifest_hash=manifest_hash,
                now=now,
                token_amount=amount,
                repository_quota=repository_quota_tokens,
            )
        except StorageError as exc:
            self._quota.release(nonce_hash)
            raise ApprovalError(exc.code) from exc

    def machine_reserved_token_total(self) -> int:
        return self._quota.reserved_total()
