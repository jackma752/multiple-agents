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
.\.venv\Scripts\python.exe tools\demo\offline_integration.py
```

桩程序 `tools/demo/stub_codex.py` 输出与**真实探测捕获完全相同**的 JSONL；
通过 `AGENT_HUB_CODEX_PREFIX`（仅测试用）以 `python stub_codex.py` 形式启动，
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
| `tests/adapters/test_codex_adapter.py` | 34 | 事件解析（真实捕获作 fixture）、argv/env 安全断言、门禁、**Agent 身份匹配**、身份固定、超时、启动期异常分类、**桩子进程**真实走通捕获与超时 |
| `tests/policy/test_codex_gate.py` | 17 | Codex 适配器**插进未改动的内核**：Agent 身份不匹配 → 0 进程；未批准/拒绝/过期 → 0 进程；批准 → 恰好 1 次；总量与并发上限拦得住；停机拦得住；超时→unknown 保留名额；成功不泄漏名额；启动期四类失败的名额归属 |
| `tests/test_hygiene.py` | 10 | 仓库无真实密钥、Token 只从环境读、**CODEX_HOME 与凭证路径在仓库之外**、**日志不打印 Token** |
| `tests/http/test_contract_api.py` | +4 | `adapter-result` 的启动证据（exit_code / 事件数 / verdict）、`agent_ids` 暴露、未服务 agent 的创建期告警 |
| `tests/http/test_ui.py` | +1 | 页面不再硬编码 agent_id，且读取 `agent_ids` / `adapter_warning` |

**全套 221 passed**。**全部离线，未启动真实 Codex。**

## 7. 启动期异常：哪些能释放名额，哪些不能

`start()` 被调用时，内核**已经**消费了审批并预留了名额（Phase 1 已提交）。
所以 `start()` 如何处理失败，直接决定这个名额是否会被释放——**抛异常是最坏的选择**：
它会让调用永远停在 `starting` 并占着名额。

| 情形 | 产物是否存在 | ok | terminal | 内核动作 | 名额 |
|---|---|---|---|---|---|
| 无 `gate_token`（未经批准） | 否 | False | True | FAILED | 释放 |
| `agent_id` 与本适配器不符 | 否 | False | True | FAILED | 释放 |
| `codex_path` 未配置（**不再抛异常**） | 否 | False | True | FAILED | 释放 |
| 固定身份（sha256/size）不符 | 否 | False | True | FAILED | 释放 |
| `CODEX_HOME` 无法创建 | 否 | False | True | FAILED | 释放 |
| `Popen` 被系统拒绝（`CodexLaunchFailed`） | 否 | False | True | FAILED | 释放 |
| **其它异常（进程可能已存在）** | **不确定** | False | **False** | **UNKNOWN** | **保留** |
| 适配器抛出未捕获异常（Worker 兜底） | **不确定** | False | **False** | **UNKNOWN**（写入 `invocation.error`） | **保留** |

判定依据只有一条：**能不能证明进程不存在**。
`subprocess.Popen` 抛出的 `OSError` 证明"操作系统没有创建进程"，因此是确定的；
其余任何异常（例如启动后读管道失败）都无法区分"没跑"和"跑着"，**一律按 unknown 处理**。

测试：`test_missing_executable_is_a_terminal_refusal_not_an_exception`、
`test_unusable_codex_home_is_a_terminal_refusal`、
`test_refused_process_creation_is_terminal_and_frees_the_slot`、
`test_ambiguous_launch_error_keeps_the_slot`、
`test_adapter_crash_keeps_the_slot_and_records_why`。

### Agent 身份匹配

`CodexAdapter.AGENT_IDS = {"codex"}`：快照里记录的 `agent_id` 必须是 `codex`，
否则**拒绝启动**（确定性失败，释放名额）。否则一个登记为其它 Agent 的调用
可能悄悄跑起真实的 Codex 进程。测试：`test_start_refuses_when_agent_id_does_not_match`、
`test_start_refuses_various_non_matching_agent_ids`、`test_start_accepts_the_declared_agent_id`。

### 凭证与日志卫生

* `CODEX_HOME` 默认**在仓库之外**（`%LOCALAPPDATA%\AgentHub\codex-home`，
  可用 `AGENT_HUB_CODEX_HOME` 覆盖）。它可能存放 `auth.json` 副本，
  放在仓库内即使被 gitignore，也只需一次 `git add -f`/打包/备份就会泄露。
* `config.codex_home()` **只做路径解析、不创建目录**（纯函数）；
  只有真正要启动进程时才由适配器创建（`config.ensure_codex_home()` 供工具使用）。
  否则"读一下设置"都会产生仓库外的写入副作用。
* 启动横幅**不打印 Token**，只说明它的来源（`value not shown`）。
  回归测试在源码层面断言，防止以后又打出来。

## 8. 仍未验证（不得当作已实现）

- **取消**：`cancel` 返回 `unsupported`；Codex 的取消语义完全未验证。
- **工具的启动前拦截**：未验证。
- **关闭 `multi_agent` 是否真能阻止子 Agent 派生**：未验证（联调用的是桩进程，本就不会派生）。
- **只读沙箱是否真拒绝写入**：弱证据（提示词从未要求写文件）。
- **台账/用量字段的稳定性**：仅一次真实调用观测到 usage 字段。
- **app-server 路线**：未评估。
- **真实调用次数**：截至本文件，**成功 1 次**（极简探测，走的是 `tools/codex_one_call.py`，
  不是本适配器）。**本适配器尚未用真实 Codex 跑过**——这正是下一节要申请的那一次。

## 9. 下一步：一次经审批的真实调用

验证脚本 `tools/demo/codex_verify.py` 用**独立的空数据库**跑完整链路，
并且**只创建一个 `agent_id="codex"` 的调用**（脚本会断言"该库中只有它一个调用/任务树"）：

```powershell
# 离线自检（桩进程，不联网、不花费额度）——已执行通过
.\.venv\Scripts\python.exe tools/demo/codex_verify.py --mode stub

# 真实调用（需操作者逐次批准；没有这个开关会直接拒绝执行）
.\.venv\Scripts\python.exe tools/demo/codex_verify.py --mode real --i-understand-this-spends-quota
```

脚本的判定顺序即为安全阀：**未批准 → 进程启动数必须为 0**；
批准后 → **恰好为 1**；然后逐项打印 CHECKS 与 DIFF（任务树 / 调用数 / 启动数 /
适配器记录 / thread_id / usage / 未释放预留）。

**"通过"的判定是严格的**（2026-10-14 收紧）：只看"启动了进程且发生了点什么"
**不算通过**。必须同时满足 12 项：未批准时启动数 0、批准后启动尝试恰好 1 次、
JSONL 事件 > 0、`verdict = finished`、退出码 0、未超时、`thread_id` 非空、
最终回复非空、返回真实 `usage`、内核状态 `succeeded`、未释放预留 0、并发名额 0。
任一项不满足即打印 `VERIFICATION FAILED` 并列出未满足项、以非零码退出。

**反向对照（证明判定确实严格）**：用桩进程跑一次注定失败的调用
（`--prompt "... FAIL ..."`，桩会输出 `turn.failed` 并以退出码 1 结束）：

```powershell
.\.venv\Scripts\python.exe tools\demo\codex_verify.py --mode stub --prompt "please FAIL this turn"
# [4] 启动 0  [5] 启动 1  → 但 verdict=finished / 退出码=0 / 最终回复 / usage / succeeded 五项 FAIL
# → VERIFICATION FAILED
```

这一次**确实启动了进程、也拿到了 `thread_id`**，但被正确判为失败——旧判定
（只看 `thread_id` 与"终态"）会把它误报为 PASSED。

判定"启动了进程"的证据也分两层：`adapter-result` 的 `launch_attempted` 表示
**已越过启动前拒绝**（即真的去启动进程了），`evidence.exit_code` /
`evidence.event_count` 才表示**进程确实跑起来并输出了 JSONL**。
真实模式两者都会打印，不做推断；`evidence` 只返回计数，不返回 argv 或错误原文
（错误原文可能引用提示词，而提示词属用户内容）。

真实模式的前置条件（脚本会在**发起任何调用之前**检查，不满足就直接拒绝）：

* `CODEX_HOME` 必须是**可写**目录且**在仓库之外**（默认优先用
  `%LOCALAPPDATA%\AgentHub\codex-home`，没有凭证时回退到操作者自己的
  `%USERPROFILE%\.codex`，也可用 `--codex-home` 指定）；
* 该目录必须**已有登录凭证** `auth.json`，否则脚本拒绝执行——避免花掉一次真实
  调用只为得到"未登录"；
* 需要放行 `chatgpt.com:443` 与 `api.openai.com:443`（或走已验证的本地回环代理，
  见 `docs/codex-proxy-setup.md`）；
* **未获批准不会发起任何真实调用。**

判定"启动了进程"的证据分两层：`adapter-result` 的 `launch_attempted` 表示**已越过
启动前拒绝**（即真的去启动进程了），`evidence.exit_code` / `evidence.event_count`
才表示**进程确实跑起来并输出了 JSONL**。真实模式两者都会打印，不做推断。

