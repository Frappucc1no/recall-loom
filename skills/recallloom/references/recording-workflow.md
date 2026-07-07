# Recording Workflow

`record --plan` is the side-effect-free recording fast lane.

It classifies a recording intent, returns an ordered helper path, and points to the current safe next step or a stop/ask reason. It does not write sidecar files, daily logs, receipts, locks, derived evidence, or revisions.

## Command Shape

Append-only progress:

```bash
recallloom.py record <project> --plan \
  --intent-text "record this progress" \
  --payload-json '{"entry_kind":"milestone","payload_ref":"redacted"}' \
  --layer-hint daily-log \
  --privacy-safety-json '{"classification":"safe"}' \
  --json
```

Compact output:

```bash
recallloom.py record <project> --plan \
  --intent-text "record this progress" \
  --payload-json '{"entry_kind":"milestone","payload_ref":"redacted"}' \
  --layer-hint daily-log \
  --privacy-safety-json '{"classification":"safe"}' \
  --compact --json
```

## Input Contract

`record --plan` accepts these input fields through CLI options:

- `intent_text`: original user or agent intent.
- `prepared_record_payload`: public-safe structured payload or placeholder reference.
- `optional_layer_hint`: one of `daily_log`, `current_state`, `stable_context`, `protocol_rule`, `metadata`.
- `privacy_safety_result`: public-safe object such as `{"classification":"safe"}` or `{"classification":"unsafe"}`.
- `expected_revision_binding`: optional revision binding object; preflight binding is attached by the dispatcher.
- `semantic_unchanged_assertion`: required by metadata-only summary refresh flows that reuse the current managed body.

Do not put raw private payloads, shell transcripts, absolute local paths, reserved RecallLoom markers, manual patches, or direct `state.json` / `config.json` edits into the payload or retry template.

## Prepared Input Templates

These templates show valid prepared shapes. Replace placeholder text with public-safe project content only, then run the helper gates returned by `record --plan` and preflight.

Append daily-log JSON:

```bash
recallloom.py append <project> \
  --entry-json '{"work_completed":["public-safe milestone"],"confirmed_facts":["public-safe fact"],"key_decisions":[],"risks_blockers":[],"recommended_next_step":["next safe step"]}' \
  --input-format json \
  --json
```

Append daily-log markdown:

```markdown
<!-- section: work_completed -->
- public-safe milestone

<!-- section: confirmed_facts -->
- public-safe fact

<!-- section: key_decisions -->
- None.

<!-- section: risks_blockers -->
- None.

<!-- section: recommended_next_step -->
- next safe step
```

Current-state JSON:

```bash
recallloom.py write <project> \
  --type current-state \
  --stdin \
  --input-format json \
  --json
```

```json
{
  "current_state": "public-safe current state",
  "active_judgments": [],
  "risks_open_questions": [],
  "next_step": "next safe step",
  "recent_pivots": []
}
```

Record-plan input:

```json
{
  "intent_text": "record public-safe progress",
  "prepared_record_payload": {
    "entry_kind": "milestone",
    "payload_ref": "redacted"
  },
  "optional_layer_hint": "daily_log",
  "privacy_safety_result": {
    "classification": "safe"
  }
}
```

Failure retry template:

```json
{
  "first_step": "rerun_preflight_or_status",
  "project_ref": "same_project",
  "input_ref": "same_public_safe_prepared_input",
  "do_not_reuse": [
    "stale_revision_binding",
    "free_text_semantic_confirmation",
    "raw_private_payload"
  ]
}
```

## Metadata-Only Refresh Assertion

Use metadata-only refresh only after preflight allows `post_append_summary_sync` and returns an assertion binding seed. The assertion JSON is bound data, not a free-text confirmation:

```json
{
  "semantic_unchanged": true,
  "assertion_source_kind": "record_plan_output_id",
  "assertion_source_id": "record-plan-output:sha256:<64hex>",
  "input_digest": "sha256:<64hex>",
  "record_plan_output": {
    "record_plan_output_id": "record-plan-output:sha256:<64hex>",
    "input_digest": "sha256:<64hex>",
    "...": "complete record --plan JSON output"
  },
  "assertion_binding_seed": {
    "assertion_kind": "post_append_summary_metadata_only_semantic_unchanged",
    "expected_workspace_revision": 0,
    "expected_file_revision": 0,
    "summary_base_workspace_revision": 0,
    "latest_daily_log_entry_id": "entry-1",
    "latest_daily_log_entry_seq": 1,
    "latest_daily_log_entry_created_at": "YYYY-MM-DDTHH:MM:SSZ",
    "latest_daily_log_entry_date": "YYYY-MM-DD",
    "latest_daily_log_entry_hash": "sha256:<64hex>",
    "latest_daily_log_digest": "sha256:<64hex>",
    "body_digest_before": "sha256:<64hex>"
  },
  "assertion_binding_seed_digest": "sha256:<64hex>"
}
```

`source_id` is not accepted. `record_plan_output_id` assertions must carry the complete `record_plan_output` so the helper can recompute its id, input digest, and preflight binding before any metadata-only refresh. `explicit_operator_confirmation` is also allowed only when it carries the same bound fields and uses an `assertion_source_id` shaped like `explicit-operator-confirmation:sha256:<64hex>`. Do not use a bare `semantic_unchanged: true` boolean or free-text confirmation.

## Output Meaning

Important fields:

- `workflow_status`: `ready_to_run`, `needs_user_confirmation`, `blocked_fixable`, `blocked_unsafe`, `no_write`, or `complete`.
- `record_class`: `append_only`, `current_state_update`, `stable_context_update`, `protocol_rule_update`, `simple_multi_layer_plan`, `metadata_refresh_only`, `amend_last`, `duplicate_noop`, `defer_no_write`, `no_write_success`, `ambiguous_needs_user`, or `unsafe_blocked`.
- `current_safe_command`: one helper command to run now, or `null` when the plan must ask/stop/no-op.
- `ordered_executable_path`: the reviewed order for helper commands; multi-layer plans may include a path while keeping `current_safe_command` null until the user confirms.
- `side_effect`: always `none` for `record --plan`.
- `record_plan_output_id` and `input_digest`: stable digests for downstream validation.

## Retry Rule

If `current_safe_command` is null:

- `needs_user_confirmation`: ask one clear question before writing.
- `blocked_unsafe`: stop and remove/redact unsafe input.
- `no_write` or `complete`: do not write; report the reason.
- `blocked_fixable`: retry only with the public-safe template returned by the helper.
