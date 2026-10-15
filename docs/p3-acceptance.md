# P3 验收：统一控制台与第一个真实适配器（Codex）

> **2026-10-15 当前结论**：原生派生绕过 Hub 审批/配额已观测到，当前 Codex 配置不满足严格协作要求。
> 新版本在消费审批前拒绝真实启动，Mock/离线桩可用。旧成功记录保留，不等于已验收严格控制。
> 见 [准入修复](p3-strict-adapter-admission.md)、[派生证据与真实子 thread](p3-native-subagent-incident.md)、[最新交接](HANDOFF_CURRENT.md)。

日期：2026-10-14。分支：`feat/p3-wrapup`（基于 `feat/p3-codex-probe`）。测试：**304 passed**。

P3 在计划 §6 中的范围是"两个 Agent 的消息、流式结果、审批和会话映射；登录和敏感配置不暴露给前端"。
**本轮只接入了一个真实适配器（Codex）**，因此 P3 **未完成**（见 §3）。
本文件把"已验证 / 未完成 / 未验证"三类严格分开。

---

## 1. 已验证（有命令与实际输出）

### 1.1 用户网页手工验收（本次，2026-10-14）

操作者在本机控制台 `/ui` 上完成一次真实 Codex 调用，观察到：

| 观察项 | 结果 |
|---|---|
| 未批准时 **推进调度** | 进程启动数 **0** |
| 批准后 **推进调度** | 进程启动数 **1** |
| 最终回复 | `OK`（逐字） |
| 适配器状态 / 内核状态 | `finished` / `succeeded` |
| 退出码 | `0` |
| 名额占用 | **归零** |

与自动测试的对应关系：`test_unapproved_codex_never_launches_a_process`、
`test_approved_codex_launches_exactly_one_process`、`test_completed_codex_call_does_not_leak_its_slot`、
`tests/http/test_ui.py::test_simulated_console_session_with_the_real_codex_adapter`。

### 1.2 经批准的脚本化真实联调（2026-10-14）

`docs/p3-codex-real-run-2026-10-14.md`：12/12 检查 PASS，退出码 0，`thread_id`
`01a13a77-316d-7b01-a59e-5323a6c0728f`，回复 `OK`，真实 usage
`input 15094 / cached 12544 / output 5`，`open_reservations 0→0`。

### 1.3 离线验证（每次提交前都跑）

| 命令 | 结果 |
|---|---|
| `python -m pytest` | **304 passed** |
| `python tools/demo/offline_integration.py` | `OFFLINE INTEGRATION PASSED` |
| `python tools/demo/codex_verify.py --mode stub` | `VERIFICATION PASSED`（12/12 检查） |
| `python tools/demo/codex_verify.py --mode stub --prompt "...FAIL..."` | `VERIFICATION FAILED`（负对照，证明判定严格） |
| `python tools/probes/capability_probe.py --probe <4 项> --mode stub` | 见 `docs/p3-permission-probes.md` §6（含正/负对照与误判对照） |

### 1.3.1 本轮的口径修正（2026-10-14，第二次）

* **凭据目录不是"不存在"**：长期 `CODEX_HOME`（`%LOCALAPPDATA%\AgentHub\codex-home`）
  **存在且已登录**（`codex login status` → `Logged in using ChatGPT`）。
  此前文档把"*我的*进程写入被拒（`os error 5`）"误写为"目录不存在/需由你创建"，已更正；
  那个"访问被拒"是执行沙箱对**我的进程**的限制，与目录是否存在、登录是否有效无关。
* **判定口径收紧**：没有实际尝试与策略拒绝证据时一律判**无法判断**——
  仅有助手自述 `REFUSED` / `NO-SUBAGENT`、仅有"没有文件"、仅有"只有一个 thread"、
  或错误文本里出现 `sandbox` 等词，都**不再**构成 PASS；启动失败也一律"无法判断"。
  正对照只能加强证据，**不能替代**只读运行的实际拒绝证据。判定规则共 42 项单元测试。
* **权限固定**：探测工具显式设定 `--sandbox read-only` 并在启动前**回读核对**实际配置，
  继承来的沙箱变量被覆盖；`workspace-write` 仅用于获批的正对照；`danger-full-access` 不可达。
* **清理范围**：只删探测自己创建的 `var/probes/<probe>-<mode>*`；
  **长期 `CODEX_HOME` 与用户自己的 `~/.codex` 一律保留**。

### 1.4 能力结论（本轮新增/确认）

| 能力 | 状态 | 依据 |
|---|---|---|
| 审批后执行、回复、thread_id、真实用量、成功结算 | **已验证** | §1.1、§1.2 |
| 未批准不启动、启动期失败分类、名额不泄漏 | **已验证** | 自动测试 + §1.1 |
| 内核与适配器记录的执行 ID 一致 | **已验证** | `test_kernel_and_adapter_records_agree_on_the_execution_id` |
| 任务状态随调用生命周期同步（不再停留在 `draft`） | **已验证** | `tests/policy/test_task_status.py`（18 项） |
| Agent 身份匹配（非 `codex` 一律拒绝启动） | **已验证** | `tests/adapters/test_codex_adapter.py` |
| 停止/取消未确认 → `unknown` + **保留名额**、不谎称已停 | **已验证** | `tests/policy/test_stop_cancel.py`（11 项） |

## 2. 未完成（本轮不做，属后续阶段）

| 项 | 现状 |
|---|---|
| **网页 SSE / 断线恢复** | 事件面板是轮询读取；计划 §6 的"SSE 断线恢复"未实现 |
| **会话恢复** | 已拿到 `thread_id`，且 CLI **有** `codex exec resume [SESSION_ID]` / `--last`（自描述确认）；**我们的适配器没有接入**，恢复行为未验证 |
| **第二个真实 Agent** | 只有 Codex；CC / WorkBuddy / DSH / Ink Stone 未接入，P3 阶段目标未达成 |
| 自动协作开关与任务分解 | P4 |
| worktree 与差异产物 | P5 |

## 3. 未验证（**不得当作已支持**）

| 能力 | 为什么没验证 | 方案 |
|---|---|---|
| 取消 | `codex exec` 无任何取消选项；协议里的 `turn/interrupt` 只服务 app-server | `docs/p3-permission-probes.md` §5（**当前模式下不具备验证条件**） |
| 工具操作启动前拦截 | exec **拒绝** `-a/--ask-for-approval`，批准通道只服务 app-server 的 `turn/start` | 同上 §4（预期结论：不支持） |
| 原生子 Agent 阻断 | `--disable multi_agent` 只置功能开关；模型可见提示词里子 Agent 说明**依然存在** | 同上 §3（PASS 只代表"本次未观测到派生"） |
| 只读沙箱是否真拒绝写入 | 离线**无法**区分 read-only 与 workspace-write（权限档案渲染相同；`codex doctor` 曾报 `sandbox backend disabled`） | 同上 §2。**已执行 1 次真实探测（2026-10-14）：结果 = 无法判断**——文件未创建，但 JSONL 里**没有任何工具/文件类 item**，驱动通道也无相关拒绝证据；**本次探针未取得实际尝试及拒绝证据**（官方文档列出 exec JSON 工具事件，本机是否存在版本差异需核实，不得外推为"该模式不可观测"）。逐项记录见 `docs/p3-probe-readonly-write-2026-10-14.md`。**仍未验证** |
| app-server 协议 | 未使用 | 未评估 |
| 按 `thread_id` 恢复会话 | 未接入（见 §2） | 未评估 |

## 4. 与计划 §6 安全阀用例的对照（P3 视角）

| 用例 | 状态 |
|---|---|
| 无审批 / 过期 / 撤销 → 启动 0 次 | ✅ 已验证（含真实适配器） |
| 同一审批并发消费最多启动一次 | ✅ 已验证 |
| 快照/策略变化 → 旧批准失效 | ✅ 已验证 |
| 全树总量 3 / 并发 2 不被突破 | ✅ 已验证（Mock；真实适配器同样走 `consume()`） |
| 总量 0 拒绝一切子 Agent | ✅ 已验证 |
| 深度 1 不允许孙 Agent | ✅ 已验证（内核层） |
| Agent 不能批准自己 / 不能绕过中转站 | ⚠️ 部分：HTTP 权限边界已验证；**"绕过"依赖原生子 Agent 阻断与工具拦截，两者未验证** |
| 关闭自动协作后无新自动分派；全部停止后无新启动 | ⚠️ 停止部分已验证；自动协作属 P4 |
| 崩溃 / 远程超时 / 重复事件：不盲目重跑、不错误宣称远程停止 | ⚠️ 部分：本地已实现并验证；**真实远程超时/取消未验证** |
| SSE 断线恢复一致；仓库与日志不含真实密钥 | ⚠️ 密钥部分已验证（`tests/test_hygiene.py`）；**SSE 未实现** |
| 只读沙箱是否真拒绝写入 | ⚠️ **1 次真实探测（2026-10-14）结果 = 无法判断；能力仍未验证**（文件未创建，但无任何尝试信号与驱动拒绝证据；助手自述 `REFUSED` 不算证据）。见 `docs/p3-probe-readonly-write-2026-10-14.md` |

## 5. 交付物

| 文件 | 作用 |
|---|---|
| `docs/p3-codex-adapter.md` | 适配器、门禁复用、启动期异常语义、任务状态与执行 ID |
| `docs/p3-codex-real-run-2026-10-14.md` | 经批准的真实联调记录 |
| `docs/p3-permission-probes.md` | 四项权限能力的验证方案、判定条件与清理步骤 |
| `docs/p3-codex-credentials.md` | 仓库外长期凭据目录与登录步骤 |
| `docs/p3-console-trial-steps.md` | 网页试用步骤（含真实模式） |
| `docs/p3-ui.md` | 控制台说明 |
| `tools/demo/codex_verify.py` | 单次调用验证（stub/real，12 项检查） |
| `tools/probes/capability_probe.py` | 四项权限探测（stub/real，三种判定） |
| `packages/adapters/agenthub_adapters/codex_probes.py` | 判定规则（纯函数，42 项单元测试） |

## 6. 下一步

1. 逐项批准并执行 `readonly-write`（含正对照最有力）→ 再依次 `subagent-block`、`tool-intercept`、`cancel`。
2. 按 §2 补 SSE、会话恢复、第二个真实 Agent。
3. `0xc0000142` 保持 OPEN（见 `docs/issue-python-0xc0000142.md` 与 `docs/issue-codex-0xc0000142.md`）。
