# RecallLoom Native Command Templates

This directory contains host-specific native command templates for supported
platforms.

Current supported hosts:

- `claude-code`
- `gemini-cli`
- `opencode`

These templates are rendered by
[`scripts/install_native_commands.py`](../scripts/install_native_commands.py).
They are convenience wrappers only and convenience entrypoints only. The wrapper
layer delegates to the unified RecallLoom dispatcher.

Current wrapper scope:

- `rl-init`
- `rl-resume`
- `rl-status`
- `rl-validate`

The wrappers delegate to the same RecallLoom dispatcher contract described in
[README.md](../../../README.md), [README.en.md](../../../README.en.md),
[README.zh-CN.md](../../../README.zh-CN.md), [INDEX.md](../../../INDEX.md),
[USAGE.md](../../../USAGE.md), and [`SKILL.md`](../SKILL.md).

Boundary rules:

- wrappers do not replace the skill package
- wrappers do not create a second logic set
- wrappers do not bypass the dispatcher or helper scripts
- wrappers do not justify bypassing the dispatcher
- wrappers do not replace host/router first-hop policy
- wrappers do not turn host bridge text into product truth
- wrapper layer text stays advisory
- natural-language restore requests stay the primary public path
