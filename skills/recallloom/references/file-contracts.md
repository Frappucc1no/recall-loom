# File Contracts

## Contents

- `STORAGE_ROOT/config.json`
- `STORAGE_ROOT/state.json`
- `STORAGE_ROOT/context_brief.md`
- `STORAGE_ROOT/rolling_summary.md`
- `STORAGE_ROOT/daily_logs/YYYY-MM-DD.md`
- `STORAGE_ROOT/update_protocol.md`
- `STORAGE_ROOT/companion/`
- Helper JSON surfaces

## `STORAGE_ROOT/config.json`

Role:

- machine-readable workspace settings

Expected fields:

- `protocol_version`
- `storage_mode`
- `workspace_language`
- `created_by`
- `created_at`

## `STORAGE_ROOT/state.json`

Role:

- machine-readable sidecar state for concurrency-aware helper behavior

Expected fields:

- `workspace_revision`
- `update_protocol_revision`
- `git_exclude_mode`
- `bridged_entries`
- `files`
- `daily_logs`

This file is maintained by helper scripts and supports:

- revision-aware checks
- bridge state
- git exclude state
- daily log state

Protocol `1.0` scope note:

- helper-only routing fields such as `sidecar_trust_state`
- `provenance_state`
- `write_readiness`
- `expected_revisions`
- `preflight_contract_identity`
- `continuity_drift_risk_level`
- `allowed_operation_level`

are helper/dispatcher JSON routing surfaces, not persisted write authority.
`state.json.provenance` may store local provenance markers such as `structurally_valid`, `review_imported_baseline`, or `helper_evidenced` after a receipt-finalized helper write.
Legacy sidecars without this metadata remain readable, but mutating helper writes must first pass review / repair import.
`helper_evidenced` is reserved for a successfully finalized helper receipt; structural validation or review import alone must remain `structurally_valid`, `review_imported_baseline`, or another non-receipt-backed provenance state.

Package support fields such as `package_support_state`, advisory source, and daily support-cache status are also not persisted into `state.json`.
They belong to the installed package runtime and user-level cache, not to the project sidecar.

Expected `files` entries:

- `files.context_brief`
- `files.rolling_summary`
- `files.update_protocol`

Each file entry must contain:

- `file_revision`
- `updated_at`
- `writer_id`
- `base_workspace_revision`

Expected `daily_logs` fields:

- `latest_file`
- `latest_entry_id`
- `latest_entry_seq`
- `entry_count`
- `updated_at` (optional helper-maintained timestamp)

Expected `bridged_entries` value shape:

- each key is a supported root entry file path
- each value may include:
  - `update_protocol_revision_seen`
  - `latest_daily_log_seen`
  - `updated_at`

`daily_logs` state semantics:

- these fields describe the latest **active** ISO-dated daily log under `STORAGE_ROOT/daily_logs/`
- archived files under `STORAGE_ROOT/daily_logs/archive/` are excluded from this cursor
- for the current package line on protocol `1.0`, `latest_entry_seq` is the file-local sequence number of the latest entry marker in that latest active daily log
- `entry_count` remains the field name for protocol `1.0`; it means the number of entry markers in the latest active daily log file, not a cross-file global cumulative count
- helper-generated canonical `latest_entry_id` mirrors the latest entry marker id, where the canonical marker id is `entry-{entry_seq}` for that file-local sequence
- archiving older logs without changing the latest active daily-log cursor should not by itself advance `workspace_revision`
- when no active daily log exists, helpers must write:
  - `latest_file = null`
  - `latest_entry_id = null`
  - `latest_entry_seq = 0`
  - `entry_count = 0`

Allowed `storage_mode` values:

- `hidden`
- `visible`

Meaning:

- `hidden` means the storage root is `PROJECT_ROOT/.recallloom/`
- `visible` means the storage root is `PROJECT_ROOT/recallloom/`

Contract rule:

- exactly one valid storage root may exist for a project at a time
- if both hidden and visible sidecars exist with `config.json`, the workspace is in an invalid conflict state
- `storage_mode` must match the physical storage root that contains this config file
- the storage root is reserved for RecallLoom-managed assets only
- helper-visible managed assets should be resolved from the package-level managed asset registry
  `recallloom/managed-assets.json`

This file should remain stable unless the workspace configuration itself changes.

Invalid states include:

- malformed JSON
- missing required fields
- unsupported `protocol_version`
- unsupported `storage_mode`
- unsupported `workspace_language`

Language consistency rule:

- all managed file markers in the storage root must use the same language value as `workspace_language`

Current supported `workspace_language` values for protocol `1.0`:

- `en`
- `zh-CN`

Managed asset registry note:

- protocol `1.0` core semantics remain defined by this document
- the current package line additionally ships `recallloom/managed-assets.json`
  as the single declaration source for:
  - required managed files
  - optional managed files
  - required managed directories
  - allowed managed directories
  - allowed dynamic file patterns
  - optional managed namespaces
- helpers should consume that registry instead of maintaining repeated hard-coded path allowlists

<!-- RecallLoom metadata sync start: file-contract-registry-summary -->
Generated from `references/contract-registry.json`.

### `config`

- path: `config.json`
- required sections:
  - none
- optional sections:
  - none
- render order:
  - none

### `state`

- path: `state.json`
- required sections:
  - none
- optional sections:
  - none
- render order:
  - none

### `context_brief`

- path: `context_brief.md`
- required sections:
  - `mission`
  - `current_phase`
  - `source_of_truth`
  - `core_workflow`
  - `boundaries`
- optional sections:
  - `audience_stakeholders`
  - `scope`
- render order:
  - `mission`
  - `audience_stakeholders`
  - `current_phase`
  - `scope`
  - `source_of_truth`
  - `core_workflow`
  - `boundaries`

### `rolling_summary`

- path: `rolling_summary.md`
- required sections:
  - `current_state`
  - `active_judgments`
  - `risks_open_questions`
  - `next_step`
  - `recent_pivots`
- optional sections:
  - none
- render order:
  - `current_state`
  - `active_judgments`
  - `risks_open_questions`
  - `next_step`
  - `recent_pivots`

### `daily_log`

- path: `daily_logs/{date}.md`
- required sections:
  - `work_completed`
  - `confirmed_facts`
  - `key_decisions`
  - `risks_blockers`
  - `recommended_next_step`
- optional sections:
  - none
- render order:
  - `work_completed`
  - `confirmed_facts`
  - `key_decisions`
  - `risks_blockers`
  - `recommended_next_step`

### `update_protocol`

- path: `update_protocol.md`
- required sections:
  - `project_specific_overrides`
- optional sections:
  - none
- render order:
  - `project_specific_overrides`
<!-- RecallLoom metadata sync end: file-contract-registry-summary -->

Version consistency rule:

- `protocol_version` must use a supported RecallLoom protocol version
- protocol `1.0` uses dotted string form such as `1.0`

## Helper JSON Surfaces

Role:

- structured JSON guidance emitted by helpers and the dispatcher
- not persisted into `STORAGE_ROOT`
- versioned independently from the sidecar file protocol

Current contract distinction:

- sidecar `protocol_version` remains `1.0`
- helper output `schema_version` may be `1.1`

Compatibility rules:

- `schema_version: "1.1"` is a helper output contract, not a sidecar protocol
  upgrade
- new helper fields are additive and optional unless this section explicitly
  marks them required for a specific surface
- existing consumers may ignore unknown helper-output fields
- existing required JSON fields on documented stable surfaces must not be
  removed or renamed within the current helper schema `1.x` line

Field semantics for helper schema `1.1`:

- `schema_version`
  - required when a payload opts into helper schema `1.1`
  - dotted string naming the helper output contract line
  - current value is `1.1`
- `next_actions`
  - ordered array of stable machine-readable action tokens
  - expresses the preferred next moves from most immediate to less immediate
  - tokens are contract identifiers, not localized display strings
- `suggestion`
  - short localized human-readable guidance string
  - summarizes the preferred recovery or follow-up action
  - complements `next_actions`; it does not rename those tokens
- `recovery_command`
  - single preferred actionable recovery instruction
  - use a literal command string when a deterministic rerun path exists
  - otherwise use a concise imperative instruction instead of fabricating a
    fake shell command
- `read_plan`
  - optional structured read recommendation for follow-up continuity loading
  - additive in schema `1.1`; consumers that do not understand it may ignore
    it entirely
- `estimated_tokens`
  - optional integer estimate for the helper-defined read budget
  - when `read_plan` is present, this top-level value is the default
    `standard` tier budget and must equal
    `read_plan.standard.estimated_tokens`
- `progressive_read_plan`
  - optional structured read plan for bounded `resume --fast` and
    `resume --full` surfaces
  - distinct from the E2 tiered `read_plan` contract used by ambient
    `status --json` and bare `resume --json`
  - consumers that do not understand it may ignore it entirely

`read_plan` contract for E2:

- `read_plan` is an object with at least these named tiers:
  - `minimal`
  - `standard`
  - `comprehensive`
- future helper schemas may add more tiers; existing consumers may ignore
  unknown tier keys

Each required tier must contain:

- `files`
  - ordered list of project-root-relative paths to read next
  - use the actual storage-root prefix under the project root, such as
    `.recallloom/...` or `recallloom/...`
  - do not emit absolute paths, bare storage-root-relative fragments, or
    ambiguous aliases
- `reason`
  - short explanation of why the listed files are sufficient for that tier
  - this is the normative place to say `review-before-write` when the plan is
    only safe after additional human review
- `estimated_tokens`
  - integer estimate for reading the files listed in that tier
  - this is a plan budget hint, not an exact tokenizer promise

Tier intent:

- `minimal`
  - smallest bounded continuity set for orientation
  - centers on current-state files instead of deep history
  - should not expand into extra evidence unless safety or freshness requires
    it
- `standard`
  - default balanced tier for most follow-up work
  - should extend `minimal` with framing and project-local constraints when
    available
- `comprehensive`
  - highest-confidence tier for stale, conflicting, or evidence-heavy cases
  - may add the latest active daily log and other directly relevant managed
    evidence

Review-before-write rule:

- when `update_protocol.md` exists or governs the action, every tier reason
  must explicitly include `review-before-write`, and every tier file list must
  include the project-root-relative `update_protocol.md` path
- when `rolling_summary.md` is stale, every tier reason must explicitly
  include `review-before-write`
- in stale-summary cases, the tier file list must include
  the project-root-relative `state.json` and `rolling_summary.md` paths under
  the resolved storage root, plus any identified newer managed artifact path
  when that path is known
- consumers must treat these markers as read gates, not as automatic write
  approval

Budget relationship to `query_continuity.py`:

- `read_plan.*.estimated_tokens` and the top-level helper `estimated_tokens`
  describe the cost of executing the suggested read plan
- they do not replace, rename, or retroactively reinterpret
  `query_continuity.py` fields `token_estimate` or `budget_hint`
- `query_continuity.py.token_estimate` continues to estimate the size of the
  returned recall text
- `query_continuity.py.budget_hint` remains its coarse answer-size bucket

`progressive_read_plan` contract for F1:

- only emit this field for explicit bounded progressive resume modes such as
  `resume --fast --json` and `resume --full --json`
- do not emit the E2 tiered `read_plan` on those explicit progressive modes
- required fields:
  - `mode`: `fast` or `full`
  - `files`: ordered project-root-relative paths the mode used for its bounded
    read surface
  - `reason`: short explanation of why that bounded surface is appropriate
  - `estimated_tokens`: integer estimate for reading the listed files
  - `bounded`: boolean value that must be `true`

Mode intent:

- `fast`
  - use when the agent only needs current-state orientation and the latest
    summary is enough to decide the next safe move
  - read `state.json` and `rolling_summary.md`
  - do not read daily logs by default
- `full`
  - use when stable project framing, source-of-truth routing, or
    `update_protocol.md` guidance is needed before choosing an action
  - extend the bounded read surface with `context_brief.md` and
    `update_protocol.md`
  - keep daily-log evidence on demand through `query_continuity.py`

## `STORAGE_ROOT/context_brief.md`

Role:

- stable onboarding and framing file

Required file marker:

- `recallloom:file=context_brief`

Required metadata marker:

- a valid file-state marker must appear in the leading metadata block
- the recommended protocol `1.0` writer position is line 2
- helper-generated files currently place it on line 2
- current readers accept it within the first four lines as a compatibility tolerance, not as a relaxed writer contract

Language rule:

- marker `lang` must match `config.json.workspace_language`

Version rule:

- marker `version` must use a supported RecallLoom protocol version
- when `config.json.protocol_version` is supported, marker `version` should match it

Required file-state marker shape:

```text
<!-- file-state: revision=<n> | updated-at=<timestamp> | writer-id=<writer> | base-workspace-revision=<n> -->
```

Operational rule:

- helper commits should refuse stale writes when the prepared file revision or workspace revision no longer matches

Recommended sections:

1. Mission
2. Audience / Stakeholders
3. Current Phase
4. Scope
5. Source of Truth
6. Core Workflow
7. Boundaries

Required section keys:

- `mission`
- `current_phase`
- `source_of_truth`
- `core_workflow`
- `boundaries`

Optional but recommended section keys:

- `audience_stakeholders`
- `scope`

Do not use it for:

- running task logs
- fast-changing details
- detailed milestone history

## `STORAGE_ROOT/companion/`

Role:

- optional managed namespace for companion-layer assets

Current package-line purpose:

- hold recovery proposals and related review records under the sidecar without creating a second root

Contract rule:

- this namespace is optional
- its absence must not make a healthy `1.0` workspace invalid
- files inside it must still be RecallLoom-managed assets, not arbitrary user files
- allowed child directories and dynamic file patterns are declared by
  `recallloom/managed-assets.json`

Current recognized subtree in this package line:

- `STORAGE_ROOT/companion/recovery/`

This subtree is for:

- recovery proposals
- review records
- archived recovery artifacts

It is not for:

- raw transcript dumps kept indefinitely
- unrelated project notes
- replacing `rolling_summary.md`, `daily_logs/`, or `context_brief.md`

## `STORAGE_ROOT/rolling_summary.md`

Role:

- overwrite-style current-state snapshot

Required file marker:

- `recallloom:file=rolling_summary`

Required metadata markers:

- line 2 must be a valid `last-writer` marker
- a valid file-state marker must appear in the leading metadata block
- the recommended protocol `1.0` writer position for the file-state marker is line 3
- helper-generated files currently place it on line 3
- current readers accept it within the first four lines as a compatibility tolerance, not as a relaxed writer contract

Language rule:

- marker `lang` must match `config.json.workspace_language`

Version rule:

- marker `version` must use a supported RecallLoom protocol version
- when `config.json.protocol_version` is supported, marker `version` should match it

Recommended sections:

1. Metadata header
2. Current State
3. Active Judgments
4. Risks / Open Questions
5. Next Step
6. Recent Pivots

Required section keys:

- `current_state`
- `active_judgments`
- `risks_open_questions`
- `next_step`
- `recent_pivots`

Required metadata header:

- line 1 must be the file marker
- line 2 must be a valid last-writer marker

Required last-writer marker shape:

```text
<!-- last-writer: [<tool_name>] | YYYY-MM-DD -->
```

Required file-state marker shape:

```text
<!-- file-state: revision=<n> | updated-at=<timestamp> | writer-id=<writer> | base-workspace-revision=<n> -->
```

Operational rule:

- if `state.json.workspace_revision` moves ahead of this file's `base_workspace_revision`, the summary may need review before it is trusted as current state

Validator rule:

- `rolling_summary.md` is invalid if the second line is missing or does not match the required last-writer marker format

Do not use it for:

- append-only history
- raw exploratory notes
- stale facts known to be false

## `STORAGE_ROOT/daily_logs/YYYY-MM-DD.md`

Role:

- append-only milestone evidence

Filename rule:

- the filename must use a valid ISO date in `YYYY-MM-DD.md` form

Required file marker:

- `recallloom:file=daily_log`

Required entry metadata:

- after the file marker, each milestone entry begins with a `daily-log-entry` marker
- helper-generated files place the first entry marker immediately after the file marker

Optional scaffold form before the first real entry:

- helper initialization may create a scaffold daily log
- scaffold logs use:
  - `<!-- daily-log-scaffold: true -->`
- scaffold logs are not counted as active entries
- scaffold logs keep `state.json.daily_logs` in the null/zero cursor shape until the first real append
- the first real append to a scaffold still becomes `entry-1`

Language rule:

- marker `lang` must match `config.json.workspace_language`

Version rule:

- marker `version` must use a supported RecallLoom protocol version
- when `config.json.protocol_version` is supported, marker `version` should match it

Required daily-log-entry marker shape:

```text
<!-- daily-log-entry: entry-id=<id> | created-at=<timestamp> | writer-id=<writer> | entry-seq=<n> -->
```

Entry unit rule:

- the file may contain multiple milestone entries
- each entry must carry its own `daily-log-entry` marker
- each entry must include the required daily-log sections
- append-only means adding a new entry block, not rewriting older entries in place

Entry sequence and id rule:

- `entry-seq` is a file-local sequence and must be contiguous as `1..N` inside each daily log file
- every daily log's first real append starts at `entry-seq=1`, including after an initialization scaffold is replaced by the first real entry
- helper-generated canonical `entry-id` is `entry-{entry_seq}` and is not a global identifier
- historical daily logs may each contain `entry-1`; duplicate `entry-id` values across different daily log files are normal under protocol `1.0`
- noncanonical ids can still be parsed, but validators should surface them with `noncanonical_daily_log_entry_id` so they are not confused with helper-generated canonical ids

Operational rule:

- helper appends should refuse stale writes when `state.json.workspace_revision` no longer matches the expected workspace revision
- entry sequences should remain contiguous inside the latest daily log
- helper append commands require an explicit target date; in normal use that date should be the latest active ISO-dated daily log suggested by preflight, or a newer ISO date when starting a new active day
- historical appends may be allowed explicitly, but they must not roll back the `state.json.daily_logs` latest-log cursor

Recommended entry shape:

- Work Completed
- Confirmed Facts
- Key Decisions
- Risks / Blockers
- Recommended Next Step

Required section keys:

- `work_completed`
- `confirmed_facts`
- `key_decisions`
- `risks_blockers`
- `recommended_next_step`

Do not use it for:

- every minor incremental update
- routine cold-start activity
- purely trivial edits

## `STORAGE_ROOT/update_protocol.md`

Role:

- recommended project-local override and extension layer

Presence rule:

- this file is generated by default by `init_context.py`
- for protocol `1.0`, it is recommended rather than strictly required
- when it is missing, validator should warn rather than fail the workspace

When this file exists, required file marker:

- `recallloom:file=update_protocol`

When this file exists, required metadata marker:

- a valid file-state marker must appear in the leading metadata block
- the recommended protocol `1.0` writer position is line 2
- helper-generated files currently place it on line 2
- protocol `1.0` readers accept it within the first four lines as a compatibility tolerance, not as a relaxed writer contract

Language rule:

- marker `lang` must match `config.json.workspace_language`

Version rule:

- marker `version` must use a supported RecallLoom protocol version
- when `config.json.protocol_version` is supported, marker `version` should match it

When this file exists, required section keys:

- `project_specific_overrides`

Use it when a project needs:

- custom read order
- custom write rules
- custom archive policy
- stronger local constraints

In protocol `1.0`, this file is a human-reviewed override layer.

That means:

- preflight, archive guidance, and bridge guidance should surface it clearly
- helpers should not silently ignore its existence
- helpers do not automatically execute its natural-language rules

Operational rule:

- cold-start flow should check this file before applying the default read order
- write-planning flow should check this file before choosing the final write set
- in the current package line, `preflight_context_check.py`, `archive_logs.py`, and bridge guidance surface this file for review rather than automatically executing its natural-language rules
- revision-aware write helpers (`commit_context_file.py`, `append_daily_log_entry.py`) do not reread or execute natural-language override prose automatically
- revision-aware helper commits should keep this file's `file-state` marker and `state.json.update_protocol_revision` in sync
