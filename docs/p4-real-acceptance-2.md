# P4 最小真实协作验收：第二次申请

**状态（2026-10-25）**：经批准在 `080fe56` 上执行一次，判定为 **INCONCLUSIVE**（只认定本次）。
- #1 和 #2 全部通过，#2 是一回合、单一模型、用量完整。
- 汇总调用失败：CLI 报告安全防护错误，`num_turns=2`，两个模型，用量 partial。
- 见[第二次实录](p4-2026-10-25-run2.md)，标签 `evidence/p4-real-run-2-inconclusive`。
- 授权已用完，结果转入离线处理。

下文保留申请原文。

## 目的

按[第一次的方案](p4-real-acceptance.md)原样再跑一次。第一次判为 INCONCLUSIVE：#2 的 Claude Code 报告了两个模型、`num_turns=2`，用量为 partial，原因未知（见[第一次实录](p4-2026-10-25-run1.md)）。这次要得到两种结果之一：

- **#2 和汇总都是一回合、单一模型、用量完整**：得到一次 PASS 记录（只认定本次）。
- **再次出现多回合或多个模型**：仍判 INCONCLUSIVE。但这次执行记录会留下第一次缺少的结果字段，可作为排查依据。

无论哪种结果，都不解释第一次的原因。

## 与第一次的差异

其余全部相同：

- **被测提交**：`feat/p4-collab` 上包含本文件的提交。执行前确认 HEAD 和工作树都没有变化。
- **新增记录**：Claude Code 执行记录增加 `result_meta`，包括：
  - result 中其余字段的数字，例如 `duration_ms`、`duration_api_ms`、`num_turns`；
  - 布尔标志；
  - UUID 形式的标识；
  - 字段名列表。

  其他字符串和所有列表都不保存，所以不含回复或提示词。`usage` 和 `modelUsage` 照旧保存。
- **判定**：`p4_collect.py` 已经包含独立的回合数检查（`06357d8`）。Claude Code 调用的 `num_turns` 不是 1 时，记 `<调用>_turns_not_one`，判为 INCONCLUSIVE，与用量是否完整无关。多模型时用量仍为 partial，规则不改，两份用量也不相加。
- **数据目录**：新的 `%TEMP%\agenthub-p4-run2`，执行前确认它不存在。
- **启动**：`.claude/launch.json` 的 `p4-real-2`。参数与 `p4-real` 相同，端口仍为 8181，只是数据目录换成 run2。
- **暗号**：执行前重新生成，格式不变。结果只记录它的 SHA-256。

## 步骤、预算、判定、停止规则

完全按 [p4-real-acceptance.md](p4-real-acceptance.md) 执行，把文中的 `p4-real` 和 `agenthub-p4-run1` 分别换成 `p4-real-2` 和 `agenthub-p4-run2`：

- 被动核验 → 启动 → `PREFLIGHT PASS` → 网页上的各个步骤 → 截图 → 停止服务 → 判定脚本运行一次。
- **最多 3 次真实调用**：Codex 1 次、Claude Code 2 次。建立任务树的 opener 不批准、不运行。
- 每次调用逐个审批，时限分别为 300、180、180 秒。
- 前置检查失败或出现异常时立即停止。出现工具请求时只点拒绝，并停止后续步骤。
- 不重试、不换模型、不放宽权限或参数、不追加调用。保留代理和 CA，不关闭证书验证。

## 证据

与第一次相同：result.json、截图、两个 Agent 的 stderr 日志、Codex 的 rollout，都只登记 SHA-256，不入库。

另外，运行记录会写明 #2 和汇总的 `models`、`num_turns`，以及 `result_meta` 中的数字、标志和字段名。标识只留在执行库中（仓库外），不写进记录。

标签：`evidence/p4-real-run-2-<pass|fail|inconclusive>`。

## 只认定 / 不认定

- **只认定（本次）**：与第一次的“只认定”相同。如果判为 PASS，还认定本次 #2 和汇总都是一回合、单一模型，用量完整。
- **不认定**：
  - 第一次出现两个模型和两个回合的原因；
  - Claude Code 在 `--max-turns 1` 下总是只用一个回合、一个模型；
  - 第一次申请中列出的其他“不认定”项。
- 如果再次出现多回合或多个模型，只记录观测到的事实，不推断原因。

## 风险

- 额度：三个模型回合，Codex 1 个，Claude Code 2 个。
- 可能再次出现两个回合：仍判 INCONCLUSIVE，授权用完。是否继续另行决定。
- 其余风险与第一次申请相同。
