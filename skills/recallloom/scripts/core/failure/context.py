"""Typed operation context for RecallLoom failure contracts (single owner).

Frozen by the v0.5.0 construction contract (unique construction plan §7.4 and
product plan V2 R4): one immutable ``OperationContext`` carries
``command, operation, write_type, input_mode, stage`` from the earliest point
where the input is known, and the context -> legacy-details mapping lives here
alone. Helpers, dispatcher adapters, and the failure-contract registry must not
hand-assemble those fields or re-derive them from error strings or helper
names.

This module is stdlib-only by contract: it must never import from ``scripts/``
or ``_common.py`` so every layer (helpers, dispatcher, core) can depend on it
without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional


# Typed operation domain (frozen §7.4 / R4): the three mutation operations.
OPERATION_MANAGED_WRITE = "managed_write"
OPERATION_DAILY_LOG_APPEND = "daily_log_append"
OPERATION_POST_APPEND_SUMMARY_SYNC = "post_append_summary_sync"
OPERATION_DOMAIN = frozenset(
    (
        OPERATION_MANAGED_WRITE,
        OPERATION_DAILY_LOG_APPEND,
        OPERATION_POST_APPEND_SUMMARY_SYNC,
    )
)

# Public command spellings of the three mutation operations (frozen §7.2).
COMMAND_WRITE = "write"
COMMAND_APPEND = "append"
COMMAND_SYNC_CURRENT_STATE_AFTER_APPEND = "sync-current-state-after-append"
COMMAND_DOMAIN = frozenset(
    (
        COMMAND_WRITE,
        COMMAND_APPEND,
        COMMAND_SYNC_CURRENT_STATE_AFTER_APPEND,
    )
)

# R4 failure-stage domain (frozen).
STAGE_INPUT = "input"
STAGE_PREFLIGHT = "preflight"
STAGE_LOCK = "lock"
STAGE_REVISION = "revision"
STAGE_CURSOR = "cursor"
STAGE_TARGET = "target"
STAGE_STATE = "state"
STAGE_POST_HASH = "post_hash"
STAGE_RECEIPT = "receipt"
STAGE_SUPPORT = "support"
STAGE_DOMAIN = frozenset(
    (
        STAGE_INPUT,
        STAGE_PREFLIGHT,
        STAGE_LOCK,
        STAGE_REVISION,
        STAGE_CURSOR,
        STAGE_TARGET,
        STAGE_STATE,
        STAGE_POST_HASH,
        STAGE_RECEIPT,
        STAGE_SUPPORT,
    )
)

# Single-owner typed -> legacy spelling mapping. The legacy strings are the
# exact v0.4.8.3 public payload vocabulary and must remain byte-identical
# (managed_write intentionally keeps the historical "managed_file_commit").
LEGACY_COMMAND_BY_OPERATION = MappingProxyType(
    {
        OPERATION_MANAGED_WRITE: COMMAND_WRITE,
        OPERATION_DAILY_LOG_APPEND: COMMAND_APPEND,
        OPERATION_POST_APPEND_SUMMARY_SYNC: COMMAND_SYNC_CURRENT_STATE_AFTER_APPEND,
    }
)
LEGACY_OPERATION_BY_OPERATION = MappingProxyType(
    {
        OPERATION_MANAGED_WRITE: "managed_file_commit",
        OPERATION_DAILY_LOG_APPEND: "daily_log_append",
        OPERATION_POST_APPEND_SUMMARY_SYNC: "post_append_summary_sync",
    }
)


def _require_typed_operation(operation: str) -> str:
    if operation not in OPERATION_DOMAIN:
        raise ValueError(f"unknown typed operation: {operation!r}")
    return operation


def legacy_command_for(operation: str) -> str:
    """Return the legacy public command spelling for a typed operation."""
    return LEGACY_COMMAND_BY_OPERATION[_require_typed_operation(operation)]


def legacy_operation_for(operation: str) -> str:
    """Return the legacy public operation spelling for a typed operation."""
    return LEGACY_OPERATION_BY_OPERATION[_require_typed_operation(operation)]


@dataclass(frozen=True)
class OperationContext:
    """Immutable typed failure context (frozen §7.4 fields).

    Created at the earliest point where the input is known and passed through
    layers verbatim. ``operation`` and ``stage`` are validated against their
    frozen domains; ``command`` must be the operation's public command
    spelling so the context can never mix one operation's command with
    another's identity. ``write_type`` and ``input_mode`` are free-form
    (not frozen domains) and may be ``None`` when not yet known.
    """

    command: str
    operation: str
    write_type: Optional[str]
    input_mode: Optional[str]
    stage: str

    def __post_init__(self) -> None:
        _require_typed_operation(self.operation)
        if self.stage not in STAGE_DOMAIN:
            raise ValueError(f"unknown failure stage: {self.stage!r}")
        canonical_command = LEGACY_COMMAND_BY_OPERATION[self.operation]
        if self.command != canonical_command:
            raise ValueError(
                f"command {self.command!r} does not match operation "
                f"{self.operation!r} (expected {canonical_command!r})"
            )
        for field_name in ("write_type", "input_mode"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")

    @property
    def legacy_operation(self) -> str:
        """The legacy v0.4.8.3 operation spelling for this context."""
        return LEGACY_OPERATION_BY_OPERATION[self.operation]

    def legacy_details_fields(self) -> dict[str, str]:
        """Project the legacy public details fields, in legacy key order.

        Emits exactly the keys v0.4.8.3 payloads carry: ``command`` and
        ``operation`` always, ``write_type`` / ``input_mode`` only when known.
        ``stage`` is typed-context only and is never emitted into legacy
        details.
        """
        fields = {
            "command": self.command,
            "operation": self.legacy_operation,
        }
        if self.write_type is not None:
            fields["write_type"] = self.write_type
        if self.input_mode is not None:
            fields["input_mode"] = self.input_mode
        return fields
