"""Exact, read-only contracts for reviewing bounded inconsistent helper evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from core.coldstart.structured import (
    REVIEW_SECTION_ALIASES,
    classify_review_action,
    extract_structured_sections,
)
from core.protocol.contracts import DAILY_LOG_PATH_PATTERN, FILE_KEYS
from core.provenance.state import (
    PROVENANCE_METADATA_SCHEMA_VERSION,
    PROVENANCE_STATE_LABELS,
    provenance_contract_identity,
)
from core.provenance.store import (
    ReceiptStoreError,
    ReceiptStoreSnapshot,
    accepted_receipt_store_contract_identities,
    capture_receipt_store_snapshot,
    receipt_store_snapshot_matches_current,
    validate_receipt_store_snapshot,
)


INCONSISTENT_REVIEW_SCHEMA_VERSION = "1.0"
INCONSISTENT_REVIEW_EVIDENCE_KEY = "inconsistent_review_evidence"
INCONSISTENT_REVIEW_BINDING_TYPE = "recallloom.inconsistent_review_binding"
INCONSISTENT_RECOVERY_PROPOSAL_TYPE = "inconsistent_recovery_proposal"
INCONSISTENT_RECOVERY_PROPOSED_ACTION = (
    "accept_current_target_as_reviewed_baseline"
)
INCONSISTENT_RECOVERY_REVIEW_TYPE = "inconsistent_recovery_review"
INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES = 1024 * 1024

INCONSISTENT_REVIEW_EVIDENCE_FIELDS = (
    "schema_version",
    "contract_identity",
    "origin",
    "reason_code",
    "failed_surface",
    "operation_class",
    "target_file_key",
    "target_date",
    "target_verification",
    "target_error_class",
    "expected_target_digest",
    "observed_target_digest",
    "state_verification",
    "expected_state_digest",
    "observed_state_digest",
    "expected_workspace_revision",
    "result_workspace_revision",
    "expected_file_revision",
    "result_file_revision",
    "preflight_binding_digest",
    "receipt_store_snapshot_status",
    "receipt_store_revision",
    "receipt_store_digest",
    "receipt_store_contract_identity",
    "previous_state_label",
    "receipt_backed",
    "evidence_digest",
)

INCONSISTENT_REVIEW_BINDING_FIELDS = (
    "schema_version",
    "binding_type",
    "failure_evidence_digest",
    "origin",
    "reason_code",
    "failed_surface",
    "operation_class",
    "target_file_key",
    "target_date",
    "target_verification",
    "target_error_class",
    "failure_expected_target_digest",
    "failure_observed_target_digest",
    "current_target_digest",
    "state_verification",
    "failure_expected_state_digest",
    "failure_observed_state_digest",
    "current_state_digest",
    "expected_workspace_revision",
    "result_workspace_revision",
    "expected_file_revision",
    "result_file_revision",
    "preflight_binding_digest",
    "receipt_store_snapshot_status",
    "failure_receipt_store_revision",
    "failure_receipt_store_digest",
    "failure_receipt_store_contract_identity",
    "current_receipt_store_revision",
    "current_receipt_store_digest",
    "current_receipt_store_contract_identity",
    "validate_contract_identity",
    "receipt_backed",
)

INCONSISTENT_RECOVERY_PROPOSAL_FIELDS = (
    "schema_version",
    "material_type",
    "inconsistent_review_binding_digest",
    "proposed_action",
)

INCONSISTENT_RECOVERY_REVIEW_FIELDS = (
    "schema_version",
    "material_type",
    "inconsistent_review_binding_digest",
    "decision",
    "accept_current_target_as_reviewed_baseline",
)

INCONSISTENT_REVIEW_RESULT_STATUSES = (
    "current",
    "ineligible",
    "binding_changed",
    "malformed",
)
INCONSISTENT_REVIEW_ORPHAN_STATUSES = (
    "current_no_orphan",
    "exact_orphan",
    "binding_or_material_changed_or_ambiguous",
)
REVIEW_IMPORTED_BASELINE_MATERIAL_STATUSES = (
    "verified",
    "not_applicable",
    "invalid",
)
REVIEW_IMPORTED_BASELINE_MATERIAL_FAILURE_REASONS = frozenset(
    {
        "review_imported_baseline_material_directory_unavailable",
        "review_imported_baseline_material_directory_unsafe",
        "review_imported_baseline_proposal_unreadable",
        "review_imported_baseline_material_scan_unbounded",
        "review_imported_baseline_proposal_filename_digest_mismatch",
        "review_imported_baseline_proposal_material_invalid",
        "review_imported_baseline_review_unreadable",
        "review_imported_baseline_review_material_invalid",
        "review_imported_baseline_material_missing_or_ambiguous",
        "review_imported_baseline_material_pair_mismatch",
        "review_imported_baseline_material_changed_during_scan",
    }
)

_CONTRACT_IDENTITY_FIELDS = (
    "contract_name",
    "contract_version",
    "contract_hash",
)
_FAILURE_REASONS = ("post_hash_read_failed", "post_hash_mismatch")
_OPERATION_CLASSES = ("managed_write", "daily_log_append")
_TARGET_VERIFICATIONS = ("read_failed", "mismatch")
_TARGET_ERROR_CLASSES = ("os_error", "unicode_decode_error")
_MANAGED_TARGET_FILE_KEYS = (
    "context_brief",
    "rolling_summary",
    "update_protocol",
)
_D5_SIGNAL_KEYS = frozenset(
    (
        "inconsistent_review_binding_digest",
        "accept_current_target_as_reviewed_baseline",
    )
)
_D5_PROPOSAL_SECTION_ALIASES = (
    "suggested promotion actions",
    "建议提升动作",
)
_D5_REVIEW_SECTION_ALIASES = (
    "promotion status",
    "提升状态",
)
_FENCED_BLOCK_RE = re.compile(
    r"(?ms)^ {0,3}```(?P<info>[^\r\n]*)\r?\n(?P<body>.*?)^ {0,3}```[ \t]*$"
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*$")
_FENCE_LINE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
_HEADING_NUMBER_PREFIX_RE = re.compile(
    r"^\s*[0-9]+(?:\.[0-9]+)*[.)、:：-]?\s*"
)
_CANONICAL_RECOVERY_PROPOSAL_FILE_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.md$"
)
_CANONICAL_RECOVERY_REVIEW_FILE_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[A-Za-z0-9._-]+\.review\.md$"
)
_STAGED_D5_PROPOSAL_DIGEST_RE = re.compile(
    r"--sha256-(?P<digest>[0-9a-f]{64})\.md$"
)
_MAX_RECOVERY_DIRECTORY_ENTRIES = 256
_MAX_RECOVERY_MATERIAL_SCAN_BYTES = 8 * 1024 * 1024


class _DuplicateJsonKey(ValueError):
    pass


class InconsistentReviewContractError(ValueError):
    """Raised when exact D5 material is malformed."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class InconsistentReviewBindingResult:
    status: str
    reason_code: str
    binding: dict[str, Any] | None = None
    binding_digest: str | None = None

    def __post_init__(self) -> None:
        if self.status not in INCONSISTENT_REVIEW_RESULT_STATUSES:
            raise ValueError("Invalid inconsistent-review binding result status.")
        if self.status == "current":
            if not isinstance(self.binding, dict) or not is_sha256_digest(
                self.binding_digest
            ):
                raise ValueError("Current binding results require an object and digest.")
        elif self.binding is not None or self.binding_digest is not None:
            raise ValueError("Non-current binding results must not carry binding material.")


@dataclass(frozen=True)
class InconsistentReviewOrphanResult:
    status: str
    reason_code: str
    binding: dict[str, Any] | None = None
    binding_digest: str | None = None
    target_read_performed: bool = False
    receipt_store_snapshot_performed: bool = False
    material_scan_performed: bool = False

    def __post_init__(self) -> None:
        if self.status not in INCONSISTENT_REVIEW_ORPHAN_STATUSES:
            raise ValueError("Invalid inconsistent-review orphan result status.")
        if self.status in {"current_no_orphan", "exact_orphan"}:
            if not isinstance(self.binding, dict) or not is_sha256_digest(
                self.binding_digest
            ):
                raise ValueError("Current orphan results require a binding and digest.")
        elif self.binding is not None or self.binding_digest is not None:
            raise ValueError("Changed orphan results must not carry binding material.")


@dataclass(frozen=True)
class ReviewImportedBaselineMaterialResult:
    status: str
    reason_code: str
    binding_digest: str | None = None
    proposal_digest: str | None = None
    review_digest: str | None = None

    def __post_init__(self) -> None:
        if self.status not in REVIEW_IMPORTED_BASELINE_MATERIAL_STATUSES:
            raise ValueError("Invalid reviewed-baseline material result status.")
        digests = (
            self.binding_digest,
            self.proposal_digest,
            self.review_digest,
        )
        if self.status == "verified":
            if not all(is_sha256_digest(digest) for digest in digests):
                raise ValueError("Verified reviewed-baseline material requires all digests.")
        elif any(digest is not None for digest in digests):
            raise ValueError("Non-verified reviewed-baseline results must not carry digests.")


def is_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def canonical_json_digest(payload: Mapping[str, Any]) -> str:
    canonical_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()


def staged_d5_proposal_filename(
    *,
    filename_stamp: str,
    proposal_id: str,
    staged_text: str,
) -> str:
    """Bind the exact staged proposal bytes into the existing proposal filename."""

    digest = hashlib.sha256(staged_text.encode("utf-8")).hexdigest()
    return f"{filename_stamp}-{proposal_id}--sha256-{digest}.md"


def staged_d5_proposal_digest_from_filename(filename: str) -> str | None:
    match = _STAGED_D5_PROPOSAL_DIGEST_RE.search(filename)
    if match is None:
        return None
    return "sha256:" + match.group("digest")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateJsonKey(key)
        payload[key] = value
    return payload


def _parse_strict_json(text: str, *, reason_code: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_strict_json_object)
    except (_DuplicateJsonKey, json.JSONDecodeError) as exc:
        raise InconsistentReviewContractError(reason_code) from exc


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if left.keys() != right.keys():
            return False
        return all(_strict_json_equal(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _is_json_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_json_int(value: Any) -> bool:
    return _is_json_int(value) and value >= 1


def _is_contract_identity(value: Any, *, expected: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_CONTRACT_IDENTITY_FIELDS)
        and value == expected
        and is_sha256_digest(value.get("contract_hash"))
    )


def _is_accepted_store_contract_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_CONTRACT_IDENTITY_FIELDS)
        and value in accepted_receipt_store_contract_identities()
        and is_sha256_digest(value.get("contract_hash"))
    )


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_operation_target(
    *,
    operation_class: Any,
    target_file_key: Any,
    target_date: Any,
) -> bool:
    if operation_class == "managed_write":
        return target_file_key in _MANAGED_TARGET_FILE_KEYS and target_date is None
    if operation_class == "daily_log_append":
        return target_file_key == "daily_log" and _is_iso_date(target_date)
    return False


def _validate_store_snapshot_fields(
    *,
    status: Any,
    revision: Any,
    digest: Any,
    contract_identity: Any,
) -> bool:
    if status == "absent":
        return revision is None and digest is None and contract_identity is None
    if status == "present":
        return (
            _is_json_int(revision)
            and revision >= 0
            and is_sha256_digest(digest)
            and _is_accepted_store_contract_identity(contract_identity)
        )
    return False


def failure_evidence_digest(evidence: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in evidence.items()
        if key != "evidence_digest"
    }
    return canonical_json_digest(payload)


def validate_inconsistent_review_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise InconsistentReviewContractError("inconsistent_review_evidence_not_object")
    if set(evidence) != set(INCONSISTENT_REVIEW_EVIDENCE_FIELDS):
        raise InconsistentReviewContractError("inconsistent_review_evidence_keys_invalid")
    if evidence.get("schema_version") != INCONSISTENT_REVIEW_SCHEMA_VERSION:
        raise InconsistentReviewContractError("inconsistent_review_evidence_version_invalid")
    if not _is_contract_identity(
        evidence.get("contract_identity"),
        expected=provenance_contract_identity(),
    ):
        raise InconsistentReviewContractError("inconsistent_review_evidence_contract_invalid")
    if evidence.get("origin") != "helper_post_hash_verification":
        raise InconsistentReviewContractError("inconsistent_review_evidence_origin_invalid")
    reason_code = evidence.get("reason_code")
    target_verification = evidence.get("target_verification")
    target_error_class = evidence.get("target_error_class")
    observed_target_digest = evidence.get("observed_target_digest")
    if reason_code not in _FAILURE_REASONS:
        raise InconsistentReviewContractError("inconsistent_review_evidence_reason_invalid")
    if evidence.get("failed_surface") != "target":
        raise InconsistentReviewContractError("inconsistent_review_evidence_surface_invalid")
    if evidence.get("operation_class") not in _OPERATION_CLASSES or not _validate_operation_target(
        operation_class=evidence.get("operation_class"),
        target_file_key=evidence.get("target_file_key"),
        target_date=evidence.get("target_date"),
    ):
        raise InconsistentReviewContractError("inconsistent_review_evidence_target_invalid")
    if target_verification not in _TARGET_VERIFICATIONS:
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_target_verification_invalid"
        )
    if reason_code == "post_hash_read_failed":
        if (
            target_verification != "read_failed"
            or target_error_class not in _TARGET_ERROR_CLASSES
            or observed_target_digest is not None
        ):
            raise InconsistentReviewContractError(
                "inconsistent_review_evidence_read_failure_invalid"
            )
    elif (
        target_verification != "mismatch"
        or target_error_class is not None
        or not is_sha256_digest(observed_target_digest)
        or observed_target_digest == evidence.get("expected_target_digest")
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_mismatch_invalid"
        )
    if not is_sha256_digest(evidence.get("expected_target_digest")):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_expected_target_digest_invalid"
        )
    if evidence.get("state_verification") != "exact_match":
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_state_verification_invalid"
        )
    expected_state_digest = evidence.get("expected_state_digest")
    observed_state_digest = evidence.get("observed_state_digest")
    if (
        not is_sha256_digest(expected_state_digest)
        or observed_state_digest != expected_state_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_state_digest_invalid"
        )
    for field_name in ("expected_workspace_revision", "result_workspace_revision"):
        if not _is_positive_json_int(evidence.get(field_name)):
            raise InconsistentReviewContractError(
                f"inconsistent_review_evidence_{field_name}_invalid"
            )
    expected_file_revision = evidence.get("expected_file_revision")
    result_file_revision = evidence.get("result_file_revision")
    if (
        not _is_json_int(expected_file_revision)
        or expected_file_revision < 0
        or not _is_positive_json_int(result_file_revision)
        or (
            evidence["operation_class"] == "managed_write"
            and expected_file_revision < 1
        )
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_file_revision_invalid"
        )
    if (
        evidence["result_workspace_revision"]
        != evidence["expected_workspace_revision"] + 1
        or result_file_revision != expected_file_revision + 1
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_revision_transition_invalid"
        )
    if not is_sha256_digest(evidence.get("preflight_binding_digest")):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_preflight_digest_invalid"
        )
    if not _validate_store_snapshot_fields(
        status=evidence.get("receipt_store_snapshot_status"),
        revision=evidence.get("receipt_store_revision"),
        digest=evidence.get("receipt_store_digest"),
        contract_identity=evidence.get("receipt_store_contract_identity"),
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_store_snapshot_invalid"
        )
    if evidence.get("previous_state_label") not in PROVENANCE_STATE_LABELS:
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_previous_state_invalid"
        )
    if evidence.get("receipt_backed") is not False:
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_receipt_backed_invalid"
        )
    if (
        not is_sha256_digest(evidence.get("evidence_digest"))
        or evidence["evidence_digest"] != failure_evidence_digest(evidence)
    ):
        raise InconsistentReviewContractError("inconsistent_review_evidence_digest_invalid")
    return dict(evidence)


def parse_inconsistent_review_evidence_json(text: str) -> dict[str, Any]:
    payload = _parse_strict_json(
        text,
        reason_code="inconsistent_review_evidence_json_invalid",
    )
    return validate_inconsistent_review_evidence(payload)


def build_inconsistent_review_evidence(
    *,
    reason_code: str,
    operation_class: str,
    target_file_key: str,
    target_date: str | None,
    target_verification: str,
    target_error_class: str | None,
    expected_target_digest: str,
    observed_target_digest: str | None,
    expected_state_digest: str,
    observed_state_digest: str,
    expected_workspace_revision: int,
    result_workspace_revision: int,
    expected_file_revision: int,
    result_file_revision: int,
    preflight_binding_digest: str,
    receipt_store_snapshot: ReceiptStoreSnapshot,
    previous_state_label: str,
) -> dict[str, Any]:
    try:
        validate_receipt_store_snapshot(receipt_store_snapshot)
    except ReceiptStoreError as exc:
        raise InconsistentReviewContractError(
            "inconsistent_review_evidence_store_snapshot_invalid"
        ) from exc
    evidence = {
        "schema_version": INCONSISTENT_REVIEW_SCHEMA_VERSION,
        "contract_identity": provenance_contract_identity(),
        "origin": "helper_post_hash_verification",
        "reason_code": reason_code,
        "failed_surface": "target",
        "operation_class": operation_class,
        "target_file_key": target_file_key,
        "target_date": target_date,
        "target_verification": target_verification,
        "target_error_class": target_error_class,
        "expected_target_digest": expected_target_digest,
        "observed_target_digest": observed_target_digest,
        "state_verification": "exact_match",
        "expected_state_digest": expected_state_digest,
        "observed_state_digest": observed_state_digest,
        "expected_workspace_revision": expected_workspace_revision,
        "result_workspace_revision": result_workspace_revision,
        "expected_file_revision": expected_file_revision,
        "result_file_revision": result_file_revision,
        "preflight_binding_digest": preflight_binding_digest,
        "receipt_store_snapshot_status": receipt_store_snapshot.status,
        "receipt_store_revision": receipt_store_snapshot.revision,
        "receipt_store_digest": receipt_store_snapshot.digest,
        "receipt_store_contract_identity": (
            dict(receipt_store_snapshot.contract_identity)
            if isinstance(receipt_store_snapshot.contract_identity, dict)
            else None
        ),
        "previous_state_label": previous_state_label,
        "receipt_backed": False,
    }
    evidence["evidence_digest"] = failure_evidence_digest(evidence)
    return validate_inconsistent_review_evidence(evidence)


def inconsistent_review_binding_digest(binding: Mapping[str, Any]) -> str:
    return canonical_json_digest(validate_inconsistent_review_binding(dict(binding)))


def parse_inconsistent_review_binding_json(text: str) -> dict[str, Any]:
    payload = _parse_strict_json(
        text,
        reason_code="inconsistent_review_binding_json_invalid",
    )
    return validate_inconsistent_review_binding(payload)


def validate_inconsistent_review_binding(
    binding: Any,
    *,
    failure_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise InconsistentReviewContractError("inconsistent_review_binding_not_object")
    if set(binding) != set(INCONSISTENT_REVIEW_BINDING_FIELDS):
        raise InconsistentReviewContractError("inconsistent_review_binding_keys_invalid")
    if (
        binding.get("schema_version") != INCONSISTENT_REVIEW_SCHEMA_VERSION
        or binding.get("binding_type") != INCONSISTENT_REVIEW_BINDING_TYPE
        or binding.get("origin") != "helper_post_hash_verification"
        or binding.get("reason_code") not in _FAILURE_REASONS
        or binding.get("failed_surface") != "target"
        or binding.get("operation_class") not in _OPERATION_CLASSES
        or not _validate_operation_target(
            operation_class=binding.get("operation_class"),
            target_file_key=binding.get("target_file_key"),
            target_date=binding.get("target_date"),
        )
    ):
        raise InconsistentReviewContractError("inconsistent_review_binding_identity_invalid")
    if not is_sha256_digest(binding.get("failure_evidence_digest")):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_failure_digest_invalid"
        )
    target_verification = binding.get("target_verification")
    target_error_class = binding.get("target_error_class")
    failure_observed_target_digest = binding.get("failure_observed_target_digest")
    if binding.get("reason_code") == "post_hash_read_failed":
        if (
            target_verification != "read_failed"
            or target_error_class not in _TARGET_ERROR_CLASSES
            or failure_observed_target_digest is not None
        ):
            raise InconsistentReviewContractError(
                "inconsistent_review_binding_read_failure_invalid"
            )
    elif (
        target_verification != "mismatch"
        or target_error_class is not None
        or not is_sha256_digest(failure_observed_target_digest)
        or failure_observed_target_digest
        == binding.get("failure_expected_target_digest")
        or binding.get("current_target_digest") != failure_observed_target_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_mismatch_invalid"
        )
    if (
        not is_sha256_digest(binding.get("failure_expected_target_digest"))
        or not is_sha256_digest(binding.get("current_target_digest"))
        or binding.get("state_verification") != "exact_match"
        or not is_sha256_digest(binding.get("failure_expected_state_digest"))
        or binding.get("failure_observed_state_digest")
        != binding.get("failure_expected_state_digest")
        or not is_sha256_digest(binding.get("current_state_digest"))
        or not is_sha256_digest(binding.get("preflight_binding_digest"))
    ):
        raise InconsistentReviewContractError("inconsistent_review_binding_digest_invalid")
    for field_name in ("expected_workspace_revision", "result_workspace_revision"):
        if not _is_positive_json_int(binding.get(field_name)):
            raise InconsistentReviewContractError(
                f"inconsistent_review_binding_{field_name}_invalid"
            )
    expected_file_revision = binding.get("expected_file_revision")
    result_file_revision = binding.get("result_file_revision")
    if (
        not _is_json_int(expected_file_revision)
        or expected_file_revision < 0
        or not _is_positive_json_int(result_file_revision)
        or (
            binding["operation_class"] == "managed_write"
            and expected_file_revision < 1
        )
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_file_revision_invalid"
        )
    if (
        binding["result_workspace_revision"]
        != binding["expected_workspace_revision"] + 1
        or result_file_revision != expected_file_revision + 1
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_revision_transition_invalid"
        )
    if not _validate_store_snapshot_fields(
        status=binding.get("receipt_store_snapshot_status"),
        revision=binding.get("failure_receipt_store_revision"),
        digest=binding.get("failure_receipt_store_digest"),
        contract_identity=binding.get("failure_receipt_store_contract_identity"),
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_failure_store_invalid"
        )
    if (
        binding.get("current_receipt_store_revision")
        != binding.get("failure_receipt_store_revision")
        or binding.get("current_receipt_store_digest")
        != binding.get("failure_receipt_store_digest")
        or binding.get("current_receipt_store_contract_identity")
        != binding.get("failure_receipt_store_contract_identity")
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_current_store_invalid"
        )
    if not _is_contract_identity(
        binding.get("validate_contract_identity"),
        expected=provenance_contract_identity(),
    ):
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_validate_contract_invalid"
        )
    if binding.get("receipt_backed") is not False:
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_receipt_backed_invalid"
        )
    if failure_evidence is not None:
        evidence = validate_inconsistent_review_evidence(failure_evidence)
        expected_values = {
            "failure_evidence_digest": evidence["evidence_digest"],
            "origin": evidence["origin"],
            "reason_code": evidence["reason_code"],
            "failed_surface": evidence["failed_surface"],
            "operation_class": evidence["operation_class"],
            "target_file_key": evidence["target_file_key"],
            "target_date": evidence["target_date"],
            "target_verification": evidence["target_verification"],
            "target_error_class": evidence["target_error_class"],
            "failure_expected_target_digest": evidence["expected_target_digest"],
            "failure_observed_target_digest": evidence["observed_target_digest"],
            "state_verification": evidence["state_verification"],
            "failure_expected_state_digest": evidence["expected_state_digest"],
            "failure_observed_state_digest": evidence["observed_state_digest"],
            "expected_workspace_revision": evidence["expected_workspace_revision"],
            "result_workspace_revision": evidence["result_workspace_revision"],
            "expected_file_revision": evidence["expected_file_revision"],
            "result_file_revision": evidence["result_file_revision"],
            "preflight_binding_digest": evidence["preflight_binding_digest"],
            "receipt_store_snapshot_status": evidence[
                "receipt_store_snapshot_status"
            ],
            "failure_receipt_store_revision": evidence["receipt_store_revision"],
            "failure_receipt_store_digest": evidence["receipt_store_digest"],
            "failure_receipt_store_contract_identity": evidence[
                "receipt_store_contract_identity"
            ],
        }
        if any(binding.get(key) != value for key, value in expected_values.items()):
            raise InconsistentReviewContractError(
                "inconsistent_review_binding_failure_evidence_mismatch"
            )
    return dict(binding)


def build_inconsistent_review_binding(
    *,
    failure_evidence: Mapping[str, Any],
    current_target_digest: str,
    current_state_digest: str,
    current_receipt_store_snapshot: ReceiptStoreSnapshot,
) -> dict[str, Any]:
    evidence = validate_inconsistent_review_evidence(failure_evidence)
    try:
        validate_receipt_store_snapshot(current_receipt_store_snapshot)
    except ReceiptStoreError as exc:
        raise InconsistentReviewContractError(
            "inconsistent_review_binding_current_store_invalid"
        ) from exc
    binding = {
        "schema_version": INCONSISTENT_REVIEW_SCHEMA_VERSION,
        "binding_type": INCONSISTENT_REVIEW_BINDING_TYPE,
        "failure_evidence_digest": evidence["evidence_digest"],
        "origin": evidence["origin"],
        "reason_code": evidence["reason_code"],
        "failed_surface": evidence["failed_surface"],
        "operation_class": evidence["operation_class"],
        "target_file_key": evidence["target_file_key"],
        "target_date": evidence["target_date"],
        "target_verification": evidence["target_verification"],
        "target_error_class": evidence["target_error_class"],
        "failure_expected_target_digest": evidence["expected_target_digest"],
        "failure_observed_target_digest": evidence["observed_target_digest"],
        "current_target_digest": current_target_digest,
        "state_verification": evidence["state_verification"],
        "failure_expected_state_digest": evidence["expected_state_digest"],
        "failure_observed_state_digest": evidence["observed_state_digest"],
        "current_state_digest": current_state_digest,
        "expected_workspace_revision": evidence["expected_workspace_revision"],
        "result_workspace_revision": evidence["result_workspace_revision"],
        "expected_file_revision": evidence["expected_file_revision"],
        "result_file_revision": evidence["result_file_revision"],
        "preflight_binding_digest": evidence["preflight_binding_digest"],
        "receipt_store_snapshot_status": evidence["receipt_store_snapshot_status"],
        "failure_receipt_store_revision": evidence["receipt_store_revision"],
        "failure_receipt_store_digest": evidence["receipt_store_digest"],
        "failure_receipt_store_contract_identity": evidence[
            "receipt_store_contract_identity"
        ],
        "current_receipt_store_revision": current_receipt_store_snapshot.revision,
        "current_receipt_store_digest": current_receipt_store_snapshot.digest,
        "current_receipt_store_contract_identity": (
            dict(current_receipt_store_snapshot.contract_identity)
            if isinstance(current_receipt_store_snapshot.contract_identity, dict)
            else None
        ),
        "validate_contract_identity": provenance_contract_identity(),
        "receipt_backed": False,
    }
    return validate_inconsistent_review_binding(
        binding,
        failure_evidence=evidence,
    )


def _snapshot_matches_failure(
    snapshot: ReceiptStoreSnapshot,
    evidence: Mapping[str, Any],
) -> bool:
    return (
        snapshot.status == evidence.get("receipt_store_snapshot_status")
        and snapshot.revision == evidence.get("receipt_store_revision")
        and snapshot.digest == evidence.get("receipt_store_digest")
        and snapshot.contract_identity == evidence.get("receipt_store_contract_identity")
    )


def _state_target_path(storage_root: Path, evidence: Mapping[str, Any]) -> Path:
    if evidence["operation_class"] == "daily_log_append":
        return storage_root / DAILY_LOG_PATH_PATTERN.format(date=evidence["target_date"])
    return storage_root / FILE_KEYS[evidence["target_file_key"]]


def _stable_utf8_text_snapshot(path: Path) -> tuple[str, str] | None:
    try:
        before_stat = path.lstat()
        if not stat.S_ISREG(before_stat.st_mode):
            return None
        raw_bytes = path.read_bytes()
        after_stat = path.lstat()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    before_identity = (
        before_stat.st_dev,
        before_stat.st_ino,
        before_stat.st_mode,
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )
    after_identity = (
        after_stat.st_dev,
        after_stat.st_ino,
        after_stat.st_mode,
        after_stat.st_size,
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
    )
    if before_identity != after_identity:
        return None
    return text, "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable_utf8_text_digest(path: Path) -> str | None:
    snapshot = _stable_utf8_text_snapshot(path)
    return snapshot[1] if snapshot is not None else None


def _current_revisions_match(state: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    if state.get("workspace_revision") != evidence.get("result_workspace_revision"):
        return False
    if evidence.get("operation_class") == "daily_log_append":
        daily_logs = state.get("daily_logs")
        expected_latest_file = DAILY_LOG_PATH_PATTERN.format(date=evidence["target_date"])
        return (
            isinstance(daily_logs, dict)
            and daily_logs.get("latest_file") == expected_latest_file
            and daily_logs.get("latest_entry_seq") == evidence.get("result_file_revision")
        )
    files = state.get("files")
    file_state = files.get(evidence["target_file_key"]) if isinstance(files, dict) else None
    return (
        isinstance(file_state, dict)
        and file_state.get("file_revision") == evidence.get("result_file_revision")
    )


def evaluate_current_inconsistent_review_binding(
    *,
    project_root: Path,
    storage_root: Path,
    state: Mapping[str, Any] | None,
    state_text: str | None,
) -> InconsistentReviewBindingResult:
    if not isinstance(state, Mapping) or not isinstance(state_text, str):
        return InconsistentReviewBindingResult("ineligible", "state_unavailable")
    try:
        parsed_state = _parse_strict_json(
            state_text,
            reason_code="inconsistent_review_state_json_malformed",
        )
    except InconsistentReviewContractError as exc:
        return InconsistentReviewBindingResult("malformed", exc.reason_code)
    if not isinstance(parsed_state, dict) or not _strict_json_equal(parsed_state, dict(state)):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_state_snapshot_changed",
        )
    provenance = state.get("provenance")
    if not isinstance(provenance, Mapping):
        return InconsistentReviewBindingResult("ineligible", "provenance_metadata_absent")
    if provenance.get("state_label") != "inconsistent_or_tampered_evidence":
        return InconsistentReviewBindingResult(
            "ineligible",
            "provenance_state_not_inconsistent",
        )
    if INCONSISTENT_REVIEW_EVIDENCE_KEY not in provenance:
        return InconsistentReviewBindingResult(
            "ineligible",
            "inconsistent_review_evidence_absent",
        )
    try:
        evidence = validate_inconsistent_review_evidence(
            provenance.get(INCONSISTENT_REVIEW_EVIDENCE_KEY)
        )
    except InconsistentReviewContractError as exc:
        return InconsistentReviewBindingResult("malformed", exc.reason_code)
    if (
        provenance.get("schema_version") != PROVENANCE_METADATA_SCHEMA_VERSION
        or provenance.get("baseline_kind") != "receipt_evidence_inconsistent"
        or provenance.get("reason_code") != evidence["reason_code"]
        or provenance.get("previous_state_label") != evidence["previous_state_label"]
        or provenance.get("receipt_backed") is not False
    ):
        return InconsistentReviewBindingResult(
            "malformed",
            "inconsistent_review_provenance_metadata_mismatch",
        )
    if evidence["contract_identity"] != provenance_contract_identity():
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_contract_changed",
        )
    if not _current_revisions_match(state, evidence):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_revision_changed",
        )
    current_target_digest = _stable_utf8_text_digest(
        _state_target_path(storage_root, evidence)
    )
    if current_target_digest is None:
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_target_unavailable",
        )
    if (
        evidence["target_verification"] == "mismatch"
        and current_target_digest != evidence["observed_target_digest"]
    ):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_target_changed",
        )
    try:
        current_store_snapshot = capture_receipt_store_snapshot(
            storage_root=storage_root,
            project_root=project_root,
        )
    except ReceiptStoreError:
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_receipt_store_unavailable",
        )
    if not _snapshot_matches_failure(current_store_snapshot, evidence):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_receipt_store_changed",
        )
    current_state_digest = "sha256:" + hashlib.sha256(
        state_text.encode("utf-8")
    ).hexdigest()
    final_state_snapshot = _stable_utf8_text_snapshot(
        storage_root / FILE_KEYS["state"]
    )
    if (
        final_state_snapshot is None
        or final_state_snapshot[0] != state_text
        or final_state_snapshot[1] != current_state_digest
    ):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_state_changed_during_binding",
        )
    final_target_digest = _stable_utf8_text_digest(
        _state_target_path(storage_root, evidence)
    )
    if final_target_digest != current_target_digest:
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_target_changed_during_binding",
        )
    if not receipt_store_snapshot_matches_current(
        storage_root=storage_root,
        project_root=project_root,
        snapshot=current_store_snapshot,
    ):
        return InconsistentReviewBindingResult(
            "binding_changed",
            "inconsistent_review_receipt_store_changed_during_binding",
        )
    try:
        binding = build_inconsistent_review_binding(
            failure_evidence=evidence,
            current_target_digest=current_target_digest,
            current_state_digest=current_state_digest,
            current_receipt_store_snapshot=current_store_snapshot,
        )
    except InconsistentReviewContractError as exc:
        return InconsistentReviewBindingResult("malformed", exc.reason_code)
    return InconsistentReviewBindingResult(
        "current",
        "inconsistent_review_binding_current",
        binding=binding,
        binding_digest=inconsistent_review_binding_digest(binding),
    )


def _normalize_heading_title(raw: str) -> str:
    title = _HEADING_NUMBER_PREFIX_RE.sub("", raw.strip())
    return title.strip().strip(":：-").strip().casefold()


def _section_for_offset(text: str, offset: int) -> str | None:
    section_title = None
    active_fence: str | None = None
    position = 0
    for line in text.splitlines(keepends=True):
        if position >= offset:
            break
        fence_match = _FENCE_LINE_RE.match(line)
        if fence_match:
            fence = fence_match.group("fence")
            if active_fence is None:
                active_fence = fence[0]
            elif fence[0] == active_fence:
                active_fence = None
        elif active_fence is None:
            heading_match = _MARKDOWN_HEADING_RE.match(line.rstrip("\r\n"))
            if heading_match:
                section_title = _normalize_heading_title(
                    heading_match.group("title")
                )
        position += len(line)
    return section_title


def _contains_d5_signal(text: str) -> bool:
    return (
        any(f'"{key}"' in text for key in _D5_SIGNAL_KEYS)
        or INCONSISTENT_RECOVERY_PROPOSAL_TYPE in text
        or INCONSISTENT_RECOVERY_REVIEW_TYPE in text
        or INCONSISTENT_RECOVERY_PROPOSED_ACTION in text
    )


def validate_inconsistent_recovery_material_size(
    text: str,
    *,
    material_role: str,
) -> int:
    """Enforce the single byte limit shared by D5 stage, promotion, and validation."""

    if material_role not in {"proposal", "review"}:
        raise ValueError("D5 recovery material role must be proposal or review.")
    byte_count = len(text.encode("utf-8"))
    if byte_count > INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES:
        raise InconsistentReviewContractError(
            f"inconsistent_recovery_{material_role}_too_large"
        )
    return byte_count


def _d5_json_fence_candidates(text: str) -> list[tuple[re.Match[str], str]]:
    candidates: list[tuple[re.Match[str], str]] = []
    for match in _FENCED_BLOCK_RE.finditer(text):
        body = match.group("body")
        parsed_payload = None
        try:
            parsed_payload = json.loads(body, object_pairs_hook=_strict_json_object)
        except (_DuplicateJsonKey, json.JSONDecodeError):
            pass
        parsed_d5_signal = bool(
            isinstance(parsed_payload, dict)
            and (
                set(parsed_payload).intersection(_D5_SIGNAL_KEYS)
                or parsed_payload.get("material_type")
                in {
                    INCONSISTENT_RECOVERY_PROPOSAL_TYPE,
                    INCONSISTENT_RECOVERY_REVIEW_TYPE,
                }
                or parsed_payload.get("proposed_action")
                == INCONSISTENT_RECOVERY_PROPOSED_ACTION
            )
        )
        if parsed_d5_signal or _contains_d5_signal(body):
            candidates.append((match, body))
    return candidates


def validate_inconsistent_recovery_proposal_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise InconsistentReviewContractError("inconsistent_recovery_proposal_not_object")
    if set(block) != set(INCONSISTENT_RECOVERY_PROPOSAL_FIELDS):
        raise InconsistentReviewContractError("inconsistent_recovery_proposal_keys_invalid")
    if (
        block.get("schema_version") != INCONSISTENT_REVIEW_SCHEMA_VERSION
        or block.get("material_type") != INCONSISTENT_RECOVERY_PROPOSAL_TYPE
        or block.get("proposed_action") != INCONSISTENT_RECOVERY_PROPOSED_ACTION
        or not is_sha256_digest(block.get("inconsistent_review_binding_digest"))
    ):
        raise InconsistentReviewContractError("inconsistent_recovery_proposal_values_invalid")
    return dict(block)


def parse_inconsistent_recovery_proposal_block_json(text: str) -> dict[str, Any]:
    payload = _parse_strict_json(
        text,
        reason_code="inconsistent_recovery_proposal_json_invalid",
    )
    return validate_inconsistent_recovery_proposal_block(payload)


def validate_inconsistent_recovery_proposal_text(
    text: str,
    *,
    expected_binding_digest: str | None = None,
) -> dict[str, Any] | None:
    if expected_binding_digest is not None or _contains_d5_signal(text):
        validate_inconsistent_recovery_material_size(
            text,
            material_role="proposal",
        )
    if expected_binding_digest is not None and not is_sha256_digest(
        expected_binding_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_recovery_proposal_expected_binding_invalid"
        )
    candidates = _d5_json_fence_candidates(text)
    if not candidates:
        if _contains_d5_signal(text):
            raise InconsistentReviewContractError(
                "inconsistent_recovery_proposal_block_count_invalid"
            )
        return None
    if len(candidates) != 1:
        raise InconsistentReviewContractError(
            "inconsistent_recovery_proposal_block_count_invalid"
        )
    match, body = candidates[0]
    if match.group("info").strip() != "json":
        raise InconsistentReviewContractError(
            "inconsistent_recovery_proposal_fence_invalid"
        )
    section_title = _section_for_offset(text, match.start())
    if section_title not in _D5_PROPOSAL_SECTION_ALIASES:
        raise InconsistentReviewContractError(
            "inconsistent_recovery_proposal_section_invalid"
        )
    validated = parse_inconsistent_recovery_proposal_block_json(body)
    if (
        expected_binding_digest is not None
        and validated["inconsistent_review_binding_digest"]
        != expected_binding_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_recovery_proposal_binding_changed"
        )
    return validated


def validate_inconsistent_recovery_review_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise InconsistentReviewContractError("inconsistent_recovery_review_not_object")
    if set(block) != set(INCONSISTENT_RECOVERY_REVIEW_FIELDS):
        raise InconsistentReviewContractError("inconsistent_recovery_review_keys_invalid")
    decision = block.get("decision")
    accept_current = block.get("accept_current_target_as_reviewed_baseline")
    if (
        block.get("schema_version") != INCONSISTENT_REVIEW_SCHEMA_VERSION
        or block.get("material_type") != INCONSISTENT_RECOVERY_REVIEW_TYPE
        or not is_sha256_digest(block.get("inconsistent_review_binding_digest"))
        or decision not in {"accept", "reject"}
        or type(accept_current) is not bool
        or (decision == "accept" and accept_current is not True)
        or (decision == "reject" and accept_current is not False)
    ):
        raise InconsistentReviewContractError("inconsistent_recovery_review_values_invalid")
    return dict(block)


def parse_inconsistent_recovery_review_block_json(text: str) -> dict[str, Any]:
    payload = _parse_strict_json(
        text,
        reason_code="inconsistent_recovery_review_json_invalid",
    )
    return validate_inconsistent_recovery_review_block(payload)


def validate_inconsistent_recovery_review_text(
    text: str,
    *,
    expected_binding_digest: str | None = None,
) -> dict[str, Any] | None:
    if expected_binding_digest is not None or _contains_d5_signal(text):
        validate_inconsistent_recovery_material_size(
            text,
            material_role="review",
        )
    if expected_binding_digest is not None and not is_sha256_digest(
        expected_binding_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_recovery_review_expected_binding_invalid"
        )
    candidates = _d5_json_fence_candidates(text)
    if not candidates:
        if _contains_d5_signal(text):
            raise InconsistentReviewContractError(
                "inconsistent_recovery_review_block_count_invalid"
            )
        return None
    if len(candidates) != 1:
        raise InconsistentReviewContractError(
            "inconsistent_recovery_review_block_count_invalid"
        )
    match, body = candidates[0]
    if match.group("info").strip() != "json":
        raise InconsistentReviewContractError(
            "inconsistent_recovery_review_fence_invalid"
        )
    section_title = _section_for_offset(text, match.start())
    if section_title not in _D5_REVIEW_SECTION_ALIASES:
        raise InconsistentReviewContractError(
            "inconsistent_recovery_review_section_invalid"
        )
    validated = parse_inconsistent_recovery_review_block_json(body)
    if (
        expected_binding_digest is not None
        and validated["inconsistent_review_binding_digest"]
        != expected_binding_digest
    ):
        raise InconsistentReviewContractError(
            "inconsistent_recovery_review_binding_changed"
        )
    return validated


def _stat_identity(stat_result: os.stat_result) -> tuple[int, ...]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _open_directory_chain_nofollow(
    storage_root: Path,
    parts: tuple[str, ...],
) -> tuple[str, int | None]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd: int | None = None
    try:
        current_fd = os.open(storage_root, flags)
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return "present", current_fd
    except OSError as exc:
        if current_fd is not None:
            os.close(current_fd)
        if exc.errno == errno.ENOENT:
            return "absent", None
        return "unsafe", None


def _bounded_canonical_directory_entries(
    directory_fd: int,
    *,
    pattern: re.Pattern[str],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    try:
        before_identity = _stat_identity(os.fstat(directory_fd))
        all_names = _capped_directory_names(directory_fd)
        if all_names is None:
            return None
        canonical_names: list[str] = []
        for name in all_names:
            if not pattern.fullmatch(name):
                continue
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(entry_stat.st_mode):
                return None
            canonical_names.append(name)
        after_names = _capped_directory_names(directory_fd)
        after_identity = _stat_identity(os.fstat(directory_fd))
    except OSError:
        return None
    if (
        after_names is None
        or before_identity != after_identity
        or all_names != after_names
    ):
        return None
    return all_names, tuple(canonical_names)


def _capped_directory_names(directory_fd: int) -> tuple[str, ...] | None:
    """Read at most limit + 1 names so overflow cannot force full enumeration."""

    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > _MAX_RECOVERY_DIRECTORY_ENTRIES:
                    return None
    except OSError:
        return None
    return tuple(sorted(names))


def _stable_material_snapshot_at(
    directory_fd: int,
    name: str,
) -> tuple[str, str, int] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        before_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before_stat.st_mode)
            or before_stat.st_size > INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_bytes = b"".join(chunks)
        after_stat = os.fstat(file_fd)
    except OSError:
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
    if (
        len(raw_bytes) > INCONSISTENT_RECOVERY_MATERIAL_MAX_BYTES
        or _stat_identity(before_stat) != _stat_identity(after_stat)
        or len(raw_bytes) != after_stat.st_size
    ):
        return None
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text, "sha256:" + hashlib.sha256(raw_bytes).hexdigest(), len(raw_bytes)


def _material_snapshots_unchanged(
    directory_fd: int | None,
    snapshots: Mapping[str, str],
) -> bool:
    if directory_fd is None:
        return not snapshots
    for name, expected_digest in snapshots.items():
        current = _stable_material_snapshot_at(directory_fd, name)
        if current is None or current[1] != expected_digest:
            return False
    return True


def _directory_snapshot_unchanged(
    *,
    storage_root: Path,
    parts: tuple[str, ...],
    original_fd: int | None,
    original_names: tuple[str, ...],
) -> bool:
    if original_fd is None:
        status, current_fd = _open_directory_chain_nofollow(storage_root, parts)
        if current_fd is not None:
            os.close(current_fd)
        return status == "absent"
    status, current_fd = _open_directory_chain_nofollow(storage_root, parts)
    if status != "present" or current_fd is None:
        return False
    try:
        same_identity = _stat_identity(os.fstat(original_fd)) == _stat_identity(
            os.fstat(current_fd)
        )
        current_names = _capped_directory_names(original_fd)
        same_names = current_names is not None and current_names == original_names
        return same_identity and same_names
    except OSError:
        return False
    finally:
        os.close(current_fd)


def _binding_evaluation_read_flags(
    result: InconsistentReviewBindingResult,
) -> tuple[bool, bool]:
    if result.status == "current":
        return True, True
    target_read_reasons = {
        "inconsistent_review_target_unavailable",
        "inconsistent_review_target_changed",
        "inconsistent_review_receipt_store_unavailable",
        "inconsistent_review_receipt_store_changed",
        "inconsistent_review_state_changed_during_binding",
        "inconsistent_review_target_changed_during_binding",
        "inconsistent_review_receipt_store_changed_during_binding",
    }
    store_read_reasons = {
        "inconsistent_review_receipt_store_unavailable",
        "inconsistent_review_receipt_store_changed",
        "inconsistent_review_state_changed_during_binding",
        "inconsistent_review_target_changed_during_binding",
        "inconsistent_review_receipt_store_changed_during_binding",
    }
    return result.reason_code in target_read_reasons, result.reason_code in store_read_reasons


def evaluate_current_inconsistent_review_orphan(
    *,
    project_root: Path,
    storage_root: Path,
    state: Mapping[str, Any] | None,
    state_text: str | None,
) -> InconsistentReviewOrphanResult:
    """Diagnose a bounded, exact D5 review orphan without mutating the workspace."""

    binding_result = evaluate_current_inconsistent_review_binding(
        project_root=project_root,
        storage_root=storage_root,
        state=state,
        state_text=state_text,
    )
    target_read, store_read = _binding_evaluation_read_flags(binding_result)
    if binding_result.status != "current":
        return InconsistentReviewOrphanResult(
            "binding_or_material_changed_or_ambiguous",
            binding_result.reason_code,
            target_read_performed=target_read,
            receipt_store_snapshot_performed=store_read,
        )

    binding_digest = binding_result.binding_digest
    assert binding_digest is not None
    proposal_parts = ("companion", "recovery", "proposals")
    review_parts = ("companion", "recovery", "review_log")
    proposal_status, proposal_fd = _open_directory_chain_nofollow(
        storage_root,
        proposal_parts,
    )
    review_status, review_fd = _open_directory_chain_nofollow(
        storage_root,
        review_parts,
    )
    try:
        if proposal_status == "unsafe" or review_status == "unsafe":
            return InconsistentReviewOrphanResult(
                "binding_or_material_changed_or_ambiguous",
                "inconsistent_review_material_directory_unsafe",
                target_read_performed=True,
                receipt_store_snapshot_performed=True,
                material_scan_performed=True,
            )
        proposal_directory = (
            _bounded_canonical_directory_entries(
                proposal_fd,
                pattern=_CANONICAL_RECOVERY_PROPOSAL_FILE_RE,
            )
            if proposal_fd is not None
            else ((), ())
        )
        review_directory = (
            _bounded_canonical_directory_entries(
                review_fd,
                pattern=_CANONICAL_RECOVERY_REVIEW_FILE_RE,
            )
            if review_fd is not None
            else ((), ())
        )
        if proposal_directory is None or review_directory is None:
            return InconsistentReviewOrphanResult(
                "binding_or_material_changed_or_ambiguous",
                "inconsistent_review_material_directory_changed_or_unbounded",
                target_read_performed=True,
                receipt_store_snapshot_performed=True,
                material_scan_performed=True,
            )
        proposal_all_names, proposal_names = proposal_directory
        review_all_names, review_names = review_directory
        proposal_snapshots: dict[str, str] = {}
        review_snapshots: dict[str, str] = {}
        current_proposals: list[str] = []
        current_reviews: list[str] = []
        current_material_invalid = False
        total_bytes = 0

        for name in proposal_names:
            snapshot = _stable_material_snapshot_at(proposal_fd, name)
            if snapshot is None:
                current_material_invalid = True
                break
            text, digest, byte_count = snapshot
            total_bytes += byte_count
            if total_bytes > _MAX_RECOVERY_MATERIAL_SCAN_BYTES:
                current_material_invalid = True
                break
            proposal_snapshots[name] = digest
            try:
                material = validate_inconsistent_recovery_proposal_text(text)
            except InconsistentReviewContractError:
                if _contains_d5_signal(text):
                    current_material_invalid = True
                continue
            if isinstance(material, dict):
                if material.get("inconsistent_review_binding_digest") == binding_digest:
                    if not text.endswith("\n") or text.endswith("\n\n"):
                        current_material_invalid = True
                    current_proposals.append(name)

        for name in review_names:
            snapshot = _stable_material_snapshot_at(review_fd, name)
            if snapshot is None:
                current_material_invalid = True
                break
            text, digest, byte_count = snapshot
            total_bytes += byte_count
            if total_bytes > _MAX_RECOVERY_MATERIAL_SCAN_BYTES:
                current_material_invalid = True
                break
            review_snapshots[name] = digest
            try:
                material = validate_inconsistent_recovery_review_text(text)
            except InconsistentReviewContractError:
                if _contains_d5_signal(text):
                    current_material_invalid = True
                continue
            if isinstance(material, dict):
                if material.get("inconsistent_review_binding_digest") == binding_digest:
                    review_action = classify_review_action(
                        extract_structured_sections(text, REVIEW_SECTION_ALIASES)
                    )
                    if (
                        not text.endswith("\n")
                        or text.endswith("\n\n")
                        or material.get("decision") != "accept"
                        or material.get("accept_current_target_as_reviewed_baseline") is not True
                        or review_action != "accept"
                    ):
                        current_material_invalid = True
                    current_reviews.append(name)

        if (
            current_material_invalid
            or len(current_proposals) > 1
            or len(current_reviews) > 1
        ):
            classification = "binding_or_material_changed_or_ambiguous"
            classification_reason = "inconsistent_review_material_changed_or_ambiguous"
        elif len(current_reviews) == 1 and len(current_proposals) != 1:
            classification = "binding_or_material_changed_or_ambiguous"
            classification_reason = "inconsistent_review_orphan_proposal_missing_or_changed"
        elif len(current_proposals) == 1:
            expected_review_name = f"{Path(current_proposals[0]).stem}.review.md"
            if len(current_reviews) == 1 and current_reviews[0] == expected_review_name:
                classification = "exact_orphan"
                classification_reason = "inconsistent_review_exact_orphan_current"
            elif expected_review_name in review_all_names:
                classification = "binding_or_material_changed_or_ambiguous"
                classification_reason = "inconsistent_review_orphan_review_changed"
            elif current_reviews:
                classification = "binding_or_material_changed_or_ambiguous"
                classification_reason = "inconsistent_review_orphan_pair_ambiguous"
            else:
                classification = "current_no_orphan"
                classification_reason = "inconsistent_review_no_exact_orphan"
        else:
            classification = "current_no_orphan"
            classification_reason = "inconsistent_review_no_exact_orphan"

        if (
            not _material_snapshots_unchanged(proposal_fd, proposal_snapshots)
            or not _material_snapshots_unchanged(review_fd, review_snapshots)
            or not _directory_snapshot_unchanged(
                storage_root=storage_root,
                parts=proposal_parts,
                original_fd=proposal_fd,
                original_names=proposal_all_names,
            )
            or not _directory_snapshot_unchanged(
                storage_root=storage_root,
                parts=review_parts,
                original_fd=review_fd,
                original_names=review_all_names,
            )
        ):
            classification = "binding_or_material_changed_or_ambiguous"
            classification_reason = "inconsistent_review_material_changed_during_scan"

        final_binding = evaluate_current_inconsistent_review_binding(
            project_root=project_root,
            storage_root=storage_root,
            state=state,
            state_text=state_text,
        )
        if (
            final_binding.status != "current"
            or final_binding.binding_digest != binding_digest
        ):
            return InconsistentReviewOrphanResult(
                "binding_or_material_changed_or_ambiguous",
                "inconsistent_review_binding_changed_during_material_scan",
                target_read_performed=True,
                receipt_store_snapshot_performed=True,
                material_scan_performed=True,
            )
        if classification == "binding_or_material_changed_or_ambiguous":
            return InconsistentReviewOrphanResult(
                classification,
                classification_reason,
                target_read_performed=True,
                receipt_store_snapshot_performed=True,
                material_scan_performed=True,
            )
        return InconsistentReviewOrphanResult(
            classification,
            classification_reason,
            binding=dict(final_binding.binding or {}),
            binding_digest=binding_digest,
            target_read_performed=True,
            receipt_store_snapshot_performed=True,
            material_scan_performed=True,
        )
    finally:
        if proposal_fd is not None:
            os.close(proposal_fd)
        if review_fd is not None:
            os.close(review_fd)


def evaluate_review_imported_baseline_materials(
    *,
    project_root: Path,
    storage_root: Path,
    state: Mapping[str, Any] | None,
    state_text: str | None,
) -> ReviewImportedBaselineMaterialResult:
    """Verify the exact D5 material committed by a reviewed-baseline transition.

    This is the non-receipt-backed half of the D5 provenance transition. It
    verifies the state metadata and exact proposal/review bytes without
    reclassifying the historical helper receipt as current-state evidence.
    """

    storage_root = Path(storage_root)
    if not isinstance(state, Mapping) or not isinstance(state_text, str):
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_state_unavailable"
        )
    state_snapshot = _stable_utf8_text_snapshot(storage_root / FILE_KEYS["state"])
    if state_snapshot is None or state_snapshot[0] != state_text:
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_state_changed"
        )
    metadata = state.get("provenance")
    if not isinstance(metadata, Mapping) or metadata.get("state_label") != (
        "review_imported_baseline"
    ):
        return ReviewImportedBaselineMaterialResult(
            "not_applicable", "review_imported_baseline_not_current"
        )
    if metadata.get("source_state_label") != "inconsistent_or_tampered_evidence":
        return ReviewImportedBaselineMaterialResult(
            "not_applicable", "review_imported_baseline_not_d5"
        )

    binding_digest = metadata.get("inconsistent_review_binding_digest")
    raw_binding = metadata.get("inconsistent_review_binding")
    proposal_digest = metadata.get("proposal_digest")
    review_digest = metadata.get("review_digest")
    if (
        metadata.get("schema_version") != PROVENANCE_METADATA_SCHEMA_VERSION
        or metadata.get("baseline_kind") != "review_import"
        or metadata.get("review_action") != "accept"
        or metadata.get("receipt_backed") is not False
        or metadata.get("source_reason_code") not in _FAILURE_REASONS
        or not is_sha256_digest(binding_digest)
        or not (
            isinstance(proposal_digest, str)
            and len(proposal_digest) == 64
            and all(character in "0123456789abcdef" for character in proposal_digest)
        )
        or not (
            isinstance(review_digest, str)
            and len(review_digest) == 64
            and all(character in "0123456789abcdef" for character in review_digest)
        )
    ):
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_metadata_invalid"
        )
    try:
        binding = validate_inconsistent_review_binding(raw_binding)
    except InconsistentReviewContractError:
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_binding_invalid"
        )
    if inconsistent_review_binding_digest(binding) != binding_digest:
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_binding_digest_mismatch"
        )
    result_workspace_revision = binding.get("result_workspace_revision")
    if (
        not _is_json_int(result_workspace_revision)
        or state.get("workspace_revision") != result_workspace_revision + 1
    ):
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_revision_mismatch"
        )
    if binding.get("operation_class") == "daily_log_append":
        daily_logs = state.get("daily_logs")
        if not (
            isinstance(daily_logs, Mapping)
            and daily_logs.get("latest_file")
            == DAILY_LOG_PATH_PATTERN.format(date=binding.get("target_date"))
            and daily_logs.get("latest_entry_seq") == binding.get("result_file_revision")
        ):
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_target_revision_mismatch"
            )
    else:
        files = state.get("files")
        file_state = (
            files.get(binding.get("target_file_key"))
            if isinstance(files, Mapping)
            else None
        )
        if not (
            isinstance(file_state, Mapping)
            and file_state.get("file_revision") == binding.get("result_file_revision")
        ):
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_target_revision_mismatch"
            )

    def binding_snapshot_is_current() -> bool:
        target_digest = _stable_utf8_text_digest(
            _state_target_path(storage_root, binding)
        )
        if target_digest != binding.get("current_target_digest"):
            return False
        try:
            store_snapshot = capture_receipt_store_snapshot(
                storage_root=storage_root,
                project_root=project_root,
            )
        except ReceiptStoreError:
            return False
        return (
            store_snapshot.status == binding.get("receipt_store_snapshot_status")
            and store_snapshot.revision == binding.get("current_receipt_store_revision")
            and store_snapshot.digest == binding.get("current_receipt_store_digest")
            and store_snapshot.contract_identity
            == binding.get("current_receipt_store_contract_identity")
        )

    if not binding_snapshot_is_current():
        return ReviewImportedBaselineMaterialResult(
            "invalid", "review_imported_baseline_binding_snapshot_changed"
        )

    expected_proposal_digest = "sha256:" + proposal_digest
    expected_review_digest = "sha256:" + review_digest
    proposal_parts = ("companion", "recovery", "proposals")
    review_parts = ("companion", "recovery", "review_log")
    proposal_status, proposal_fd = _open_directory_chain_nofollow(
        storage_root, proposal_parts
    )
    review_status, review_fd = _open_directory_chain_nofollow(
        storage_root, review_parts
    )
    try:
        if (
            proposal_status != "present"
            or review_status != "present"
            or proposal_fd is None
            or review_fd is None
        ):
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_material_directory_unavailable"
            )
        proposal_directory = _bounded_canonical_directory_entries(
            proposal_fd,
            pattern=_CANONICAL_RECOVERY_PROPOSAL_FILE_RE,
        )
        review_directory = _bounded_canonical_directory_entries(
            review_fd,
            pattern=_CANONICAL_RECOVERY_REVIEW_FILE_RE,
        )
        if proposal_directory is None or review_directory is None:
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_material_directory_unsafe"
            )
        proposal_all_names, proposal_names = proposal_directory
        review_all_names, review_names = review_directory
        proposal_snapshots: dict[str, str] = {}
        review_snapshots: dict[str, str] = {}
        matching_proposals: list[str] = []
        matching_reviews: list[str] = []
        total_bytes = 0

        for name in proposal_names:
            snapshot = _stable_material_snapshot_at(proposal_fd, name)
            if snapshot is None:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_proposal_unreadable"
                )
            text, digest, byte_count = snapshot
            total_bytes += byte_count
            if total_bytes > _MAX_RECOVERY_MATERIAL_SCAN_BYTES:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_material_scan_unbounded"
                )
            proposal_snapshots[name] = digest
            if digest != expected_proposal_digest:
                continue
            if staged_d5_proposal_digest_from_filename(name) != digest:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_proposal_filename_digest_mismatch"
                )
            try:
                material = validate_inconsistent_recovery_proposal_text(
                    text,
                    expected_binding_digest=binding_digest,
                )
            except InconsistentReviewContractError:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_proposal_material_invalid"
                )
            if not isinstance(material, dict) or not text.endswith("\n") or text.endswith(
                "\n\n"
            ):
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_proposal_material_invalid"
                )
            matching_proposals.append(name)

        for name in review_names:
            snapshot = _stable_material_snapshot_at(review_fd, name)
            if snapshot is None:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_review_unreadable"
                )
            text, digest, byte_count = snapshot
            total_bytes += byte_count
            if total_bytes > _MAX_RECOVERY_MATERIAL_SCAN_BYTES:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_material_scan_unbounded"
                )
            review_snapshots[name] = digest
            if digest != expected_review_digest:
                continue
            try:
                material = validate_inconsistent_recovery_review_text(
                    text,
                    expected_binding_digest=binding_digest,
                )
            except InconsistentReviewContractError:
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_review_material_invalid"
                )
            review_action = classify_review_action(
                extract_structured_sections(text, REVIEW_SECTION_ALIASES)
            )
            if (
                not isinstance(material, dict)
                or material.get("decision") != "accept"
                or material.get("accept_current_target_as_reviewed_baseline") is not True
                or review_action != "accept"
                or not text.endswith("\n")
                or text.endswith("\n\n")
            ):
                return ReviewImportedBaselineMaterialResult(
                    "invalid", "review_imported_baseline_review_material_invalid"
                )
            matching_reviews.append(name)

        if len(matching_proposals) != 1 or len(matching_reviews) != 1:
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_material_missing_or_ambiguous"
            )
        expected_review_name = f"{Path(matching_proposals[0]).stem}.review.md"
        if matching_reviews[0] != expected_review_name:
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_material_pair_mismatch"
            )
        if (
            not _material_snapshots_unchanged(proposal_fd, proposal_snapshots)
            or not _material_snapshots_unchanged(review_fd, review_snapshots)
            or not _directory_snapshot_unchanged(
                storage_root=storage_root,
                parts=proposal_parts,
                original_fd=proposal_fd,
                original_names=proposal_all_names,
            )
            or not _directory_snapshot_unchanged(
                storage_root=storage_root,
                parts=review_parts,
                original_fd=review_fd,
                original_names=review_all_names,
            )
        ):
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_material_changed_during_scan"
            )
        final_state_snapshot = _stable_utf8_text_snapshot(
            storage_root / FILE_KEYS["state"]
        )
        if (
            final_state_snapshot is None
            or final_state_snapshot[0] != state_text
            or not binding_snapshot_is_current()
        ):
            return ReviewImportedBaselineMaterialResult(
                "invalid", "review_imported_baseline_binding_changed_during_scan"
            )
        return ReviewImportedBaselineMaterialResult(
            "verified",
            "review_imported_baseline_material_verified",
            binding_digest=binding_digest,
            proposal_digest=expected_proposal_digest,
            review_digest=expected_review_digest,
        )
    finally:
        if proposal_fd is not None:
            os.close(proposal_fd)
        if review_fd is not None:
            os.close(review_fd)
