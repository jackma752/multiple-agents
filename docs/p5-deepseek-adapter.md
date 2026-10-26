# P5 · DeepSeek 适配器（OpenAI 兼容 chat/completions）

状态：**已实现，离线验证通过；真实往返未验证**（需要你自己的 `sk-...` key）。
日期：2026-10-26。分支：`main`（未推送、无 remote）。

这份文档只写能站得住的部分。"未验证"一节列出所有我没有证据的说法，别把
它们当结论。

---

## 1. 它是什么

一条 **HTTP 请求 = 一次调用** 的适配器：

```
POST {base_url}/chat/completions
Authorization: Bearer <key>        # 只出现在这个 header 里
{
  "model": "deepseek-chat",
  "messages": [{"role": "user", "content": "<调用快照里的任务文本>"}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

- `base_url` 默认 `https://api.deepseek.com`，`AGENT_HUB_DEEPSEEK_BASE_URL` 可覆盖
  （将来若有别的兼容端点，改环境变量即可，不改代码）。
- 请求体只允许四个键：`model` / `messages` / `stream` / `stream_options`。
  `packages/adapters/agenthub_adapters/deepseek.py` 里的 `ALLOWED_BODY_KEYS`
  在发出前再拦一次，**没有 `tools`**。
- 事件流按 SSE 逐行解析，`data: [DONE]` 结束；每一段文本通过 `STREAM_DELTA`
  转给页面，所以聊天窗口是逐字出现的。

## 2. 为什么这个通道可以进严格模式

Codex / Claude Code 在严格模式被拦，是因为它们会**在本机启动进程**，而进程
可能派生我们控制不了的子 Agent（`native_subagents_uncontrolled`）。DeepSeek
通道没有这个东西：

| 检查项 | 这个通道的事实 |
|---|---|
| 本地进程 | 没有。只有一次 HTTPS 请求 |
| 工具通道 | 没有。请求体里不存在 `tools` / `functions` |
| 子 Agent 通道 | 没有。一次请求一条消息，没有双向通道 |
| 工作目录 / 沙箱 | 不存在。不读也不写本机文件 |
| 中途取消 | **做不到**。请求发出后只能停止读取 |

所以 `execution_admission()` 返回 `mode="strict"`、`code="no_local_execution_channel"`，
并在 `reason` 里直接写明残余风险：提示词会发送到 provider，且无法中途取消。

**这不是"更安全的模型"，是"更窄的通道"。** 它换来的确定性只关于"这台机器上
不会发生什么"，不关于回复内容是否可靠。

## 3. 凭据

- 位置：`%LOCALAPPDATA%\AgentHub\deepseek\credentials.json`，
  内容 `{"api_key": "sk-..."}`。`AGENT_HUB_DEEPSEEK_HOME` 可改目录。
- **路径在仓库内 → 直接拒绝**（`deepseek_credentials_inside_repository`）：
  一个 `git add -f` 就能把 key 推出去，这条线不能靠自觉。
- **不读任何环境变量里的 key**。shell 里飘着一个 `DEEPSEEK_API_KEY` 不会改变
  哪张账单付钱。（`AGENT_HUB_DEEPSEEK_HOME` 只改文件位置，不提供 key。）
- key 只出现在 `Authorization` header；不进请求体、不进事件、不进数据库记录。
  日志路径上还有一层 `redact()`：任何形如 `sk-...` 的字符串会被替换成 `sk-***`。
- 页面/接口只显示"文件存在 / 大小"，**从不显示内容**（`credential_status()`）。

## 4. 失败语义（与项目其余部分一致）

| 情况 | 结果 | 是否占槽 | 理由 |
|---|---|---|---|
| 没有 gate_token | `refused: missing_gate_token` | 否 | 未经审批就启动是 bug，不是模式 |
| 凭据缺失 / 路径非法 / JSON 坏 | `refused: <code>` | 否 | 本地拒绝，一个 token 都没花 |
| 401 / 403 | `refused: deepseek_unauthorized` | 否 | 没生成任何内容 |
| 402 | `refused: deepseek_insufficient_balance` | 否 | 余额不足，重试只会重复失败 |
| 429 | `refused: deepseek_rate_limited` + `provider_quota` | 否 | 阶段层据此进入"等待额度恢复"并按 `reset_at` 重试 |
| 连接中断 / 超时 / SSL | `unknown: transport_error` | **是** | 请求可能已被受理并计费，不能宣称"没发生" |
| 流结束但没有 `finish_reason` | `failed: stream ended before finish_reason` | 否 | 连接已关闭、无残留工作，但回复不完整，绝不算成功 |

"流断了"判成 unknown 而不是 failed，是因为只有 provider 能证明那次请求没被
计费——我们证明不了。反过来"没有 `finish_reason`"可以判 failed，因为我们确实
拿到了连接关闭这个事实。

## 5. 用量

末 chunk 的 `usage` 字段映射到账本：

| DeepSeek | 账本字段 |
|---|---|
| `prompt_tokens` | `input_total` |
| `completion_tokens` | `output_total` |
| `prompt_cache_hit_tokens` | `cache_read` |
| `prompt_cache_miss_tokens` | `input_uncached` |
| `completion_tokens_details.reasoning_tokens` | `reasoning` |
| —（该 API 没有缓存写入计费） | `cache_write = None`（不假装成 0） |

映射名 `deepseek.chat_completions.usage` 已加入 `agenthub/usage.py` 的封闭集合
`MAPPINGS`；自相矛盾的数字（命中数大于总数、总数对不上）会记 `mapping_mismatch`，
缺缓存字段记 `cache_fields_missing`，都不会被悄悄吞掉。

没有记录（比如凭据缺失、根本没发请求）时 `usage_report()` 返回 `None`，账本按
`not_launched` 记**已知的零**，而不是"未知"。

## 6. 怎么用

### 离线（不需要 key，推荐先跑这个）

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\tools\demo\serve.py' --port 8080 `
    --adapter deepseek --deepseek-stub ok `
    --data-dir 'E:\plug\agent-hub\var\p5-demo\data' --files-root docs
```

打开 `http://127.0.0.1:8080/chat`，Agent 选 `deepseek`，执行模式选**严格模式**，
发一条消息并批准 —— 回复是固定桩文本（"离线回复。"）。

桩场景 `--deepseek-stub X`：

| X | 模拟什么 |
|---|---|
| `ok` | 正常两段增量 + `finish_reason` + usage |
| `quota` | 429 → 阶段层进入"等待额度恢复" |
| `unauthorized` | 401 → 终态拒绝 |
| `truncated` | 有增量但没有 `finish_reason` → failed |
| `empty` | 一个事件都没有 → failed（流不完整） |

### 真实调用（这一步要花钱，必须你自己决定）

1. 建凭据文件（**不要**放进仓库）：

   ```powershell
   $dir = Join-Path $env:LOCALAPPDATA 'AgentHub\deepseek'
   New-Item -ItemType Directory -Force -Path $dir | Out-Null
   Set-Content -Path (Join-Path $dir 'credentials.json') -Encoding utf8 -NoNewline `
       -Value '{"api_key":"sk-把这里换成你的key"}'
   ```

2. 起服务（去掉 `--deepseek-stub`），代理与 CA 按你机器上的实际情况设：

   ```powershell
   $env:HTTPS_PROXY = 'http://127.0.0.1:8090'
   $env:HTTP_PROXY  = 'http://127.0.0.1:8090'
   $env:NO_PROXY    = 'localhost,127.0.0.1'
   & 'E:\plug\agent-hub\.venv\Scripts\python.exe' 'E:\plug\agent-hub\tools\demo\serve.py' --port 8080 `
       --adapter deepseek --data-dir 'E:\plug\agent-hub\var\p5-demo\data' --files-root docs
   ```

3. 页面里 Agent 选 `deepseek`、严格模式、发一条**便宜**的提示（比如"用一句话
   说明你是谁"），批准。这条会真的花钱，量很小。

启动横幅会打印 endpoint / model / 凭据是否在位（不打印内容）/ 代理 / CA / 超时，
先看一眼再发。

## 7. 未验证（别当成已通过）

以下都没有真实证据，只有实现与离线测试：

- **真实 api.deepseek.com 往返**：本机还没发过一次带 key 的请求。
- **真实 SSE 分片节奏**：桩是两段，真实可能是几十段或整段一次到。
- **`prompt_cache_hit_tokens` 的真实语义**：DeepSeek 文档描述与 OpenAI 的
  `cached_tokens` 不完全等价，我没有实测数字可对照。
- **429 的 `Retry-After` 真实格式**：代码同时接受秒数与缺失，实际返回哪一种没验。
- **代理链路**：`http://127.0.0.1:8090` 是按本机 Codex 的历史配置推测的，
  对 `api.deepseek.com` 是否放行未验。
- **取消**：`cancel` 返回 `unsupported`，中途停止的实际表现（socket 关闭 vs 请求
  继续在服务端跑完）未验。

## 8. 已知限制（设计选择，不是缺陷）

- **没有会话恢复**：API 无状态，`supports_native_resume() = False`，Worker 会在
  消费审批前就拒绝 `native_resume` 调用。
- **没有工具**：一次请求一条消息，没有可阻塞的审批回合。要让 DeepSeek 用工具，
  得等 app-server 那种双向审批通道。
- **不在 `multi` 里**：`multi` 目前只服务 `codex` 和 `claude-code`（已验收的行为
  和测试断言都在），加第三个 agent 属于单独变更，等这批稳定后再做。
- **会出境**：提示词与回复都发往 provider。这一条没有任何本地控制能消除。

## 9. 接入点（给后来的人）

| 文件 | 加了什么 |
|---|---|
| `packages/adapters/agenthub_adapters/deepseek.py` | 适配器本体、凭据读取、HTTP/桩传输、SSE 解析 |
| `packages/contracts/agenthub/usage.py` | `MAPPING_DEEPSEEK` + `_deepseek()` 映射 |
| `apps/worker/agenthub_kernel/config.py` | `ENV_DEEPSEEK_*`、`deepseek_home()`、`deepseek_settings()` |
| `apps/worker/agenthub_kernel/adapters.py` | `DEEPSEEK` kind、`REMOTE_KINDS`、`_build_deepseek()`、`describe()` 的 deepseek 字段 |
| `tools/demo/serve.py` | `--adapter deepseek`、`--deepseek-stub`、启动横幅 |
| `tests/adapters/test_deepseek_adapter.py` | 离线测试（凭据、请求体、失败语义、SSE、用量、取消） |
