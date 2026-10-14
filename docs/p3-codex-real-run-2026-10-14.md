# P3 真实联调记录：经适配器跑通一次真实 Codex 调用（2026-10-14）

性质：**一次经操作者批准的、只跑一次的真实调用**记录。命令、原始结果与状态变化
全部如实抄录；**未重试、未切模型、未放宽权限**。凭证副本已在结束后删除。

代码版本：分支 `feat/p3-codex-probe`，`d7e73be`（工作树干净）。**未合并、未推送。**

---

## 1. 执行的完整命令

```powershell
cd E:\plug\agent-hub
.\.venv\Scripts\python.exe tools/demo/codex_verify.py --mode real --i-understand-this-spends-quota `
    --codex-home "$env:TEMP\agenthub-codex-verify-home"
```

| 项 | 值 |
|---|---|
| 数据库 | `var/codex-verify/verify-real-1791982249.db`（新建，**断言为空**：`/api/tasks -> []`） |
| 调度范围 | 只创建 1 个任务树、1 个调用，`agent_id="codex"` |
| CODEX_HOME | `%TEMP%\agenthub-codex-verify-home`（**仓库之外**；凭证副本，运行后已删除） |
| 可执行文件 | 固定路径 + sha256 + size 三项核对通过（`identity_ok=True`） |
| 沙箱 | `--sandbox read-only`；`--disable multi_agent` |
| 网络 | 本机代理 `http://127.0.0.1:8090` + `CODEX_CA_CERTIFICATE`=certifi PEM（仅注入子进程） |
| 提示词 | `Reply with exactly: OK. Do not modify any files.` |

**实际 argv**（适配器构建，原样抄录）：

```
codex.exe exec --json --disable multi_agent --sandbox read-only --skip-git-repo-check
    -C E:\plug\agent-hub\var\codex-verify\codex-runs\inv-c6d3ab9103d5
    "Reply with exactly: OK. Do not modify any files."
```

无 `dangerously-bypass-*` 系列参数。

## 2. 结果

| 项 | 值 |
|---|---|
| 未批准时启动数 | **0**（`[4] drain WITHOUT approval`） |
| 批准后启动尝试 | **恰好 1 次** |
| 退出码 | **0** |
| 本地超时 | 否 |
| JSONL 事件 | 4：`thread.started:1, turn.started:1, item.completed:1, turn.completed:1` |
| thread_id | `01a13a77-316d-7b01-a59e-5323a6c0728f` |
| 最终回复 | **`OK`**（逐字一致） |
| usage | `input_tokens=15094, cached_input_tokens=12544, cache_write_input_tokens=0, output_tokens=5, reasoning_output_tokens=0` |
| 驱动错误 | 0（`errors: []`） |
| 工作目录事后条目 | `[]`（提示词未要求写文件，**只读是否真拒绝写入仍未验证**） |
| 实测耗时 | 适配器记录 `elapsed=11.79s`；HTTP 往返 `12.6s` |

### 12 项检查全部 PASS

```
[PASS] 未批准时启动数 = 0          [PASS] thread_id 非空
[PASS] 批准后启动尝试 = 恰好 1 次   [PASS] 最终回复非空
[PASS] 进程确实运行（事件 > 0）     [PASS] 返回真实 usage
[PASS] verdict = finished          [PASS] 内核状态 = succeeded
[PASS] 进程退出码 = 0              [PASS] 未释放的配额预留 = 0
[PASS] 未发生本地超时              [PASS] 并发名额已释放 = 0
→ VERIFICATION PASSED
```

### 内核侧状态与配额（从运行库直接读出的行，非推断）

```
tasks             : task-346ee3e2cd58 | root | agent_id=codex | depth=0 | kind=user | status=draft
invocations       : inv-c6d3ab9103d5 | status=succeeded
                    adapter_invocation_id=01a13a77-316d-7b01-a59e-5323a6c0728f
                    started_at=12:51:04.056618  finished_at=12:51:04.058113  error=None
approvals         : appr-1477f8e9a629 | status=consumed | consumed_at=12:50:51.427562
tree_quota        : launched_total=0 | concurrent=0 | total_invocations=1
quota_reservations: res-6254b583c2f1 | created_at=12:50:51 | released_at=12:51:04.059356
```

**配额变化怎么读**（避免误判为"没计数"）：`launched_total` / `concurrent` 是
**子 Agent** 配额，主 Agent 调用不占用它们——本次是 `depth=0` 的主调用，因此
`launched_total` 保持 0 而 `total_invocations` 由 0 变 1，且预留行
（`res-6254b583c2f1`）**已释放**（`released_at` 非空）→ 无泄漏。

## 3. Codex 输出的 3 行非 JSON 文本（原样保留，便于以后比对）

```
WARNING: proceeding, even though we could not create PATH aliases: Refusing to create
         helper binaries under temporary dir "C:\Users\Administrator\AppData\Local\Temp\dsh-...\"
         (codex_home: ...\agenthub-codex-verify-home)
Reading additional input from stdin...
2026-10-14T12:50:57.401889Z ERROR codex_models_manager::manager: failed to refresh
         available models: request timed out
```

三点如实记录：

1. **CODEX_HOME 放在 %TEMP% 下的代价**：Codex **拒绝**在临时目录下创建 helper
   binaries（PATH 别名），会打印 WARNING 但**继续执行**。本次调用成功，但这个
   能力被降级；若要完整功能，CODEX_HOME 应放在临时目录之外的普通目录。
2. `Reading additional input from stdin...` 是因为子进程 stdin 被设为 `DEVNULL`；
   Codex 读到 EOF 继续，不影响结果。
3. `failed to refresh available models: request timed out` 是**模型列表刷新超时**
   （非致命）：它出现在日志里但**不是** JSONL `error` 事件（`errors: []`），
   turn 仍正常完成。与既有结论一致：`api.openai.com` 刷新不稳，实际对话走的是
   `chatgpt.com` 路由。

## 4. 本次仍**未**验证（不得当作已实现）

取消、工具启动前拦截、`--disable multi_agent` 是否真能阻止派生子 Agent、
只读沙箱是否真拒绝写入（提示词不要求写文件）、按 `thread_id` 恢复会话、
app-server 协议。以上全部保持在 `docs/agent-codex-capabilities.md` 中的
`unsupported` / `unknown` 状态。

## 5. 收尾与证据

* **已删除**：临时凭据副本 `%TEMP%\agenthub-codex-verify-home\auth.json`
  （先删凭证，再删整个临时 home）；操作者的 `%USERPROFILE%\.codex` **未被写入**。
* **保留的脱敏证据**（均在 gitignore 的 `var/` 内，未提交、未外带）：
  `var/codex-verify/verify-real-1791982249.db`（内核状态与配额行）、
  `var/codex-verify/codex-executions.db`（适配器记录与解析载荷）。
* 本文档只记录计数、时间、id 与 Codex 自己的日志行；**不含凭证、不含聊天内容**。
