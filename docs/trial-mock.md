# 本地 Mock 试用说明

版本：P2 阶段性基线 + P3 控制台。仅使用 **Mock Agent**。

> ## ⚠️ 真实 Codex **尚未验证成功**，本试用不涉及任何真实 Agent
>
> 本版本**只使用 Mock 适配器**。它不调用 Codex、Claude Code、WorkBuddy、DSH 或任何
> 真实产品，不发网络请求，不读写你的项目文件。Mock 适配器只做两件事：
> **记录自己"被启动过几次"**，以及**写一条可查询的执行记录**。
>
> 因此本试用能验证的是**审批、配额、停止、恢复这套安全阀本身**，
> **不能验证任何真实 Agent 的能力**。
>
> 真实 Codex 的接入验证**尚未成功**：两次经批准的只读调用均以退出码 1 结束
> （第一次受阻于其自身临时目录写入被拒，第二次受阻于到 `chatgpt.com` /
> `api.openai.com` 的**连接失败，原因待确定**）。详见
> `docs/agent-codex-capabilities.md` 与 `docs/p3-codex-manual-run.md`。
> **真实适配器整体状态：未验证。**

---

## 0. 控制台快速试用（推荐先走这条）

**启动**（在仓库根目录 `E:\plug\agent-hub`）：

```powershell
.\.venv\Scripts\python.exe tools\demo\serve.py
```

启动后会打印访问地址与该进程 **PID**（同时写入 `var\serve-8080.pid`）。

**访问地址**：

| 地址 | 用途 |
|---|---|
| **http://127.0.0.1:8080/ui** | **控制台**：聊天 / 审批卡片 / 任务树 / 配额 / 停止 |
| http://127.0.0.1:8080/docs | Swagger UI（手工调接口） |

**试用步骤**（以下每一步都已实机验证）：

1. 打开 `/ui`，token 输入框已预填 `dev-user-token`，点 **连接**
   （页面顶部显示 "Mock 模式 · 未接入真实 Agent"）。
2. **新建会话** → 在聊天框输入任务目标（如 `做一个登录页面`）→ **发送并创建调用**。
   界面会显示 `task_id` / `invocation_id` / `approval_id`，**这些 id 由界面自动保存与传递，
   你不需要复制或填写任何 id**。
3. **创建任务后未批准时不启动**：点右上 **推进调度**，会显示"启动 0，拒绝 0"。
4. **批准**：中间栏「审批队列」出现审批卡片 → 点 **同意并允许执行** →
   再点 **推进调度** → 显示"启动 1"。
5. **拒绝**：在需要子 Agent 时（`POST /api/tasks/{id}/delegations`，或由文档步骤触发），
   新审批卡片上点 **拒绝** → 该调用状态变为 `rejected`，**永不启动**。
6. **配额**：右侧「配额」面板实时显示 已用总量/上限、并发/上限、最大深度、调用数、
   名额占用、策略版本；下方可填新上限后点 **应用（版本 +1）**。
   降低上限**不会重置**已用累计数，也**不会取消**运行中的调用。
7. **停止**：点 **停止整棵树**（会弹确认）→ 运行中调用被取消、并发归 0、
   **累计用量不返还**；停止后再批准新调用并推进调度，**仍不会启动**。
   面板会**分开报数**：`已确认停止` 与 `未能确认停止`。未确认的那部分会置
   `unknown` 并**保留并发名额**（Mock 适配器支持取消，因此正常路径是"已确认"；
   Codex 目前不支持取消，属于"未确认"）。

**停止服务**（按 PID 精确停止，**不要**按名批量杀进程）：

```powershell
.\.venv\Scripts\python.exe tools\demo\stop.py
```

---

## 1. 启动（3 步）

在仓库根目录 `E:\plug\agent-hub` 执行：

```powershell
# 1) 若还没建虚拟环境（只需一次）
python -m venv .venv
# 若本机 pip 下载卡住，用离线方式：见 tools/bootstrap/README.md

# 2) 启动 Mock API
.\.venv\Scripts\python.exe tools\demo\serve.py

# 3) 打开控制台
#    http://127.0.0.1:8080/ui
#    或交互式 API 页： http://127.0.0.1:8080/docs
```

`serve.py` 会把包目录加入 `sys.path`（仓库未 pip 安装，直接跑
`uvicorn agenthub_rt.api:app` 会导入失败）。

启动时会打印：

```
Agent Hub - Mock API (no real Agent is contacted)
  API page : http://127.0.0.1:8080/docs
  OpenAPI  : http://127.0.0.1:8080/openapi.json
  Database : sqlite:///E:/plug/agent-hub/var/agent-hub.db
  Data dir : E:\plug\agent-hub\var
  User token (from AGENT_HUB_USER_TOKEN or default): dev-user-token
```

常用参数：

```powershell
.\.venv\Scripts\python.exe tools\demo\serve.py --port 8090   # 换端口
.\.venv\Scripts\python.exe tools\demo\serve.py --reload      # 改代码自动重载
```

---

## 2. 访问地址

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8080/docs` | **交互式 API 页（Swagger UI）**，可直接点按钮调用 |
| `http://127.0.0.1:8080/openapi.json` | 接口契约 |
| `http://127.0.0.1:8080/redoc` | 另一种只读文档视图 |

Swagger 页右上角有 **Authorize** 按钮，填入用户 token 后即可调用受保护的接口。

---

## 3. 测试用户凭证

后端强制鉴权：**只有 `user` 角色能批准/拒绝/撤销、改配额、暂停/停止、裁决 unknown**。
`agent` 与 `worker` 身份会被 **403 拒绝**（用于证明 Agent 不能批准自己）。

| 角色 | 默认 token | 能做什么 |
|---|---|---|
| `user` | `dev-user-token` | 全部人工操作 |
| `agent` | `dev-agent-token` | 除人工操作外可读；批准类一律 403 |
| `worker` | `dev-worker-token` | 同上 |

**改成你自己的 token**（推荐；环境变量必须在**启动服务前**设置，因为是进程环境）：

```powershell
$env:AGENT_HUB_USER_TOKEN = '我的-私有-token'
.\.venv\Scripts\python.exe tools\demo\serve.py
```

也可用 `AGENT_HUB_AGENT_TOKEN` / `AGENT_HUB_WORKER_TOKEN` 改另两个身份。
服务不会把 token 写入仓库；源码只从环境变量读取（默认值是明确标注的开发值）。

**在 Swagger 里设置**：点 **Authorize** → 在 `Value` 填 `dev-user-token`（或你的 token）
→ Authorize。之后所有受保护请求自动带上。

> 不带 token 调用受保护接口会得到 `401 unauthenticated`；
> 带 `agent` token 会得到 `403 not_permitted`。

---

## 4. 推荐先跑演示脚本

最快看到全流程（需要服务已在运行）：

```powershell
.\.venv\Scripts\python.exe tools\demo\demo_flow.py
```

它会自动走完整条链路并打印 Mock 适配器的**启动次数**，逐条展示安全阀生效
（下面是实测输出节选）：

```
[2] Create the main agent invocation (a PENDING approval is raised with it)
    task_id       = task-1974dc8e4cf7
    invocation_id = inv-b2e04f0d09c5
    approval_id   = appr-39a2ebbf614e  status=awaiting_approval
[3] Drain the worker WITHOUT approving
    Mock adapter start_count = 0   (expected 0 - nothing is approved)
    OK: unapproved work never reached the adapter
[4] Approve the main call, then drain
    approve appr-39a2ebbf614e -> approved
    started  inv-b2e04f0d09c5
    Mock adapter start_count = 1   (expected 1 - the approved main call ran)
[6] Drain again - sub-agents are still not approved
    Mock adapter start_count = 1   (expected still 1 - pending approvals are not consent)
[7] Approve BOTH sub-agents, then drain
    Mock adapter start_count = 3   (expected 3 - main + both approved sub-agents)
[8] Try to add a THIRD sub-agent beyond the quota
    REFUSED  inv-e237ba66ac75  code=max_subagents_total
    Mock adapter start_count = 3   (expected still 3 - the tree is at its sub-agent cap)
[10] Stop the whole tree ...
    stopped=True cancelled=3 already_stopped=False
[11] Approve one more call AFTER the stop - it must not start
    REFUSED  inv-...  code=tree_stopped
```

脚本在关键处会**断言**（如 `start_count == 0`），一旦安全阀失效会直接以非零码退出。

参数：`--base-url http://127.0.0.1:8090`、`--token 我的-私有-token`。

> **注意**：演示脚本与直接用 `httpx` 写的客户端都设置了 `trust_env=False`。
> 某些环境里有代理会拦截 localhost 请求并返回 **502**，绕过代理后才正常。

---

## 5. 手工操作步骤（Swagger 或 curl/httpx 均可）

以下假设你已在 `/docs` 点过 **Authorize**。

### 5.1 创建第一个任务

1. **建会话**：`POST /api/conversations` → 拿到 `conversation_id`
2. **发消息**：`POST /api/conversations/{id}/messages`，body `{"text": "build a login page"}`
   （P2 只回执，消息转调用草稿属 P3）
3. **创建调用**：`POST /api/invocations`

   ```json
   {"task_id": "-", "agent_id": "mock", "task_input": "build a login page"}
   ```

   → 返回 `task_id`（这棵根任务树的 id）、`invocation_id`、**`approval_id`**、`payload_hash`。
   按计划 §5，此接口同时**创建待审批调用**：调用直接进入 `awaiting_approval`，
   并带出一条 `pending` 审批。`task_id` 就是这棵树后续所有操作的根。

### 5.2 批准 / 拒绝 / 撤销

| 操作 | 接口 | 结果 |
|---|---|---|
| **批准** | `POST /api/approvals/{id}/approve` | 调用 → `queued`，可被调度 |
| **拒绝** | `POST /api/approvals/{id}/reject` | 调用 → `rejected`，永不启动 |
| **撤销** | `POST /api/approvals/{id}/revoke` | 审批作废，调用退回 `draft`，需重新审批 |

子 Agent 的审批由 `POST /api/tasks/{task_id}/delegations` 提议产生（提议**不会执行**）：

```json
{"agent_id": "mock", "goal": "write the HTML", "parent_task_id": "<task_id>"}
```

→ 返回子任务的 `task_id`、`invocation_id`、`approval_id`，状态为 `awaiting_approval`。

### 5.3 让已批准的任务执行

```http
POST /api/worker/drain     （需要用户 token）
```

它执行**一轮**调度：把 `queued` 且审批有效的调用原子消费并交给适配器。
再次调用会处理新出现的可执行调用。

**观察是否真的启动**：

```http
GET /api/dev/mock-activity     （需要用户 token）
```

返回 `start_count`、已启动的 `invocation_id` 列表、每个调用的执行记录。
**这是 Mock 专用调试接口**，真实适配器不会提供。

### 5.4 配额限制

配额在**整棵根任务树**上生效，默认：子 Agent 总量 **3**、并发 **2**、深度 **1**、
单次运行时限 600 秒。查看/修改策略（仅 user）：

```http
PATCH /api/tasks/{task_id}/policy
{"max_subagents_total": 1, "max_subagents_concurrent": 1, "max_depth": 1, "max_agent_invocations": 10}
```

- 每次修改**版本递增**（`v1 → v2`），旧版本保留可审计
- 修改会**撤销该树所有未消费的审批**（需重新批准）
- **降低上限不会重置已用累计数量**，也**不会取消正在运行的调用**
- 非法参数（如并发 > 总量、负数）返回 `422`，**不写入任何东西**

超限时 `drain` 会返回拒绝码：
`max_subagents_total` / `max_subagents_concurrent` / `max_agent_invocations`。

### 5.5 停止 / 暂停

| 操作 | 接口 | 行为 |
|---|---|---|
| **停止整棵树** | `POST /api/tasks/{id}/stop` | 先持久化停止 → 取消运行中调用 → **仅在取消被确认时**释放并发名额；**累计数量不返还**；之后不再调度任何新调用。未确认（如适配器不支持取消）→ 置 `unknown` 并**保留名额** |
| **暂停** | `POST /api/tasks/{id}/pause` | 阻止后续调度，但**不取消**运行中的调用 |
| **恢复** | `POST /api/tasks/{id}/resume` | 解除暂停（不会复活已过期/已撤销的审批） |

停止是**幂等**的：重复调用返回 `already_stopped=true`，且只释放一次名额。

### 5.6 运行超时与 unknown 裁决

- 超过策略运行时限的调用由 `POST /api/worker/reap` 处理：
  - 取消**已确认** → 置 `timed_out` 并释放名额
  - 取消**未确认**（不支持取消／外部仍报运行中／证据丢失）→ 置 `unknown` 并**保留名额**
- `unknown` 的调用只能由用户裁决：`POST /api/invocations/{id}/reconcile`

  ```json
  {"action": "settle", "outcome": "canceled"}   // 认定结果，释放名额一次（幂等）
  {"action": "retry"}                           // 重试
  ```

  `retry` **必须先确认旧执行已结束**；未确认时返回 `409 not_stopped`，
  **不改旧状态、不释放名额、不新建调用**。确认后才释放一次并创建
  **新调用 + 新待批审批**（重试必须重新批准，不会自动启动）。

### 5.7 崩溃恢复

```http
POST /api/worker/recover     （需要用户 token）
```

重启后核对 `starting`/`running` 的调用：确认在跑的保留名额；确认已结束的结算并释放一次；
**无法确认的置 `unknown`、不自动重启、不提前释放名额**。

---

## 6. 数据位置与重置

| 项 | 位置 | 说明 |
|---|---|---|
| 数据库 | `<repo>/var/agent-hub.db` | 路径锚定仓库根，**不依赖当前工作目录** |
| Mock 执行记录 | 同上库内的 `mock_executions` 表 | 模拟"外部系统自己的状态" |
| 路径覆盖 | 环境变量 `AGENT_HUB_DB_URL` | 例如 `sqlite:///D:/tmp/trial.db` |

**重置为干净状态**：

```powershell
# 先按 PID 停掉服务（精确，不会误杀其他 python 进程）
.\.venv\Scripts\python.exe tools\demo\stop.py
# 再删除运行库
Remove-Item .\var\agent-hub.db* -Force
```

> **不要**用 `Get-Process python | Stop-Process -Force`：它会终止机器上所有 python
> 进程（含无关工作），本环境还可能产生 `0x40010004` 强制终止码。
> `stop.py` 会先核对 PID 与端口归属（`openapi.json` 的 `title` 必须是 `Agent Hub`），
> 再请求服务**优雅退出**；核对不通过时**拒绝终止**。
> 详见 `docs/tooling-diagnostics.md`。

`var/` 已在 `.gitignore` 中，不会被提交。

---

## 7. 跑自动化测试

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

**140 passed**。验收依据与逐条证据见 `docs/p2-acceptance.md`。

---

## 8. 已知限制（请勿当作已实现）

1. **没有真实 Agent**：仅 Mock。P3 才接真实产品适配器。
2. **无前端界面**：只有 Swagger UI 与演示脚本；聊天/审批卡片/任务树界面属 P3。
3. **无 SSE**：`GET /api/tasks/{id}/events` 返回普通事件列表，不是流式推送。
4. **单用户**：只有一个 `local-user` 身份，没有多用户与项目隔离。
5. **本地队列**：Worker 由 `POST /api/worker/drain` 手动驱动；
   真实部署中 Worker 是独立进程自主轮询（`agenthub-kernel.worker` 的 `main()` 也是可用入口）。
6. **SQLite 单机**：并发验证到多线程独立连接；跨机与高并发未验证。
7. **`POST /api/worker/drain` 已加鉴权**（用户 token），但它仍是一个"手动步进调度器"的
   开发接口，真实部署不会暴露。
8. **`GET /api/dev/mock-activity` 是 Mock 专用**调试接口，不属于产品契约。
