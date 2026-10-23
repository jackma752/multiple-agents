# Claude Code 最小接入（P3 工作流 B，2026-10-23，离线）

本轮只做离线实现：**没有运行过 Claude Code**（连 `--version` 都没有），没有任何模型回合，没有读取凭据。下文凡是来自官方文档、未在本机实测的内容，都标为“文档，未实测”。

## 1. 本机安装（静态核对）

只读取文件属性、哈希和签名，未执行。

| 项 | 结果 |
|---|---|
| 安装形态 | 只有 Claude 桌面应用自带的 CLI（MSIX 包缓存）。不在 PATH 上；没有 npm、原生安装器或 WinGet 安装；本机没有 node |
| 固定版本 | `2.1.308`（FileVersion 资源 `2.1.308.0`，ProductName `Claude Code`） |
| 路径 | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.308\c17a819afb40\claude.exe`（`%APPDATA%\Claude\claude-code\...` 是同一文件的虚拟化视图，哈希相同） |
| 大小 / SHA-256 | 297,376,928 字节 / `c9796ba22a9221586b36371b36f5b66198d66eb4badf6a4cf4a5f7b07becea53`（与同目录 `.payload` 记录一致） |
| 签名 | Authenticode 有效，签名者 `Anthropic, PBC`，DigiCert G4 代码签名 CA，证书指纹 `60C2D9C51F09822FFF4F194DE9EEDB67451292D1`，带时间戳 |
| 另一个版本 | `2.1.304`（293,227,168 字节，`019e5174…5fdd`），同样有效签名；不使用 |
| 登录 | 默认配置目录 `%USERPROFILE%\.claude` 存在，但**没有** `.credentials.json`（只检查存在与否）。桌面会话的认证由桌面应用管理（本会话环境中有 `ANTHROPIC_BASE_URL`，未读取取值），独立 CLI 不能借用 |
| 代理 | `127.0.0.1:8090` 在监听（mihomo）。按 [代理说明](codex-proxy-setup.md) 第 7 节，它是普通 CONNECT 隧道，不拦截 TLS |
| 二进制字符串 | 固定参数中的每个标志、`error_max_turns` / `error_during_execution` / `error_max_budget_usd`、`permission_denials`、`total_cost_usd`、`NODE_EXTRA_CA_CERTS`、`CLAUDE_CONFIG_DIR`、`DISABLE_AUTOUPDATER` 都能在文件中找到（静态扫描，未实测） |

风险：这是桌面应用管理的文件，桌面更新可能删除 2.1.308 目录（与 Codex alpha.11 被更新删除相同）。删除或改动后身份核验失败，调用被拒，需要重新固定（代码评审决定）。

## 2. 接口（文档，未实测）

依据 code.claude.com 的 headless、CLI reference、authentication、network-config 页面（2026-10-23 读取）。

- 一次调用 = 一个进程：`claude.exe` 加固定参数，提示词从 **stdin** 传入，不放进 argv（用户文本不会被当成标志解析，也不出现在进程列表里）。
- 固定参数（`CLAUDE_ARGV_TAIL`）：`-p --output-format json --safe-mode --tools "" --strict-mcp-config --disable-slash-commands --permission-mode dontAsk --max-turns 1 --max-budget-usd 1.00 --no-session-persistence`。
  - `--safe-mode`：不加载 CLAUDE.md、skills、插件、hooks、MCP、自动记忆，但保留正常登录。`--bare` 不读 OAuth 登录（只认 API key），所以不用。
  - `--tools ""`：不提供内置工具（含 Bash、编辑、Agent）。**只是请求，不是证明**。
  - `--no-session-persistence`：不把会话写盘，也就不能恢复。
  - `--max-budget-usd` 是客户端估算，文档说明实际花费可能超过。
- 输出：stdout 上一个 JSON 对象，`type=result`，`subtype` 为 `success` 或 `error_*`，`is_error`，成功时 `result` 是回复文本，另有 `session_id`、`num_turns`、`total_cost_usd`、`usage`、`modelUsage`、`permission_denials`。运行中的失败（例如未登录）也以 result 对象输出；无效参数在开始前报到 stderr，退出码非 0。
- 子进程环境：去掉所有继承的 `CLAUDE*` / `ANTHROPIC*` 变量（不借用桌面会话的端点、令牌或嵌套会话标记），以及 `NODE_TLS_REJECT_UNAUTHORIZED`、`NODE_OPTIONS` 和继承的代理变量；再设置 `CLAUDE_CONFIG_DIR=<专用目录>`、`DISABLE_AUTOUPDATER=1`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`、`HTTPS_PROXY/HTTP_PROXY=http://127.0.0.1:8090`、`NO_PROXY=localhost,127.0.0.1`、`NODE_EXTRA_CA_CERTS=<venv certifi>`（追加，不替换系统证书库）。**从不关闭证书验证。**
- 专用配置目录默认 `%LOCALAPPDATA%\AgentHub\claude-config`（仓库外，`AGENT_HUB_CLAUDE_CONFIG_DIR` 可改）。用户在自己的终端里登录一次；Hub 只检查 `.credentials.json` 是否存在，从不读取。
- 工作目录：每次调用一个新的空目录，默认在 `%LOCALAPPDATA%\AgentHub\claude-code-runs\<invocation_id>`（仓库外）。stderr 留在同级 `_logs`，只按大小和哈希记录；stdout 解析后立即删除。

## 3. 准入与结算

| 情况 | 结果 |
|---|---|
| 离线桩（进程内 `FakeClaudeRunner`，或 `tools/demo/stub_claude_code.py`） | simulation |
| 真实 CLI，严格模式 | 拒绝：`claude_code_controls_unverified`（原生子 Agent 与工具事前拦截未验证） |
| 真实 CLI，监督模式 | 只放行用代码常量构造的 `ClaudeCodeLauncher`，并要求：身份等于常量、文件存在且大小一致、参数未改、配置目录和工作目录在仓库外、有代理和可读 CA、时限 0–600 秒，**且 `ZERO_MODEL_IDENTITY_REPORT` 已记录**。当前该常量为 `None`，所以真实启动**一律被拒**（`identity_unconfirmed`）。批准时仍需本次调用的显式同意 |
| 启动前 | 再做一遍上述检查并完整计算 SHA-256，不符就不启动 |

结算沿用现有规则：

- 成功 result 且退出码 0 → succeeded，释放名额，`result` 作为回复存入适配器记录，再由 `capture_reply` 写入会话；
- 错误 result（`is_error` 或 `error_*`）→ failed，释放名额，没有回复；
- 没有输出且退出码非 0 → failed（进程已结束，未报告任何结果）；
- 本地超时、本地停止、输出不是一个 result 对象、成功 result 但退出码非 0 → unknown，**保留名额**；
- `cancel` 返回 `unsupported`；本地停止只结束本次入口进程，不声称远端已停。

提示词和回复不进入事件和日志；适配器记录只保存提示词的 SHA-256 与长度。

## 4. 能力（全部未验证）

发消息、取最终回复、读取用量（有则记录）已实现，**都没有在真实 Claude Code 上运行过**。流式、恢复会话、取消、工具拦截、控制原生子 Agent 都没有实现或未验证，见 [能力矩阵](adapter-capabilities.md) 的 Claude Code 一节。

## 5. 离线验证

- `tests/adapters/test_claude_code_adapter.py`：结果解析与判定、成功（有/无用量）、错误 result、达到回合上限、输出损坏、挂起超时、非 0 退出、成功但退出码非 0；stdin 传入、argv 固定；环境变量剥离；准入矩阵；能力表措辞。
- `tests/policy/test_claude_code_gate.py`：未批准/拒绝/撤销/过期启动 0 次；并发运行最多启动 1 次；严格模式在消费审批之前拒绝；监督模式需要本次显式同意；成功结算并释放名额；失败路径；事件与内核记录中没有提示词和回复原文；`build_adapter` 选择。
- `tests/test_claude_code_min.py`：真实测试驱动的桩模式与拒绝路径。

桩的输出是**按文档手写的，不是捕获的**。

## 6. 有界真实测试申请

**状态（2026-10-24）**：
- 步骤 A 执行了一次，结果 **INCONCLUSIVE**（见第 7 节）。
- 用户选择 (a)：保留 `--max-turns`，报告哈希已记录为 `ZERO_MODEL_IDENTITY_REPORT`。
- 步骤 L 和步骤 B 的最新申请见第 8 节，**未执行**。下文保留申请原文。

驱动：`tools/probes/claude_code_min.py`。三步依次进行，每步之后停下核对；任何一步不是 PASS 就停止。

**前置（离线）**：工作树 `feat/claude-code-min` 上的提交未变、工作区干净；固定文件大小和 SHA-256 仍等于常量；8090 在监听；venv 的 certifi 哈希为 `50f79f98…0785`。

**步骤 A：零模型身份核验**（执行 `--version` 和 `--help` 各一次，不登录、不调用模型）

```powershell
cd E:\plug\agent-hub\.worktrees\claude-code-min
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\claude_code_min.py --step identity --mode real --approve-zero-model-identity-check
```

- 每个命令本地时限 30 秒，输出写入 `var/claude-code-min/identity-real-*/`，报告只登记 SHA-256。
- PASS：两个命令退出码 0；版本行以 `2.1.308` 开头；运行前后文件哈希都等于常量；固定参数的 9 个标志都出现在 `--help` 中。
- INCONCLUSIVE：只有部分标志不在 `--help` 中（文档说明 `--help` 不列出全部标志）。此时不进入步骤 B，先由用户决定。
- FAIL：退出码非 0、超时、版本不符、哈希变化。
- PASS 后，用一个离线提交把报告的 SHA-256 写入 `ZERO_MODEL_IDENTITY_REPORT`（代码评审决定），重新跑离线测试。

**步骤 L：用户自己登录专用配置目录**（用户在自己的终端操作，助手不参与，也不读取任何凭据）

```powershell
$env:CLAUDE_CONFIG_DIR = "$env:LOCALAPPDATA\AgentHub\claude-config"
& "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.308\c17a819afb40\claude.exe" auth login
```

完成后只检查 `.credentials.json` 是否存在。若用户不愿另建登录，替代方案（`claude setup-token` 加 `CLAUDE_CODE_OAUTH_TOKEN`，或 Console API key）需要另改代码和评审，不在本申请内。

**步骤 B：最多一个模型回合**（经过 Hub：API → 审批并显式同意 → Worker 消费与预留名额 → 适配器 → 结算）

```powershell
cd E:\plug\agent-hub\.worktrees\claude-code-min
E:\plug\agent-hub\.venv\Scripts\python.exe tools\probes\claude_code_min.py --step turn --mode real --approve-one-model-turn --acknowledge-supervised-boundary
```

- 固定提示词：`Reply with exactly: OK. Do not use tools, create subagents, or modify any files.`；没有工具，`--max-turns 1`。
- 预算：`--max-budget-usd 1.00`（客户端估算）；本地时限 180 秒；1 次调用，不重试。
- 预检（任何一项不满足就不启动）：身份报告已记录、固定身份匹配、配置目录在仓库外且有登录文件、工作目录在仓库外、CA 哈希一致、监督准入通过、严格准入仍被拒。
- PASS：succeeded；启动 1 次；名额已释放；绑定身份一致；回复恰好为 `OK`；有用量；`num_turns=1`；`permission_denials` 为 0；工作目录为空。
- INCONCLUSIVE：调用 succeeded，但回复不完全是 `OK`、没有用量、回合数不是 1、出现权限拒绝或工作目录不空。
- FAIL：没有 succeeded（failed、unknown）、启动次数不是 1、名额未释放、绑定身份不一致或超时。

**能确认的**（只认定当次）：固定版本能以这组参数非交互运行一次；stdin 传入提示词可用；JSON result 的实际形状；回复与用量能经过 Hub 结算并写入会话；代理路径可用。

**不能确认的**：工具确实被关闭（模型没有尝试工具不等于不能用）；原生子 Agent 可控；取消或中断；流式；会话恢复；后代进程（本适配器不检查）；`~/.claude.json` 等文件是否被写入；长对话或其他提示词下的行为。

**停止规则**：出现任何异常（退出码、超时、版本或哈希变化、预检失败、意外的工具或权限活动）立即停止并报告；不重试，不换模型，不放宽权限或参数，不去掉 `--safe-mode`/`--tools ""`；保留代理与 CA 设置，**不关闭证书验证**；不进入下一步。

## 7. 步骤 A 实录（2026-10-23 23:35，用户批准，执行一次）

- **授权**（用户原话）：“批准按顺序执行两项零模型检查，各一次：……再 Claude Code 身份核验（feat/claude-code-min 8ba6244，docs/claude-code-adapter.md 第 6 节步骤 A）。模型回合为 0；任何异常立即停止，不重试，不进入模型回合。”
- **执行前确认**：HEAD 为 `8ba6244`，工作树干净；8090 在监听；certifi 哈希为 `50f79f98…0785`。
- **命令**：第 6 节步骤 A 的命令，只执行一次，退出码 1（驱动在结果不是 PASS 时返回 1）。
- **结果：INCONCLUSIVE**（`help_missing_flags`）：
  - `--version` 与 `--help` 都返回 0，没有超时；
  - 版本行为 `2.1.308 (Claude Code)`；
  - 运行前后文件哈希都等于常量；
  - 固定参数的 9 个标志中，`--help` 列出了 8 个，**只有 `--max-turns` 没有列出**。二进制字符串中能找到它（第 1 节，静态扫描），官方文档也列出了它（文档，未实测）。
- **子进程环境**：
  - 去掉了继承的 `ANTHROPIC_BASE_URL`、全部 `CLAUDE*` 会话变量和代理变量；
  - 只设置了 `CLAUDE_CONFIG_DIR`、`DISABLE_AUTOUPDATER`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`；
  - 运行后专用配置目录 `%LOCALAPPDATA%\AgentHub\claude-config` 仍不存在，说明这两条命令没有写配置。
- **按申请停止**：
  - 没有把报告哈希写入 `ZERO_MODEL_IDENTITY_REPORT`，所以真实启动仍一律被拒；
  - 没有进入步骤 L（登录）和步骤 B（模型回合）；没有读取任何凭据。
- **需要用户决定**：
  - (a) 接受这个结果：记录报告哈希，`--max-turns` 继续保留。依据是文档说明无效参数会在开始前报错并以非 0 退出。在模型回合中如果这个标志被拒，会判 FAIL，不消耗模型回合，但会用掉那一次已批准的尝试。
  - (b) 从固定参数中去掉 `--max-turns`：`--tools ""` 时没有工具循环，回合数由单条提示决定，预算仍由 `--max-budget-usd` 约束。需要改代码并做离线回归，不需要重做步骤 A，因为其余 8 个标志都已列出。

| 证据（`var\claude-code-min\identity-real-v6v3sh1q\`，不入库） | 字节 | SHA-256 |
|---|---|---|
| `report.json` | 2105 | `b57a1d5f0d2a0bd1b7c35031c3fdafb74e2a763d82a12a1f40844b8b8cbd5089` |
| `version.txt` | 22 | `b354fe63f3ea7b46441790aa41c704340be3e4722cd759d1b77da284764c82ef` |
| `help.txt` | 22319 | `b10f5c36adfe3285f6c5a0d18284f466e6c4486499de56ef62745e13312a7fbc` |

## 8. 记录身份核验，以及步骤 L、步骤 B 的申请（2026-10-24）

### 8.1 已做（离线）

- 按用户决定 (a)，`ZERO_MODEL_IDENTITY_REPORT` 改为第 7 节 `report.json` 的 SHA-256：`b57a1d5f…5089`。`--max-turns` 保留。
- 影响：
  - 监督准入不再因 `identity_unconfirmed` 拒绝；其余条件照旧，包括固定身份、参数、仓库外目录、代理、CA 和时限；
  - 严格模式仍然拒绝；每次调用仍需审批，以及本次的显式同意；
  - 绑定身份不变。
- 测试：原来依赖“常量为空”的测试改为在测试内把常量设为空，拒绝路径仍有覆盖；另加一项测试，固定常量等于已登记的报告哈希。

### 8.2 步骤 L：用户自己登录专用配置目录（未执行，助手不参与）

由你在自己的终端里操作。助手不运行这些命令，不查看浏览器授权页面，不读取任何凭据。

```powershell
$env:CLAUDE_CONFIG_DIR = "$env:LOCALAPPDATA\AgentHub\claude-config"
$env:HTTPS_PROXY = "http://127.0.0.1:8090"; $env:HTTP_PROXY = $env:HTTPS_PROXY
$env:NODE_EXTRA_CA_CERTS = "E:\plug\agent-hub\.venv\Lib\site-packages\certifi\cacert.pem"
& "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.308\c17a819afb40\claude.exe" auth login
```

- 代理和 CA 与适配器给子进程设置的相同；`NODE_EXTRA_CA_CERTS` 是追加证书，不关闭验证。不加代理也能登录的话，可以省略这两行。
- `--help` 列出了 `auth`（Manage authentication）。如果 `auth login` 不可用，先运行 `... claude.exe auth --help` 查看子命令；不要改用其他登录方式，换方式需要另行评审。
- 登录完成后告诉我。我只检查 `%LOCALAPPDATA%\AgentHub\claude-config\.credentials.json` 是否存在，不打开。

### 8.3 步骤 B：最多一个模型回合（未执行，需单独批准）

- **被测提交**：`feat/claude-code-min` 上包含本节的提交。执行前确认 HEAD 和工作树都没有变化。
- **前置**：
  - 固定文件的大小和 SHA-256 仍等于常量；
  - 8090 在监听；venv 的 certifi 哈希为 `50f79f98…0785`；
  - 专用配置目录中有登录文件（只检查是否存在）。
- **命令**（一次）：

  ```powershell
  Set-Location E:\plug\agent-hub\.worktrees\claude-code-min
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\claude_code_min.py --step turn --mode real --approve-one-model-turn --acknowledge-supervised-boundary
  ```

- **驱动的预检**（任何一项不满足就不启动）：
  - 身份报告已记录、固定身份匹配；
  - 配置目录在仓库外且有登录文件；工作目录在仓库外；
  - CA 哈希一致；
  - 监督准入通过，严格准入仍被拒。
- **路径**：进程内 API → 审批并给出本次同意 → Worker 消费审批并预留名额 → 适配器启动一个 `claude -p` 进程 → 结算。
- **提示词与参数**：固定提示词 `Reply with exactly: OK. Do not use tools, create subagents, or modify any files.`；`--tools ""`、`--max-turns 1`、`--safe-mode`、`--no-session-persistence`。
- **上限**：
  - 1 次调用，不重试，不发第二个回合；
  - `--max-budget-usd 1.00`（客户端估算，实际花费可能超出）；
  - 本地时限 180 秒。
- **判定**：
  - **PASS**：succeeded；启动 1 次；名额已释放；绑定身份一致；回复恰好为 `OK`；有用量；`num_turns=1`；`permission_denials` 为 0；工作目录为空。
  - **INCONCLUSIVE**：调用 succeeded，但回复不完全是 `OK`、没有用量、回合数不是 1、出现权限拒绝或工作目录不空。
  - **FAIL**：没有 succeeded（failed 或 unknown）；启动次数不是 1；名额未释放；绑定身份不一致；超时；CLI 拒绝了某个固定参数（例如 `--max-turns`），这种情况在开始前退出，不消耗模型回合，但会用掉这次批准。
- **只认定**：固定版本能用这组参数非交互运行一次；stdin 传入提示词可用；JSON result 的实际形状；回复和用量能经过 Hub 结算并写入会话；代理路径可用。
- **不认定**：
  - 工具确实被关闭：模型没有尝试使用工具，不等于不能用；
  - 原生子 Agent 可控；取消或中断；流式；会话恢复；后代进程；
  - 配置目录之外的文件是否被写入；
  - 长对话或其他提示词下的行为。
- **停止规则**：任何异常立即停止，不重试，不换模型，不放宽权限或参数，不去掉 `--safe-mode` 和 `--tools ""`；保留代理与 CA，**不关闭证书验证**。
- **证据**：登记 `var/claude-code-min/turn-real-*/report.json` 和 stderr 日志的 SHA-256；打标签 `evidence/cc-turn-real-run-1-<pass|fail|inconclusive>`。
- **之后**：通过后，Codex 和 Claude Code 都已各有一次真实运行。再申请在 `feat/p3-two-agent` 上做一次有界的双 Agent 真实对话（含一次经批准的交接）。为此需要先把本分支和 `feat/codex-runtime` 的记录提交合并进 `feat/p3-two-agent`，合并需要你另行批准。
