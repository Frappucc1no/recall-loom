#!/usr/bin/env python3
"""Single owner for atomic file replacement in the RecallLoom write path.

Frozen contract (v0.5.0 unique construction plan §7.7; retry numbers from the
Gate P freeze record §4 item 3):

``atomic_replace_if_unchanged(path, expected_identity, new_bytes, retry_policy)``
has exactly four outcomes:

- ``REPLACED_VERIFIED``: the target now holds exactly ``new_bytes``, proven by
  an exact byte readback after the replace. A target that already holds exactly
  ``new_bytes`` counts as already successful: the mutation is never replayed.
- ``OLD_VERIFIED_RETRYABLE``: every replace attempt failed with an allowlisted
  transient error while the old bytes+identity were still verified exact
  before each attempt, and the bounded retry budget (attempts/deadline) ran
  out. The operation may be retried as a whole; the target was never observed
  in any other state.
- ``EXTERNAL_CHANGE``: the target bytes differ from ``expected_identity``
  (checked before every attempt, including after a backoff). The foreign
  bytes are preserved; the replace is never attempted against a changed
  target.
- ``UNKNOWN``: the target was unreadable, the replace failed with a
  non-allowlisted error, or the post-replace readback failed or mismatched.
  An unknown-result mutation is never replayed.

``expected_identity`` is the exact expected current byte content of the
target; identity equality is byte-exact equality, re-verified before every
attempt. Only allowlisted transient errors may be retried, and only within
the bounded attempts/deadline/backoff budget. Temp-file residue is cleaned on
every outcome.

Retry policy (Gate P freeze record §4 item 3): the Linux transient errno
candidates EAGAIN/EBUSY/ETXTBSY may be retried; EACCES is NOT retried by
default; attempts <= 4, deadline 2 s, backoff 0.05/0.1/0.2 s. The Windows
winerror allowlist is residual W2: the policy is configurable and fails
closed by default (an empty allowlist means no retries at all).

Stdlib only. Core modules must not import scripts/* or ``_common.py``; this
module only depends on ``core.errors`` for the LockBusyError parity wrappers.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import tempfile
import time

from core.errors import LockBusyError

OUTCOME_REPLACED_VERIFIED = "REPLACED_VERIFIED"
OUTCOME_OLD_VERIFIED_RETRYABLE = "OLD_VERIFIED_RETRYABLE"
OUTCOME_EXTERNAL_CHANGE = "EXTERNAL_CHANGE"
OUTCOME_UNKNOWN = "UNKNOWN"
ATOMIC_REPLACE_OUTCOMES = (
    OUTCOME_REPLACED_VERIFIED,
    OUTCOME_OLD_VERIFIED_RETRYABLE,
    OUTCOME_EXTERNAL_CHANGE,
    OUTCOME_UNKNOWN,
)

REASON_ALREADY_NEW_NO_REPLAY = "already_new_no_replay"
REASON_RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
REASON_TARGET_UNREADABLE = "target_unreadable"
REASON_REPLACE_FAILED_NON_TRANSIENT = "replace_failed_non_transient"
REASON_READBACK_FAILED = "readback_failed"
REASON_READBACK_MISMATCH = "readback_mismatch"

# Frozen retry bounds (Gate P freeze record §4 item 3).
MAX_RETRY_ATTEMPTS = 4
MAX_RETRY_DEADLINE_S = 2.0
DEFAULT_BACKOFF_S = (0.05, 0.1, 0.2)

# Linux transient errno candidates proven by P050-E2. ETXTBSY does not exist
# on every platform, so resolve the names defensively.
LINUX_TRANSIENT_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "ETXTBSY", None),
    )
    if value is not None
)


@dataclass(frozen=True)
class AtomicReplaceRetryPolicy:
    """Configurable bounded retry policy; empty allowlists mean no retries."""

    transient_errnos: frozenset[int]
    transient_winerrors: frozenset[int]
    max_attempts: int
    deadline_s: float
    backoff_s: tuple[float, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_RETRY_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be within 1..{MAX_RETRY_ATTEMPTS}, got {self.max_attempts}"
            )
        if not 0.0 <= self.deadline_s <= MAX_RETRY_DEADLINE_S:
            raise ValueError(
                f"deadline_s must be within 0..{MAX_RETRY_DEADLINE_S} s, got {self.deadline_s}"
            )
        if not self.backoff_s or any(value < 0 for value in self.backoff_s):
            raise ValueError("backoff_s must be a non-empty tuple of non-negative seconds")


def default_retry_policy() -> AtomicReplaceRetryPolicy:
    """Frozen defaults: Linux-proven transient errnos on POSIX, fail-closed elsewhere."""

    transient_errnos = LINUX_TRANSIENT_ERRNOS if os.name == "posix" else frozenset()
    return AtomicReplaceRetryPolicy(
        transient_errnos=transient_errnos,
        transient_winerrors=frozenset(),
        max_attempts=MAX_RETRY_ATTEMPTS,
        deadline_s=MAX_RETRY_DEADLINE_S,
        backoff_s=DEFAULT_BACKOFF_S,
    )


@dataclass(frozen=True)
class AtomicReplaceResult:
    outcome: str
    attempts: int
    reason: str | None = None
    error: OSError | None = None


# Primitive seams. Tests inject faults at this seam (the sanctioned fault
# injection point); production code must call the public functions below.
def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _is_transient(error: OSError, retry_policy: AtomicReplaceRetryPolicy) -> bool:
    if error.errno is not None and error.errno in retry_policy.transient_errnos:
        return True
    winerror = getattr(error, "winerror", None)
    return winerror is not None and winerror in retry_policy.transient_winerrors


def atomic_write_bytes(path: Path, data: bytes, *, before_replace=None) -> None:
    """Unconditional atomic write: staged same-dir temp file + fsync + replace.

    No compare-and-swap and no retry: any OSError propagates to the caller
    (byte-identical parity with the superseded per-module write bodies).
    Temp-file residue is cleaned on every failure.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        _replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            with suppress(FileNotFoundError):
                temp_path.unlink()
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_replace_if_unchanged(
    path: Path,
    expected_identity: bytes,
    new_bytes: bytes,
    retry_policy: AtomicReplaceRetryPolicy,
    *,
    _sleep=time.sleep,
) -> AtomicReplaceResult:
    """Atomically replace ``path`` with ``new_bytes`` while the identity holds.

    Implements the frozen four-outcome semantics documented in the module
    docstring. ``expected_identity`` is re-verified before every attempt; the
    replace is followed by an exact byte readback.
    """

    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        # Re-verify the expected identity before EVERY attempt.
        try:
            current = _read_bytes(path)
        except OSError as exc:
            return AtomicReplaceResult(OUTCOME_UNKNOWN, attempt, REASON_TARGET_UNREADABLE, exc)
        # Current bytes already equal the new bytes: success, never replay.
        if current == new_bytes:
            return AtomicReplaceResult(
                OUTCOME_REPLACED_VERIFIED, attempt, REASON_ALREADY_NEW_NO_REPLAY
            )
        if current != expected_identity:
            return AtomicReplaceResult(OUTCOME_EXTERNAL_CHANGE, attempt)
        try:
            atomic_write_bytes(path, new_bytes)
        except OSError as exc:
            if _is_transient(exc, retry_policy):
                within_budget = (
                    attempt < retry_policy.max_attempts
                    and (time.monotonic() - start) < retry_policy.deadline_s
                )
                if within_budget:
                    _sleep(
                        retry_policy.backoff_s[
                            min(attempt - 1, len(retry_policy.backoff_s) - 1)
                        ]
                    )
                    continue
                return AtomicReplaceResult(
                    OUTCOME_OLD_VERIFIED_RETRYABLE,
                    attempt,
                    REASON_RETRY_BUDGET_EXHAUSTED,
                    exc,
                )
            return AtomicReplaceResult(
                OUTCOME_UNKNOWN, attempt, REASON_REPLACE_FAILED_NON_TRANSIENT, exc
            )
        # Exact bytes readback after the replace.
        try:
            readback = _read_bytes(path)
        except OSError as exc:
            return AtomicReplaceResult(OUTCOME_UNKNOWN, attempt, REASON_READBACK_FAILED, exc)
        if readback != new_bytes:
            return AtomicReplaceResult(OUTCOME_UNKNOWN, attempt, REASON_READBACK_MISMATCH)
        return AtomicReplaceResult(OUTCOME_REPLACED_VERIFIED, attempt)


def _changed_after_read_error(path: Path) -> LockBusyError:
    return LockBusyError(
        f"Refusing to write {path} because the file changed after it was read."
    )


def _raise_for_non_success(path: Path, result: AtomicReplaceResult) -> None:
    if result.outcome == OUTCOME_EXTERNAL_CHANGE:
        raise _changed_after_read_error(path)
    if result.error is not None:
        raise result.error
    raise LockBusyError(
        f"Refusing to continue because {path} changed during post-write verification."
    )


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _cas_existing_file(path: Path, *, expected_text: str, new_text: str) -> AtomicReplaceResult:
    current_text = _read_text(path)
    if current_text != expected_text:
        raise _changed_after_read_error(path)
    return atomic_replace_if_unchanged(
        path,
        expected_text.encode("utf-8"),
        new_text.encode("utf-8"),
        default_retry_policy(),
    )


def atomic_write_if_unchanged(path: Path, *, expected_text: str, new_text: str) -> None:
    """Text-level parity wrapper over the CAS primitive (legacy LockBusyError)."""

    if not path.exists():
        if expected_text != "":
            raise _changed_after_read_error(path)
        atomic_write_text(path, new_text)
        return
    result = _cas_existing_file(path, expected_text=expected_text, new_text=new_text)
    if result.outcome == OUTCOME_REPLACED_VERIFIED:
        return
    _raise_for_non_success(path, result)


def atomic_write_and_verify_if_unchanged(
    path: Path,
    *,
    expected_text: str,
    new_text: str,
) -> None:
    """Bound a helper-owned atomic replace to exact before/after text snapshots."""

    if not path.exists():
        if expected_text != "":
            raise _changed_after_read_error(path)
        atomic_write_text(path, new_text)
        _verify_text_readback(path, new_text)
        return
    result = _cas_existing_file(path, expected_text=expected_text, new_text=new_text)
    if result.outcome == OUTCOME_REPLACED_VERIFIED:
        # The primitive's post-replace readback is the exact-bytes verification.
        return
    if result.outcome == OUTCOME_UNKNOWN and result.reason == REASON_READBACK_FAILED:
        if isinstance(result.error, FileNotFoundError):
            raise LockBusyError(
                f"Refusing to continue because {path} disappeared after the helper write."
            ) from result.error
    if result.outcome == OUTCOME_UNKNOWN and result.reason == REASON_READBACK_MISMATCH:
        raise LockBusyError(
            f"Refusing to continue because {path} changed during post-write verification."
        )
    _raise_for_non_success(path, result)


def _verify_text_readback(path: Path, new_text: str) -> None:
    try:
        verified_text = _read_text(path)
    except FileNotFoundError as exc:
        raise LockBusyError(
            f"Refusing to continue because {path} disappeared after the helper write."
        ) from exc
    if verified_text != new_text:
        raise LockBusyError(
            f"Refusing to continue because {path} changed during post-write verification."
        )
