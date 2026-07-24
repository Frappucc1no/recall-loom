# Contributing to RecallLoom

Thanks for helping improve RecallLoom.

感谢你帮助改进 RecallLoom。

RecallLoom is an installable project-memory skill for long-running AI-assisted
work. Contributions are welcome when they make the package clearer, safer,
easier to install, or easier to use across AI tools.

RecallLoom 是一个面向长期 AI 协作项目的可安装项目记忆技能。欢迎提交能让它更清楚、更安全、更容易安装或更容易跨 AI 工具使用的贡献。

## Good Contributions / 适合的贡献

Good first contributions include:

适合开始的贡献包括：

- clearer README, USAGE, or SKILL wording
- 更清楚的 README、USAGE 或 SKILL 表述
- install and setup feedback
- 安装和初始化反馈
- small documentation examples
- 小型文档示例
- host integration notes for Codex, Claude Code, Gemini CLI, OpenCode, or
  similar tools
- Codex、Claude Code、Gemini CLI、OpenCode 或类似工具的集成说明
- reproducible bug reports
- 可复现的 bug 报告
- focused fixes to documented commands or helper scripts
- 对公开命令或 helper 脚本的聚焦修复
- portable public CI checks
- 可移植的公开 CI 检查

## Repository Boundary / 仓库边界

Keep this public repository focused on the installable package, public
documentation, public examples, and public checks.

这个公开仓库应聚焦于可安装包、公开文档、公开示例和公开检查。

Please do not add:

请不要加入：

- large unrelated directories or generated files that are not part of the
  installable package
- 与可安装包无关的大型目录或生成文件
- generated caches such as `.DS_Store`, `__pycache__/`, `*.pyc`, or temporary
  logs
- `.DS_Store`、`__pycache__/`、`*.pyc` 或临时日志等生成缓存
- machine-local paths, host metadata, tokens, token-like sample values,
  personal chat transcripts, or customer/project data
- 本机路径、宿主工具元数据、token、形似 token 的示例值、个人聊天记录或客户/项目数据
- copied `.recallloom/` project memory from a real workspace
- 从真实工作区复制出来的 `.recallloom/` 项目记忆
- copied project memory, review records, local operation outputs, or
  maintainer-only working files that are not required for installation or
  everyday use
- 复制出来的真实项目记忆、审阅记录、本地运行输出，或安装和日常使用不需要的维护者工作文件
- broad rewrites that mix product behavior, version or support facts, and
  documentation style changes in one pull request
- 在一个 PR 里混合产品行为、版本或支持状态信息和文档风格的大范围改写

Public CI or required checks may be useful, but describe them only as
repository content and metadata checks. They are not proof of a user's local
workspace state, host behavior, or project-memory trust status.

公开 CI 或 required check 可以有帮助，但只能把它们描述为仓库内容和元数据检查；
它们不是用户本地工作区状态、宿主行为或项目记忆可信状态的证明。

When in doubt, keep the pull request small and explain the user-facing reason
for the change.

不确定时，请把 PR 做小，并说明它解决了什么用户可感知的问题。

## Before Opening an Issue / 提 issue 前

For bugs, please include:

报告 bug 时，请包含：

- RecallLoom package version
- RecallLoom 包版本
- install method
- 安装方式
- host tool, if relevant
- 相关宿主工具（如果有）
- operating system
- 操作系统
- Python version
- Python 版本
- command or prompt that triggered the problem
- 触发问题的命令或提示语
- expected behavior
- 预期行为
- actual behavior
- 实际行为
- redacted output or error message
- 已脱敏的输出或错误信息

Do not include secrets, sensitive file paths, personal project memory, or raw
workspace data.

请不要包含密钥、敏感文件路径、个人项目记忆或原始工作区数据。

## Before Opening a Pull Request / 提 PR 前

### Verification Rhythm / 验证节奏

For a behavior change, start from a reproducible scenario where feasible,
make the smallest root-cause fix, and run the smallest relevant check before
calling the change complete. A code review, syntax check, or clean diff is not
enough evidence for state-machine, write-path, recovery, privacy, or security
behavior.

行为改动应尽可能先有可复现场景，再做最小根因修复，并在宣称完成前运行最小相关检查。
对于状态机、写入路径、恢复、隐私或安全行为，代码审阅、语法检查或干净 diff 都不能代替动态证据。

Maintainers may run additional affected-subsystem, compatibility, and release
checks in a private workspace. Those tests, fixtures, raw outputs, and process
records do not belong in this public repository. Passing development checks
also does not by itself authorize a release; release preparation and public
publication remain separate maintainer decisions.

维护者可能在私有工作区继续运行受影响子系统、兼容性和发布检查。这些测试、fixture、原始输出和过程记录不属于本公开仓库。开发验证通过也不自动授权发布；发布准备和正式公开仍是分开的维护者决策。

Please check:

请确认：

- The change has a clear user-facing purpose.
- 这次改动有清楚的用户价值。
- Public docs do not expose sensitive paths, secrets, generated workspace state,
  or token-like sample values.
- 公开文档没有暴露敏感路径、密钥、生成的工作区状态或形似 token 的示例值。
- Package metadata, README, SKILL, and release advisory facts remain consistent
  when version or support fields are changed.
- 修改版本或支持状态字段时，package metadata、README、SKILL 和 release advisory 保持一致。
- New helper behavior uses the existing package structure and does not create a
  host-specific copy of product logic.
- 新 helper 行为沿用现有包结构，不为某个宿主工具复制一套产品逻辑。
- The pull request keeps the public package focused on product code,
  documentation, examples, and public checks.
- PR 保持公开包聚焦于产品代码、文档、示例和公开检查。

Useful public checks:

常用公开检查：

```bash
python3 skills/recallloom/scripts/sync_contract_docs.py --check --json
python3 skills/recallloom/scripts/recallloom.py --help
```

If your change touches Python helpers, also run the smallest smoke check you
can describe in the pull request.

如果改动涉及 Python helper，请同时运行一个你能在 PR 中说明的最小 smoke check。

## Documentation Style / 文档风格

Write for users first.

优先为用户而写。

- Prefer plain words such as "initialize", "restore", "record progress", and
  "validate".
- 优先使用 “initialize / 初始化”、“restore / 恢复”、“record progress / 记录进展”、
  “validate / 校验” 等直白表达。
- Explain the workflow before naming helper scripts.
- 先解释工作流，再提 helper 脚本名称。
- Keep examples small and synthetic.
- 示例保持小而虚构。
- Use relative links for repository files.
- 仓库内文件使用相对链接。
- Keep English and Chinese guidance aligned when editing this file.
- 编辑这份文件时，请保持中英文说明一致。

## License / 许可

By contributing, you agree that your contribution will be licensed under the
same license as this repository: Apache-2.0.

提交贡献即表示你同意贡献内容按本仓库相同许可发布：Apache-2.0。
