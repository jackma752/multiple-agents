# Codex 真实调用：手工执行指南与结果记录模板

用途：本环境**无法完成** Codex 真实调用（见 §1 诊断），因此把这一步交给操作者在
网络可达的终端里完成。工具已备好，**只跑一次、绝不重试**。

---

## 0. 更新（2026-10-14，晚于下文）

本文档 §1 的诊断仍然有效（直连确实失败），但后半段的**操作方式已被取代**，
原因有二：

1. **直连失败已定位并绕过**：失败来自 Codex 没有使用代理；把代理与 CA
   注入子进程后，一次真实调用**成功**（退出码 0，含 `thread_id` 与 `usage`）。
   经过与命令见 `docs/codex-proxy-setup.md`；能力结论见 `docs/agent-codex-capabilities.md`。
2. **凭证位置已移到仓库之外**：下文 §2/§3 里的 `--codex-home var\codex-home`
   **不要再用**。`CODEX_HOME` 可能存放 `auth.json` 副本，必须位于仓库之外：
   默认 `%LOCALAPPDATA%\AgentHub\codex-home`，或设置 `AGENT_HUB_CODEX_HOME`。
   代码层回归测试见 `tests/test_hygiene.py`。

**待清理（需操作者确认）**：早期探测遗留的 `E:\plug\agent-hub\var\codex-home\`
仍在仓库内（已被 Git 忽略，其中**已无** `auth.json`），但它包含 Codex 的
`sessions\` 会话记录。建议删除；删除属于破坏性操作，**未擅自执行**。

真实调用的当前入口是 `tools/demo/codex_verify.py`
（`--mode stub` 离线自检 / `--mode real --i-understand-this-spends-quota` 真实调用），
说明见 `docs/p3-codex-adapter.md` §9。

---

## 1. 为什么必须手工执行：实测网络诊断

| 目标 | DNS | TCP 443（本机观测） | 判定 |
|---|---|---|---|
| `chatgpt.com` | OK → `128.121.243.235` | **超时** | **连接失败** |
| `api.openai.com` | **解析失败**（`getaddrinfo failed`） | — | **连接失败** |
| `pypi.org`（对照组） | OK → `151.101.0.223` | OK / TLSv1.3 / HTTP 200 | 连通 |

### 结论：**连接失败，原因待确定**

对 `chatgpt.com` 与 `api.openai.com` 的观测结果是**连接失败**。
**失败原因尚未确定**，可能来自多个层面，本环境的手工探测**无法区分**：

| 候选原因 | 现有证据能否排除 |
|---|---|
| 本执行环境/沙箱的网络限制 | **不能排除** |
| 本机或网络层的过滤、代理、DNS 策略 | 不能排除 |
| 目标侧不可达或限制该地区/网络 | 不能排除 |
| Codex 自身网络栈/依赖问题 | 不能排除 |

**重要更正（此前的判断有误）**：早先文档写过"这不是文件沙箱行为，因为文件沙箱会给出显式
`[sandbox: ...]` 标记"。这个推断**不成立**——**缺少 sandbox 标记并不能排除执行环境的限制**：
标记只覆盖被显式实现的文件访问策略，网络出口限制可能以超时/解析失败的形式静默生效，
也可能由环境之外的网络层造成。因此本条**只保留"连接失败、原因待确定"**。

本机观测到 `chatgpt.com` 解析为 `128.121.243.235`、`api.openai.com` 直接解析失败，
这与"正常公共 DNS 解析结果"不符，**提示**存在 DNS 层面的干预，但**仅是提示，不构成结论**。

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
