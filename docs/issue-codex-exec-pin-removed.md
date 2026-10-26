# 未决问题：`codex exec` 被钉住的二进制目录被自动更新移除

**状态：未解决（OPEN）。**
**不得因为本文件之外的工作完成、或 app-server 通道可用，而视为已修复。**

最后更新：2026-10-26。

---

## 1. 现象（实际观测）

真实 `codex exec` 调用被内核在启动前拒绝：

| 项 | 值 |
|---|---|
| 调用 id | `inv-1da9945872d3`（2026-10-26T02:05Z） |
| 内核记录 | `error = 'refused: executable missing'` |
| `identity_detail` | `executable missing` |
| 结果 | 未启动进程、未产生模型回合、未消耗额度 |

## 2. 涉及的身份（代码常量）

`apps/worker/agenthub_kernel/config.py:97-103` 钉住的 exec 运行时：

| 项 | 值 |
|---|---|
| 路径 | `%LOCALAPPDATA%\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe` |
| 版本 | `codex-cli 0.162.0-alpha.17.2` |
| sha256 | `d13914ced6c7af174d8d638db938284231227312104b06a25e9608c632a7daec` |
| 大小 | 334121776 |

## 3. 已确认与未确认

**已确认**

- 该路径下不再存在可用的 `codex.exe`：适配器每次启动前都做一次存在性检查，检查结果为 missing。
- 另一份受钉的运行时**存在**：`%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-c81ed464e5e7cafd\codex.exe`
  （`PINNED_APPSERVER`，`agenthub_adapters/appserver_adapter.py:114-119`，`codex-cli 0.163.0-alpha.11.2`，
  sha256 `c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a`，大小 336134960）。
- `multi` 适配器**不受影响**：`apps/worker/agenthub_kernel/adapters.py:58-64` 会把 codex 强制到
  app-server 传输，因此 `--adapter multi` 下不会走 exec。

**未确认**

- "Codex 自动更新删除了旧版本目录"是**推断**，没有捕获到删除动作本身（没有更新日志、没有时间点证据）。
- 0.163 运行时在 **exec** 传输下能否工作**未验证**——它只在 app-server 下被验证过。

## 4. 影响

- 单 kind `codex` 且 `--codex-transport exec`（或 `AGENT_HUB_CODEX_TRANSPORT=exec`）时，真实调用一律被拒。
- 不影响审批/配额/阶段等内核逻辑；拒绝发生在启动前，不消耗槽位。

## 5. 明确不采用的做法

- **不**把 exec 钉子改指到未评审、未核验的二进制去"让它先跑起来"。
- **不**在缺少哈希核验的情况下放开存在性检查。

## 6. 关闭条件（任一）

1. 原路径恢复且 sha256 / 大小与上表一致；或
2. 经用户批准，把 exec 钉子改成另一份**已评审**的 artifact，重新记录 sha256 / 大小 / 版本，并在
   app-server 之外**单独验证 exec 传输**能出一个真实回合（未验证就写未验证）。

可用 `AGENT_HUB_CODEX_PATH` / `AGENT_HUB_CODEX_SHA256` / `AGENT_HUB_CODEX_SIZE` /
`AGENT_HUB_CODEX_VERSION`（`apps/worker/agenthub_kernel/config.py:292-297`）临时覆盖，但覆盖本身
不构成核验，覆盖后仍必须有独立的真实运行记录。
