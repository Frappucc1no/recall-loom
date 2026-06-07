# Package Support Policy

RecallLoom package support is separate from workspace protocol compatibility.

Protocol `1.0` describes the sidecar file contract. Package support describes whether the currently installed RecallLoom skill package is still allowed to perform the requested action.

## Daily Check

RecallLoom performs a lightweight package support check through helper startup.

- checks are cached by local date
- cache scope is the installed package path
- dispatcher checks may pass same-day support payloads to child helpers through `RECALLLOOM_SUPPORT_STATE_JSON`, but helpers still authorize from their own same-day cache or advisory read rather than trusting external env payloads alone
- support state is stored in the user cache, not in project `.recallloom/`
- network or advisory failures without a usable cache resolve to `unknown_offline`; diagnostic and read-only actions can continue, while mutating actions stay blocked until support can be verified
- an advisory payload that is present but malformed is treated as invalid and reduced to diagnostic-only behavior until repaired

The default advisory URL is read from `package-metadata.json` as `support_advisory_url`.
This URL should remain the canonical public location on `origin/main`.
Remote support/readiness checks should be rerun after publishing package updates, not worked around by rewriting the canonical URL for each environment.
Operators can override it with `RECALLLOOM_SUPPORT_ADVISORY_URL` or provide a local JSON file with `RECALLLOOM_SUPPORT_ADVISORY_FILE`.

## Repository Boundary

Package-support advisories, public CI, and required checks validate public
package metadata and support-policy posture. They must not present repository
checks as proof of a user's local workspace state, host behavior, or sidecar
trust status.

Public support metadata must stay limited to information needed for package
support decisions and must not include copied project memory, generated runtime
output, machine-local data, maintainer-only working files, or material that is
not required by the installable package.

## Advisory Schema

The current advisory shape is:

```json
{
  "latest_version": "0.4.4",
  "minimum_mutating_version": "0.3.3",
  "minimum_readonly_version": "0.3.3",
  "advisory_level": "supported",
  "release_channel": "stable",
  "public_release_status": "released",
  "reason_code": "current_release_supported",
  "user_message": "This RecallLoom package version is currently supported.",
  "support_advisory_url_status": "canonical_public_location",
  "remote_parity_rule": "The default support_advisory_url points at the canonical public origin/main location. Re-run remote support/readiness checks after publishing package updates.",
  "update_hints": {
    "skills_cli": "Run npx skills update.",
    "directory_install": "Replace the installed recallloom/ skill directory with the latest package copy.",
    "native_wrappers": "After updating the package, rerun install_native_commands.py if you use native command wrappers."
  }
}
```

`advisory_level` may be `supported`, `upgrade_recommended`, `readonly_only`, `diagnostic_only`, or `upgrade_required`.
`upgrade_recommended` and `upgrade_required` only become an upgrade state when the current package is below `latest_version`; hard blocking is controlled by `minimum_mutating_version` and `minimum_readonly_version`.
`latest_version`, `minimum_mutating_version`, and `minimum_readonly_version` are required dotted numeric version strings.
Additional current top-level advisory metadata may include:

- `release_channel`: package line label such as `stable`
- `public_release_status`: public-release posture such as `released`
- `support_advisory_url_status`: advisory-location status label such as `canonical_public_location`
- `remote_parity_rule`: operator guidance for rerunning remote support/readiness checks after package updates

These extra top-level fields are descriptive metadata for operators and release workflows.
Helpers currently validate and gate behavior from the core support fields above, while preserving unknown top-level metadata for diagnostics and documentation.

## Runtime States

- `supported`: all actions are allowed
- `upgrade_recommended`: all actions are allowed, but diagnostics may surface upgrade guidance
- `readonly_only`: diagnostic and read-only actions are allowed; mutating actions are blocked
- `diagnostic_only`: only diagnostic actions are allowed
- `unknown_offline`: no fresh advisory could be obtained and no usable cache exists; diagnostic and read-only actions are allowed, while mutating actions are blocked

## Action Levels

- diagnostic: `validate`, `status`, root detection, support diagnostics, write-lock inspection
- readonly: `resume`, query, preflight, workday recommendation, cold-start proposal generation, recovery promotion preparation
- mutating: init, bridge apply/remove, context commits, daily-log appends, archive, recovery staging/review recording, uninstall, native wrapper installation

Daily-log cursor repair is a structural repair action, not a receipt-backed
mutation. The direct `repair_daily_log_cursor.py` helper and dispatcher
`repair-daily-log-cursor` command use the same support policy:

- preview/default mode is read-only and may run anywhere read-only actions are allowed
- `--apply --yes` is mutating and is blocked under `readonly_only`, `diagnostic_only`, and `unknown_offline`
- both entrypoints are explicitly registered in support action-level maps; apply must not rely on the default read-only fallback

When a support gate blocks an action, helpers return the shared failure contract with `blocked_reason: package_support_blocked` plus a `package_support` object describing state, action level, advisory source, cache source, update hints, and diagnostic reason.

## Environment Overrides

- `RECALLLOOM_SUPPORT_CACHE_DIR=/path/to/cache`: choose cache directory
- `RECALLLOOM_SUPPORT_DATE=YYYY-MM-DD`: force the local support-check date, intended for tests
- `RECALLLOOM_SUPPORT_ADVISORY_FILE=/path/to/release-advisory.json`: read advisory from a local file
- `RECALLLOOM_SUPPORT_ADVISORY_URL=https://.../release-advisory.json`: override the default advisory URL
- `RECALLLOOM_SUPPORT_FETCH_TIMEOUT_SECONDS=2`: set fetch timeout
