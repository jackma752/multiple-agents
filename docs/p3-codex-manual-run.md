# Codex 真实调用：手工执行指南与结果记录模板

用途：本环境**无法完成** Codex 真实调用（见 §1 诊断），因此把这一步交给操作者在
网络可达的终端里完成。工具已备好，**只跑一次、绝不重试**。

---

## 1. 为什么必须手工执行：实测网络诊断

| 目标 | DNS | TCP 443 | 结论 |
|---|---|---|---|
| `chatgpt.com` | OK → `128.121.243.235` | **超时** | **被阻断** |
| `api.openai.com` | **解析失败**（`getaddrinfo failed`） | — | **被阻断** |
| `pypi.org`（对照组） | OK → `151.101.0.223` | **OK / TLSv1.3 / HTTP 200** | 正常 |

即：本环境**允许一般出站 HTTPS**（`pypi.org` 可通），但对 **OpenAI / ChatGPT 端点做了定向阻断**。
这不是文件沙箱行为——文件沙箱会给出显式 `[sandbox: ...]` 标记，而这里只有 TCP 超时与 DNS 失败，
且 harness **未暴露任何网络审批入口**（无相关 `DSH_*` 变量、无代理配置）。

**Codex 侧的实际报错**（`var/_codex_retry_out.txt`，退出码 1）：

```
ERROR codex_models_manager::manager: failed to refresh available models: Connection failed: error sending request
ERROR rmcp::transport::worker: worker quit with fatal: Transport channel closed,
      http/request failed: error sending request for url (https://chatgpt.com/backend-api/ps/mcp)
{"type":"error","message":"Reconnecting... 5/5 (workspace routing discovery failed)"}
{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}
```

**需要放行的目标（仅这两个）**：

1. `chatgpt.com`（TCP 443）—— 登录态与模型路由
2. `api.openai.com`（TCP 443 / DNS）—— 模型列表刷新

不需要放行其他目标。

---

## 2. 执行前的一次性准备

```powershell
cd E:\plug\agent-hub

# 建专用、可写的 CODEX_HOME（避免 Codex 去写 ~/.codex/tmp 而被拒）
.\.venv\Scripts\python.exe tools\codex_one_call.py --init-home var\codex-home
```

> `var/` 已被 Git 忽略。该目录会放一份 `auth.json` 副本用于继承登录态；
> **验证完成后请删除**：`Remove-Item var\codex-home\auth.json`。

**先确认登录状态**（不发起模型调用）：

```powershell
$env:CODEX_HOME = "$PWD\var\codex-home"
& "$env:LOCALAPPDATA\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe" login status
# 期望输出包含： Logged in using ChatGPT
```

---

## 3. 只跑一次的真实调用

```powershell
.\.venv\Scripts\python.exe tools\codex_one_call.py `
    --codex-home var\codex-home `
    --prompt "Reply with exactly: OK. Do not modify any files."
```

脚本会：使用**只读沙箱**（`--sandbox read-only`）、**禁用原生子 Agent**
（`--disable multi_agent`）、在工作区外的**空临时目录**里执行、**只调用一次**、
并把结果写入 `var/codex_onecall_result.json` 与原始捕获 `var/codex_onecall_raw.txt`。

安全护栏：若 `--workdir` 非空会**拒绝执行**（退出码 2）。

---

## 4. 结果记录模板

把 `var/codex_onecall_result.json` 连同下表填好回传（**不要粘贴凭据或聊天内容**）：

```markdown
### Codex 真实调用结果（单次）

- 执行时间：      <YYYY-MM-DD HH:MM>
- Codex 版本：    <codex --version 输出，形如 codex-cli x.y.z>
- 完整命令：      <粘贴脚本打印的 FULL COMMAND 一行>
- CODEX_HOME：    <路径>
- 工作目录 (-C)： <路径>
- 沙箱模式：      read-only
- 子 Agent：      已禁用（--disable multi_agent）
- 放行的网络目标：chatgpt.com:443, api.openai.com:443

| 项 | 值 |
|---|---|
| 退出码 | <0 或 非0> |
| 耗时 | <秒> |
| JSON 事件数 | <N> |
| 事件类型统计 | <如 thread.started:1, turn.started:1, item.completed:2, turn.completed:1> |
| thread_id | <有/无；有则填> |
| 最终回复文本 | <应为 OK> |
| **usage 事件数** | <N；若为 0 请说明> |
| usage 内容 | <token 数/费用字段；无则写"未提供"> |
| 工作目录事后条目 | <应为 0，验证只读> |
| 是否出现子 Agent 派生 | <有/无；有则贴相关事件> |

- 原始捕获文件：<codex_onecall_raw.txt 的相对路径>
- 异常/报错原文（若有）：<粘贴，勿含凭据>
```

---

## 5. 这次调用能回答哪些"未知"

| 之前的未知项 | 本次能否回答 |
|---|---|
| 流式事件序列（成功路径） | ✅ 能（观察 `turn.started` → `item.*` → `turn.completed` 完整序列） |
| **真实用量是否返回** | ✅ 能（`usage` 事件数与内容） |
| 只读沙箱是否真生效 | ✅ 能（工作目录事后应为空） |
| 会话恢复句柄 | ✅ 能（`thread.started` 的 `thread_id`） |
| **关闭 `multi_agent` 能否阻止子 Agent 派生** | ⚠️ 部分（本次提示词不会触发派生；第 3 步需另行批准） |
| 取消能力 | ❌ 不能（需专门设计的长任务，另行批准） |
| 工具启动前拦截 | ❌ 不能（需让 Codex 执行 shell 命令，另行批准） |

---

## 6. 约束（务必遵守）

1. **只跑一次**。失败即停，**不要自动重试、不要切换模型**。
2. 保持 `--sandbox read-only` 与 `--disable multi_agent`，不要为提高成功率放宽。
3. **不要粘贴凭据**（`auth.json`、token、cookie）到任何记录或聊天中。
4. 验证完成后删除 `var\codex-home\auth.json`。
5. 若仍需第 3 步（子 Agent 开关对比）或第 4/5 步（取消、工具栏截），
   **需另行批准**后再执行。
