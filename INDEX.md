# RecallLoom Entry Index

This file is the full repository map for RecallLoom v0.4.8.
Start with the short README front doors:

- [README.md](./README.md)
- [README.en.md](./README.en.md)
- [README.zh-CN.md](./README.zh-CN.md) (compatibility entry)

Then use this index to choose the right deeper document.

## Reading Order

1. `README.md` or `README.en.md`
2. `README.zh-CN.md` for legacy compatibility
3. `INDEX.md`
4. `USAGE.md`
5. `skills/recallloom/SKILL.md`
6. `skills/recallloom/native_commands/README.md`

That split is intentional:

- README.md and README.en.md are the short first-use landing pages; `README.zh-CN.md`
  is the compatibility entry.
- `INDEX.md` is the full map.
- `USAGE.md` is the operator guide for the packaged dispatcher and helpers.
- `SKILL.md` is the installed agent-facing entrypoint.
- `native_commands/README.md` explains optional host wrappers as convenience
  entrypoints only.

## Stable Surface

The stable operator-facing action names for this package line are:

- `rl-init`
- `rl-resume`
- `rl-status`
- `rl-validate`

The dispatcher surface also includes:

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

Natural-language project prompts stay the primary public path. Wrapper aliases
and native commands are conveniences over the same contract. This is not a second logic set.

## What To Read For Each Question

### First attach

Read:

- [README.md](./README.md)
- [USAGE.md](./USAGE.md)
- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)

Use this when you want to know how initialization and first-attach routing work.

### Restore or resume

Read:

- [README.md](./README.md)
- [USAGE.md](./USAGE.md)
- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)
- [skills/recallloom/references/operation-playbooks.md](./skills/recallloom/references/operation-playbooks.md)

Use this when you need current-state recovery, read-plan guidance, or restore
contract details.

### Helper behavior and file contract

Read:

- [USAGE.md](./USAGE.md)
- [skills/recallloom/references/file-contracts.md](./skills/recallloom/references/file-contracts.md)
- [skills/recallloom/references/protocol.md](./skills/recallloom/references/protocol.md)
- [skills/recallloom/references/package-support-policy.md](./skills/recallloom/references/package-support-policy.md)

Use this when you need the durable sidecar rules, helper safety rules, or
runtime boundaries.

### Recording workflow

Read:

- [USAGE.md](./USAGE.md)
- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)
- [skills/recallloom/references/recording-workflow.md](./skills/recallloom/references/recording-workflow.md)

Use this when you need a side-effect-free milestone suggestion, need to classify
a "record this progress" intent, inspect the ordered helper path, or prepare a
metadata-only post-append refresh assertion.

### Package installation and agent entrypoint

Read:

- [skills/recallloom/SKILL.md](./skills/recallloom/SKILL.md)
- [skills/recallloom/package-metadata.json](./skills/recallloom/package-metadata.json)
- [skills/recallloom/managed-assets.json](./skills/recallloom/managed-assets.json)

Use this when you need the installed skill contract itself.

### Native wrappers

Read:

- [skills/recallloom/native_commands/README.md](./skills/recallloom/native_commands/README.md)
- [skills/recallloom/native_commands/claude-code/](./skills/recallloom/native_commands/claude-code/)
- [skills/recallloom/native_commands/gemini-cli/](./skills/recallloom/native_commands/gemini-cli/)
- [skills/recallloom/native_commands/opencode/](./skills/recallloom/native_commands/opencode/)

Use this when you want host-native convenience commands, not a separate
authority layer.

## Repository Map

### Public entry surface

- `README.md`
- `README.en.md`
- `README.zh-CN.md`
- `INDEX.md`
- `USAGE.md`

### Release notes

- `docs/releases/v0.4.8.md`
- `docs/releases/v0.4.7.md`
- `docs/releases/v0.4.6.2.md`
- `docs/releases/v0.4.6.1.md`
- `docs/releases/v0.4.6.md`
- `docs/releases/v0.4.4.md`
- `docs/releases/v0.4.3.md`
- `docs/releases/v0.4.2.md`
- `docs/releases/v0.4.1.md`
- `docs/releases/v0.4.0.md`
- `docs/releases/v0.3.5.md`

### Installed skill package

- `skills/recallloom/SKILL.md`
- `skills/recallloom/package-metadata.json`
- `skills/recallloom/managed-assets.json`
- `skills/recallloom/references/`
- `skills/recallloom/profiles/`
- `skills/recallloom/scripts/`
- `skills/recallloom/native_commands/`

## Boundary Notes

- The README files are intentionally short. The full file map lives here.
- `USAGE.md` should stay aligned with `SKILL.md` on the stable helper contract.
- `native_commands/README.md` should describe wrappers as convenience
  entrypoints only.
- Host bridge or wrapper text must not become a second product logic set.
- The package does not promise a manual sidecar fallback when runtime support is
  missing.

## Where To Go Next

- For a quick public intro, use `README.md` or `README.en.md`.
- For legacy compatibility links, use `README.zh-CN.md`.
- For operator behavior, use `USAGE.md`.
- For installed-skill behavior, use `skills/recallloom/SKILL.md`.
- For native wrapper installation, use `skills/recallloom/native_commands/README.md`.
