# 控制台试用步骤（准确版，2026-10-14）

> **2026-10-15 更新**：新版本的真实 Codex 已拒绝启动，原生派生控制存在缺口。
> 试用请选 Mock；真实模式显示“已阻止新调用”，仍可读取记录。
> 本轮代码在 `.worktrees/strict-adapter-gate`，解释器用 `E:\plug\agent-hub\.venv\Scripts\python.exe`。
> 原主目录及旧的 8080 服务不会自动获得修复；建议用独立端口 8150 验收新版本。
> 详见 [准入说明](p3-strict-adapter-admission.md)。下方真实执行步骤保留为历史参考。

面向：想在本机点一遍 `/ui` 控制台的人。**每一步都写了"应该看到什么"**，
以及哪些行为**尚不支持**（取消）。真实 Agent 调用**逐次审批**。

---

## 0. 先看清当前状态（本机实测）

| 事实 | 值 |
|---|---|
| 已有一个**旧构建**的服务在跑 | `http://127.0.0.1:8080`（PID 文件 `var/serve-8080.pid`，PID 14608，`openapi` 标题 `Agent Hub`） |
| 它的问题 | 它在**本轮改动之前**启动，因此**没有**新的结果面板字段、Agent 选择与取消声明；且 `/api/adapter` 在它上面返回 **404**（旧代码未注册该端点） |
| 结论 | 试用前请**先停掉它再重启**，否则你看到的不是本轮交付的页面 |

停掉旧实例（按 PID 精确停止，不按名批量杀进程）：

```powershell
cd E:\plug\agent-hub
.\.venv\Scripts\python.exe tools\demo\stop.py            # 默认端口 8080
# 只看状态、不停止： .\.venv\Scripts\python.exe tools\demo\stop.py --status
```

期望输出：`HEALTHY` → `stopped` / 端口关闭。若端口仍被占用，说明还有别的进程，**不要**用
`Get-Process python | Stop-Process`（会杀掉无关进程）。

## 1. 启动控制台

**A. Mock 模式（不接触任何真实 Agent，推荐先试这个）**

```powershell
.\.venv\Scripts\python.exe tools\demo\serve.py
```

**B. 真实 Codex 模式（会启动真实 CLI，需先登录）**

```powershell
# 先准备好登录（见 docs/p3-codex-credentials.md；必须由你本人执行）
.\.venv\Scripts\python.exe tools\demo\codex_login.py

# 再启动（启动本身不调用模型，只有"批准 + 推进调度"才会）
.\.venv\Scripts\python.exe tools\demo\serve.py --adapter codex
```

启动横幅**不打印 token**，只说明来源；codex 模式会额外打印可执行文件、版本、
`CODEX_HOME`、代理、CA 与"凭证是否存在"（**不显示内容**）。

## 2. 打开页面

浏览器访问 **http://127.0.0.1:8080/ui**（Swagger：`/docs`）。

1. 右上 token 框已预填 `dev-user-token`，点 **连接**。
   期望：提示"已连接（适配器：…）"，顶部徽标显示 `Mock 模式` 或 `REAL Codex`。
2. 左侧 **新建会话**。期望：`会话 conv-…`。
3. 在 **聊天** 输入目标，先确认中间那行 **调用哪个 Agent**：
   * Mock 模式：该适配器**不限制** `agent_id`，字段默认 `mock`；
   * Codex 模式：只会出现 `codex`；若你手填别的值，字段旁会**立刻警告**
     "该调用会在启动时被拒绝"，发送后后端也会返回 `adapter_warning`。
4. 点 **发送并创建调用**。
   期望：聊天区出现 `已创建调用 inv-…（agent_id=…，待审批 appr-…）`，
   右侧 **审批队列** 出现审批卡片，**任务树** 该节点显示 `awaiting_approval`。
   > 这一步**不会**启动任何进程。

## 3. 批准与推进

5. 审批卡片点 **同意并允许执行**。
   期望：任务状态变为 `queued`（**不再是 `draft`**——本轮修正）。
6. 顶部 **推进调度**。
   * Mock 模式：仅记录一次启动，**不启动真实进程**；调用会停在 `running`
     （Mock 是"启动后仍在跑"的语义）。
   * Codex 模式：**这会启动一次真实 Codex 进程**（`codex exec …`，只读沙箱、
     禁用子 Agent）。**必须是你本次已批准的调用**；页面不会替你批准。
   期望：`执行结果` 面板出现调用卡片。

## 4. 看结果面板（本轮新增/补全的内容）

在 **执行结果（适配器记录）** 面板，逐项核对：

| 面板项 | 含义 | Codex 真实调用的实测样例 |
|---|---|---|
| 状态 | 适配器自己的结论 | `finished` |
| 内核：… | 内核对这次调用的状态 | `succeeded` |
| thread_id | 外部执行 ID（恢复/查证用） | `01a13a77-…c0728f` |
| 最终回复 | Codex 的回复，**逐字** | `OK` |
| usage | 真实用量 | `input_tokens=15094, cached_input_tokens=12544, output_tokens=5` |
| 事件 | JSONL 事件统计 + 总数 | `thread.started×1 → … → turn.completed×1`（共 4） |
| 退出码 | 进程退出码（真实运行的证据） | `0`（超时会标 ⚠） |
| 工作目录 | 事后条目数 | `0 条目（只读生效）` |
| 取消 | **尚不支持** | `取消：不支持（适配器未验证取消能力…）` |

> **mock 模式**看不到真实回复：Mock 只是计数，没有外部执行结果，
> 面板会显示"尚无适配器执行记录"。要看真实回复必须用 `--adapter codex` 且完成登录。

## 5. 试停止（看它如何如实报数）

7. 右侧 **停止整棵树** → 确认。
   期望：聊天区显示
   `已请求停止：N 个运行中调用，其中 M 个已确认停止`；
   若有未确认的，会追加
   `⚠ K 个未能确认停止（适配器 cancel=unsupported）：已标记 unknown 并保留配额…`。

   为什么：**Codex 不能取消**。未确认的停止**不会**释放并发名额，也不会假装"已停止"；
   它会变成 `unknown`，等你在裁决入口处理。Mock 适配器支持取消，因此 Mock 试用里
   正常显示"已确认"。

8. 停止后再批准一个新调用并 **推进调度** → 期望：**不会启动**（已持久化停止）。

## 6. 结束

```powershell
.\.venv\Scripts\python.exe tools\demo\stop.py     # 按 PID 停掉本次控制台
```

## 7. 本机已完成的离线核对（可复现）

* `python -m pytest` → **304 passed**
* `tools/demo/offline_integration.py` → `OFFLINE INTEGRATION PASSED`
* `tools/demo/codex_verify.py --mode stub` → **VERIFICATION PASSED**（12/12 检查）
* `tools/probes/capability_probe.py --probe <四项> --mode stub` → 判定矩阵见
  `docs/p3-permission-probes.md` §6（含正/负对照与误判对照）
* 真实服务冒烟（端口 8147，`--adapter codex`，独立库，**未批准任何调用**）实测：
  `GET /ui → 200（25931 bytes）`、`/api/adapter` → `kind=codex, agent_ids=[codex],
  identity_ok=True, cancel=False`、创建调用后未批准 drain → `adapter-result.available=false`
  （**0 次启动**）、任务状态 `awaiting_approval`（推导值与存储值一致）、
  空树停止 → `confirmed=0 / unconfirmed=0`、**未创建长期 `CODEX_HOME`**（解析无副作用）。

页面**无法在本环境用浏览器点击验证**（无浏览器）：以上结论来自源码级断言 + 真实 HTTP
调用序列，真实点击与样式渲染仍需你本机确认。

## 8. 明确"尚不支持"的部分

取消、工具启动前拦截、`--disable multi_agent` 是否真能阻止派生子 Agent、
只读沙箱是否真拒绝写入、按 `thread_id` 恢复会话、app-server 协议——**全部未验证**，
页面与文档一律标注为不支持/未知，不做任何升级声明。
