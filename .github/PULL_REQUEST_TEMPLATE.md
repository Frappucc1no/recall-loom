# Pull Request / 拉取请求

## Summary / 摘要

What changed?

改了什么？

## Why / 原因

What user-facing problem does this solve?

它解决了什么用户可感知的问题？

## Type / 类型

- [ ] Documentation / 文档
- [ ] Helper behavior / Helper 行为
- [ ] Packaging or metadata / 打包或元数据
- [ ] Host integration / 宿主工具集成
- [ ] Public CI or repository maintenance / 公开 CI 或仓库维护

## Validation / 验证

Please list the checks you ran.

请列出你运行过的检查。

```bash
python3 skills/recallloom/scripts/sync_contract_docs.py --check --json
python3 skills/recallloom/scripts/recallloom.py --help
```

## Privacy and Scope Check / 隐私与范围检查

- [ ] This PR only includes product code, documentation, examples, or public validation checks.
- [ ] 这个 PR 只包含产品代码、文档、示例或公开验证检查。
- [ ] I did not include secrets, sensitive paths, personal project memory, or token-like sample values.
- [ ] 我没有包含密钥、敏感路径、个人项目记忆或形似 token 的示例值。
- [ ] I kept package metadata, README, SKILL, and advisory facts consistent where relevant.
- [ ] 涉及时，我保持 package metadata、README、SKILL 和 advisory 信息一致。
- [ ] I kept the change focused and explained the user-facing reason.
- [ ] 我保持改动聚焦，并说明了用户可感知的原因。
