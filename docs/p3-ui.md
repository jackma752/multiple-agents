# P3 统一聊天控制台（Mock 后端）

日期：2026-10-14。范围：连接现有 Mock 后端的统一聊天界面、审批卡片、任务状态、
配额显示与停止按钮。**未接入任何真实 Agent。**

---

## 1. 与计划的技术栈偏离（刻意且有据）

计划 §5 建议控制台用 **React + TypeScript**。本环境**不具备可用前端工具链**：
`npm` 不存在，`node`/`pnpm` 只有 DSH harness 自带的二进制，离线安装整套 React
工具链不现实（网络不稳定，见 `docs/tooling-diagnostics.md` §2）。

因此 P3 交付的是**零构建单页界面**：原生 HTML/CSS/JS，由 FastAPI 直接托管在 `/ui`。
契约与计划一致，只是没有构建步骤与框架依赖。

> 待前端工具链可用时，应把该页面迁移到 React + TypeScript。
> 迁移不影响后端：读取模型与动作端点已经分离好（见 §4）。

---

## 2. 启动与访问

```powershell
cd E:\plug\agent-hub
.\.venv\Scripts\python.exe tools\demo\serve.py
```

| 地址 | 用途 |
|---|---|
| **http://127.0.0.1:8080/ui** | **控制台（本次交付的界面）** |
| http://127.0.0.1:8080/docs | Swagger UI（手工调接口） |

停止（按 PID，精确）：

```powershell
.\.venv\Scripts\python.exe tools\demo\stop.py
```

页面顶部固定显示 **"Mock 模式 · 未接入真实 Agent"** 徽标。

---

## 3. 使用流程（界面自动管理全部 id）

1. 打开 `/ui`，在右上 token 框填入用户 token（默认已预填 `dev-user-token`），点 **连接**。
2. **新建会话** → 在聊天框输入任务目标（如"做一个登录页面"）→ **发送并创建调用**。
   界面会显示创建的 `invocation_id` 与 `approval_id`；**这些 id 由界面自动保存与传递，
   用户无需复制或填写任何 id**。
3. 中间栏 **审批队列** 出现审批卡片，含任务目标、调用 id、审批 id、深度、过期时间，
   以及三个按钮：**同意并允许执行** / **拒绝** / **撤销**。
4. 右上 **推进调度** 交给 Mock 适配器执行（界面上方会记录"启动 N，拒绝 M（原因）"）。
5. 需要子 Agent 时调用 `POST /api/tasks/{id}/delegations`（当前由文档/脚本触发，
   界面面板已预留同一调用路径）。
6. 右侧 **配额** 面板实时显示：已用总量 / 上限、并发 / 上限、最大深度、调用数、
   名额占用、策略版本（含历史版本列表），并有两条进度条。
7. **控制**：停止整棵树 / 暂停 / 恢复。停止会弹确认框，并显示"取消 N 个运行中调用"。
8. **修改配额**：填写总量/并发/深度/调用数 → **应用（版本 +1）**；非法值会被拒绝并提示。
9. **事件** 面板按序号倒序显示最近 50 条事件。

### 界面元素清单（验收用）

| 需求 | 界面元素 |
|---|---|
| 统一聊天 | `#chat` 消息区 + `#msg` 输入 + 发送按钮 + `#new-conv` |
| 审批卡片 | `#approvals`，每张卡含 approve / reject / revoke |
| 任务状态 | `#tree` 按深度缩进的树，每个节点带状态 pill |
| 配额显示 | `#quota`（含进度条）+ `#p-total` 等输入 + `#apply-policy` |
| 停止按钮 | `#stop`（另有 `#pause` / `#resume`） |
| Mock 标识 | 顶部 `Mock 模式 · 未接入真实 Agent` 徽标 |

---

## 4. 后端新增：读取模型

界面不再靠用户提供 id，而是从两个只读端点**发现**状态：

| 端点 | 返回 |
|---|---|
| `GET /api/tasks` | 根任务列表（目标、调用数、运行数、待批数、配额、是否已停止） |
| `GET /api/tasks/{id}/view` | 单棵树全量视图：`root` / `tasks` / `invocations` / `approvals` / `quota` / `policy` / `policy_versions` / `stopped` / `paused` / `events` |

**鉴权取舍（有意为之）**：读取端点只要**已认证身份**；`agent` / `worker` 身份
可以读但**不能**批准、改配额、暂停或停止（那些仍为 403）。
理由：Plan §4.1 限制的是"审批权限"，Worker 需要读状态来工作。

---

## 5. 验证结果

命令：`python -m pytest -v`（仓库根目录，使用 `.venv`）

**158 passed，exit code 0**。本次新增 `tests/http/test_ui.py`（9 项）：

| 验收点 | 测试 |
|---|---|
| 页面可访问且含全部必需控件 | `test_ui_page_is_served` |
| 页面**不要求用户填写任何 id**（无 task_id/invocation_id/approval_id 占位输入） | `test_ui_page_has_no_placeholder_ids_to_fill_in` |
| 页面可匿名加载，但数据调用需 token | `test_ui_page_loads_without_auth_but_data_requires_it` |
| 读取端点鉴权边界（认证即可读；批准/停/改配额仍 user-only） | `test_read_model_requires_auth_but_not_the_user_role` |
| view 结构 = 界面渲染所需字段 | `test_view_shape_matches_what_the_console_renders` |
| 侧栏汇总 | `test_tasks_list_summarises_for_the_sidebar` |
| **模拟界面完整会话**（见下） | `test_simulated_ui_session_end_to_end` |
| 暂停状态反映到界面 | `test_view_reports_paused_state_for_the_header` |
| 未知任务 404 | `test_view_404_for_unknown_task` |

### 模拟界面会话（无浏览器环境下的最强验证）

`test_simulated_ui_session_end_to_end` 按**界面 JavaScript 的真实调用顺序**逐步执行，
并**只从 view 端点重新取得 id**（不使用创建响应里的 id），从而证明界面能自动管理 id：

1. 连接 → 新建会话 → 发消息 → 创建调用（响应含 task/invocation/approval 三个 id）
2. 从 `view` 取回 **pending 审批的 id**（而非用创建响应的）
3. 未批准时 **推进调度 → 启动 0**
4. 批准 → 推进调度 → **启动 1**，`view.invocations[0].status == running`
5. 分派 2 个子 Agent → `view.tasks` 长度 3，pending 审批 2
6. 批准两者 → 推进调度 → **启动 3**，`quota.launched_total == 2`、`concurrent == 2`
7. 改配额 → `root.policy_version == 2`，且**降上限后 `launched_total` 仍为 2**（不重置）
8. 停止 → `view.stopped == true`、`concurrent == 0`、`launched_total` 仍为 2
9. 停止后再推进调度 → **启动数仍为 3**（不再调度）

### 实机验证（真实 HTTP）

```
GET /ui -> 200  17274 bytes  text/html
  含 Agent Hub 控制台 / Mock 模式 / #chat / #approvals / #tree / #quota / #stop / #token
openapi 已注册 /ui : True
创建调用: task-7b471ce73c39 inv-ea21129842fa appr-d979da42942e
view keys: approvals, events, invocations, paused, policy, policy_versions, quota, root, stopped, tasks
quota: {'launched_total': 0, 'concurrent': 0, 'total_invocations': 0, 'open_reservations': 0}
侧栏: 5 条，最新 goal='UI smoke'，pending=1
```

停止：`stop.py` → `graceful: {'stopping': True}` → 端口关闭 → **退出码 0**。

---

## 6. 限制（不得当作已实现）

1. **未接入真实 Agent**：界面全部操作都作用于 Mock 适配器。
2. **未做浏览器交互验证**：本环境无浏览器，验证止于"页面可访问 + 元素齐全 +
   模拟调用序列全通"。**真实点击、样式渲染、并发操作未验证**。
3. **非 React**：见 §1，属刻意偏离，待工具链可用后迁移。
4. **无流式输出**：事件面板为轮询/刷新式读取，**不是 SSE**。
5. **单用户**：只有 `local-user`，无多用户与项目隔离。
6. **子 Agent 分派未做成按钮**：分派需要父任务 id，界面目前通过统一入口创建调用，
   子 Agent 分派在测试与文档中给出调用路径；界面按钮属后续补充。
7. **无附件、无产物、无 Git 差异视图**：计划 §5 的产物/差异面板属后续阶段。
8. **worker 需手动步进**：界面上的"推进调度"即 `POST /api/worker/drain`；
   真实部署中 Worker 是独立进程自主轮询。
