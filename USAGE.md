# Using RecallLoom

RecallLoom is an installable skill package, not a standalone binary.

Start with [README.md](./README.md) or [README.en.md](./README.en.md) for the
public front door. Use [README.zh-CN.md](./README.zh-CN.md) only for
compatibility links. Use [INDEX.md](./INDEX.md) when you need the full map.
This file is the operator guide for the packaged dispatcher, helpers, and
native-wrapper boundary.

## Stable Contract

RecallLoom keeps one stable helper contract across its public entry surface,
the installed skill, and native wrappers:

- natural language is the primary public path
- `rl-init`, `rl-resume`, `rl-status`, and `rl-validate` are stable operator
  action names
- `init`, `resume`, `status`, `validate`, `quick-summary`, `record --plan`,
  `record --suggest`,
  `append`, `write`, `sync-current-state-after-append`,
  `repair-daily-log-cursor`, and `bridge` are dispatcher subcommands
- native wrappers are convenience entrypoints only
- the package does not promise a second logic set in bridge files or wrapper
  text

## First Attach

On first attach:

1. detect whether a valid sidecar already exists
2. if it exists, continue normally
3. if it does not exist, ask before initializing
4. if the user confirms, or says `rl-init`, run the standard initialization
5. if Python `3.10+` is unavailable, report blocked instead of hand-building
   `.recallloom/` or `recallloom/`

`rl-init` is the primary operator-friendly first-attach action name.

## Restore And Resume

Use `rl-resume` for the initialized-project restore checkpoint.
Use `rl-status` when you only need orientation.
Use `resume --fast` when `state.json` plus `rolling_summary.md` are enough.
Use `resume --full` when you need stable framing or `update_protocol.md`
before acting.

Natural-language restore requests remain the primary public path. The host or
router should still check for a valid sidecar before broader fan-out.

## Package Rules

- `STORAGE_ROOT` may be `.recallloom/` or `recallloom/`, but only one valid
  storage root may exist at a time.
- Managed sidecar writes go through the packaged helpers.
- `state.json` and `config.json` are not meant for blind hand edits during
  normal operation.
- `update_protocol.md`, when present, can narrow or strengthen the default
  read/write rules for the project.
- Host-memory inputs stay opt-in and hint-only.

Current package / protocol runtime limits:

<!-- RecallLoom metadata sync start: runtime-requirements -->
- minimum Python version: `3.10`
- supported `workspace_language` values:
  - `en`
  - `zh-CN`
- supported root entry files for thin bridges:
  - `AGENTS.md`
  - `CLAUDE.md`
  - `GEMINI.md`
  - `.github/copilot-instructions.md`
<!-- RecallLoom metadata sync end: runtime-requirements -->

## Dispatcher Surface

The dispatcher command surface includes:

- `init`
- `resume`
- `status`
- `validate`
- `quick-summary`
- `record --plan`
- `record --suggest`
- `append`
- `write`
- `sync-current-state-after-append`
- `repair-daily-log-cursor`
- `bridge`

Typical use:

```bash
python skills/recallloom/scripts/recallloom.py init /absolute/path/to/project
python skills/recallloom/scripts/recallloom.py resume /absolute/path/to/project --fast --json
python skills/recallloom/scripts/recallloom.py status /absolute/path/to/project
python skills/recallloom/scripts/recallloom.py quick-summary /absolute/path/to/project --json
python skills/recallloom/scripts/recallloom.py record /absolute/path/to/project --plan --intent-text "Record this progress." --payload-json '{"work_completed":"<public-safe summary>","confirmed_facts":"<confirmed fact>","key_decisions":"<decision or none>","risks_blockers":"<risk or none>","recommended_next_step":"<next step>"}' --layer-hint daily-log --json
python skills/recallloom/scripts/recallloom.py record /absolute/path/to/project --suggest --intent-text "Completed a durable public-safe milestone." --json
python skills/recallloom/scripts/recallloom.py append /absolute/path/to/project --entry-json '{"work_completed":"Recorded the milestone.","confirmed_facts":"The prepared entry was reviewed before append.","key_decisions":"Keep the entry scoped to current work.","risks_blockers":"None.","recommended_next_step":"Continue from the refreshed summary."}' --json
python skills/recallloom/scripts/recallloom.py write /absolute/path/to/project --type current-state --source-file /absolute/path/to/prepared-current-state.md --dry-run --json
python skills/recallloom/scripts/recallloom.py sync-current-state-after-append /absolute/path/to/project --reuse-current-summary --semantic-unchanged-assertion-json '{"semantic_unchanged":true,"assertion_source_kind":"record_plan_output_id","assertion_source_id":"record-plan-output:sha256:<64-hex>","record_plan_output":{}}' --json
python skills/recallloom/scripts/recallloom.py repair-daily-log-cursor /absolute/path/to/project --json
python skills/recallloom/scripts/recallloom.py repair-daily-log-cursor /absolute/path/to/project --apply --yes --expected-workspace-revision 12 --json
python skills/recallloom/scripts/recallloom.py bridge /absolute/path/to/project --file AGENTS.md --yes
```

`repair-daily-log-cursor` previews by default. Use apply mode only after
reviewing the preview and confirming the current `workspace_revision`; apply
repairs `state.json.daily_logs` from the parsed latest active daily log and
does not create helper receipts or rewrite daily-log content. A successful
apply remains `review_required`; rerun repair preview or validation before any
later write.

## Recording Workflow

Use `record --suggest` after a durable milestone when the agent should decide
whether to offer a recording prompt. It is side-effect-free, returns only a
sanitized candidate and suggested `record --plan` path, and never authorizes a
write.

Use `record --plan` when the user asks to record progress and the agent needs a
short, auditable route before choosing `append`, `write`, or a stop/ask result.
The plan is read-only: it classifies the intent, returns an ordered helper path,
and names the current safe command when one exists.

For append-only happy paths, the returned command can be followed by `append`.
When preflight later proves that only a single daily-log append made
`rolling_summary.md` stale, use `sync-current-state-after-append
--reuse-current-summary` only with a bound semantic-unchanged assertion. The
assertion must carry the full `record_plan_output`; bare booleans, free-text
confirmations, stale plan ids, and legacy `source_id` are rejected.

The detailed public-safe input and retry templates live in
[skills/recallloom/references/recording-workflow.md](./skills/recallloom/references/recording-workflow.md).

## Native Wrappers

The package can render native wrappers for supported hosts:

- `claude-code`
- `gemini-cli`
- `opencode`

Wrapper scope:

- `rl-init`
- `rl-resume`
- `rl-status`
- `rl-validate`

They remain convenience entrypoints only. They delegate to the same dispatcher
and do not replace host/router first-hop policy for generic continue/restore
requests.

## Read-Side Helpers

- `preflight_context_check.py`
- `summarize_continuity_status.py`
- `query_continuity.py`

These helpers are read-only and keep the same freshness baseline. Use them when
you need orientation, trust/drift signals, or write-target guidance before a
formal write.

## Write Helpers

- `init_context.py`
- `validate_context.py`
- `append_daily_log_entry.py`
- `commit_context_file.py`
- `archive_logs.py`
- `manage_entry_bridge.py`
- `repair_daily_log_cursor.py`

Use the concrete helper scripts for managed sidecar writes. Preserve revision
checks and the shared failure contract.

## Where To Read More

- [INDEX.md](./INDEX.md)
- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)
- [skills/recallloom/references/file-contracts.md](./skills/recallloom/references/file-contracts.md)
- [skills/recallloom/references/operation-playbooks.md](./skills/recallloom/references/operation-playbooks.md)
- [skills/recallloom/references/protocol.md](./skills/recallloom/references/protocol.md)
- [skills/recallloom/native_commands/README.md](./skills/recallloom/native_commands/README.md)
