# 会话后端（2026-10-21）

本轮只实现后端，前端最后再接。会话和消息写入运行数据库，不能把运行库打进源码发布包。无需读取凭据或发起模型调用。

## HTTP 契约

以下接口均须用户 Bearer token；未知会话或非本人的会话返回 404。

| 接口 | 输入 | 效果 |
|---|---|---|
| POST `/api/conversations` | 可省略；`title` 可选 | 创建持久化会话，不创建任务或审批 |
| GET `/api/conversations` | `offset=0, limit=50`（最大 100） | 按创建时间倒序列出本人的会话 |
| POST `/api/conversations/{id}/messages` | `text` | 只保存用户消息；保留旧页面的 `acked/text` 返回值，不隐式创建调用 |
| GET `/api/conversations/{id}/messages` | `after_id=0, limit=100`（最大 200） | 按递增消息 ID 读取；返回 `next_after_id`、`has_more`、关联调用/审批状态 |
| POST `/api/conversations/{id}/invocations` | `text, agent_id, client_request_id, execution_mode=strict` | 在一个事务中保存用户消息、调用快照及待审批对象；不批准、不执行 |
| POST `/api/invocations/{id}/run` | 无 | 只尝试执行指定调用；仍经过既有准入、consume、审批 CAS、配额及停机检查 |

消息与任务输入保留原文；空白文本、超长输入、不认识的输入字段被拒绝。提交调用的 `client_request_id` 必填，同会话同键同内容返回原对象；同键改内容/Agent/执行模式返回 409。幂等重放不重新审批、不重置策略、不启动调用。

`run` 未就绪/准入或配额拒绝返回 409，未知调用返回 404；已启动的调用不能通过重复请求再启动。进入适配器后的失败结果返回 200，并以 `ok/terminal/status` 表达，不能只凭 HTTP 200 判断执行成功。`adapter_start_attempted=true` 只说明调用了适配器的 start 方法；是否真正启动外部进程须另看适配器执行证据。

## 事务与配额

会话第一次提交调用时创建一个根任务；后续调用复用同一个根任务及累计配额，快照绑定当前策略版本。每一条调用都有独立审批。暂停时可保存待审批调用，执行仍被阻止；已停止的会话拒绝创建新调用。新建会话属于新任务树，不提供全账号总额限制。

`conversations` 和 `chat_messages` 为新增表，启动时增量建表，不改写旧任务/审批。旧页面先保存消息再调旧 `/api/invocations` 的流程继续可用，但这些旧调用不会自动关联到会话；不可猜测历史配对。

人工裁决重试仍须先确认旧执行已停止。重试从被重试调用的快照保留原文、Agent、权限等，绑定当前策略版本；在同一会话新增一条关联用户消息和全新待审批调用。不得从根任务最初的目标重建后续消息，亦不得沿用旧批准。

## 回复与恢复边界

只把已确认终态调用的适配器 `final_message` 保存为助手消息，原样保存，不把状态说明、Mock 状态或错误文本编造成模型回复。每个调用最多一条助手消息。回复保存与内核终态结算同事务；若适配器记录暂不可读，结算仍完成，下一次 `worker/recover` 可从已有适配器记录补回，绝不重新调用模型。unknown 调用不自动补成成功回复或释放名额。

`context_mode=independent`（默认）：这里恢复的是 Hub 的本地历史，不自动把旧聊天内容转发给下一次调用或另一个 Agent。原生 thread 续接是逐条显式选择的 `native_resume`，见末节；第二个真实 Agent 留待后续。

目前仍为本机单用户产品；既有任务管理/全队列 drain 是用户级管理入口，不宣称多租户隔离。真实控制边界、监督模式、两项 DLL OPEN 状态不变。

## 参考与验证

按用户要求，在检查重试与消息对应关系时查看了 GitHub 原项目：

- [Paseo provider 契约](https://github.com/getpaseo/paseo/blob/main/docs/providers.md) 用 `clientMessageId` 对应唯一用户消息，避免 provider 回显再生成一条消息。这里只参考消息身份分离原则，未复制实现。
- [Orca orchestration 文档](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) 区分任务与执行派发，并显式记录 retry-of。这里只参考显式执行身份；本项目仍逐次审批，不自动重试不确定调用。

本轮验收入口（全离线，不发模型请求）：

```powershell
cd E:\plug\agent-hub\.worktrees\chat-workbench
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' -m pytest tests/http/test_conversations.py -o addopts='' -q
```

覆盖文件库重开、消息分页、原文保持、权限与输入拒绝、事务回滚、并发幂等、精确调度、所有旧门禁、配额连续累计、重试快照/历史、回复落盘与恢复、unknown 保留、Codex 离线捕获、旧库增量建表。现有前端未接入新会话调用端点，8080 服务未在本轮替换。

最终全量验证：同一目录执行 `E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest -o addopts='' -q`，实际输出 **550 passed in 30.00s**。本轮新增 37 项会话后端测试；没有真实模型回合。

## 有序事件、游标补读与 SSE（2026-10-21 续）

本节只增加后端读侧，不改审批、配额、unknown 处理，也不触发任何调用。前端尚未接入。

### 存储与写入

- 新表 `conversation_events`（`conversation_id, seq, type, payload, created_at`，`(conversation_id, seq)` 唯一）和 `conversation_event_cursors`（每会话 `last_seq`）。启动时 `create_all` 增量建表，旧表不改写。
- 事件只在一个入口捕获：ORM `after_flush` 钩子（`agenthub_kernel/events.py`），用 flush 自己的连接写入，所以事件与它描述的状态变更**同事务提交或回滚**；回滚不留事件，也不留 seq 空洞。
- seq 用 `UPDATE ... SET last_seq = last_seq + n` 分配：先拿写锁（SQLite）/行锁（PostgreSQL）再定号，同一会话的写入按 seq 顺序提交；读到 seq N 时，N 之前的事件都已可见，游标补读不会漏。每个会话从 1 开始连续递增。
- 载荷只有 ID 和状态，**不复制聊天原文**；文本仍从 `/messages?after_id=` 读取。树级事件只复制白名单字段，停止证据、错误文本、策略原文不进入事件流。

| 事件 | 触发 | 载荷要点 |
|---|---|---|
| `conversation.created` | 创建会话 | `conversation_id` |
| `message.created` | 用户/助手消息插入（含人工重试新增的用户消息） | `message_id, role, invocation_id`；带调用的用户消息另附当时的 `invocation_status, approval_id, approval_status` |
| `approval.status` | 审批 ORM 状态变化（批准/拒绝/撤销/过期） | `approval_id, invocation_id, from, to` |
| `invocation.status` | 调用状态变化（含 starting/running/终态/unknown/timed_out） | `invocation_id, task_id, from, to`，以及数据库中**当前**的审批 ID/状态 |
| `task.<type>` | 会话根任务的 `tree_stop / tree_pause / tree_resume / local_stop_requested / unknown_settled / retry_requested / policy_updated` | `root_task_id` + 白名单字段 |

已知边界：审批 consume 是 Core CAS，没有单独的 `approved→consumed` 事件，由随后 `invocation.status`（→starting）中的 `approval_status=consumed` 如实反映，不伪造事件。同一次 flush 内状态连改两次只记首尾一条。旧 `/api/invocations` 调用不关联会话，因此不产生会话事件；本节之前写入的旧会话没有历史事件，新事件从 seq 1 开始，不补造历史。

### HTTP

| 接口 | 参数 | 效果 |
|---|---|---|
| GET `/api/conversations/{id}/events` | `after_seq=0, limit=100`（最大 500） | 返回 `seq > after_seq` 的事件、`next_after_seq`、`has_more`、`last_seq` |
| GET `/api/conversations/{id}/events/stream` | `after_seq=0, timeout=30`（0–300 秒）；请求头 `Last-Event-ID` 优先 | SSE：先发 `retry: 3000`，补发积压后继续轮询数据库跟随新事件；每帧 `id=seq, event=type, data=事件 JSON`；15 秒心跳注释；到 `timeout` 自然关闭，客户端用最后收到的 `id` 重连 |

两者都要求用户 Bearer token，非本人或未知会话返回 404；游标大于 `last_seq`（例如数据库被替换）返回 409 `cursor_ahead` 并给出 `last_seq`，客户端应重新全量同步；`Last-Event-ID` 非数字返回 400。SSE 轮询数据库（默认 0.5 秒），因此独立 Worker 进程写入的事件同样可见。浏览器原生 `EventSource` 不能带 Authorization 头，前端接入时需用 `fetch` 流式读取，或另行设计不降低鉴权的方案。

尚未完成：保留期/清理策略；多进程下的推送唤醒（目前是轮询）。app-server 增量与原生续接见末节。

### 验证（本节）

新增 `tests/http/test_conversation_events.py`，17 项：完整生命周期顺序与连续性、载荷不含原文、分页补读与 409、鉴权与归属、回滚无事件无空洞、提交失败回滚、幂等重放不重复发事件、24 路并发写入 seq 唯一连续且与提交顺序一致、会话间隔离、unknown 保留名额且无伪造回复、人工重试与停止事件、SSE 积压/`Last-Event-ID` 续读/400/409、实时跟随、超过单批的积压、重启后编号延续、旧库增量建表。

本轮在云端 Linux 副本上运行（Python 3.13，依赖取自本仓库 `wheelhouse` 的 fastapi 0.115.6 / starlette 0.41.3 / SQLAlchemy 2.0.36 纯 Python 部分 / pytest 8.3.4，pydantic 为环境自带 2.13.5），**未在 Windows `.venv` 上执行**：

- `python -m pytest -o addopts='' -q tests/http/test_conversation_events.py tests/http/test_conversations.py` → 54 passed（连续 3 次）。
- 全量 `python -m pytest -o addopts='' -q` → 566 passed / 1 failed；失败项 `test_suspended_real_tools.py[codex_one_call]` 因 Linux 无 `USERPROFILE` 环境变量，设置该变量后全量 **567 passed in 36.26s**。没有真实模型回合。

请在 Windows 上复核：

```powershell
cd E:\plug\agent-hub\.worktrees\chat-workbench
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' -m pytest -o addopts='' -q
```

复核结果（下一轮开工前，Windows `.venv`）：**567 passed in 37.44s**。

## app-server 增量输出与原生续接（2026-10-21 续二）

本节把 app-server 增量输出接入同一条会话事件流，作为可选传输；同时接入逐条显式选择的原生 thread 续接。审批、配额、unknown 处理和停止门禁的规则不变，新增的检查都放在消费审批**之前**。全程离线，没有真实模型回合。

### 传输选择与准入

- `AGENT_HUB_CODEX_TRANSPORT=exec|app-server`，默认 `exec`，无法识别的值回退到 exec。`AGENT_HUB_APPSERVER_STUB=<场景>` 仅供开发测试，用于选择内置 stdio 桩。选择传输本身不授予任何准入。
- `AppServerCodexAdapter`（`agenthub_adapters/appserver_adapter.py`）每次调用依次执行 `initialize → initialized → thread/start|thread/resume → turn/start`，然后跟随**本 turn** 的通知；判定靠精确匹配 thread id 与 turn id，其他 turn 的输出不计入。
- 只有本 turn 的 `turn/completed`，且状态为 completed、failed 或 interrupted，才能结算调用。超时、断线、`turn/start` 没有应答都记为 unknown，并保留名额。
- 服务端发来的审批请求一律回 `decline`，其他服务端请求回 `-32601`，都不会被接受。取消返回 `unsupported`。本地停止只关闭自有传输，不代表远端已停。
- 准入：只有 `InMemoryStubFactory` 和指向内置脚本的 `StdioStubFactory` 两种工厂（按类型判断，子类不算）记为 `simulation`。真实 `codex app-server --stdio --disable multi_agent` 在严格模式**和**监督模式下都被拒（`appserver_real_unverified`），原因是原生派生不可控，且 S3–S5 尚未运行。这比 10-20 的监督模式修订更保守，属于有意为之。

### 增量文本：单独存储，服务端组装

依用户决定（2026-10-21），继续保持“事件载荷不含聊天原文”的规则。

| 事件 | 持久化载荷 | 说明 |
|---|---|---|
| `stream.turn_started` | `invocation_id, thread_id, turn_id, resumed` | 真实模型回合已开始 |
| `stream.delta` | `invocation_id, item_id, chunk_id` | 文本在 `stream_chunks`，**不在载荷中** |
| `stream.request_declined` | `invocation_id, method, item_id, decision` | 不复制 command 或理由文本 |
| `stream.turn_ended` | `invocation_id, turn_status, verdict, terminal` | 适配器自己的判定，不等于内核结算 |

- **存储**：新表 `stream_chunks`（`id` 自增且不复用；`conversation_id, invocation_id, item_id, text`）按聊天内容管理，启动时增量建表。Worker 为支持流式的适配器提供 `StreamSink`：同一 item 的相邻增量会合并（约 0.2 秒或 2000 字一批）。每批在一个 `BEGIN IMMEDIATE` 事务中先插入 chunk，再按现有 seq 分配写入事件，两者同提交、同回滚，seq 不留空洞。
- **读取**：`/events` 和 `/events/stream` 在原有的 Bearer 鉴权和会话归属检查之后，由服务端按 `chunk_id` 在**本会话内**查出文本，填入返回副本的 `payload.text`；存储行本身不修改。其他会话的 chunk id 不会被解析。前端只需消费一条 SSE，不必按事件逐条请求。断线后用 `Last-Event-ID` 或 `after_seq` 续读，增量不丢失也不重复。
- **顺序**：所有 stream 事件都排在 `invocation.status → starting` 之后，并在结算前全部写入，所以一定排在终态 `invocation.status` 和助手 `message.created` 之前。运行期间调用状态保持 `starting`，以 `stream.turn_started` 表示模型回合已开始。
- **不是回复**：`stream.delta` 只用于临时显示。权威回复仍是结算时写入的助手消息；unknown 调用会留下已收到的部分增量，但不会生成回复。
- **敏感内容**：模型输出同样可能包含用户原文。sink 与适配器不写日志；写入失败只计数（`stream_events.dropped`），不记录异常文本，也不影响结算。树级审计事件仍走白名单；`invocations.error` 和适配器的 `detail` 只放判定说明。chunk 位于运行库 `var/`，已被 `.gitignore` 的 `/var/` 和 `*.db` 排除。适配器执行库保存 `final_message`，与 exec 适配器一致，用于补回回复。

### 原生续接（`context_mode=native_resume`）

| 接口 | 新字段或返回码 |
|---|---|
| POST `/api/conversations/{id}/invocations` | `context_mode=independent`（默认）或 `native_resume`；其他值返回 422 |
| 同上，`native_resume` | 取**该 Agent 最新一条回复**记录的 `native_session.thread_id`，绑定进快照的 `resume_thread_id` 并参与 `payload_hash`。没有记录 → 409 `no_native_session`；会话内有 starting/running/unknown 调用 → 409 `native_session_busy`。被拒时不创建根任务、调用或审批 |
| POST `/api/invocations/{id}/run` | 消费审批**之前**检查：适配器不支持续接 → 409 `native_resume_unsupported`；该线程可能仍被占用 → 409 `native_session_busy`。被拒时审批保持 `approved`，启动次数不变 |

- 只有确实能续接的适配器（目前只有 app-server 传输）才会在执行记录中写入 `native_session`；Mock 和 exec 不写，所以无法对它们发起 native_resume。
- 续接时只发送本条消息的新文本，Hub 不重放历史。上下文来自 provider 自己的 thread，这正是它必须写进审批快照的原因。
- 同一进程内，一个 thread 同时只允许一个 turn。人工重试会沿用原快照中的 `resume_thread_id`，并且仍要经过忙碌检查。
- `ConversationTurn.request_hash()` 在 independent 时与旧版完全相同；`InvocationSnapshot` 只有 native_resume 时才写入 `context_mode` 和 `resume_thread_id`，因此旧快照的哈希不变。

### 与文档的差异和限制

- 真实 app-server 没有运行。增量和续接只在内存桩与 stdio 子进程桩上验证；桩不保存 thread 历史，**真实续接效果未验证（S5）**。工具审批往返（S3）和中断（S4）同样未验证。
- `stream_chunks` 尚无保留期和清理，会话删除时也没有级联清理（目前没有删除接口）。多进程下仍靠轮询唤醒。
- 本机没有 git：本轮改动未提交，也无法给出 `git diff --cached`。

### 验证（本节）

新增 `tests/http/test_conversation_stream.py`，共 28 项：

- 事件顺序：turn_started < delta < turn_ended < 终态；拼接文本等于最终回复；其他 turn 的增量不计入；
- 存储层：直接查表，`conversation_events` 中没有模型文本或用户文本；chunk 与 delta 一一对应；树级审计事件和 `error` 字段不含增量文本；
- 读取：SSE 回放；从中途 `Last-Event-ID` 续读，增量不丢失、不重复；`/events` 组装出相同文本；
- 鉴权：401、403、他人会话 404 三种情况都拿不到文本；chunk 只在本会话内解析；
- 事务：chunk 与事件同时回滚，seq 不留空洞；运行中即可读到增量；载荷白名单生效；写入失败不影响执行；
- 结算：超时或断线进入 unknown 并保留名额、没有回复；失败时释放名额；审批请求只回 decline；
- 门禁：未审批时启动次数为 0；真实工厂在严格和监督模式下都被拒，审批不被消费；仿冒桩工厂不被放行；缺少 gate token 时直接拒绝；本地停止后为 unknown 并保留名额；
- 续接：快照绑定 thread、哈希随之变化、发出 `thread/resume` 且只发新文本；没有记录、会话忙碌、Worker 侧忙碌、适配器不支持四种拒绝路径；Mock 不记录 native_session；
- 兼容：快照与请求哈希不变；
- 端到端：通过配置启动内置 stdio 子进程桩，跑完首轮和续接两个回合，本地进程退出得到确认。

Windows `.venv` 实际输出：

- `python -m pytest tests/http/test_conversation_stream.py -o addopts='' -q -p no:cacheprovider` → **28 passed**，连续 4 次（7.7–7.9 秒）。
- 全量 `python -m pytest -o addopts='' -q -p no:cacheprovider` → **595 passed in 44.52s**。没有真实模型回合。

## 异步执行与工具审批往返（2026-10-22）

分支 `feat/async-tool-approval`。审批、配额、准入、unknown 和停止的规则不变；全程离线，没有真实模型回合。

### 异步 `/run`

| 接口 | 行为 |
|---|---|
| POST `/api/invocations/{id}/run` | 在请求内同步完成第 1 阶段：准入、原生续接检查、消费审批、预留名额，并提交事务。被拒仍返回 **409**，审批保持原状。被接受时立即返回 **202** `{accepted, launch_scheduled, status: "starting"}`，第 2、3 阶段（启动适配器、结算）交给 Worker 自己的有界线程池执行，进度通过会话事件流推送 |
| 同上，`?wait=true` | 走同一条后台路径，等结算完成后返回 **200** 和结果，供脚本和测试使用，页面不要用 |

- 线程数由 `AGENT_HUB_WORKER_THREADS` 配置，默认 4，范围 1–32。线程用满时，已接受的调用保持 `starting` 并占着名额排队。
- 后台执行中，适配器异常或结算本身失败（例如数据库写入出错）时，调用一律记为 `unknown` 并保留名额，不猜测结果。
- `recover()` 会跳过本进程正在执行的调用，因为它们的适配器还活着，不属于"重启后"需要对账的对象。
- 应用关闭时会等待在途调用结束（最多 30 秒），不会强杀；之后不再接受新调用（409 `worker_stopped`）。超时仍未结束的调用，留给下一次 `recover()` 对账。
- 同一调用重复 `/run` 返回 409，只会启动一次。

### 工具审批卡（每个工具请求一张）

批准运行 ≠ 批准工具操作。适配器支持 `supports_tool_approval()` 时，Worker 会传入 `tool_gate`。provider 在本 turn 内发来的每个工具审批请求都会生成一张卡（表 `tool_approvals`），turn 在等待期间暂停；只有用户能回应这张卡。

| 接口 | 说明 |
|---|---|
| GET `/api/conversations/{id}/tool-approvals?status=` | 列出本会话的卡，包含 `details`（command/cwd/reason） |
| GET `/api/tool-approvals/{id}` | 单张卡 |
| POST `/api/tool-approvals/{id}/approve` | `{card_hash}` 必填。只批准这一个请求，不是会话级授权，也不覆盖以后的请求 |
| POST `/api/tool-approvals/{id}/decline` | `card_hash` 可选，拒绝总是安全的 |

- **绑定**：一张卡对应 `invocation_id` + JSON-RPC `request_key` + `thread_id`/`turn_id`/`item_id`。适配器内部按"卡 → 原请求对象"一一对应回复，批准卡 A 不可能回答请求 B。其他 turn 的请求不生成卡，直接回 `decline`。
- **状态**：`pending → approved / declined / expired / void`。只有 `approved` 会被回复为 `accept`，而且只回一次；`declined`、`expired` 回 `decline`。`void` 表示请求、turn 或传输已先结束，不再回复。回复始终落在 provider 给出的 `availableDecisions` 范围内，不会被升级为 accept。
- **用户决定的前提**：卡仍是 `pending`、未过期、调用仍在 `starting/running`；批准时还必须提交当前的 `card_hash`，这样过时的页面无法批准它没见过的内容；provider 必须提供了 `accept` 选项。任一条件不满足返回 409：`not_pending` / `expired` / `call_not_active` / `card_hash_required` / `card_changed` / `accept_not_offered`。
- **过期**：`AGENT_HUB_TOOL_APPROVAL_TTL` 默认 120 秒（1–600），并且不超过本轮剩余的运行时限。没人回应时按过期处理，等同拒绝。
- **并发**：所有状态变更都先取写锁（SQLite `BEGIN IMMEDIATE`，PostgreSQL `FOR UPDATE`）再读当前状态。同时提交多个批准，或批准与过期同时发生，最终只会有一个结果。
- **送达**：`delivery` 记录 provider 是否真的收到回复（`sent / failed / not_sent`）和实际发出的决定（`decision_sent`）。批准不等于命令已经执行。
- **清理**：turn 或传输结束、本地停止、`recover()` 时，未回应的卡一律置为 `void`；之后再批准返回 409。
- **鉴权**：只有 user 身份可以读取或回应卡；agent/worker 令牌返回 403，未登录返回 401，他人会话的卡返回 404。
- **敏感内容**：`details` 可能包含用户或模型的内容，按聊天内容管理，只通过上面的卡接口返回。事件只带 ID 和状态：`tool_approval.requested`（`tool_approval_id, invocation_id, method, thread_id, turn_id, item_id, status, expires_at`）、`tool_approval.status`（`from, to, decided_by`）、`tool_approval.delivery`（`delivery, decision_sent`）。
- **没有 gate 时**（直接调用适配器）：行为和以前一样，全部回 `decline`。
- **顺序**（2026-10-22 页面实测后补充）：建卡前，适配器先处理已排队的通知，再由 gate 调用 StreamSink 的 flush，因此卡片之前产生的输出一定排在 `tool_approval.requested` 之前。

### 限制

- 只在离线桩上验证。真实 app-server 在严格和监督模式下仍被准入拒绝，所以不会有 `accept` 发给真实 Codex。provider 是否会在每次工具操作前都先请求审批，属于 S3，**未验证**；本功能管的是"请求来了之后怎么答"，不能强迫 provider 先来问。
- SSE 只推送卡的 ID 和状态，`details` 需要页面另外调用卡接口读取。
- 仍未实现：中断（S4）、原生子 thread 的观测、用量事件、`stream_chunks` 与卡的保留期。

### 验证（本节）

新增 `tests/http/test_async_and_tool_approval.py`，共 22 项：
- 异步：202 立即返回且后台完成结算；拒绝路径保持同步、不启动；在途调用不会被重复启动；后台适配器异常记为 unknown 并保留名额；结算失败记为 unknown；`recover()` 跳过在途调用；关闭时等待在途调用、之后拒绝新调用且审批不被消费。
- 工具卡：批准只回答对应请求且回 `accept`，桩执行后输出 `ran`；事件顺序为 requested < status < delivery < 终态，事件和审计载荷里都没有 command；拒绝回 `decline` 且什么都不执行；过期回 `decline`，过期后不能补批；缺少或错误的 `card_hash` 被拒；401 / 403 / 他人会话 404；传输先结束时卡被置为 `void`，之后不能批准。
- 内核规则：provider 未提供 `accept` 时不能批准；8 路并发批准只成功一次；过期优先于迟到的批准；已结束调用的卡不能批准；卡的有效期受本轮时限约束；`recover()` 清理孤立的卡；没有建表的旧库不受影响；适配器内一张卡只回答它自己的请求。

另外按新规则改写了一项旧测试：没人回应的工具请求会过期并回复 `decline`；新增一项测试，确认没有 gate 时仍然全部回 `decline`。

Windows `.venv` 实际输出：新文件 **22 passed**，连续 4 次（约 5.7 秒）；全量 **618 passed in 51.76s**。

## 中断与原生子 Agent 观测（2026-10-22 续）

分支 `feat/interrupt-stop`。全程离线；真实中断（S4）未验证。

### 中断一个调用

| 接口 | 说明 |
|---|---|
| POST `/api/invocations/{id}/interrupt` | 仅限 user，权限归属规则与 `/run` 相同。只对 `starting/running` 的调用有效，否则返回 409 `not_active`。这**不是**停止整棵任务树，会话之后仍可以继续发消息 |

- 适配器向本进程正在驱动的回合发送 `turn/interrupt`（`threadId` + `turnId`）。服务端接受请求不等于已经停止。
- 调用继续由后台执行跟随。只有当**该回合自己的** `turn/completed` 报告 `interrupted` 时，`AdapterStartResult.outcome="canceled"` 才会生效，调用结算为 `canceled` 并释放名额。如果回合恰好自己正常结束或失败，则按实际结果结算。
- 返回值逐项说明当前知道的情况：`interrupt_requested`、`interrupt_accepted`（服务端已应答）、`terminal_received`（已收到终态并结算）、`remote_stop_confirmed`、`quota_retained`、`status`。最多等待 5 秒；没有收到终态时如实返回，调用之后会按超时等证据结算为 `unknown`，并保留名额，**绝不会**记为 `canceled`。
- **生效点**：Worker 在**向 provider 发送 `turn/interrupt` 之前**，在同一个事务里关闭本调用的工具审批（表 `tool_gate_closures`，每个调用一行），并写审计事件 `interrupt_requested`（`invocation_id, actor, repeat, closed_cards, blocked_decisions, in_flight, already_sent`）。这个事务提交的时刻就是生效点。之后 provider 的应答单独记为 `interrupt_acknowledged`（`accepted, code`）。两条事件都会在会话事件流中以 `task.*` 推送。
- **生效点之后，不再向 provider 发送任何工具决定**：
  - 待决卡置为 `void` / `not_sent`；
  - 已决定但尚未发送的，保留用户原来的决定（例如仍为 `approved`），但 `delivery=blocked`，不会被送出；
  - 之后新到的请求也会记一张卡，但状态为 `void` / `blocked`，没有人能回答；
  - API 批准或拒绝返回 409 `tool_decisions_closed`。
- **发送前认领；关闭后不再授予新的认领**：适配器在写出每个决定之前，都要先向 gate 认领（`claim`）。认领在写锁下进行：认领成功则 `delivery=sending`，并返回 `accept`（仅对 `approved`）或 `decline`；如果决定已经关闭，则改为 `blocked`，返回 None，什么都不发。生效点与认领互相串行，谁先发生，记录里就写谁。
- **关闭前已认领的，可能仍会发出**：关闭前已经认领（`sending`）的决定，在关闭之后仍可能被写出，也无法撤回。它们计入 `in_flight`，记录里**不声称**已经写出，也**不声称**可以撤回；最终是否发出，以该卡的 `delivery` 为准。`sent` 只表示适配器的写操作已经返回，不代表 provider 已经收到或执行；已经 `sent` 的计入 `already_sent`，不改写。
- **没有卡片的回答也要经过关闭门禁**：建卡失败、请求不属于本回合、或请求方法不受支持时，适配器只有在 `may_answer()`（同样在写锁下读取关闭记录）确认本调用仍然开放时，才发送自动 decline 或 -32601。关闭之后，或关闭状态无法读取时，**什么都不发送**，请求记入执行记录的 `unanswered_requests`，之后也不会再被当作新请求处理。这修复了两个缺陷：一是被拒绝发送的请求会被重新处理，退回到自动 decline；二是建卡失败时直接自动 decline。两者都会导致生效点之后仍发出 decline。
- **重复中断**：每次都写事件（`repeat=true`）并再次发送 `turn/interrupt`；关闭记录只有一行；调用只结算一次。
- **完成与中断竞争**：如果回合在中断到达时恰好正常结束，就按实际结果结算为 `succeeded`，而不是 `canceled`。如果适配器不支持中断，请求同样会关闭工具审批，但不声称任何结果，也不把调用标为 unknown，调用之后照常结束。
- 整树停止和本地停止也是生效点（`reason=tree_stop` / `local_stop`）。
- 整树停止（`/api/tasks/{id}/stop`）遇到本进程在途的调用时，走同一条中断路径，由后台执行负责结算，不会重复结算；没有收到终态时，按原有规则记为 `unknown` 并保留名额。
- 中断时还在等待决定的工具卡会被置为 `void`（`not_sent`），之后不会再回答它们。
- 没有活动回合时（例如进程重启后），通用取消仍返回 `unsupported`。

### 原生子 Agent 观测

依据本机的 schema 记录（`var/codex-schema/ServerNotification.json`）：`ThreadItem` 中的 `collabAgentToolCall` 带有 `tool`（`spawnAgent` 等）、`receiverThreadIds`、`status`；`subAgentActivity` 带有 `agentThreadId`、`kind`。

- 本回合中出现这两类 item 时（`item/started` 或 `item/completed`），会发出 `stream.native_agent_observed`，载荷为 `item_type, tool, item_id, status, child_thread_ids, ended_child_thread_ids`（最多 16 个 ID），并写入执行记录的 `native_agents`。collab 调用的 `prompt` 和 `agentsStates[*].message` 都不会被复制。
- **结束证据**（2026-10-22，用户决定）：只有子 Agent **自己**的状态才算可信的结束证据，包括 collab 调用 `agentsStates[child].status` 为 `completed / interrupted / errored / shutdown`，或 `subAgentActivity.kind` 为 `completed / interrupted`（契约常量 `NATIVE_CHILD_END_STATES`）。`spawnAgent` 调用本身 `completed` 只表示派生成功；`notFound` 含义不明。这两种都不算结束证据。
- **规则**：父回合结束时，只要有任何观测到的子 Agent 没有结束证据，无论父回合报告的是什么，调用都记为 `unknown` 并**保留原有名额**，原因文字中列出这些子 thread，并说明“not counted in Hub quota”。父回合的实际终态单独保存：执行记录中的 `parent_turn`（`turn_status, verdict, terminal`）、`native_children`（每个子 thread 最新的自身状态）、`native_unfinished`，以及 `stream.turn_ended.native_unfinished`。这种情况下不生成助手回复，也不记录可续接的 `native_session`。
- **不虚构**：不声称子 Agent 已停止，不把子 Agent 计入 Hub 配额，也不去控制它们。这只是一条保守的结算规则，不是子 Agent 控制系统。

### 验证（本节）

新增 `tests/http/test_interrupt_and_native.py`，共 8 项：
- 中断被终态确认后结算为 canceled、释放名额，事件顺序正确，发出的 `turn/interrupt` 参数正确；
- 中断已被接受但没有终态时，不声称任何结果，最终为 unknown 并保留名额，原因文字写明中断未被确认；
- 鉴权：401 / 403 / 他人会话 404，已结束的调用返回 409；没有活动回合时适配器返回 `no_live_turn`；
- 整树停止：有终态时确认 1 个且只结算一次；没有终态时确认 0 个、保留名额；
- 原生 `spawnAgent` 被观测到，事件和 chunk 中都没有 prompt 文本，调用照常为 succeeded；
- 等待工具卡期间中断：卡片被置为 void、不发送任何回答，部分输出作为回复保存。

另外修复了离线桩的一个问题：等待卡片期间收到中断后，桩不再继续阻塞读取。

续（竞态与子 Agent 规则）：新增 `tests/http/test_interrupt_races.py`，共 17 项，连续 3 次通过：
- 中断未确认期间批准或拒绝卡片，返回 409 `tool_decisions_closed`，provider 什么都没收到；
- 中断前已批准、尚未发送的决定被拦截（保留 `approved` / `blocked`），桩没有收到 accept；
- 中断前已送达的决定如实记录（`already_sent`），不被改写；
- 重复中断：两次请求、两次 `turn/interrupt`、一行关闭记录、只结算一次；
- 正常完成与中断竞争时，结算为 succeeded；
- 不支持中断的适配器也会关闭工具审批，但不声称结果，调用照常完成；
- 整树停止先关闭工具审批；
- gate 规则：只写关闭记录、未清扫卡片时认领也被拦截；关闭前已认领的计入 in_flight；关闭后新到的请求被记录但无法回答；
- 子 Agent：没有结束证据时为 unknown 并保留名额，`parent_turn` 记录父回合实际的 completed，不生成回复；子 Agent 有自身 completed 时正常结算；只有子 Agent 自身的状态才算证据（5 种情形，message 和 prompt 都不复制）。

这次按新规则调整了几项已有测试：`stream.turn_ended` 多了 `native_unfinished`；送达状态多了 `sending`；中断事件拆成了 requested 和 acknowledged 两条；本地停止后再批准返回 `tool_decisions_closed`；原生 spawn 没有结束证据时为 unknown。

收尾（同一文件，增至 24 项，连续 3 次通过）：
- “认领 → 暂停 → 关闭 → 恢复发送”的确定性测试：认领后让适配器停在写出之前，此时关闭；关闭结果计入 `in_flight`，卡片保持 `sending`（不改写成 sent，桩也还没有收到）。恢复后决定照常写出（桩收到 accept），卡片才变为 `sent`。
- “关闭后的新请求 + 建卡失败”：没有发出任何响应（桩没有收到 srv-1 的回答），请求记入 `unanswered_requests`；对照组是调用仍开放时建卡失败，照常自动 decline。
- 适配器单元测试：gate 没有 `may_answer`、`may_answer` 抛异常、或已关闭这三种情况下，对本回合请求和未知请求都不发送任何内容，也不会再处理同一请求；`may_answer` 能反映关闭状态，数据库不可用时返回 False。
