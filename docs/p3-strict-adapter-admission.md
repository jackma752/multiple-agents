# P3：适配器执行准入（2026-10-15）

依据 `AGENT_HUB_PLAN.md` §4.3：无法关闭、事前拦截或完整计数原生子 Agent 的适配器，不满足严格协作要求。批准一次主调用不授权未经登记的派生。

## 发现与处置

已有一次真实 Codex 调用在 argv 包含 `--disable multi_agent` 时仍产生原生子 Agent，Hub 未登记该子任务。详见 [观测记录](p3-native-subagent-incident.md)。当前配置的 `control_subagent=false`；适配器的回复、usage 等历史成功记录继续保留，但不代表它具备严格执行资格。

本轮实现的是**阻止新的不受控执行**。并未修复上游派生行为，也未关闭历史子 Agent。

## 执行契约

新增 `ExecutionAdmission`（`packages/contracts/agenthub/contracts.py`）：

| 字段 | 含义 |
|---|---|
| `allowed` | 严格布尔值，是否允许进入原有审批门 |
| `mode` | `blocked` / `simulation` / `strict` |
| `code` | 稳定的拒绝/模拟原因码 |
| `reason` | 用户可读说明，不含凭证或任务文本 |

由可信适配器实现 `execution_admission()`，不可从 API 请求、注册表能力字段或环境变量授权。默认、缺失、异常和非法声明均拒绝。未来真实适配器要由代码实现并提供控制证据才能使用 `strict`；目前没有真实适配器获得此资格。

执行顺序：

1. Worker 找到已批准的 queued 调用。
2. 查询准入；拒绝则返回 `launch_attempted=false`，**不消费审批、不预留名额、不启动进程**。调用仍 queued，审批仍 approved，可撤销。
3. 通过准入后，原有 `consume()` 仍是审批/快照/策略/配额的唯一放行点；事务提交后才调用适配器。
4. `CodexAdapter.start()` 再检查一次准入，防止绕过 Worker 或检查后更换 runner。拒绝时启动次数保持 0。

返回码：当前真实 Codex 为 `native_subagents_uncontrolled`；未提供有效声明的适配器为 `adapter_controls_unverified`。已批准、配额为 0、提示词承诺不派生，都不能覆盖拒绝。

## 离线开发与页面

Mock、代码注入的 `FakeCodexRunner`、受限的 `StubCodexRunner` 可以以 `simulation` 运行。进程桩只接受**本仓库固定的 `tools/demo/stub_codex.py` + 当前 Python 解释器**，执行前再次核对；任意脚本/命令前缀不能冒充桩。它们只证明 Hub 的行为，不证明真实 Codex 的权限能力。

`GET /api/adapter` 新增 `execution_admission`；`subagent_disable_requested` 只说明参数请求，兼容字段 `subagents_disabled` 返回 false。页面显示“已阻止新调用”，禁用推进调度，并说明 Hub 配额不包含未登记的历史原生子 Agent。无法读取适配器时保持禁止执行。后端独立检查，禁用按钮不是执行边界。

历史单次调用工具 `codex_one_call.py`、`codex_verify.py --mode real`、`capability_probe.py --mode real` 在访问凭据/启动进程前退出 2。stub 模式保留。没有 `allow_uncontrolled` 或环境变量绕过开关。

已有数据库、审批、历史调用结果、凭证、会话 rollout 均不迁移或改写。这里不会把已完成的父调用改为 unknown，也不会凭空补记一个已经无法确认现状的子调用配额。

## 验证与后续

回归文件：`tests/policy/test_adapter_admission.py`、`tests/test_suspended_real_tools.py`，覆盖已批准根/子调用、重复与并发调度、重建 Worker、零子配额、直接 start、模拟切换为真实 runner、伪造能力与命令前缀、历史工具拒绝和 Mock/离线桩正常工作。

下一步按交接推进 app-server 的 S1–S5，逐项取得实际控制证据。**更换传输层本身不恢复准入**。S1 不包含模型回合；S2+ 每次真实调用仍需单独批准。浏览器 SSE、会话恢复与第二个真实 Agent 仍属 P3 待办；两项 DLL 初始化失败记录保持 OPEN。

## GitHub 参考核查

按用户要求先检索已有实现。本轮只阅读参考，未复制第三方源码或引入依赖。

- [openai/codex #31097](https://github.com/openai/codex/issues/31097)：社区报告配置关闭后仍出现 v2 派生。它与本次现象相近，是后续源码排查线索，**不是本机版本根因或修复有效性的证明**；不据此自动修改模型目录或升级 CLI。
- [Vibe Kanban 的 Codex 执行器](https://github.com/BloopAI/vibe-kanban/blob/main/crates/executors/src/executors/codex.rs)：已有 app-server 初始化、客户端与审批服务衔接，可供后续传输实现参考；[仓库许可证](https://github.com/BloopAI/vibe-kanban/blob/main/LICENSE) 为 Apache-2.0。没有发现足以替代本项目原生子 Agent 审批/配额验证的依据。
- [官方 App Server 协议](https://learn.chatgpt.com/docs/app-server)：用于核对握手与回合事件；协议可用不等于本机控制行为已经验证。
