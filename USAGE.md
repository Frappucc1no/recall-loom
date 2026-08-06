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
python skills/recallloom/scripts/recallloom.py repair-daily-log-cursor /absolute/path/to/project --json
python skills/recallloom/scripts/recallloom.py repair-daily-log-cursor /absolute/path/to/project --apply --yes --expected-workspace-revision 12 --json
python skills/recallloom/scripts/recallloom.py bridge /absolute/path/to/project --file AGENTS.md --json
```

`repair-daily-log-cursor` previews by default. Use apply mode only after
reviewing the preview and confirming the current `workspace_revision`; apply
repairs `state.json.daily_logs` from the parsed latest active daily log and
does not create helper receipts or rewrite daily-log content. A successful
apply remains `review_required`; rerun repair preview or validation before any
later write.

`sync-current-state-after-append --reuse-current-summary` requires the complete
bound semantic-unchanged assertion emitted for the current preflight/record
plan; a partial hand-written JSON object is rejected. See the recording
workflow reference before using that lane.

For the three mutation commands only (`append`, `write`, and
`sync-current-state-after-append`), `--compact-json` emits the
`recallloom.transaction.compact/1.0` result projection. It is mutually exclusive
with legacy `--json`; it preserves the command exit code, contains exactly one
safe next action, and stays below 2048 UTF-8 bytes. Use legacy `--json` when a
consumer requires the established schema 1.1 payload.

Bridge and archive operations are preview-only in the current public package
contract. Review their candidate targets, but do not rely on or document an
apply command such as `bridge --yes` until each surface has its own
receipt-backed contract.

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

## Provenance Recovery Boundary

A general, legacy, or unbound `inconsistent_or_tampered_evidence` state blocks
mutating writes and is non-waivable. Run structural validation and the bounded
current provenance check; do not hand-edit the sidecar, reuse stale material,
or treat the failure as an ordinary retry.

D5 is the sole narrow recovery transition. It applies only to helper-path
evidence that is contract-valid for the current D5 schema, for a target-only post-hash read failure or
mismatch with an exact failure-time state match, then requires a fresh binding,
human proposal, human review, and expected-binding promotion. This is not a
waiver and does not establish cryptographic authorship. A promotion creates a
non-receipt-backed reviewed baseline, so run fresh validate, status, and
preflight afterwards. The exact human-material sections, JSON keys, and
proposal/review promotion commands are in the
[operation playbook](./skills/recallloom/references/operation-playbooks.md#d5-human-material-and-promotion).

For the first write from that reviewed baseline, use dispatcher `write` or
`append` with `--confirm-review-imported-baseline`; it issues the
confirmation-bound binding and matching lease. The underlying commit/append
helpers do not accept that flag. Normal operation never calls the underlying
helpers directly: they are internal dispatcher/integration surfaces. A read-only preflight does not issue
a binding or lease, there is no independent operator pickup interface, and a
hand-invoked helper without dispatcher-issued material is expected to fail.
The existing `sync-current-state-after-append` lane also accepts the confirmation
when its own post-append contract requires it; this does not make the internal
helpers standalone entrypoints.

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
- `validate_context.py`

These helpers are read-only and keep the same freshness baseline. Use them when
you need orientation, trust/drift signals, or write-target guidance before a
formal write. `validate_context.py` performs structural or bounded provenance
diagnosis; it is not a repair or promotion write surface.

## Write Helpers

Normal operator writes use dispatcher subcommands only:

- `init`
- `append`
- `write`
- `sync-current-state-after-append`
- `repair-daily-log-cursor`

The dispatcher owns fresh preflight, revision checks, bindings, leases, and the
shared failure contract. `repair-daily-log-cursor` remains the real public
cursor-repair entry; use its preview first and its documented apply contract
only when eligible.

## Internal Implementation Helpers

- `init_context.py`
- `append_daily_log_entry.py`
- `commit_context_file.py`

These are internal dispatcher/integration helpers, not standalone operator
commands. They do not provide an independent write path.

## Preview-Only Helpers

- `archive_logs.py`
- `manage_entry_bridge.py`

These helpers are preview-only in the current public contract. Treat legacy
archive-apply and bridge `--yes` attempts as unsupported/blocked; they are not
public write operations or a replacement for receipt-backed mutation.

## Where To Read More

- [INDEX.md](./INDEX.md)
- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)
- [skills/recallloom/references/file-contracts.md](./skills/recallloom/references/file-contracts.md)
- [skills/recallloom/references/operation-playbooks.md](./skills/recallloom/references/operation-playbooks.md)
- [skills/recallloom/references/protocol.md](./skills/recallloom/references/protocol.md)
- [skills/recallloom/native_commands/README.md](./skills/recallloom/native_commands/README.md)
