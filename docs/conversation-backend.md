# 会话后端（2026-10-21）

**2026-10-24 契约补充**（详见[多 Agent 对话](multi-agent.md)）：
- `ConversationTurn` 新增 `handoff_message_ids`：只接受同一会话中的 Agent 回复，最多 3 条。
- `InvocationSnapshot` 新增 `handoff`：记录每段来源，被交接的文本已写入 `task_input`；没有交接时哈希不变。
- 回合视图新增 `handoff` 和 `sent_text`；有交接时，`sent_text` 是完整的发送内容。
- `adapter_binding(adapter, agent_id)` 在路由器下取该 Agent 适配器的身份。

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

## S6-1：app-server 加固（2026-10-23）

依据 [S6 规划](appserver-s6-plan.md)（修订 1）第 3–6 节。全部离线完成，**没有经过 Hub 的真实运行**。

### 术语（统一用法）

“门禁”只作总称。具体的检查各用下面一个名字，本节和 S6 规划都按这个用法：

| 名称 | 在哪里 | 含义 |
|---|---|---|
| 准入 | `execution_admission`，Worker 认领时和适配器启动时 | 适配器代码声明能否启动：strict、supervised、simulation 或 blocked |
| 身份复核 | Worker 认领时，消费门禁之前 | 比较快照记录的 `adapter_version` 和当前适配器的绑定身份，双方比较 |
| 消费门禁 | `PolicyService.consume`，同一事务 | 原子地消费审批、预留名额。之前的任何拒绝（准入、身份复核、续接检查）都不消费审批 |
| 工具决定关闭 | `close_decisions`，中断或停止请求的同一事务 | 写入关闭记录的时刻就是生效点。之后不再授予认领，也不发送任何工具决定。没有关闭记录 = 工具决定未关闭 |
| 认领 | `ToolGate.claim`，写出前 | 唯一的发送路径。作废卡、未决卡和工具决定已关闭时都认领不到 |

### 准入与固定身份（D7）

- 固定身份是 `agenthub_adapters.appserver_adapter.PINNED_APPSERVER`，取 alpha.11 的值，与 exec 的 `PINNED_CODEX_*` 分开，不读取环境变量。
- `build_adapter()` 用 `CodexAppServerFactory.pinned(...)` 构造 factory。如果设置了 `AGENT_HUB_CODEX_PATH`、`AGENT_HUB_CODEX_SHA256`、`AGENT_HUB_CODEX_SIZE` 或 `AGENT_HUB_CODEX_VERSION`，它们不会被使用，而是记入 `env_overrides`，监督准入因此被拒。
- 严格模式继续返回 `appserver_real_unverified`。
- 监督模式在以下条件**全部**满足时返回 `user_supervised_boundary`，否则返回 `appserver_supervised_conditions`，原因中列出未满足的项：
  - factory 的类型恰好是 `CodexAppServerFactory`；
  - 路径、哈希、大小、版本都等于常量；
  - 没有环境变量覆盖；
  - 文件存在，大小一致；
  - argv 为 `app-server --stdio --disable multi_agent`；
  - 设置了代理，CA 可读；
  - 时限大于 0 且不超过 600 秒；
  - 不能回答 accept。
- 准入只做低成本的检查，因为它在写锁内执行。启动前，factory 再检查一遍上述条件，并完整计算 SHA-256；不符就抛出 `LaunchRefused`，不启动进程，调用判 failed。
- 批准时仍需显式同意 `accept_unmanaged_native_agents: true`。

### 身份绑定（D11）

- 适配器的 `binding_identity()`：
  - 真实 app-server：`codex-app-server@<版本>;sha256=<哈希>;size=<大小>`；
  - 内存桩：`appserver-stub:in-memory`；
  - stdio 桩：`appserver-stub:stdio`。
- 会话提交、`POST /api/invocations`、子任务提议这三处，把它写入快照的 `adapter_version`，因此参与 `payload_hash`。没有声明绑定的适配器（Mock、exec）为 `"unknown"`，旧快照的哈希不变。声明了却取不到值的为 `"unavailable"`。
- **身份复核**（消费门禁之前，比较双方）：快照记录的值必须等于当前适配器的值。
  - `"unknown"` 只等于 `"unknown"`；`"unavailable"` 永不放行；
  - 当前适配器没有绑定接口时同样比较，不会跳过；
  - 不一致就返回 409 `adapter_identity_changed`，审批保持原状，启动 0 次。
  - 评审修正：原实现只在当前适配器有绑定接口时才比较。把 app-server 下批准的调用切换到 exec 的 Fake runner，就会被消费并启动一次。
- 纵深防御：app-server 适配器在 `start_streaming` 中再比较一次；exec 适配器拒绝 `adapter_version` 不是 `"unknown"` 的快照。
- 重试复制原快照，不更换身份；重新固定之后，旧调用需要重新提交。
- `describe()` 增加 `binding_identity` 字段。

### 固定设置与回读（D2）

- `thread/start` 发送 `{cwd, sandbox:"read-only", approvalPolicy:"on-request", approvalsReviewer:"user"}`。
- `thread/resume` 在此基础上加 `threadId`。
- `turn/start` 另带 `approvalPolicy`、`approvalsReviewer` 和 `sandboxPolicy:{type:"readOnly", networkAccess:false}`。
- 线程请求的应答按 S3–S5 的规则回读：reviewer、policy、sandbox 对象和 cwd。任何一项不符，就判 failed（`appserver_settings_mismatch: <项>`），不发 `turn/start`，释放名额。

### 工具审批（D1、第 4.4 节）

- 真实 factory 的适配器 `accept_enabled=False`：
  - 卡片的 `available_decisions` 为 `["decline"]`，provider 的原始列表放在 `details.provider_available_decisions`；
  - API 的批准请求因此返回 409 `accept_not_offered`；
  - 页面上不显示“批准”按钮，并提示“这一阶段只能拒绝”；
  - 即使数据库里出现伪造的 approved，适配器也改发 `decline`，并把卡片记入 `accept_suppressed`。
- 离线桩仍可回答 accept，以覆盖现有的往返测试。
- `_fit_decision` 只会返回 `accept` 或 `decline`，**从不返回 `cancel`**。可选列表里没有 decline 时，照样发 `decline`（S3 的实测结论）。
- 唯一的发送路径仍是发送前的认领（`ToolGate.claim`）：
  - 作废卡永远不发送；
  - 工具决定关闭之后什么都不发；
  - 过期卡在工具决定未关闭时发一次 `decline`。
- 卡片详情新增：
  - `command_view`：把 `command` 按 argv 拆开，PowerShell 或 cmd 包装显示为外层程序加内层命令。只用于展示，不作为安全依据。2026-10-23 修订：改为只接受 `shlex_join` 会产生的形式的严格拆分（`split_quoted_argv`）。未加引号的反斜杠、shell 语法或非 ASCII 字符、引号不配对，都判为 `parsed: false`，页面只显示原文（见 [会话页面](chat-frontend.md)）。
  - `reason_source: "model"`：页面把理由标为“模型提供的说明”。

### 工具活动（D4）、中断（D5）、事件绑定（D8）、后代进程（D10）

- **工具活动**：观测 `commandExecution`、`fileChange`、`mcpToolCall`、`dynamicToolCall` 类型的 item，以及这些类型的 item 级通知（`item/<类型>/<事件>`，例如 `item/commandExecution/outputDelta`）。
  - 每个 item 记录：类型、ID、是否报告了 processId、是否见过生命周期事件（`lifecycle_seen`）、输出通知的条数（`output_events`）、是否完成。不保存命令内容和输出原文。
  - 评审修正：原实现忽略输出通知。只有输出、没有 item 生命周期时，现在也记为一条活动，没有完成证据，所以状态为 unknown。
  - 执行记录增加 `tool_activity` 和 `tool_process_state`（`none / items_completed / unknown`），`stream.turn_ended` 也带 `tool_process_state`。
  - 状态为 unknown 时，原因文字写明“tool process state unknown, not claimed stopped”。
- **中断**：`turn/interrupt` 应答超时时，返回 `reason: "not_yet_acknowledged"`（页面显示“服务端尚未答复；以回合终态为准”），回合仍按终态结算。
- **事件绑定**：计入本回合的事件必须带与本 thread 一致的 `threadId` 和本回合的 turn ID；缺少 threadId 的事件不再计入。终态之后已收到的绑定事件计入 `late_events`，包括与终态同一批取出、排在终态之后的事件，以及结算时队列中剩下的事件；迟到事件不计入本回合的工具活动或用量。评审修正：原实现会丢弃同一批中排在终态之后的事件。
- **后代进程**：stdio 传输在关闭前后各读一次进程表，真实 factory 宽限 3 秒，结果记入 `descendants`（`before_close`、`after_grace`）。只记录，不结束任何进程。

### 原生续接（D6、D9）

- 首次调用的 cwd 是 `workdir_root/<invocation_id>`，执行记录中写入 `workdir`。续接时，从执行库中找出同一 thread 最近一次开始过回合的记录，取回其 cwd 和 `turn_id`。
  - 没有这样的记录，或者目录已不存在，就拒绝续接，0 次启动，不改用新目录。
  - 恢复的应答必须包含上一个回合，否则判 failed（history not confirmed），不发 `turn/start`。
- 恢复时的用量重放按 S5 修订后的规则处理：只接受一条，带本 thread 和上一个回合的 ID，在 `turn/started` 之前到达，记入 `replayed_usage`，不算本回合的用量。其余的重放计入 `usage_anomalies`。
- app-server 和 stdio 桩的 stderr 日志写到 `workdir_root/_logs/`，cwd 保持为空。

### S6-3 的离线准备（2026-10-23）

- **适配器的审计字段**：执行记录新增以下字段，都只有 ID、哈希和布尔值，不含文本。
  - `opening_method`：实际发出的是 `thread/start` 还是 `thread/resume`；
  - `input_sha256`：`turn/start` 实际发送的输入的 SHA-256；
  - `resume_check`：恢复应答中返回的回合数、上一个回合是否存在、它的状态、其中是否有 userMessage；
  - `turn_id_reused`：续接后的回合是否复用了上一个回合的 ID。复用时，这个回合的事件和上一个回合的用量重放无法区分，原因文字会写明“usage not attributable”；
  - `process_id`：本次 app-server 进程的 PID，供驱动确认它已经退出。
- **后代进程判定的修正**：
  - Windows 会复用 PID，进程的父 PID 在父进程退出后也不会更新。离线时观察到一个无关的托盘程序（`acrotray.exe`），它早已退出的父进程的 PID 恰好被新的桩启动器复用了，于是被误算成后代，并被判为“残留”。
  - 现在只有当子进程的创建时间不早于父进程时，才算父子关系；判断“残留”时，还要求 PID、映像名和创建时间都一致，被新进程复用的 PID 不算残留。读不到创建时间时，按保守处理保留这条关系。
  - S6-2 的真实运行中，后代名单前后都为空，不受影响。S4、S5 探针中的同名函数有同样的弱点，但它只会多报、不会漏报，而且当时的名单都为空，所以没有改动已收口的 S4、S5 代码。
- **stdio 桩**：
  - 工厂的关闭宽限期设为 3 秒，与真实 factory 一致。venv 的 python.exe 是一个启动器，真正的解释器是它的子进程，关闭后可能还在退出中。
  - 新增一个“thread 存储”状态文件，放在 `_logs` 下、cwd 之外，让后一个桩进程能返回前一个回合（包括 userMessage），用来离线模拟冷恢复。
  - 新增场景：`recall`、`recall-forgetful`、`recall-tool`、`recall-native`、`plant-tool`、`recall-same-turn-id`、`recall-wrong-thread`、`recall-replay-twice`。

### 离线桩

- 线程应答回显所请求的 cwd，并且在请求 read-only 时，把 `sandbox` 报告为对象。
- 恢复时返回本 factory 在该 thread 上发出过的回合，并发送一条上一个回合的用量重放。stdio 桩通过环境变量获得回合 ID 和历史。
- 新场景：`approval-default`（默认决定列表、包装后的命令）、`settings-mismatch`、`resume-no-history`、`replay-twice`、`replay-other-turn`、`replay-after-start`、`tool-unfinished`、`tool-output-only`、`unbound-delta`。

### 验证（本节）

新增 `tests/http/test_appserver_s6.py`（65 项，连续 3 次通过），覆盖准入矩阵、D11、设置回读、只拒绝的卡片规则、`_fit_decision`、命令拆分、工具活动、中断应答超时、事件绑定、后代进程记录、续接与重放，以及文案。

变异检查：以下 5 个变异体都被这些测试发现，之后已还原：
- 恢复 cancel 回退：6 项失败；
- 去掉消费前的身份复核：1 项失败；
- 去掉 accept 抑制：1 项失败；
- 允许缺少 threadId：1 项失败；
- 跳过设置回读：1 项失败。

全量测试（Windows `.venv`）：`python -m pytest -o addopts='' -q -p no:cacheprovider`，结果 **1042 passed in 200.99s**。浏览器在 demo 离线桩上走完“发消息 → 批准 → 工具卡 → 拒绝 → 完成”，卡片显示命令原文、拆开后的命令，理由标为“模型提供的说明”。没有执行 Codex，没有模型调用。

评审修正之后，同一文件增至 70 项，连续 3 次通过。新增的 5 项回归在修复前的代码上都失败：
- 切换到 exec 适配器后被拒，exec 自身也拒绝；
- `"unavailable"` 永不放行；
- 只有工具输出通知时状态为 unknown，不保存原文；
- 同一批中终态之后的事件计入 `late_events`（未路由请求和断线两条路径各一项）。

按新规则调整的已有测试：
- `stream.turn_ended` 多了 `tool_process_state`；
- 直接调用适配器时，快照需要带绑定身份；
- 真实 factory 在监督模式下的拒绝码改为 `appserver_supervised_conditions`；
- `thread/resume` 的参数多了 cwd 和固定设置。

## T-1a：用量归一化与账本（2026-10-24，离线）

依据 [T-0 基线](t0-usage-baseline.md) 第 3、5、6、7 节，修复缺陷 D-T0-1 到 D-T0-7。分支 `feat/t1-usage-ledger`，全程只用离线桩，没有真实调用。

### 契约 `agenthub.usage`（新模块）

- `UsageRecord`：`mapping`、`mapping_version`、`completeness`、`source`，以及 `input_total`、`input_uncached`、`cache_read`、`cache_write`、`output_total`、`reasoning`，再加 `notes`、`raw`、`model`。
  - 不知道的数字是 `None`，永远不写成 0。
- `normalize_usage(report)`：按映射版本 1 计算：
  - Codex：输入已含缓存，输出已含推理；`total` 不等于输入加输出，或者缓存比输入还大，记 `mapping_mismatch`；`cacheWriteInputTokens` 不为 0 时记 `mapping_unverified`，不参与减法。
  - Claude Code：总输入 = `input_tokens` + 缓存写 + 缓存读；缺少缓存字段时，总输入为 `None`，并记 `cache_fields_missing`。
  - exec：一律带 `exec_usage_semantics_unverified`。
- 完整性由说明代码决定：`UNKNOWN_NOTES` 中的任何一个都判为 unknown，此时数字全部清空；`PARTIAL_NOTES` 中的任何一个都判为 partial。
- `notes` 只能是机器代码，正则为 `[a-z0-9_.:-]{1,64}`，其余一律替换为 `invalid_note_dropped`。`raw` 只保留数字和嵌套数字；字符串只保留 `service_tier` 这种短枚举值。
- `not_launched_usage()`：适配器确认没有启动时的已知零值。
- 适配器的可选扩展 `usage_report(invocation_id)`，说明写在 `contracts.py` 的扩展注释中：
  - 返回 `None` 表示适配器没有这次调用的记录；
  - 提供这个方法的适配器必须在启动之前就写下记录。

### app-server：一个回合的用量（D-T0-1、4、6、7）

- 新模块 `agenthub_adapters/appserver_usage.py`。本回合用量 = 回合结束时 thread 的 `total` − 回合开始前的 `total`：
  - 新 thread 从 0 开始；
  - 续接从那一条重放的 `total` 开始。
- 各条 `last` 之和作为第二种推导：
  - 两者一致时为 complete；
  - 不一致时记 `last_sum_mismatch`，取 `total` 的差；
  - 续接时没有可用的重放，记 `resume_baseline_unknown`，取各条 `last` 之和；
  - `total` 变小时记 `total_decreased`，取各条 `last` 之和。
- 同一 `total` 的更新重复投递只算一次（`duplicate_usage_update`）。格式不对的更新跳过（`usage_update_malformed`）。
- 终态之后才到的、属于本回合的用量：
  - 照常计入 `late_events`；
  - 同时保留并计入用量，记 `late_usage`，判为 partial。
- 不再读取 `turn/completed.turn.usage`（schema 中没有这个字段）。
- 执行记录新增 `usage_source`、`usage_notes`、`usage_updates`（只有数字）、`replayed_usage_total`、`native_child_usage`、`model`。`usage` 现在是推导出的本回合用量。
- 子 thread 的用量通知记入 `native_child_usage`，仍然计为异常，不计入父回合。只要观测到子 Agent，就记 `native_children_not_metered`。
- 其他情况：
  - 没有发 `turn/start` 就被拒绝：已知零值（`no_turn_requested`）；
  - 回合没有终态：记 `turn_not_terminal`；
  - 续接复用了上一回合的 ID：记 `turn_id_reused`，判为 unknown。
- T-1a 之前的执行记录只有最后一次请求的用量，按 `legacy_last`（partial）报告，不重新推导。
- 桩：普通回合改为在 `turn/completed` 之前发 `thread/tokenUsage/updated`，`total` 跨续接累计；被中断的回合不发。新场景：
  - `usage-multi-request`、`usage-missed-update`：S3 第三次的两组真实数字；
  - `usage-duplicate`、`usage-none`、`usage-late`、`usage-cache-write`、`native-spawn-usage`。

### Claude Code 与 exec（D-T0-5）

- Claude Code：执行记录新增 `usage_raw`（整个 `usage` 的数字部分，包括 `server_tool_use`、`cache_creation` 和 `service_tier`）以及 `model_usage`（`modelUsage` 的数字部分）。原来的 4 个字段保留不变。
  - `ClaudeLaunchFailed` 记 `launched: false`，报告已知零值；
  - 有多个模型时记 `multi_model_usage_unverified`。
- exec：同样提供 `usage_report`，至多为 partial。

### 内核账本（D-T0-3）

- 新表 `usage_ledger`（`UsageLedgerRow`），以 `invocation_id` 为主键，带 `root_task_id`、`task_id`、`agent_id`、`conversation_id`。启动时 `create_all` 增量建表，不改旧表。
- 写入点 `agenthub_kernel/usage_ledger.record_usage`，与结算在同一个事务中：
  - Worker 的正常结算，不论有没有回复；
  - 启动时抛出异常（`adapter_start_raised`）；
  - 并发停止已标为 unknown 的分支；
  - `recover()` 的已结算和 unknown 分支；
  - 重启补记 `restore_usage`，摘要中返回 `usage_restored`。
- 只能提升，不能降级：unknown → partial → complete，已知的不会被更少的覆盖。数据相同时不重写。每次调用最多一行，所以 SSE 重放、重启补回、重复结算都不会重复入账。
- 在本进程中，适配器刚给出确定的拒绝，又没有记录：判为已知零值（`not_launched`）。重启之后不做这个推断。
- 视图：
  - `turn_view` 增加 `usage` 字段：账本行的公开形状，不含 `raw`；
  - 已结算但没有账本行时，返回 `not_recorded`（unknown），不返回 0；
  - 没有结算时为 `null`。
- `GET /api/conversations/{cid}/usage`：
  - 按调用、按 Agent 和合计返回；
  - 合计只加已知的数字，同时给出 complete、partial、unknown 各有多少次调用，以及每个字段缺失的调用数；
  - 鉴权与其他会话接口相同：未登录 401，Agent token 403，他人的会话 404。
  - 根任务的合计（`root_task_usage`）也是对账本的查询，暂时没有对外接口。
- 事件流**没有**新增事件类型。页面在调用进入 succeeded、failed、canceled、timed_out 或 unknown 之后，以及读取消息之后，重新读取 `/usage`。

### 页面（D-T0-2）

- 用量一律按归一化后的口径显示，例如：
  - “输入 1,798（缓存读 1,161 / 写 635）/ 输出 15”；
  - 不完整时附上原因：“（不完整：按各次请求相加；续接起点未知）”；
  - 未知时显示“未知（provider 未报告用量；回合未正常结束）”。
- 调用卡在结算后都会显示用量，没有回复的调用也一样；回复行显示同一个值。

### 未做与未验证

- 预算、准入预留和审批卡上的估算（T-1c）；按根任务和项目的页面汇总（T-1b）；金额。
- 真实验证（T-1h 单独申请）：
  - 重放中的 `total` 就是上一回合结束时的累计：离线由两种推导互相印证，真实情况只有 `last` 的记录；
  - 多请求回合经过 Hub 的入账；
  - Claude Code 的账本口径。
- 在 T-1a 之前就已结算、又不在执行库中的调用，补记时为 `no_adapter_record`（unknown）。

### 验证

- 新测试共 60 项，全部离线：
  - `tests/test_usage_normalization.py`：28 项，用 T-0 的真实数字作为测试数据；
  - `tests/adapters/test_usage_reports.py`：19 项；
  - `tests/http/test_usage_ledger.py`：13 项。
- 浏览器在 `t1-usage-demo` 离线桩（端口 8175）上核对：
  - Codex 调用卡和回复都显示“输入 30,539（缓存读 24,832）/ 输出 79”，修复前只会显示最后一次请求的值；
  - Claude Code 桩显示“输入 12 / 输出 3”；
  - 控制台没有错误。
- 全量结果见交接文档和提交说明。

## T-1b：用量视图（2026-10-24，离线）

所有数字都是对 `usage_ledger` 的查询，不另外记账。合计只加已知的数字，同时给出不完整、未知的调用数，以及每个字段缺失的调用数；未知不计为 0。已结算但没有账本行的调用按 `not_recorded`（未知）计入。

- **根任务**：`root_task_usage` 读取整棵任务树的全部调用，包括没有聊天消息的子任务。返回：
  - 合计；
  - 按 Agent 的分组；
  - 按任务的分组：`task_id`、`parent_task_id`、`depth`、`agent_id`、`kind` 和该任务的合计，不返回任务目标原文。
- **会话**：`GET /api/conversations/{cid}/usage` 新增 `root_task` 字段。会话的 `total` 只含本会话的聊天调用，`root_task.total` 是整棵树的合计。
- **项目（本实例）**：`GET /api/usage/summary?since=&until=`。
  - Hub 还没有项目实体：方案 §5 的 `projects` 表尚未实现，所以“项目视图”是当前用户在本 Hub 实例中的全部会话。实际部署中每个实例有自己的数据目录。等项目实体实现后再按项目划分。
  - 只读取调用者自己的会话（`ConversationRow.user_id`）所属的根任务。其他用户的数据，以及不经会话、直接在旧控制台创建的任务，都不计入。
  - 返回合计、`by_agent`、`by_day`、`by_conversation`（会话 ID、标题、合计），以及 `scope_note`、`day_basis`。
  - 日期按结算记录时间的 UTC 日期；已结算但没有账本行的调用，按完成或创建时间。`since`、`until` 都包含当天；`since` 晚于 `until`，或者日期格式不对，返回 422。
  - 鉴权：未登录 401，Agent token 403。
- **页面**：
  - 会话上方显示“本会话用量”。有多个 Agent 时分行列出；子任务也有消耗时，另起一行“根任务（含子任务调用）”。
  - 左栏显示“全部会话 · 近 7 天”：合计和按 Agent 的分组，并注明“本 Hub 实例，未区分项目”。
  - 文案示例：“已知 输入 32,337（缓存读 25,993 / 写 635）/ 输出 94 · 3 次调用（1 次不完整，另有 1 次未知，1 次缺少输入数）”。
  - 调用结算、读取消息、读取会话列表之后都会重新读取。

### 未做

- 项目实体与按项目的划分（需要先实现方案 §5 的 `projects`）；
- 页面上的按日明细（接口已经提供 `by_day`）；
- 金额；
- 预算（T-1c）。

### 验证

- `tests/http/test_usage_views.py` 共 8 项，覆盖：
  - 根任务包含子任务调用，会话视图不包含；
  - 只计入本用户的会话；
  - 按 Agent、按天、按会话的分组之和等于合计，重复执行 recover 后不变；
  - 未知和未记录的调用都计入次数，不算 0；
  - 日期过滤与 422；鉴权；页面元素。
- 浏览器在 `t1-usage-demo` 离线桩上核对：会话栏和左栏的数字与账本一致；修正了一处页面缺陷（`replaceChildren` 把 null 显示成文本）；重新加载后没有失败的请求。

## T-1c：token 预算与原子准入（2026-10-24，离线）

### 策略字段（`Policy`，契约变更）

- `token_budget_total`：根任务的总预算（整棵任务树），单位是账本 token，即 `input_total`（含缓存读写）+ `output_total`。`null` 表示不限。
- `token_budget_per_call`：单次预算，也是每次调用对总预算的**预留量**。
  - 与需求中“单次输入预算”的差异：这里同时包含输出，因为总预算也同时计算输入和输出。
  - **不是硬上限**：没有适配器能在第 N 个 token 处停下回合，超出的部分只能在事后记为 `overrun`。
- `token_budget_warn_percent`：提醒阈值，1–100，默认 80。
- `token_budget_mode`：`strict`（默认）或 `soft`。
- 旧策略没有这些字段时，按不限处理，行为与以前完全相同。
- 修改预算走现有的 `PATCH /api/tasks/{id}/policy`：版本号加 1，并撤销这棵树中未消费的审批。字段整体替换，省略即清空。不合法时返回 422，不写入，也不升版本：
  - 不是正整数；
  - 单次预算大于总预算；
  - 模式不是 strict 或 soft；
  - 阈值不在 1–100 之间。

### 规则（`agenthub.budget`）

- **估算**：只估 Hub 自己发送的文本：ASCII 约 4 个字符算 1 token，其他字符每个算 1 token。provider 侧的指令、工具定义和 thread 历史一律标为“未知”，不并入估算，也不拿 T-0 的历史观测值当预估。
- **两种模式都会拒绝**：
  - 估算已经超过单次预算（`estimate_exceeds_per_call_budget`）；
  - 已知用量 ≥ 总预算（`token_budget_exhausted`）。
- **严格模式另外拒绝**：
  - 该 Agent 不报告用量（`budget_unmeterable`，没有 `usage_report`）；
  - 树中有用量未知、又没有预留可抵的调用（`budget_unknown_unbounded`）；
  - 设了总预算却没有单次预留量（`budget_needs_per_call`）；
  - 已知 + 未结算 + 在途 + 本次预留 > 总预算（`token_budget_exceeded`）。
- **软模式**：除了上面两种情况都放行，并给出提醒：
  - `unmeterable`、`unknown_unbounded`；
  - `unreserved_in_flight`：有预算开启前已获准、仍在执行、没有预留的调用（见下文“策略切换修补”）；
  - `reservation_is_estimate`：没有单次预算时，按估算预留；
  - `projected_over_budget`、`warn_threshold`。
- **结算**（`settle`）：
  - complete：按已知数字计；
  - partial：已知部分，再加上预留中尚未被已知部分用掉的余量（记为未结算）；
  - unknown：整笔预留都保留为未结算，**不归零**；
  - 没有预留又未知的调用，记为 unbounded。

### 内核（`agenthub_kernel/budget.py`）

- 新表：
  - `tree_token_budget`：每棵树一行，记 known、unsettled、reserved、unbounded；
  - `token_budget_items`：每次调用一行。
  - 用新表而不是在 `tree_quota` 上加列，因为 `create_all` 只建新表、不改旧表。
- 计数器在第一次需要预算时，按树中已有的账本行惰性建立，所以设置预算之前的用量也计入。
- **准入**在 `PolicyService.consume` 中进行：先占名额，然后做预算判断和预留，最后才消费审批。
  - 预留是一条条件 UPDATE，WHERE 中写明总预算，两个调用不能一起越过同一笔余量。
  - 拒绝时**先回滚**再抛出 `QuotaError`：Worker 在 QuotaError 后会提交，用来保存拒绝的副作用；不回滚的话，已占的名额会被提交。
  - 结果：不占名额、不预留、审批保持 approved、适配器启动次数为 0，接口返回 409 和拒绝码。
- Worker 把“该 Agent 是否报告用量”（适配器有没有 `usage_report`）传给 `consume(metered=...)`。
- **结算**：`record_usage` 写账本时，在同一事务中调用 `settle_budget`，在途预留转为按账本计的用量。之后账本行变好时，按差额重新结算。计数器始终等于各调用行之和。重启后，`recover()` 把遗留的在途预留转为未结算。
- **视图**：
  - `turn_view.budget`：审批卡用。包括策略、本次估算、`provider_context: "unknown"`、树的已知、未结算、在途、unbounded 和剩余，以及本次调用的预留与结算结果；未准入时附带 `preview`，即现在执行会得到的判断。`preview` 假定该 Agent 报告用量，实际在执行时原子地重新判断。
  - `/api/conversations/{cid}/usage` 增加 `budget` 字段，含当前策略，供页面表单使用。
  - `describe()` 增加 `usage_reporting`。

### 页面

- 审批卡显示：
  - 预算行：模式、总预算、单次预留、已知、未结算、在途、unbounded、剩余；
  - 本次估算，并注明 provider 侧上下文未知；
  - “按当前预算，执行时会被拒绝：…”，或者“执行时预留 N token；提示：…”；
  - 严格模式下，Agent 不报告用量时另有提示。
- 已执行的调用显示预留、已知实耗、未结算，以及是否超出单次预留。
- 会话用量栏增加预算行和预算表单（总、单次、提醒、模式）。保存会生成新的策略版本，并撤销未消费的审批。表单在输入时不会被重绘覆盖。
- `task.policy_updated` 显示为“策略已更新为 vN（配额或预算变更）；撤销了 M 个未消费的审批”，并重新读取用量。

### 未做与未验证

- 没有硬上限：需求中的“硬限制”只在适配器支持并验证了输出或总量上限时才能显示。目前两家都没有，所以单次预算只能事后标为 overrun。
- 估算不使用分词器（T-1d 可以按 provider 增加）；金额预算未做。
- 用户怎样处理 unbounded（用量未知又没有预留的调用）：目前只能切换为软预算，或者等 recover 补上用量。没有“人工确认用量”的入口。
- 真实 Codex 和 Claude Code 上的预算准入未验证（T-1h）。

### 验证

- `tests/test_budget_rules.py` 共 33 项：估算、各种拒绝码、软模式提醒、结算（unknown 不归零）、策略校验、旧策略兼容。
- `tests/http/test_budget_admission.py` 共 17 项，覆盖：
  - 拒绝时启动次数为 0、审批未消费、不占名额、`total_invocations` 不变、计数器不变；
  - 读到过期数字时，条件 UPDATE 仍然拒绝；
  - 两个并发调用只启动一个；
  - 不报告用量的 Agent：严格拒绝、软模式放行；
  - 严格模式需要单次预算；
  - 软模式下用量已知耗尽也拒绝；
  - 预算设置之前的用量计入；
  - unknown 保留预留；
  - unbounded 在严格模式下拒绝，切换为软模式后放行；
  - 重启后预留转为未结算；
  - 审批卡字段、422、`describe`。
- 变异检查：去掉拒绝时的回滚，7 项失败；去掉条件 UPDATE 中的总预算条件，“过期读取”一项失败。两处都已还原。
- 浏览器在 `t1-usage-demo` 离线桩上走了一遍：
  - 设置严格预算（总 50,000、单次 40,000）；
  - 审批卡显示估算、已用 30,633、剩余 19,367，以及“执行时会被拒绝”；
  - 点击同意后返回 409 `token_budget_exceeded`，调用保持“已同意，待执行”；
  - 切换为软预算后（v3，撤销 1 个），新的调用被放行，审批卡显示预留 40,000、已知实耗 30,618，剩余为负；
  - 修正了两处页面问题：保存按钮获得焦点时预算行不刷新；策略更新的横幅显示原始事件名。

### 策略切换修补：开启预算时的在途调用（2026-10-24，离线）

- **漏洞**：首次开启预算，或关闭后重新开启时，树中可能有在无预算期间获准、仍在执行（`starting`/`running`）的调用，它们没有预留。原来的 `ensure_counters` 只结算已结束的调用，计数器一旦存在就直接返回；`spend_preview` 也只算已结束的调用。这类调用因此被忽略，严格模式会照常放行新调用，余量里却有一笔没有上限的支出。
- **修法**：
  - 每次在有预算的情况下准入，`ensure_counters` 都先对账这棵树：没有预算行的已结束调用照常结算；仍在执行的调用记一行 `state = "unreserved"`（预留 0、unbounded 1），计数器的 unbounded 加 1。它与预留 CAS 处在 Worker 认领的同一个 `BEGIN IMMEDIATE` 事务里，所以对账和预留之间插不进任何结算。
  - 这类调用归入“用量未知且无预留”的风险：严格模式拒绝新调用（`budget_unknown_unbounded`，消息分别说明已结束的和仍在执行的各几次）；软模式放行，并给出提醒 `unreserved_in_flight`。
  - 旧调用继续执行，审批和快照都不改写（只加一行记账）。
  - 旧调用结算时，`settle_budget` 按差额把 unbounded 1 换成账本里的实际用量：complete 就解除风险，并且只计一次；unknown 仍记为 unbounded（已结束），不归零。
  - 被拒绝的准入会连同对账一起回滚；之后由下一次准入重新对账，或者旧调用自己结算入账，两条路径都只计一次。
  - `spend_preview` 不写库，按同样的规则计算，所以审批卡的 `preview` 和实际准入看到的是同一组数字。视图新增 `unreserved_in_flight`。仍在执行、尚无记账行的调用，其审批卡的 `this_call` 显示 `state: "unreserved"`。
- **契约**：`TreeSpend` 增加 `unreserved_in_flight`（`unbounded` 中仍在执行的那部分）；软模式提醒增加 `unreserved_in_flight`。不改表结构。
- **页面**：预算行显示“N 次未知且无预留（其中 M 次仍在执行）”；该调用的卡片显示“此调用在预算开启前已获准、没有预留；结算前计为‘用量未知且无预留’”。
- **验证**：
  - `tests/test_budget_rules.py` 新增 1 项：严格拒绝时的消息、软模式提醒，以及已结束和仍在执行两类混合的情况。
  - `tests/http/test_budget_admission.py` 新增 3 项反例：
    - 首次开启：严格拒绝，审批卡预览与实际准入一致；软模式放行并记账，再次对账不重复记账；旧调用结算后风险解除、实际用量只计一次（recover 后不变）；切回严格后重新放行。
    - 关闭后重新开启：期间获准的调用在严格模式下被计入并拒绝新调用；它在任何准入记账之前就结算了，也只计一次；之后严格放行。
    - 旧调用以 unknown 结束：不再计为仍在执行，但仍是 unbounded；软模式提醒从 `unreserved_in_flight` 变为 `unknown_unbounded`；严格模式仍然拒绝。
    - 每项都核对计数器等于各调用行之和，已知用量等于 complete 账本行之和。旧调用的审批保持 consumed，快照的策略版本不变。
  - 变异检查：去掉准入时对在途调用的记账，2 项失败；去掉预览中对在途调用的计算，3 项失败。两处都已还原。
  - 浏览器在 `t1-usage-demo` 上只检查了页面能加载、没有脚本错误，以及新文案能渲染。在浏览器里没有构造在途调用。

## T-1d：发送前上下文预览（2026-10-24，离线，分支 `feat/t1-context-refs`）

### 契约（`agenthub.context`，契约变更）

- 会话路径上的每次调用，其输入（`task_input`）都由若干段组成：
  - `user_input`：用户输入；
  - `separator`：各块之间的空行；
  - `handoff_frame`：Hub 加在交接回复前后的固定行；
  - `handoff_content`：被交接的回复原文；
  - `file_snippet`：预留，T-1f 开始产生。
- `compose(segments)` 拼出要发送的文本，同时生成 `context_preview`：每段记录类型、字数、估算 token 和 SHA-256。交接段还记录来源 `message_id`、`agent_id`。
- **各部分恰好拼成实际发送的文本**：按顺序连接等于 `task_input`，字数之和等于其长度。每段的哈希确定哪段文字属于哪一部分；哈希只是标识，正文照常发送。
- **估算**：每段按 `estimate_visible_tokens` 计算（`chars_v1`：ASCII 约 4 字符 1 token，其他字符每个 1 token）。合计等于各段之和，逐段向上取整，所以略偏大。不是分词计数，没有安装分词器。
- **适配器附加文本**（`adapter_text`）：适配器声明“原样发送 `task_input`”时为 `{"chars": 0}`，否则为“未知”。
- **provider 侧**（`provider_hidden`）始终为 `unknown`，不并入估算：
  - 列出基础指令、工具定义、环境信息；`native_resume` 时另列 thread 历史。
  - 可附同一配置下的历史观测 `observed`：T-0 第 4.1、4.2 节的真实运行总输入，带证据标签和版本。这些观测含当时很短的输入，只是观测，不是本次预估。
- **绑定审批**：`InvocationSnapshot.context_preview` 进入 `payload_hash`。
  - 快照构造时用 `check_preview` 核对：版本和估算方法、每段的字数、估算和哈希、总字数、总估算，以及 provider 侧必须为 `unknown`。
  - 批准后预览被改动：
    - 不再描述发送文本的，在 consume 时以 `snapshot_invalid` 拒绝；
    - 仍然自洽但与批准时不同的，以 `snapshot_changed` 拒绝。
    - 两种情况调用都标为 invalid，适配器启动次数为 0。
  - T-1d 之前的快照没有该字段，哈希不变，预算估算退回按整段文本计算。
- 适配器可选扩展 `context_profile()`：
  - app-server 和 exec 的 Codex、Claude Code 都声明原样发送；
  - app-server 附 S6-2 第二次（15,183）和双 Agent 第二次（15,753）的观测；
  - Claude Code 附单回合（1,752）和双 Agent 第二次（1,798）的观测；
  - exec 不附观测，因为当时的固定版本已不存在；
  - Mock 不声明。
  - `describe()` 显示这些信息。

### 内核与 API

- `submit_turn` 用同一组片段生成发送文本和预览，所以交接框的格式与以前逐字相同。API 从服务该 Agent 的适配器取 `context_profile`；经路由时取该 Agent 自己的适配器。
- `turn_view` 增加 `context_preview`，与快照中的字段相同。
- 预算估算（T-1c）改用预览的合计（`visible_estimate`），审批卡预览和准入使用同一个数字。
- `consume` 把快照校验失败（`TypeError`/`ValueError`）当作 `snapshot_invalid` 处理，不再抛出未处理的异常。
- 其他创建调用的路径（任务 API、子任务、重试）不生成预览。重试会复制原快照，包括预览。

### 页面

- 审批卡增加“上下文预览”：待审批时展开，之后折叠。内容包括：
  - 你的消息；
  - 每段交接的正文和 Hub 框架；
  - 分隔空行；
  - 文件片段（目前为无）；
  - 适配器附加文本；
  - provider 侧未知，附历史观测；
  - 合计和估算方法。

### 未做与未验证

- 文件片段和 `@` 引用：T-1e（解析与路径边界）、T-1f（不可变上下文包）。
- 分词器、provider 远端计数：没有。
- 预览只覆盖会话路径。
- 历史观测是 T-0 的数字，不随适配器更新，换版本后需要重新观测。
- 真实 Codex 和 Claude Code 上没有运行过（T-1h）。

### 验证

- `tests/test_context_preview.py` 共 23 项，覆盖：
  - 精确拆分；
  - 空段和未知类型；
  - provider 侧不计入估算；
  - 适配器附加文本；
  - 12 种篡改都被拒，包括“总数和估算不变、只挪动分界”；这一项促使每段加上了哈希；
  - 哈希绑定和旧快照兼容；
  - `visible_estimate`；
  - 预留的 `file_snippet` 类型；
  - 经路由取适配器的 profile。
- `tests/http/test_context_preview.py` 共 10 项，覆盖：
  - 预览各部分之和等于 app-server 桩收到的 `turn/start` 文本；
  - 交接拆成框架和正文，Claude Code 收到的正是各部分之和；
  - `native_resume` 只多列 thread 历史，估算不变；
  - 批准后改动预览：4 种情况都被拒，启动次数为 0；
  - 预算估算等于预览合计；单次预算比它小 1 时，审批卡和准入都以 `estimate_exceeds_per_call_budget` 拒绝，Claude Code 没有启动；
  - 重放请求得到同一预览；
  - `describe`。
- 变异检查：
  - 快照不再调用 `check_preview`，14 项失败；
  - 哈希不含预览，3 项失败；
  - 两处都已还原。
- 浏览器在离线桩 `t1-context-demo`（8176，multi）上检查：Codex 回复交接给 Claude Code，审批卡显示 83 字约 28 token（8 + 2 + 2 + 71 字），provider 侧未知并附 1,752 和 1,798 的观测，与 API 返回一致；没有脚本错误。
