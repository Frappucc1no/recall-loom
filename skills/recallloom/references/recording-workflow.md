# Recording Workflow

`record --plan` is the side-effect-free recording fast lane.

It classifies a recording intent, returns an ordered helper path, and points to the current safe next step or a stop/ask reason. It does not write sidecar files, daily logs, receipts, locks, derived evidence, or revisions.

`record --suggest` is the side-effect-free proactive prompt lane.

It reviews public-safe candidate text and returns one of three outcomes:

- `candidate`: show a sanitized candidate summary and suggested `record --plan` path.
- `silent`: do not prompt because the text is exploratory, unstable, or explicitly says not to record.
- `blocked`: do not suggest a write path because the candidate looks sensitive or private.

It does not run in the background, watch files, auto-listen, write sidecar files, or approve a later record action.

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

Proactive suggestion:

```bash
recallloom.py record <project> --suggest \
  --intent-text "completed a public-safe milestone" \
  --privacy-safety-json '{"classification":"safe"}' \
  --json
```

## Input Contract

`record --plan` and `record --suggest` accept these input fields through CLI options:

- `intent_text`: original user or agent intent.
- `prepared_record_payload`: public-safe structured payload or placeholder reference.
- `optional_layer_hint`: one of `daily_log`, `current_state`, `stable_context`, `protocol_rule`, `metadata`.
- `privacy_safety_result`: public-safe object such as `{"classification":"safe"}` or `{"classification":"unsafe"}`.
- `expected_revision_binding`: optional revision binding object; preflight binding is attached by the dispatcher.
- `semantic_unchanged_assertion`: required by metadata-only summary refresh flows that reuse the current managed body.

Do not put raw private payloads, shell transcripts, absolute local paths, reserved RecallLoom markers, manual patches, or direct `state.json` / `config.json` edits into the payload or retry template.

## Prepared Input Templates

These templates show valid prepared shapes. Replace placeholder text with public-safe project content only, then use the dispatcher so it can run a fresh preflight and issue the revision binding before any mutation.

Append daily-log JSON:

```bash
recallloom.py append <project> \
  --entry-json '{"work_completed":["public-safe milestone"],"confirmed_facts":["public-safe fact"],"key_decisions":["none"],"risks_blockers":["none"],"recommended_next_step":["next safe step"]}' \
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
  --source-file <prepared-current-state.json> \
  --input-format json \
  --dry-run \
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

The JSON block is the complete UTF-8 content of
`<prepared-current-state.json>`. Remove `--dry-run` only after the dispatcher
has returned a fresh preflight result that authorizes the write.

## Dispatcher Internal Helper Boundary

For normal operations, use only the applicable dispatcher surface. Managed-file
writes and daily-log entries use `write` and `append`;
`sync-current-state-after-append` is used only when its post-append contract
requires that lane. `commit_context_file.py` and `append_daily_log_entry.py` are
internal dispatcher and integration surfaces, not standalone operator paths.
The dispatcher runs a fresh preflight, constructs the binding, and writes the
matching lease immediately before calling a helper. A read-only
`preflight_context_check.py <project> --json` result does not issue either
material. There is no independent operator binding/lease pickup interface: a
hand-invoked helper without the still-current dispatcher-issued material is
expected to fail. Never reconstruct, copy from preflight output, reuse, or
amend that material.

After a D5 promotion produces `review_imported_baseline`, use dispatcher
`write` or `append` with its existing `--confirm-review-imported-baseline` flag
for the first write. The dispatcher then issues the
confirmation-bound binding and matching lease. The internal helpers do not accept
that flag; the internal helpers only verify and consume the dispatcher-issued binding and lease
inside the dispatcher/integration path.
The existing `sync-current-state-after-append` lane also accepts the confirmation
when its own post-append contract requires it; it still delegates through the
same internal binding/lease boundary.

The three dispatcher mutation commands (`append`, `write`, and
`sync-current-state-after-append`) also accept `--compact-json` for the bounded
`recallloom.transaction.compact/1.0` result. It is mutually exclusive with
legacy `--json`, preserves the exit code, contains exactly one safe next action,
and must remain below 2048 UTF-8 bytes.
If the binding is omitted, stale, malformed, missing its lease or confirmation,
or has a mismatched hash, stop and return the helper failure contract rather
than attempting a write.

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

Before `record --plan` may expose the metadata-refresh helper path, the plan input must explicitly include `--semantic-unchanged`. If that decision is omitted, the plan remains confirmation-only with no current safe command. If meaning changed, use `--semantic-changed` and route to a separately reviewed current-state update instead.

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

For `record --suggest`:

- `suggestion_status`: `candidate`, `silent`, or `blocked`.
- `should_prompt`: `true` only when the user should see a sanitized candidate recording prompt.
- `candidate_summary`: sanitized text only; sensitive candidates use a redacted digest reference instead.
- `suggested_path`: a suggested `record --plan` path only, never a write command.
- `side_effect`: always `none`.

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
