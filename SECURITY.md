# Security Policy / 安全政策

RecallLoom stores project-continuity information in local project files. Please
report security or privacy issues carefully so affected users can update before
details are widely shared.

RecallLoom 会把项目连续性信息保存在本地项目文件中。请谨慎报告安全或隐私问题，以便受影响用户能在细节广泛公开前完成更新。

## Supported Versions / 支持版本

Security fixes target the latest public release.

安全修复面向最新公开版本。

| Version / 版本 | Security support / 安全支持 |
|---|---|
| `0.4.x` | Supported / 支持 |
| Older versions / 更早版本 | Please update to the latest public release / 请更新到最新公开版本 |

## What To Report / 适合报告的问题

Please report issues such as:

请报告以下类型的问题：

- unsafe writes to RecallLoom-managed files
- 对 RecallLoom 管理文件的不安全写入
- path traversal or command-injection behavior
- 路径穿越或命令注入行为
- accidental exposure of sensitive paths, host metadata, secrets, or token-like
  values
- 意外暴露敏感路径、宿主工具元数据、密钥或形似 token 的值
- bypasses that allow unsupported or unsafe file-changing actions
- 绕过安全检查并导致不安全文件写入的情况
- privacy failures in helper output or generated summaries
- helper 输出或生成摘要中的隐私问题

## How To Report / 如何报告

Do not open a public issue for a security vulnerability.

请不要用公开 issue 报告安全漏洞。

Use GitHub private vulnerability reporting if it is available for this
repository. If it is not available, open a minimal public issue asking for a
security contact without sharing vulnerability details.

如果本仓库启用了 GitHub private vulnerability reporting，请优先使用它。如果没有启用，请开一个最小公开 issue 请求安全联系渠道，不要在 issue 中披露漏洞细节。

Please include:

请包含：

- affected RecallLoom version
- 受影响的 RecallLoom 版本
- operating system and Python version
- 操作系统和 Python 版本
- install method
- 安装方式
- affected command or helper
- 受影响的命令或 helper
- impact summary
- 影响摘要
- minimal reproduction steps
- 最小复现步骤
- redacted output
- 已脱敏输出

Do not include real secrets, personal project files, real `.recallloom/`
directories, customer data, or full local paths unless they are strictly needed
and already redacted.

除非确有必要且已经脱敏，请不要包含真实密钥、个人项目文件、真实 `.recallloom/` 目录、客户数据或完整本地路径。

## Disclosure / 披露

Please give the maintainer reasonable time to investigate and publish a fix
before sharing exploit details publicly.

公开漏洞细节前，请给维护者合理时间调查并发布修复。

Reports that are documentation-only, general support requests, or feature
requests should use the normal issue templates instead.

纯文档问题、一般支持请求或功能建议请使用普通 issue 模板。
