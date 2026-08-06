"""Single input-acquisition owner for RecallLoom mutation input (T050-01B seed).

Frozen by the v0.5.0 unique construction plan (§7.2 public CLI x input mode,
§7.3 capsule wire + scratch handoff, §7.4 frozen callable contracts, §7.8
landing map row 01B/01C) and the Gate P freeze record §4
(``max_raw_bytes = 4096``, ``max_envelope_bytes = 5600``).

What lives here (single owner):

- The RLCP1 ASCII capsule decoder for the exact frozen wire form
  ``RLCP1:<role>:<raw_byte_length>:<raw_sha256>:<base64url_no_padding>`` with
  the frozen §7.3 validation order: ASCII -> five-segment structure -> role ->
  decimal length -> 64-hex lowercase digest -> base64url canonical re-encode
  -> P050-E1 limits (4096 raw / 5600 envelope) -> strict UTF-8. Every step has
  a typed rejection reason (P050-E1 evidence vocabulary, now product
  vocabulary). The decoder records the distinct evidence digests
  ``producer_raw_sha256`` / ``received_raw_sha256`` / ``canonical_text_sha256``
  and never passes a canonical digest off as a raw digest.
- The frozen ``AcquiredInput`` (§7.4): the only acquisition output of this
  module. Bytes and text stay strictly separated; ``source_identity`` is a
  path-independent stable identity; no field can leak a public-unsafe secret
  or host path.
- The two mutation helpers' legacy input acquisition (``read_limited_stdin``,
  ``read_limited_file_text``, ``load_prepared_text``, ``load_entry_source``)
  and their prepared-input failure mapping, extracted verbatim
  (extract -> delegate -> parity). The helpers keep thin delegating wrappers
  and remain the only layer that projects typed failures onto legacy exit
  codes; the helper originals are deleted only after V050-09/10 (§7.8). The
  existing safety validators in ``core/safety/prepared_input.py`` and
  ``core/safety/scratch_residue.py`` are reused as-is, never reimplemented or
  weakened.

Typed failure model: this module raises ``InputTransportError`` carrying the
exact legacy projection (message, blocked-reason family, exit code, details);
it never calls ``SystemExit`` and never imports ``scripts/`` or ``_common.py``
(core must not depend on adapter facades).

Derived (not contract-frozen) vocabulary introduced here, flagged for the
review record: ``capsule_role_mismatch`` (capsule role must equal the calling
command's operation), ``expected_input_digest_mismatch``,
``invalid_expected_input_digest``, ``missing_expected_input_digest``,
``prepared_input_ref_invalid``, and ``input_mode_not_available`` (fail-closed
reason while capsule/scratch mutation execution awaits the T050-04+ wiring).
``host_handoff_unavailable`` is the frozen §7.3 spelling. ``both_input_sources``
reuses the existing legacy conflict vocabulary.

T050-01C adds the §7.3 authenticated-scratch capability lifecycle, orchestrated
here without a second token/path/symlink owner: the file-level checks are the
single-owner primitives in ``core/safety/prepared_input.py``
(``lstat_regular_file_identity`` / ``read_stable_regular_file_bytes``) and the
residue report shapes come from ``core/safety/scratch_residue.py``. What lives
here (single orchestrator):

- The frozen ``PreparedInputHandoff`` (§7.4): frozen, deliberately
  non-serializable (no JSON/compact codec, no dict projection); the host
  ``write_target`` path never appears in any public payload.
- ``provision_prepared_input_capability`` (PROVISIONED row): dispatcher-callable;
  creates the external system-temp capability (mode 0700 token dir, random
  256-bit token, ``RLS1.<base64url>`` ref, open role/expiry/max_raw_bytes
  manifest, single empty ``content.bin``).
- Identity + stable-read acquisition (``acquire_prepared_input_scratch``) and
  the §7.3 state transitions: WRITTEN_UNCLAIMED -> CLAIMED
  (``claim_prepared_input_scratch``, product-owned atomic manifest temp+replace;
  validation failure keeps the capability open), CLAIMED -> open release with
  identity/digest re-check (``release_prepared_input_scratch_claim``), CLAIMED
  -> CONSUMED cleanup (``consume_prepared_input_scratch``; cleanup failure is
  an explicit public-safe diagnostic, never a false success), CLAIMED ->
  INSPECT_REQUIRED (``mark_prepared_input_scratch_inspect_required``;
  afterwards read-only, never auto-cleanup, never auto-replay), the read-only
  residue report (``inspect_prepared_input_scratch``) and the expiry residue
  scan (``scan_expired_prepared_input_capabilities``; reports only, never
  deletes unverified content). The manifest ``os.replace`` transition is the
  §7.3-assigned product-owned atomic claim, a non-write-path metadata replace
  owned here (registered for the T050-09A replace-audit allowlist).

Derived (not contract-frozen) 01C vocabulary, flagged for the review record:
``scratch_capability_missing`` / ``scratch_capability_invalid`` /
``scratch_capability_expired`` / ``scratch_capability_role_mismatch`` /
``scratch_capability_state_invalid`` / ``scratch_capability_provision_failed``
/ ``scratch_capability_transition_failed`` /
``scratch_capability_cleanup_failed``, the residue finding reasons
``prepared_input_capability_expired`` /
``prepared_input_capability_inspect_required`` /
``prepared_input_capability_malformed``, and the path category
``scratch_capability``. The manifest state spellings ``open`` / ``claimed`` /
``inspect_required`` follow §7.3 ("manifest.open -> manifest.claimed");
WRITTEN_UNCLAIMED is the logical open state after the host write returns (the
commit itself is the ready signal; no host-created ready marker). The 0700
directory mode is the POSIX shape proven by P050-E1; the Windows ACL-equivalent
verification is residual W1 (provisioning fails closed where the mode cannot
hold, and no trusted host integration exists on any host yet, so bare CLI
``--prepare-input`` stays ``host_handoff_unavailable`` everywhere today).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
from typing import Optional

from core.failure.context import (
    COMMAND_APPEND,
    COMMAND_WRITE,
    OPERATION_DAILY_LOG_APPEND,
    OPERATION_DOMAIN,
    OPERATION_MANAGED_WRITE,
    STAGE_INPUT,
    OperationContext,
)
from core.safety.prepared_input import (
    MAX_SCRATCH_MARKER_BYTES,
    PreparedInputSafetyError,
    PreparedInputSource,
    RegularFileIdentity,
    read_prepared_input_source_text,
    read_stable_regular_file_bytes,
    validate_prepared_input_source_path,
)
from core.safety.scratch_residue import (
    MAX_RESIDUE_COUNT,
    ScratchResidueFinding,
    ScratchResidueReport,
    public_scratch_path_ref,
)


# --- frozen capsule wire constants (§7.3 + Gate P freeze record §4) ----------

CAPSULE_WIRE_PREFIX = "RLCP1"
CAPSULE_MAX_RAW_BYTES = 4096
CAPSULE_MAX_ENVELOPE_BYTES = 5600
# Dispatcher legacy stdin has no frozen lower limit. Keep its established
# 4 MiB default owned beside the byte-level acquisition primitive.
DEFAULT_STDIN_MAX_INPUT_BYTES = 4 * 1024 * 1024

# Input-mode vocabulary. The legacy channel spellings ("entry-json", "file",
# "stdin") predate v0.5.0 and stay byte-identical; "capsule" and "scratch" are
# the additive §7.2 channels.
INPUT_MODE_ENTRY_JSON = "entry-json"
INPUT_MODE_FILE = "file"
INPUT_MODE_STDIN = "stdin"
INPUT_MODE_CAPSULE = "capsule"
INPUT_MODE_SCRATCH = "scratch"
INPUT_MODE_DOMAIN = frozenset(
    (
        INPUT_MODE_ENTRY_JSON,
        INPUT_MODE_FILE,
        INPUT_MODE_STDIN,
        INPUT_MODE_CAPSULE,
        INPUT_MODE_SCRATCH,
    )
)

# Decoder typed rejection vocabulary (P050-E1 evidence names; the §7.3
# validation order fixes the sequence in which they fire).
CAPSULE_NOT_ASCII = "capsule_not_ascii"
CAPSULE_STRUCTURE_INVALID = "capsule_structure_invalid"
CAPSULE_ROLE_INVALID = "role_invalid"
CAPSULE_ROLE_MISMATCH = "capsule_role_mismatch"
CAPSULE_LENGTH_INVALID = "capsule_length_invalid"
CAPSULE_DIGEST_INVALID = "capsule_digest_invalid"
CAPSULE_BASE64_NOT_CANONICAL = "capsule_base64_not_canonical"
CAPSULE_LENGTH_MISMATCH = "capsule_length_mismatch"
CAPSULE_DIGEST_MISMATCH = "capsule_digest_mismatch"
CAPSULE_ENVELOPE_TOO_LONG = "capsule_envelope_too_long"
CAPSULE_RAW_TOO_LONG = "capsule_raw_too_long"
CAPSULE_NOT_UTF8 = "capsule_not_utf8"
EXPECTED_INPUT_DIGEST_MISMATCH = "expected_input_digest_mismatch"

# Input-transport failure vocabulary shared with the dispatcher matrix (the
# first is the frozen §7.3 spelling; "both_input_sources" is legacy vocabulary).
REASON_HOST_HANDOFF_UNAVAILABLE = "host_handoff_unavailable"
REASON_INPUT_MODE_NOT_AVAILABLE = "input_mode_not_available"
REASON_INVALID_EXPECTED_INPUT_DIGEST = "invalid_expected_input_digest"
REASON_MISSING_EXPECTED_INPUT_DIGEST = "missing_expected_input_digest"
REASON_PREPARED_INPUT_REF_INVALID = "prepared_input_ref_invalid"
REASON_BOTH_INPUT_SOURCES = "both_input_sources"
REASON_MISSING_INPUT_SOURCE = "missing_input_source"

# --expected-input-digest is a 64-char lowercase SHA-256 hex (§7.2).
EXPECTED_INPUT_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# prepared_input_ref is exactly "RLS1.<base64url 256-bit token, no padding>"
# (§7.3): no path, no role, no user text.
PREPARED_INPUT_REF_PREFIX = "RLS1."
PREPARED_INPUT_REF_TOKEN_BYTES = 32


class InputTransportError(Exception):
    """Typed acquisition failure carrying the exact legacy exit projection.

    Helpers and the dispatcher adapter project ``message`` / ``reason`` /
    ``exit_code`` / ``details`` onto the existing failure contract
    (``invalid_prepared_input`` family, exit 2); this module never raises
    ``SystemExit`` itself.
    """

    def __init__(
        self,
        *,
        message: str,
        reason: str = "invalid_prepared_input",
        exit_code: int = 2,
        details: Optional[dict] = None,
    ) -> None:
        self.message = message
        self.reason = reason
        self.exit_code = exit_code
        self.details = details
        super().__init__(message)

    @property
    def reason_code(self) -> Optional[str]:
        """The typed ``details.reason_code``, when the failure carries one."""
        if isinstance(self.details, dict):
            value = self.details.get("reason_code")
            if isinstance(value, str):
                return value
        return None


@dataclass(frozen=True)
class AcquiredInput:
    """Frozen §7.4 acquisition output (single representation of any input).

    ``raw_bytes`` / ``decoded_text`` / ``canonical_text`` are strictly
    separated; the digests are independently computed evidence (a canonical
    digest never masquerades as a raw digest). ``producer_raw_sha256`` is the
    producer-declared digest and exists only for channels that carry one
    (capsule today); it must equal ``received_raw_sha256`` because the decoder
    rejects mismatches before construction. ``source_identity`` is a
    path-independent stable identity (the prepared-input public path ref for
    file channels); no field may carry a public-unsafe secret or host path.
    ``scratch_ref`` / ``scratch_disposition`` stay ``None`` until the §7.3
    scratch orchestration lands (T050-01C).
    """

    raw_bytes: bytes
    decoded_text: str
    canonical_text: str
    raw_byte_length: int
    producer_raw_sha256: Optional[str]
    received_raw_sha256: str
    canonical_text_sha256: str
    input_mode: str
    role: str
    source_identity: Optional[str]
    scratch_ref: Optional[str]
    scratch_disposition: Optional[str]

    def __post_init__(self) -> None:
        if self.role not in OPERATION_DOMAIN:
            raise ValueError(f"unknown input role: {self.role!r}")
        if self.input_mode not in INPUT_MODE_DOMAIN:
            raise ValueError(f"unknown input mode: {self.input_mode!r}")
        if not isinstance(self.raw_bytes, bytes):
            raise TypeError("raw_bytes must be bytes")
        if self.raw_byte_length != len(self.raw_bytes):
            raise ValueError("raw_byte_length must equal len(raw_bytes)")
        # decoded_text must be the exact strict UTF-8 decoding of raw_bytes
        # (raises UnicodeDecodeError, a ValueError, when that is impossible).
        if self.decoded_text != self.raw_bytes.decode("utf-8"):
            raise ValueError("decoded_text must be the strict UTF-8 decoding of raw_bytes")
        if self.received_raw_sha256 != _sha256_hex(self.raw_bytes):
            raise ValueError("received_raw_sha256 must be the SHA-256 of raw_bytes")
        if self.canonical_text_sha256 != _sha256_hex(self.canonical_text.encode("utf-8")):
            raise ValueError("canonical_text_sha256 must be the SHA-256 of canonical_text")
        if self.producer_raw_sha256 is not None:
            if not EXPECTED_INPUT_DIGEST_RE.match(self.producer_raw_sha256):
                raise ValueError("producer_raw_sha256 must be 64 lowercase hex")
            if self.producer_raw_sha256 != self.received_raw_sha256:
                raise ValueError(
                    "producer_raw_sha256 must equal received_raw_sha256 after validation"
                )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def acquired_input_from_text(
    *,
    text: str,
    input_mode: str,
    role: str,
    producer_raw_sha256: Optional[str] = None,
    source_identity: Optional[str] = None,
    scratch_ref: Optional[str] = None,
    scratch_disposition: Optional[str] = None,
) -> AcquiredInput:
    """Build a digest-consistent AcquiredInput for text-carrying channels."""
    raw = text.encode("utf-8")
    return AcquiredInput(
        raw_bytes=raw,
        decoded_text=text,
        canonical_text=text,
        raw_byte_length=len(raw),
        producer_raw_sha256=producer_raw_sha256,
        received_raw_sha256=_sha256_hex(raw),
        canonical_text_sha256=_sha256_hex(text.encode("utf-8")),
        input_mode=input_mode,
        role=role,
        source_identity=source_identity,
        scratch_ref=scratch_ref,
        scratch_disposition=scratch_disposition,
    )


# --- prepared-input failure mapping (extracted from both helpers, verbatim) --

def prepared_input_failure_details(
    error: PreparedInputSafetyError,
    *,
    input_mode: Optional[str] = None,
) -> dict[str, object]:
    """The single owner of PreparedInputSafetyError -> public details mapping."""
    details = error.details
    if input_mode is not None:
        details["input_mode"] = input_mode
    return details


def prepared_input_transport_error(
    error: PreparedInputSafetyError,
    *,
    input_mode: Optional[str] = None,
) -> InputTransportError:
    """Map a safety validator rejection onto the typed transport failure."""
    return InputTransportError(
        message=error.message,
        details=prepared_input_failure_details(error, input_mode=input_mode),
    )


# --- RLCP1 capsule decoder (§7.3, frozen validation order) -------------------

def _capsule_reject(
    reason_code: str,
    message: str,
    *,
    extra: Optional[dict] = None,
) -> InputTransportError:
    return InputTransportError(
        message=message,
        details={"reason_code": reason_code, "side_effect": "none", **(extra or {})},
    )


def decode_input_capsule(
    envelope: str,
    *,
    expected_role: Optional[str] = None,
    expected_digest: Optional[str] = None,
) -> AcquiredInput:
    """Decode one RLCP1 ASCII capsule in the frozen §7.3 validation order.

    ``expected_role`` binds the capsule role to the calling command's typed
    operation; ``expected_digest`` is the optional --expected-input-digest
    binding, compared against the capsule-carried raw digest (§7.2). Every
    rejection raises ``InputTransportError`` with a typed reason code; the
    capsule content itself is never echoed into failure details.
    """
    if not isinstance(envelope, str):
        raise TypeError("input capsule envelope must be a string")
    try:
        envelope.encode("ascii")
    except UnicodeEncodeError:
        raise _capsule_reject(
            CAPSULE_NOT_ASCII,
            "Input capsule is not pure ASCII; RLCP1 capsules must be ASCII-only.",
        )
    parts = envelope.split(":")
    if len(parts) != 5 or parts[0] != CAPSULE_WIRE_PREFIX:
        raise _capsule_reject(
            CAPSULE_STRUCTURE_INVALID,
            "Input capsule must use the wire form "
            "RLCP1:<role>:<raw_byte_length>:<raw_sha256>:<base64url_no_padding>.",
        )
    _, role, length_s, digest_s, body = parts
    if role not in OPERATION_DOMAIN:
        raise _capsule_reject(
            CAPSULE_ROLE_INVALID,
            "Input capsule role is not one of the frozen mutation roles "
            "(managed_write, daily_log_append, post_append_summary_sync).",
        )
    if expected_role is not None and role != expected_role:
        raise _capsule_reject(
            CAPSULE_ROLE_MISMATCH,
            f"Input capsule role {role!r} does not match this command's operation "
            f"{expected_role!r}.",
        )
    if not length_s.isdecimal():
        raise _capsule_reject(
            CAPSULE_LENGTH_INVALID,
            "Input capsule raw_byte_length segment is not a decimal byte count.",
        )
    if (
        len(digest_s) != 64
        or digest_s != digest_s.lower()
        or any(char not in "0123456789abcdef" for char in digest_s)
    ):
        raise _capsule_reject(
            CAPSULE_DIGEST_INVALID,
            "Input capsule raw_sha256 segment is not 64 lowercase hex characters.",
        )
    if expected_digest is not None and expected_digest != digest_s:
        raise _capsule_reject(
            EXPECTED_INPUT_DIGEST_MISMATCH,
            "Input capsule raw digest does not equal --expected-input-digest.",
        )
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        raise _capsule_reject(
            CAPSULE_BASE64_NOT_CANONICAL,
            "Input capsule payload is not canonical base64url without padding.",
        )
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != body:
        raise _capsule_reject(
            CAPSULE_BASE64_NOT_CANONICAL,
            "Input capsule payload is not canonical base64url without padding.",
        )
    declared_length = int(length_s)
    if len(raw) != declared_length:
        raise _capsule_reject(
            CAPSULE_LENGTH_MISMATCH,
            "Input capsule declared raw_byte_length does not match the decoded "
            "payload length.",
            extra={
                "declared_raw_byte_length": declared_length,
                "received_raw_byte_length": len(raw),
            },
        )
    received_raw_sha256 = _sha256_hex(raw)
    if received_raw_sha256 != digest_s:
        raise _capsule_reject(
            CAPSULE_DIGEST_MISMATCH,
            "Input capsule raw_sha256 does not match the decoded payload bytes.",
        )
    if len(envelope) > CAPSULE_MAX_ENVELOPE_BYTES:
        raise _capsule_reject(
            CAPSULE_ENVELOPE_TOO_LONG,
            f"Input capsule envelope exceeds the frozen {CAPSULE_MAX_ENVELOPE_BYTES}-byte "
            "limit.",
            extra={
                "envelope_bytes": len(envelope),
                "max_envelope_bytes": CAPSULE_MAX_ENVELOPE_BYTES,
            },
        )
    if len(raw) > CAPSULE_MAX_RAW_BYTES:
        raise _capsule_reject(
            CAPSULE_RAW_TOO_LONG,
            f"Input capsule payload exceeds the frozen {CAPSULE_MAX_RAW_BYTES}-byte raw "
            "limit.",
            extra={
                "raw_byte_length": len(raw),
                "max_raw_bytes": CAPSULE_MAX_RAW_BYTES,
            },
        )
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _capsule_reject(
            CAPSULE_NOT_UTF8,
            "Input capsule payload is not valid UTF-8.",
        )
    return AcquiredInput(
        raw_bytes=raw,
        decoded_text=text,
        canonical_text=text,
        raw_byte_length=len(raw),
        producer_raw_sha256=digest_s,
        received_raw_sha256=received_raw_sha256,
        canonical_text_sha256=_sha256_hex(text.encode("utf-8")),
        input_mode=INPUT_MODE_CAPSULE,
        role=role,
        source_identity=None,
        scratch_ref=None,
        scratch_disposition=None,
    )


def validate_prepared_input_ref_shape(ref: str) -> None:
    """Enforce the frozen ``RLS1.<base64url-256-bit-token>`` ref shape (§7.3).

    Shape validation only: no path, role, or user text may ride in the ref,
    and the token must be a canonical no-padding base64url encoding of exactly
    256 bits. Raises ``InputTransportError`` with reason
    ``prepared_input_ref_invalid``.
    """
    if not isinstance(ref, str):
        raise TypeError("prepared input ref must be a string")
    token = None
    if ref.startswith(PREPARED_INPUT_REF_PREFIX):
        token = ref[len(PREPARED_INPUT_REF_PREFIX):]
    if not token:
        raise InputTransportError(
            message="--prepared-input-ref must use the form RLS1.<base64url-256-bit-token>.",
            details={"reason_code": REASON_PREPARED_INPUT_REF_INVALID, "side_effect": "none"},
        )
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        raw = b""
    if (
        len(raw) != PREPARED_INPUT_REF_TOKEN_BYTES
        or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token
    ):
        raise InputTransportError(
            message="--prepared-input-ref must use the form RLS1.<base64url-256-bit-token>.",
            details={"reason_code": REASON_PREPARED_INPUT_REF_INVALID, "side_effect": "none"},
        )


# --- §7.3 authenticated scratch capability orchestration (T050-01C) ----------
#
# Single orchestrator for the external system-temp capability lifecycle
# PROVISIONED -> WRITTEN_UNCLAIMED -> CLAIMED -> CONSUMED / INSPECT_REQUIRED.
# File-level token/path/link/identity checks stay with their single owner
# core/safety/prepared_input.py; residue report shapes stay with
# core/safety/scratch_residue.py. None of this is wired into mutation
# execution (T050-04+): the claim path is the product-owned primitive the
# transaction will call inside the identity lock, and dry-run/preview callers
# must only ever run the read-only acquisition, never claim/consume.

SCRATCH_CAPABILITY_DIR_PREFIX = "recallloom-prepared-input-"
SCRATCH_CONTENT_FILENAME = "content.bin"
SCRATCH_MANIFEST_FILENAME = "manifest.json"
SCRATCH_MANIFEST_SCHEMA_VERSION = "recallloom.prepared-input/1.0"
SCRATCH_MANIFEST_MAX_BYTES = MAX_SCRATCH_MARKER_BYTES

# Manifest state spellings (§7.3 "manifest.open -> manifest.claimed").
SCRATCH_STATE_OPEN = "open"
SCRATCH_STATE_CLAIMED = "claimed"
SCRATCH_STATE_INSPECT_REQUIRED = "inspect_required"
SCRATCH_STATE_DOMAIN = frozenset(
    (SCRATCH_STATE_OPEN, SCRATCH_STATE_CLAIMED, SCRATCH_STATE_INSPECT_REQUIRED)
)

# §7.3 logical dispositions (AcquiredInput.scratch_disposition / reports).
SCRATCH_DISPOSITION_PROVISIONED = "provisioned"
SCRATCH_DISPOSITION_WRITTEN_UNCLAIMED = "written_unclaimed"
SCRATCH_DISPOSITION_CLAIMED = "claimed"
SCRATCH_DISPOSITION_INSPECT_REQUIRED = "inspect_required"
SCRATCH_DISPOSITION_CONSUMED = "consumed"

# Derived typed failure vocabulary (flagged in the module docstring).
REASON_SCRATCH_CAPABILITY_MISSING = "scratch_capability_missing"
REASON_SCRATCH_CAPABILITY_INVALID = "scratch_capability_invalid"
REASON_SCRATCH_CAPABILITY_EXPIRED = "scratch_capability_expired"
REASON_SCRATCH_CAPABILITY_ROLE_MISMATCH = "scratch_capability_role_mismatch"
REASON_SCRATCH_CAPABILITY_STATE_INVALID = "scratch_capability_state_invalid"
REASON_SCRATCH_CAPABILITY_PROVISION_FAILED = "scratch_capability_provision_failed"
REASON_SCRATCH_CAPABILITY_TRANSITION_FAILED = "scratch_capability_transition_failed"
REASON_SCRATCH_CAPABILITY_CLEANUP_FAILED = "scratch_capability_cleanup_failed"

SCRATCH_PATH_CATEGORY = "scratch_capability"

# Derived residue finding reasons (report-only; scratch_residue shapes).
SCRATCH_FINDING_EXPIRED = "prepared_input_capability_expired"
SCRATCH_FINDING_INSPECT_REQUIRED = "prepared_input_capability_inspect_required"
SCRATCH_FINDING_MALFORMED = "prepared_input_capability_malformed"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _scratch_reject(
    reason_code: str,
    message: str,
    *,
    extra: Optional[dict] = None,
) -> InputTransportError:
    return InputTransportError(
        message=message,
        details={"reason_code": reason_code, "side_effect": "none", **(extra or {})},
    )


def _scratch_temp_root(temp_root: Optional[Path]) -> Path:
    return Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())


@dataclass(frozen=True)
class PreparedInputHandoff:
    """Frozen §7.4 prepared-input handoff (trusted host integration only).

    Frozen and deliberately non-serializable: there is no JSON/compact codec
    and no dict projection, so the host ``write_target`` path can never ride
    into public output. Public payloads only ever carry ``ref`` /
    ``expires_at`` / ``max_raw_bytes`` (§7.3). A bare CLI never obtains this
    object; only the dispatcher's trusted in-process host integration does.
    """

    ref: str
    write_target: Path
    role: str
    expires_at: str
    max_raw_bytes: int

    def __repr__(self) -> str:
        # The host path must never leak, even into logs/diagnostics.
        return (
            f"PreparedInputHandoff(ref={self.ref!r}, write_target=<host-only>, "
            f"role={self.role!r}, expires_at={self.expires_at!r}, "
            f"max_raw_bytes={self.max_raw_bytes!r})"
        )

    def __post_init__(self) -> None:
        validate_prepared_input_ref_shape(self.ref)
        if not isinstance(self.write_target, Path):
            raise TypeError("write_target must be a pathlib.Path")
        if self.role not in OPERATION_DOMAIN:
            raise ValueError(f"unknown input role: {self.role!r}")
        _parse_iso_utc(self.expires_at)
        if isinstance(self.max_raw_bytes, bool) or not isinstance(self.max_raw_bytes, int):
            raise TypeError("max_raw_bytes must be an int")
        if self.max_raw_bytes <= 0:
            raise ValueError("max_raw_bytes must be positive")


def scratch_capability_dir_for_ref(ref: str, *, temp_root: Optional[Path] = None) -> Path:
    """Resolve the capability dir for a shape-valid RLS1 ref (path math only)."""
    validate_prepared_input_ref_shape(ref)
    token = ref[len(PREPARED_INPUT_REF_PREFIX):]
    return _scratch_temp_root(temp_root) / (SCRATCH_CAPABILITY_DIR_PREFIX + token)


def provision_prepared_input_capability(
    *,
    role: str,
    max_raw_bytes: int,
    ttl_seconds: int,
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> PreparedInputHandoff:
    """Create the §7.3 PROVISIONED external system-temp capability.

    Mode 0700 token dir, random 256-bit token, ``RLS1.<base64url>`` ref, an
    open role/expiry/max_raw_bytes manifest and a single empty
    ``content.bin`` write target. Returns the frozen non-serializable
    ``PreparedInputHandoff``; only a trusted in-process host integration may
    receive it (a bare CLI must stay ``host_handoff_unavailable``).
    """
    if role not in OPERATION_DOMAIN:
        raise ValueError(f"unknown input role: {role!r}")
    if isinstance(max_raw_bytes, bool) or not isinstance(max_raw_bytes, int) or max_raw_bytes <= 0:
        raise ValueError("max_raw_bytes must be a positive int")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive int")
    created = now if now is not None else _utc_now()
    expires = created + timedelta(seconds=ttl_seconds)
    token = secrets.token_bytes(PREPARED_INPUT_REF_TOKEN_BYTES)
    ref = PREPARED_INPUT_REF_PREFIX + base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    capability_dir = _scratch_temp_root(temp_root) / (
        SCRATCH_CAPABILITY_DIR_PREFIX + ref[len(PREPARED_INPUT_REF_PREFIX):]
    )
    try:
        os.mkdir(capability_dir, 0o700)
    except OSError as exc:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_PROVISION_FAILED,
            "Could not create the prepared-input capability directory; nothing was provisioned.",
            extra={"errno": exc.errno if exc.errno is not None else 5},
        ) from exc
    try:
        _validate_scratch_capability_dir(capability_dir)
        content_fd = os.open(
            capability_dir / SCRATCH_CONTENT_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(content_fd)
        _write_scratch_manifest_atomic(
            capability_dir,
            {
                "schema_version": SCRATCH_MANIFEST_SCHEMA_VERSION,
                "ref": ref,
                "role": role,
                "state": SCRATCH_STATE_OPEN,
                "created_at": _iso_utc(created),
                "expires_at": _iso_utc(expires),
                "max_raw_bytes": max_raw_bytes,
            },
        )
    except Exception:
        # Only the directory this call just created is removed, best effort.
        shutil.rmtree(capability_dir, ignore_errors=True)
        raise
    return PreparedInputHandoff(
        ref=ref,
        write_target=capability_dir / SCRATCH_CONTENT_FILENAME,
        role=role,
        expires_at=_iso_utc(expires),
        max_raw_bytes=max_raw_bytes,
    )


def _validate_scratch_capability_dir(capability_dir: Path) -> str:
    """lstat the capability dir (0700, real directory); return its public ref."""
    path_ref = public_scratch_path_ref(capability_dir)
    try:
        dir_lstat = capability_dir.lstat()
    except FileNotFoundError:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_MISSING,
            "Prepared input capability does not exist (unknown or already consumed ref).",
            extra={"path_ref": path_ref},
        )
    except OSError as exc:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_INVALID,
            "Prepared input capability directory could not be inspected.",
            extra={"path_ref": path_ref, "errno": exc.errno if exc.errno is not None else 5},
        )
    if stat.S_ISLNK(dir_lstat.st_mode) or not stat.S_ISDIR(dir_lstat.st_mode):
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_INVALID,
            "Prepared input capability path is not a product-created directory.",
            extra={"path_ref": path_ref},
        )
    if stat.S_IMODE(dir_lstat.st_mode) != 0o700:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_INVALID,
            "Prepared input capability directory does not have mode 0700.",
            extra={"path_ref": path_ref},
        )
    return path_ref


def _write_scratch_manifest_atomic(capability_dir: Path, manifest: dict) -> None:
    """Product-owned atomic manifest transition: same-dir temp file + replace.

    A failure before the replace leaves the previous manifest byte-identical,
    so the capability keeps its prior state and a partial transition is never
    replayable.
    """
    manifest_path = capability_dir / SCRATCH_MANIFEST_FILENAME
    temp_path = capability_dir / (SCRATCH_MANIFEST_FILENAME + ".tmp")
    data = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temp_path, flags, 0o600)
    except OSError as exc:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_TRANSITION_FAILED,
            "Prepared input manifest transition failed before commit; prior state is unchanged.",
            extra={"errno": exc.errno if exc.errno is not None else 5},
        ) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _scratch_reject(
                    REASON_SCRATCH_CAPABILITY_TRANSITION_FAILED,
                    "Prepared input manifest transition failed before commit; prior state is unchanged.",
                )
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
    except InputTransportError:
        raise
    except OSError as exc:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_TRANSITION_FAILED,
            "Prepared input manifest transition failed before commit; prior state is unchanged.",
            extra={"errno": exc.errno if exc.errno is not None else 5},
        ) from exc


def _load_scratch_manifest(capability_dir: Path, *, ref: str, path_ref: str) -> dict:
    """Read and strictly validate the capability manifest (stable read)."""
    try:
        raw, _identity = read_stable_regular_file_bytes(
            capability_dir / SCRATCH_MANIFEST_FILENAME,
            max_input_bytes=SCRATCH_MANIFEST_MAX_BYTES,
            label="scratch manifest",
            path_category=SCRATCH_PATH_CATEGORY,
            path_ref=path_ref,
        )
    except PreparedInputSafetyError as exc:
        raise prepared_input_transport_error(exc, input_mode=INPUT_MODE_SCRATCH) from exc

    def invalid(message: str) -> InputTransportError:
        return _scratch_reject(
            REASON_SCRATCH_CAPABILITY_INVALID, message, extra={"path_ref": path_ref}
        )

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid("Prepared input manifest is not valid UTF-8 JSON.")
    if not isinstance(manifest, dict):
        raise invalid("Prepared input manifest is not a JSON object.")
    if manifest.get("schema_version") != SCRATCH_MANIFEST_SCHEMA_VERSION:
        raise invalid("Prepared input manifest schema_version is not supported.")
    if manifest.get("ref") != ref:
        raise invalid("Prepared input manifest does not match the requested ref.")
    if manifest.get("role") not in OPERATION_DOMAIN:
        raise invalid("Prepared input manifest carries an unknown role.")
    if manifest.get("state") not in SCRATCH_STATE_DOMAIN:
        raise invalid("Prepared input manifest carries an unknown state.")
    try:
        _parse_iso_utc(manifest.get("created_at"))
        _parse_iso_utc(manifest.get("expires_at"))
    except ValueError:
        raise invalid("Prepared input manifest timestamps are not valid ISO-8601 UTC.")
    max_raw_bytes = manifest.get("max_raw_bytes")
    if (
        isinstance(max_raw_bytes, bool)
        or not isinstance(max_raw_bytes, int)
        or max_raw_bytes <= 0
    ):
        raise invalid("Prepared input manifest max_raw_bytes is not a positive int.")
    if manifest["state"] in (SCRATCH_STATE_CLAIMED, SCRATCH_STATE_INSPECT_REQUIRED):
        claimed_digest = manifest.get("claimed_raw_sha256")
        if not isinstance(claimed_digest, str) or not EXPECTED_INPUT_DIGEST_RE.match(claimed_digest):
            raise invalid("Prepared input manifest claim record has no valid content digest.")
        claimed_identity = manifest.get("claimed_identity")
        if not isinstance(claimed_identity, dict) or any(
            isinstance(claimed_identity.get(key), bool)
            or not isinstance(claimed_identity.get(key), int)
            or claimed_identity[key] < 0
            for key in ("st_dev", "st_ino", "st_size")
        ):
            raise invalid("Prepared input manifest claim record has no valid identity.")
        try:
            _parse_iso_utc(manifest.get("claimed_at"))
        except ValueError:
            raise invalid("Prepared input manifest claim record timestamp is invalid.")
    return manifest


def _read_scratch_content(
    capability_dir: Path,
    manifest: dict,
    *,
    path_ref: str,
) -> tuple[bytes, RegularFileIdentity]:
    """Stable-read content.bin under the single-owner identity checks."""
    try:
        return read_stable_regular_file_bytes(
            capability_dir / SCRATCH_CONTENT_FILENAME,
            max_input_bytes=manifest["max_raw_bytes"],
            label="scratch content",
            path_category=SCRATCH_PATH_CATEGORY,
            path_ref=path_ref,
        )
    except PreparedInputSafetyError as exc:
        raise prepared_input_transport_error(exc, input_mode=INPUT_MODE_SCRATCH) from exc


def _validated_open_scratch(
    ref: str,
    *,
    expected_role: str,
    temp_root: Optional[Path],
    now: Optional[datetime],
) -> tuple[Path, dict, str]:
    capability_dir = scratch_capability_dir_for_ref(ref, temp_root=temp_root)
    path_ref = _validate_scratch_capability_dir(capability_dir)
    manifest = _load_scratch_manifest(capability_dir, ref=ref, path_ref=path_ref)
    state = manifest["state"]
    if state != SCRATCH_STATE_OPEN:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_STATE_INVALID,
            f"Prepared input capability is {state}, not open; claimed or inspect-required "
            "capabilities are never auto-replayed.",
            extra={"path_ref": path_ref, "capability_state": state},
        )
    if manifest["role"] != expected_role:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_ROLE_MISMATCH,
            f"Prepared input capability role {manifest['role']!r} does not match this "
            f"command's operation {expected_role!r}.",
            extra={"path_ref": path_ref},
        )
    current = now if now is not None else _utc_now()
    if current > _parse_iso_utc(manifest["expires_at"]):
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_EXPIRED,
            "Prepared input capability has expired; it is reported as residue and never "
            "silently deleted.",
            extra={"path_ref": path_ref, "expires_at": manifest["expires_at"]},
        )
    return capability_dir, manifest, path_ref


def _acquire_scratch(
    *,
    ref: str,
    expected_role: str,
    expected_digest: Optional[str],
    temp_root: Optional[Path],
    now: Optional[datetime],
) -> tuple[AcquiredInput, Path, dict, RegularFileIdentity, str]:
    capability_dir, manifest, path_ref = _validated_open_scratch(
        ref,
        expected_role=expected_role,
        temp_root=temp_root,
        now=now,
    )
    raw, identity = _read_scratch_content(capability_dir, manifest, path_ref=path_ref)
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _scratch_reject(
            "source_path_not_utf8",
            "Prepared input scratch content is not valid UTF-8.",
            extra={"path_ref": path_ref},
        )
    if expected_digest is not None:
        if not EXPECTED_INPUT_DIGEST_RE.match(expected_digest):
            raise _scratch_reject(
                REASON_INVALID_EXPECTED_INPUT_DIGEST,
                "--expected-input-digest must be a 64-character lowercase SHA-256 hex digest.",
            )
        if _sha256_hex(raw) != expected_digest:
            raise _scratch_reject(
                EXPECTED_INPUT_DIGEST_MISMATCH,
                "Prepared input scratch content digest does not equal --expected-input-digest.",
                extra={"path_ref": path_ref},
            )
    acquired = acquired_input_from_text(
        text=text,
        input_mode=INPUT_MODE_SCRATCH,
        role=expected_role,
        producer_raw_sha256=expected_digest,
        source_identity=ref,
        scratch_ref=ref,
        scratch_disposition=SCRATCH_DISPOSITION_WRITTEN_UNCLAIMED,
    )
    return acquired, capability_dir, manifest, identity, path_ref


def acquire_prepared_input_scratch(
    *,
    ref: str,
    expected_role: str,
    expected_digest: Optional[str] = None,
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> AcquiredInput:
    """Read-only WRITTEN_UNCLAIMED acquisition (the only dry-run/preview form).

    Validates ref/dir/manifest/state/role/expiry, stable-reads the content
    under the single-owner identity checks, and binds the optional
    --expected-input-digest. Never writes the manifest, never touches the
    content, and never claims: scratch bytes and status are unchanged.
    """
    acquired, _dir, _manifest, _identity, _path_ref = _acquire_scratch(
        ref=ref,
        expected_role=expected_role,
        expected_digest=expected_digest,
        temp_root=temp_root,
        now=now,
    )
    return acquired


def claim_prepared_input_scratch(
    *,
    ref: str,
    expected_role: str,
    expected_digest: Optional[str],
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> AcquiredInput:
    """WRITTEN_UNCLAIMED -> CLAIMED via the product-owned atomic transition.

    The caller holds the workspace identity lock (T050-04+); the commit
    itself is the ready signal (no host-created marker). Any validation
    failure keeps the capability open; the claim record binds the exact
    content digest and identity that were stably read.
    """
    if expected_digest is None:
        raise _scratch_reject(
            REASON_MISSING_EXPECTED_INPUT_DIGEST,
            "Scratch commit requires --expected-input-digest (64 lowercase hex).",
        )
    acquired, capability_dir, manifest, identity, path_ref = _acquire_scratch(
        ref=ref,
        expected_role=expected_role,
        expected_digest=expected_digest,
        temp_root=temp_root,
        now=now,
    )
    claim_instant = now if now is not None else _utc_now()
    _write_scratch_manifest_atomic(
        capability_dir,
        {
            "schema_version": SCRATCH_MANIFEST_SCHEMA_VERSION,
            "ref": ref,
            "role": manifest["role"],
            "state": SCRATCH_STATE_CLAIMED,
            "created_at": manifest["created_at"],
            "expires_at": manifest["expires_at"],
            "max_raw_bytes": manifest["max_raw_bytes"],
            "claimed_at": _iso_utc(claim_instant),
            "claimed_raw_sha256": acquired.received_raw_sha256,
            "claimed_identity": {
                "st_dev": identity.st_dev,
                "st_ino": identity.st_ino,
                "st_size": identity.st_size,
            },
        },
    )
    reloaded = _load_scratch_manifest(capability_dir, ref=ref, path_ref=path_ref)
    if (
        reloaded.get("state") != SCRATCH_STATE_CLAIMED
        or reloaded.get("claimed_raw_sha256") != acquired.received_raw_sha256
    ):
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_TRANSITION_FAILED,
            "Prepared input claim transition could not be verified; the capability is "
            "left for inspection and never auto-replayed.",
            extra={"path_ref": path_ref},
        )
    return replace(acquired, scratch_disposition=SCRATCH_DISPOSITION_CLAIMED)


def _load_claimed_scratch(
    ref: str,
    *,
    temp_root: Optional[Path],
) -> tuple[Path, dict, str]:
    capability_dir = scratch_capability_dir_for_ref(ref, temp_root=temp_root)
    path_ref = _validate_scratch_capability_dir(capability_dir)
    manifest = _load_scratch_manifest(capability_dir, ref=ref, path_ref=path_ref)
    if manifest["state"] != SCRATCH_STATE_CLAIMED:
        raise _scratch_reject(
            REASON_SCRATCH_CAPABILITY_STATE_INVALID,
            f"Prepared input capability is {manifest['state']}, not claimed.",
            extra={"path_ref": path_ref, "capability_state": manifest["state"]},
        )
    return capability_dir, manifest, path_ref


def _verify_claimed_content_unchanged(
    capability_dir: Path,
    manifest: dict,
    *,
    path_ref: str,
) -> None:
    """Re-check the claimed content identity/digest (rotate/refuse on drift)."""
    raw, identity = _read_scratch_content(capability_dir, manifest, path_ref=path_ref)
    claimed_identity = manifest["claimed_identity"]
    if (
        _sha256_hex(raw) != manifest["claimed_raw_sha256"]
        or identity.st_dev != claimed_identity["st_dev"]
        or identity.st_ino != claimed_identity["st_ino"]
        or identity.st_size != claimed_identity["st_size"]
    ):
        raise _scratch_reject(
            "source_path_changed_after_validation",
            "Prepared input scratch content changed after the claim; refusing to reopen "
            "or consume unverified content.",
            extra={"path_ref": path_ref},
        )


def release_prepared_input_scratch_claim(
    *,
    ref: str,
    temp_root: Optional[Path] = None,
) -> None:
    """CLAIMED -> open release after a pre-mutation failure.

    Allowed only when the content identity/digest still exactly match the
    claim record; on any drift the capability stays claimed and the release
    is refused (§7.3 rotate/refuse, never an automatic replay).
    """
    capability_dir, manifest, path_ref = _load_claimed_scratch(ref, temp_root=temp_root)
    _verify_claimed_content_unchanged(capability_dir, manifest, path_ref=path_ref)
    _write_scratch_manifest_atomic(
        capability_dir,
        {
            "schema_version": SCRATCH_MANIFEST_SCHEMA_VERSION,
            "ref": ref,
            "role": manifest["role"],
            "state": SCRATCH_STATE_OPEN,
            "created_at": manifest["created_at"],
            "expires_at": manifest["expires_at"],
            "max_raw_bytes": manifest["max_raw_bytes"],
        },
    )


def consume_prepared_input_scratch(
    *,
    ref: str,
    temp_root: Optional[Path] = None,
) -> str:
    """CLAIMED -> CONSUMED cleanup after final evidence passed.

    Re-verifies the claimed identity/digest before deleting (never silently
    deletes unverified content). A deletion failure raises a typed diagnostic
    carrying the public-safe residue ref; it must be surfaced explicitly and
    never folded into a transaction false success (§7.3 CONSUMED row).
    """
    capability_dir, manifest, path_ref = _load_claimed_scratch(ref, temp_root=temp_root)
    _verify_claimed_content_unchanged(capability_dir, manifest, path_ref=path_ref)
    try:
        (capability_dir / SCRATCH_CONTENT_FILENAME).unlink()
        (capability_dir / SCRATCH_MANIFEST_FILENAME).unlink()
        capability_dir.rmdir()
    except OSError as exc:
        raise InputTransportError(
            message=(
                "Prepared input capability cleanup failed after a successful mutation; "
                "the residue is reported and never silently retried."
            ),
            details={
                "reason_code": REASON_SCRATCH_CAPABILITY_CLEANUP_FAILED,
                "side_effect": "none",
                "path_ref": path_ref,
                "scratch_ref": ref,
                "errno": exc.errno if exc.errno is not None else 5,
            },
        ) from exc
    return SCRATCH_DISPOSITION_CONSUMED


def mark_prepared_input_scratch_inspect_required(
    *,
    ref: str,
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> None:
    """CLAIMED -> INSPECT_REQUIRED after a target attempt or partial evidence.

    Afterwards the capability is inspect-first residue: read-only, never
    auto-cleanup, never auto-replay, never restored to ready (§7.3).
    """
    capability_dir, manifest, path_ref = _load_claimed_scratch(ref, temp_root=temp_root)
    marked = dict(manifest)
    marked["state"] = SCRATCH_STATE_INSPECT_REQUIRED
    marked["inspect_required_at"] = _iso_utc(now if now is not None else _utc_now())
    _write_scratch_manifest_atomic(capability_dir, marked)


def inspect_prepared_input_scratch(
    *,
    ref: str,
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Read-only public-safe residue report for one capability (never mutates)."""
    validate_prepared_input_ref_shape(ref)
    capability_dir = scratch_capability_dir_for_ref(ref, temp_root=temp_root)
    path_ref = public_scratch_path_ref(capability_dir)
    report: dict[str, object] = {
        "ref": ref,
        "path_ref": path_ref,
        "present": False,
        "state": None,
        "role": None,
        "expires_at": None,
        "expired": None,
        "max_raw_bytes": None,
        "content_present": False,
        "content_raw_sha256": None,
        "content_error": None,
        "claimed_raw_sha256": None,
        "scratch_disposition": None,
        "manifest_error": None,
    }
    try:
        dir_lstat = capability_dir.lstat()
    except OSError:
        return report
    report["present"] = True
    if stat.S_ISLNK(dir_lstat.st_mode) or not stat.S_ISDIR(dir_lstat.st_mode):
        report["manifest_error"] = REASON_SCRATCH_CAPABILITY_INVALID
        return report
    try:
        manifest = _load_scratch_manifest(capability_dir, ref=ref, path_ref=path_ref)
    except InputTransportError as exc:
        report["manifest_error"] = exc.reason_code or REASON_SCRATCH_CAPABILITY_INVALID
        return report
    content_sha256 = None
    content_error = None
    content_size = 0
    try:
        raw, _identity = _read_scratch_content(capability_dir, manifest, path_ref=path_ref)
        content_sha256 = _sha256_hex(raw)
        content_size = len(raw)
    except InputTransportError as exc:
        content_error = exc.reason_code or REASON_SCRATCH_CAPABILITY_INVALID
    current = now if now is not None else _utc_now()
    state = manifest["state"]
    if state == SCRATCH_STATE_OPEN:
        disposition = (
            SCRATCH_DISPOSITION_WRITTEN_UNCLAIMED
            if content_sha256 is not None and content_size > 0
            else SCRATCH_DISPOSITION_PROVISIONED
        )
    else:
        disposition = state
    report.update(
        {
            "state": state,
            "role": manifest["role"],
            "expires_at": manifest["expires_at"],
            "expired": current > _parse_iso_utc(manifest["expires_at"]),
            "max_raw_bytes": manifest["max_raw_bytes"],
            "content_present": content_sha256 is not None,
            "content_raw_sha256": content_sha256,
            "content_error": content_error,
            "claimed_raw_sha256": manifest.get("claimed_raw_sha256"),
            "scratch_disposition": disposition,
        }
    )
    return report


def _capability_residue_count(capability_dir: Path) -> int:
    count = 1
    try:
        children = list(capability_dir.iterdir())
    except OSError:
        return count
    return min(count + len(children), MAX_RESIDUE_COUNT)


def scan_expired_prepared_input_capabilities(
    *,
    temp_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> ScratchResidueReport:
    """Expiry/inspect residue scan over the system-temp capability area.

    Report-only: expired, inspect-required, or malformed capabilities become
    public-safe report-only findings. Nothing is ever deleted or modified
    here (§7.3: after expiry only report residue; never silently delete
    unverified content).
    """
    root = _scratch_temp_root(temp_root)
    current = now if now is not None else _utc_now()
    findings: list[ScratchResidueFinding] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        children = []
    for child in children:
        if not child.name.startswith(SCRATCH_CAPABILITY_DIR_PREFIX):
            continue
        path_ref = public_scratch_path_ref(child)
        reason_code: Optional[str] = None
        try:
            child_lstat = child.lstat()
        except OSError:
            reason_code = SCRATCH_FINDING_MALFORMED
        else:
            if stat.S_ISLNK(child_lstat.st_mode) or not stat.S_ISDIR(child_lstat.st_mode):
                reason_code = SCRATCH_FINDING_MALFORMED
        if reason_code is None:
            ref = PREPARED_INPUT_REF_PREFIX + child.name[len(SCRATCH_CAPABILITY_DIR_PREFIX):]
            try:
                validate_prepared_input_ref_shape(ref)
                manifest = _load_scratch_manifest(child, ref=ref, path_ref=path_ref)
            except InputTransportError:
                reason_code = SCRATCH_FINDING_MALFORMED
            else:
                if manifest["state"] == SCRATCH_STATE_INSPECT_REQUIRED:
                    reason_code = SCRATCH_FINDING_INSPECT_REQUIRED
                elif current > _parse_iso_utc(manifest["expires_at"]):
                    reason_code = SCRATCH_FINDING_EXPIRED
        if reason_code is None:
            continue
        findings.append(
            ScratchResidueFinding(
                reason_code=reason_code,
                path_category="external_helper_scratch",
                path_ref=path_ref,
                scope="report_only",
                residue_count=_capability_residue_count(child),
                blocking=False,
            )
        )
    return ScratchResidueReport(
        blocking_findings=(),
        report_only_findings=tuple(findings),
    )


# --- legacy loader extraction (byte-identical behavior; §7.8 row 01B/01C) ----

def read_limited_stdin_bytes(*, max_input_bytes: int) -> bytes:
    """The two helpers' former ``read_limited_stdin``, verbatim."""
    try:
        raw = sys.stdin.buffer.read(max_input_bytes + 1)
    except OSError as exc:
        raise InputTransportError(message=f"Failed to read stdin: {exc}")
    if len(raw) > max_input_bytes:
        raise InputTransportError(
            message=(
                f"Stdin input exceeds --max-input-bytes ({len(raw)} > {max_input_bytes})."
            ),
            details={"size": len(raw), "max_input_bytes": max_input_bytes},
        )
    return raw


def acquire_stdin_text(*, max_input_bytes: int) -> str:
    """The shared stdin tail of both legacy loaders (isatty/empty/UTF-8)."""
    if sys.stdin.isatty():
        raise InputTransportError(
            message="Stdin input is empty. Pipe or redirect UTF-8 prepared content when using --stdin."
        )
    raw = read_limited_stdin_bytes(max_input_bytes=max_input_bytes)
    if raw == b"":
        raise InputTransportError(
            message="Stdin input is empty. Pipe or redirect UTF-8 prepared content when using --stdin."
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InputTransportError(message="Stdin input must be valid UTF-8.")


def read_validated_source_text(
    source: PreparedInputSource,
    *,
    max_input_bytes: int,
    label: str,
) -> str:
    """The two helpers' former ``read_limited_file_text`` core, verbatim."""
    try:
        return read_prepared_input_source_text(
            source,
            max_input_bytes=max_input_bytes,
            label=label,
        )
    except PreparedInputSafetyError as exc:
        raise prepared_input_transport_error(exc) from exc


@dataclass(frozen=True)
class EntryInputAcquisition:
    """Daily-log-append acquisition: the AcquiredInput plus the validated
    prepared-source path (file channel only; a non-public transport handle the
    legacy helper still returns downstream)."""

    acquired: AcquiredInput
    source_path: Optional[Path]


def acquire_daily_log_entry_input(
    *,
    entry_json: Optional[str],
    entry_file: Optional[str],
    use_stdin: bool,
    max_input_bytes: int,
    project_root: Optional[Path] = None,
    storage_root: Optional[Path] = None,
) -> EntryInputAcquisition:
    """append_daily_log_entry.py's former ``load_entry_source``, verbatim."""
    selected_sources = int(entry_json is not None) + int(entry_file is not None) + int(use_stdin)
    if selected_sources != 1:
        input_mode = "ambiguous" if selected_sources > 1 else "missing"
        details = {
            **OperationContext(
                command=COMMAND_APPEND,
                operation=OPERATION_DAILY_LOG_APPEND,
                write_type=None,
                input_mode=input_mode,
                stage=STAGE_INPUT,
            ).legacy_details_fields(),
            "input_contract": "entry-json_xor_entry-file_xor_stdin",
            "entry_json_present": entry_json is not None,
            "entry_file_present": entry_file is not None,
            "stdin_present": bool(use_stdin),
            "side_effect": "none",
            "trust_effect": "none",
            "reason_code": (
                REASON_BOTH_INPUT_SOURCES if selected_sources > 1 else REASON_MISSING_INPUT_SOURCE
            ),
        }
        if selected_sources > 1:
            raise InputTransportError(
                message="Use exactly one prepared-entry input: --entry-json, --entry-file, or --stdin.",
                details=details,
            )
        raise InputTransportError(
            message="Provide prepared entry content with exactly one of --entry-json, --entry-file, or --stdin.",
            details=details,
        )

    if entry_json is not None:
        entry_json_size = len(entry_json.encode("utf-8"))
        if entry_json_size > max_input_bytes:
            raise InputTransportError(
                message=(
                    f"Entry JSON input exceeds --max-input-bytes ({entry_json_size} > {max_input_bytes})."
                ),
                details={"size": entry_json_size, "max_input_bytes": max_input_bytes},
            )
        return EntryInputAcquisition(
            acquired=acquired_input_from_text(
                text=entry_json,
                input_mode=INPUT_MODE_ENTRY_JSON,
                role=OPERATION_DAILY_LOG_APPEND,
            ),
            source_path=None,
        )

    if entry_file:
        if project_root is None or storage_root is None:
            raise InputTransportError(
                message="Internal error: project root is required before reading --entry-file.",
                details={"input_mode": "file", "reason_code": "prepared_input_context_missing"},
            )
        try:
            entry_source = validate_prepared_input_source_path(
                entry_file,
                project_root=project_root,
                storage_root=storage_root,
                input_role="entry-file",
                label="entry",
            )
        except PreparedInputSafetyError as exc:
            raise prepared_input_transport_error(exc, input_mode="file") from exc
        return EntryInputAcquisition(
            acquired=acquired_input_from_text(
                text=read_validated_source_text(
                    entry_source,
                    max_input_bytes=max_input_bytes,
                    label="entry",
                ),
                input_mode=INPUT_MODE_FILE,
                role=OPERATION_DAILY_LOG_APPEND,
                source_identity=entry_source.path_ref,
            ),
            source_path=entry_source.path,
        )

    return EntryInputAcquisition(
        acquired=acquired_input_from_text(
            text=acquire_stdin_text(max_input_bytes=max_input_bytes),
            input_mode=INPUT_MODE_STDIN,
            role=OPERATION_DAILY_LOG_APPEND,
        ),
        source_path=None,
    )


def acquire_managed_write_input(
    *,
    source_file: Optional[str],
    use_stdin: bool,
    max_input_bytes: int,
    file_key: Optional[str] = None,
    write_type: Optional[str] = None,
    project_root: Optional[Path] = None,
    storage_root: Optional[Path] = None,
    role: str = OPERATION_MANAGED_WRITE,
) -> AcquiredInput:
    """commit_context_file.py's former ``load_prepared_text``, verbatim."""
    if bool(source_file) == bool(use_stdin):
        details = {
            **OperationContext(
                command=COMMAND_WRITE,
                operation=OPERATION_MANAGED_WRITE,
                write_type=None,
                input_mode=None,
                stage=STAGE_INPUT,
            ).legacy_details_fields(),
            "input_contract": "source-file_xor_stdin",
            "source_file_present": source_file is not None,
            "stdin_present": bool(use_stdin),
            "side_effect": "none",
            "reason_code": (
                REASON_BOTH_INPUT_SOURCES if source_file and use_stdin else REASON_MISSING_INPUT_SOURCE
            ),
        }
        if file_key is not None:
            details["file_key"] = file_key
        if write_type is not None:
            details["write_type"] = write_type
        if source_file and use_stdin:
            raise InputTransportError(
                message="Use exactly one prepared-content input: --source-file or --stdin.",
                details=details,
            )
        raise InputTransportError(
            message="Provide prepared content with --source-file or --stdin.",
            details=details,
        )

    if source_file:
        if project_root is None or storage_root is None:
            raise InputTransportError(
                message="Internal error: project root is required before reading --source-file.",
                details={"input_mode": "file", "reason_code": "prepared_input_context_missing"},
            )
        try:
            source = validate_prepared_input_source_path(
                source_file,
                project_root=project_root,
                storage_root=storage_root,
                input_role="source-file",
                label="source",
            )
        except PreparedInputSafetyError as exc:
            raise prepared_input_transport_error(exc, input_mode="file") from exc
        return acquired_input_from_text(
            text=read_validated_source_text(
                source,
                max_input_bytes=max_input_bytes,
                label="source",
            ),
            input_mode=INPUT_MODE_FILE,
            role=role,
            source_identity=source.path_ref,
        )

    return acquired_input_from_text(
        text=acquire_stdin_text(max_input_bytes=max_input_bytes),
        input_mode=INPUT_MODE_STDIN,
        role=role,
    )
