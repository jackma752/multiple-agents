# P3：Codex 真实适配器（最小版本）

日期：2026-10-14。范围：把 Codex 执行接入已有 API / Worker / 控制台，
**复用**审批、快照与配额门禁。**本文件所述全部为离线验证**；
真实调用的验证已单独申请并逐次批准。

---

## 1. 组件与数据流

```
控制台 /ui ──► API ──► Worker._process_one
                          │
                          ├─ Phase 1: PolicyService.consume()
                          │     校验审批有效性 / 停机 / 暂停 / 策略版本
                          │     → 原子消费审批(CAS) + 原子预留配额(tree_quota)
                          │     → 提交；产出 gate_token
                          │
                          ├─ Phase 2: CodexAdapter.start(..., gate_token=...)
                          │     └─ 需要 gate_token，否则拒绝启动（纵深防御）
                          │        构建 argv/env → 启动 codex 进程 → 读取 JSONL
                          │
                          └─ Phase 3: 依 terminal 语义结算
                                ok+terminal  → SUCCEEDED，释放名额一次
                                ok 非 terminal → RUNNING，保留名额
                                !ok terminal → FAILED，释放名额一次
                                !ok 非 terminal → UNKNOWN，**保留名额**
```

**门禁没有被绕过，也没有被复制**：适配器不判断能不能跑；
`consume()` 仍是唯一的放行点，且现在把 `gate_token` 作为**已批准的证据**传给适配器。

## 2. 文件

| 文件 | 作用 |
|---|---|
| `packages/adapters/agenthub_adapters/store.py` | 通用执行记录存储（Codex 与 Mock 共用；表名参数化并校验） |
| `packages/adapters/agenthub_adapters/codex.py` | CodexAdapter、argv/env 构建、JSONL 解析、runner 抽象 |
| `apps/worker/agenthub_kernel/adapters.py` | 适配器选择（mock 默认 / codex 显式开启） |
| `apps/worker/agenthub_kernel/config.py` | 可执行文件固定（路径+sha256+size）、home、代理、CA、超时 |
| `apps/api/agenthub_rt/api.py` | `GET /api/adapter`、`GET /api/invocations/{id}/adapter-result` |
| `apps/api/agenthub_rt/static/index.html` | 顶部适配器徽标 + 「执行结果」面板 |
| `tools/demo/serve.py` | `--adapter codex` |

## 3. 安全姿态（有测试断言，不靠约定）

| 要求 | 实现 | 证据 |
|---|---|---|
| **未批准不得启动进程** | 适配器要求 `gate_token`；`gate_token` 仅由 `consume()` 产出 | `test_start_without_gate_token_launches_nothing`、`test_unapproved_codex_never_launches_a_process` |
| 批准后才允许启动 | Worker 只在 Phase 2 调用 `start()`，且已提交 | `test_approved_codex_launches_exactly_one_process` |
| 只读沙箱 | `--sandbox read-only` | `test_build_argv_enforces_read_only_and_no_subagents`；联调中 `workdir after = []` |
| 禁用原生子 Agent | `--disable multi_agent` | 同上（并断言**从不出现** `dangerously-bypass` 系列） |
| 固定可执行文件 | 路径 + sha256 + size 三项核对，不符则**拒绝启动** | `test_identity_mismatch_refuses_to_launch`；联调 `identity_ok=True` |
| 进程专用代理与证书 | `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`/`CODEX_CA_CERTIFICATE` 只注入子进程 | `test_build_env_sets_proxy_and_ca_without_touching_the_system`（并断言**不修改调用方环境**） |
| 事件 / 最终回复 / thread_id / usage 解析 | `parse_events()` | `test_parse_verified_capture` 等 4 项（用**真实探测捕获**作 fixture） |
| **无法取消就如实声明** | `cancel` 返回 `unsupported`，不伪造停止 | `test_cancel_is_reported_unsupported_not_faked` |

### 输出捕获为何用文件而非管道

本环境的沙箱**拒绝原生进程的管道 stdio**（实测 `subprocess.run(capture_output=True)`
报 `PermissionError WinError 5`），而**文件句柄可用**（已验证的真实探测即用此法）。
因此 `SubprocessCodexRunner` 把 stdout 写入文件再读取。

## 4. 要求 4：超时 / 取消未确认 → unknown 且保留配额

这是本轮修出的一个**真实集成缺陷**：适配器超时后刻意返回 `ok=False`
（因为本地杀死进程**不能**证明远端 turn 已结束），但 Worker 原先一律按
"not ok → FAILED + 释放名额"处理，**会错误释放仍在占用的名额**。

修法是给结果加上**终态语义**：

```python
AdapterStartResult(ok, terminal)      # terminal=True 表示结果确定
```

| 情形 | ok | terminal | 内核动作 | 名额 |
|---|---|---|---|---|
| 成功且完成 | True | True | SUCCEEDED | 释放一次 |
| 启动成功但仍在运行（Mock） | True | False | RUNNING | 保留 |
| 确定失败（`turn.failed`） | False | True | FAILED | 释放一次 |
| **超时 / 取消未确认** | False | **False** | **UNKNOWN** | **保留** |

`cancel` 未验证 → 一律 `unsupported` → 停止无法确认 → 保留名额，直到**用户**裁决
（`settle` 释放一次 / `retry` 需先确认已停）。测试：
`test_codex_timeout_produces_unknown_and_keeps_the_slot`、
`test_codex_reap_keeps_the_slot_because_cancel_is_unsupported`。

### 附带修出的第二个缺陷：同步成功会泄漏名额

`codex exec` 是**同步**的（进程退出即结束），但内核原先只把调用置为 RUNNING，
**名额不释放**——已完成的子 Agent 会永久占住一个并发名额。
现在成功的同步运行返回 `terminal=True`，内核立即结算为 SUCCEEDED 并释放一次。

回归测试：`test_completed_codex_call_does_not_leak_its_slot`
（断言 `concurrent == 0`、无未释放预留、不再出现在超时列表、且**下一个调用能用该名额**）。
联调前后对比：`open_reservations` **1 → 0**，内核状态 `running → succeeded`。

## 5. 离线联调（模拟进程，非 Codex）

用桩进程驱动**完整栈**，不调用 Codex、不联网：

```powershell
.\.venv\Scripts\python.exe var\offline_integration.py
```

桩程序 `var/stub-codex.py` 输出与**真实探测捕获完全相同**的 JSONL；
通过 `AGENT_HUB_CODEX_PREFIX`（仅测试用）以 `python stub-codex.py` 形式启动，
从而**真实走通** argv/env 构建、子进程启动、文件捕获、JSONL 解析与存储。

**实测结果（节选）**：

```
1) adapter identity: kind=codex  identity_ok=True  sandbox=read-only
   subagents_disabled=True  proxy=http://127.0.0.1:8090
   capabilities: post_message=True stream=True real_usage=True
                 cancel=False intercept_tool=False
                 limit_dir=unknown control_subagent=unknown
2) created invocation ... after drain WITHOUT approval -> stub launched? False   ← 安全阀
3) after approval -> drain outcomes: [{ok:true, terminal:true, adapter_invocation_id:"stub-thread-0001"}]
   stub process launches: 1
   argv: exec --json --disable multi_agent --sandbox read-only --skip-git-repo-check -C ...
   env keys: ['CODEX_CA_CERTIFICATE','CODEX_HOME','HTTPS_PROXY','HTTP_PROXY','NO_PROXY']
4) adapter result: status=finished  thread_id=stub-thread-0001  final reply='OK'
   usage={input_tokens:14481, cached_input_tokens:12544, output_tokens:5, ...}
   event_types={thread.started:1, turn.started:1, item.completed:1, turn.completed:1}
   workdir after=[]                       ← 只读生效
5) quota: open_reservations=0             ← 无泄漏（修复前为 1）
   invocation status in kernel: succeeded  ← 修复前为 running
6) failure path -> adapter status=failed  detail='stub: simulated turn failure'
```

## 6. 自动化测试

| 文件 | 数量 | 内容 |
|---|---|---|
| `tests/adapters/test_codex_adapter.py` | 20 | 事件解析（真实捕获作 fixture）、argv/env 安全断言、门禁、身份固定、超时、**桩子进程**真实走通捕获与超时 |
| `tests/policy/test_codex_gate.py` | 12 | Codex 适配器**插进未改动的内核**：未批准/拒绝/过期 → 0 进程；批准 → 恰好 1 次；总量与并发上限拦得住；停机拦得住；超时→unknown 保留名额；成功不泄漏名额 |

**全套 190 passed**（原 158 + 32）。**全部离线，未启动真实 Codex。**

## 7. 仍未验证（不得当作已实现）

- **取消**：`cancel` 返回 `unsupported`；Codex 的取消语义完全未验证。
- **工具的启动前拦截**：未验证。
- **关闭 `multi_agent` 是否真能阻止子 Agent 派生**：未验证（联调用的是桩进程，本就不会派生）。
- **只读沙箱是否真拒绝写入**：弱证据（提示词从未要求写文件）。
- **台账/用量字段的稳定性**：仅一次真实调用观测到 usage 字段。
- **app-server 路线**：未评估。
- **真实调用次数**：截至本文件，**成功 1 次**（极简探测）。适配器本身**尚未**用真实 Codex 跑过。

## 8. 下一步（需逐次批准）

用**控制台**提交一次真实 Codex 验证：`--adapter codex` 启动，走
"创建调用 → 未批准不启动 → 批准 → 启动 → 查看 thread_id/usage/最终回复"。
**未获批准不会发起任何真实调用。**
