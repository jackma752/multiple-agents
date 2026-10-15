# 原生子 Agent 派生观测（2026-10-15）

结论：**当前实测 Codex 配置未阻止原生派生，不满足严格协作要求。** 这是一次实际观测，不外推所有版本与场景；既不能说子 Agent 已停止，也不能说它现在仍在运行。

## 归属与证据

| 标识 | 值 |
|---|---|
| Hub 调用 | `inv-01a29718e59d` |
| Hub 根任务 | `task-2910815f9e9e` |
| 父 thread | `01a13b43-bdeb-74e2-be6f-127478b1ab37` |
| 父 turn | `01a13b43-be20-7f32-b2e2-d3367ae906ba` |
| spawn 工具 call id | `call_73986da07eff48ad84532888247dba22` |
| **子 thread** | **`01a13b43-e437-7c82-8f45-8deeeaa40082`** |
| 子任务路径 | `/root/standby` |

原始记录在宿主的 `%LOCALAPPDATA%\AgentHub\codex-home\sessions\2026\10\15\rollout-2026-10-15T00-34-17-01a13b43-bdeb-74e2-be6f-127478b1ab37.jsonl`。本轮只读提取第 12 行结构字段，确认 `SubAgentActivity`、`kind=started`、`agent_thread_id`、`agent_path`。未复制完整会话、提示词或凭证。

先前只读核查已报告第 11 行 `spawn_agent` 调用、第 14 行工具结果 `task_name=/root/standby`、第 21 行父 turn 完成。早期排查报告“只有 call_id，没有子 thread id”漏了 `agent_thread_id` 字段，应以表中标识更正。助手自然语言声称创建成功不作为证据。

用户提供的数据库核查还记录：本次 argv 带 `--disable multi_agent`，内核父调用 succeeded、子任务数 0；适配器 stdout 只有四个生命周期事件和 `agent_message`。由此原生派生没有进入 Hub 的审批/配额链路，不能用 stdout 中未出现派生事件来断言没有派生。

## 未决与处理边界

这份父会话证据只显示 started，未取得子任务 completed/stopped 证据。未确认受支持的、针对该历史子 thread 的关闭接口。父调用完成、CLI 退出、名额为 0 均不替代子任务停止证明。

本轮未发起关闭模型回合、未终止任何既有进程、未删除凭证或审计记录。后续若有受支持的管理接口，必须用真实子 thread 和父归属定位，不能仅按 `/root/standby` 的同名路径跨会话操作，也不按进程名强杀共享 daemon。

新调用的后端拦截见 [适配器执行准入](p3-strict-adapter-admission.md)。历史父调用结果保留，未做无证据的状态改写或配额回填。`0xc0000142` 两项问题仍 OPEN，与本次准入修复分开跟踪。
