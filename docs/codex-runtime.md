# Codex 运行时：仓库外的固定副本与新的步骤 A 申请（2026-10-23）

## 1. 为什么要另存一份

2026-10-23 18:20，Codex 桌面应用自动更新，删除了固定版本所在的 `%LOCALAPPDATA%\OpenAI\Codex\bin\6257d9e8f63c0f2b\`（alpha.11，S3–S6-3 都在这个版本上运行）。当天的 S6-4 因此在前置检查阶段停止：没有启动服务，也没有模型调用。

桌面应用每次更新都会替换 `bin\<hash>` 目录，所以固定的运行时不能放在那里。现在改放在 AgentHub 自己的目录里，按内容寻址：

```
%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-<codex.exe 的 SHA-256 前 16 位>\
```

由 `tools/probes/codex_runtime_stage.py` 复制。复制前后都按代码里登记的哈希和大小核对，任何文件对不上就拒绝；已经暂存的文件从不覆盖；文件设为只读。同目录写入 `provenance.json`，内容包括来源路径、时间、大小、SHA-256 和 Authenticode 签名。这个脚本**不执行 Codex**，签名用 PowerShell 的 `Get-AuthenticodeSignature` 读取。

## 2. 只读盘点（2026-10-23，未执行任何程序）

| 位置 | 内容 |
|---|---|
| `bin\37f54d68e5533a51\` | `codex.exe`、`codex-command-runner.exe`、`codex-windows-sandbox-setup.exe`、`codex-code-mode-host.exe`（文件时间 05:06–05:07，目录 18:20 写入） |
| `bin\2d8a5d5756dbf318\` | 只有 `rg.exe` |
| `%USERPROFILE%\.codex\.sandbox-bin\` | alpha.11 的 `codex.exe`，与原固定值**逐字节相同**（`00c0f1e4…d67c`，342187312 字节）。还有历次版本的 `codex-command-runner-<版本>.exe`，其中 `…-0.163.0-alpha.11.2.exe` 与上面新 runner 的哈希相同 |
| `%USERPROFILE%\.codex\plugins\.plugin-appserver\` | 与 `bin\37f54d68e5533a51\` 相同的四个文件（大小和时间相同） |
| 应用商店包 | `OpenAI.Codex 26.1016.8622.0`（桌面应用版本，不是 CLI 版本） |

所有 exe 都由 `OpenAI OpCo, LLC` 签名，状态 `Valid`，带 Microsoft 时间戳。每个构建用一张短期证书：新构建的证书 2026-10-24 06:46 到期，alpha.11 的那张 2026-10-23 06:49 已到期。因为有时间戳，签名在证书到期后仍然有效。

**版本推断（未验证）**：新 runner 与 `codex-command-runner-0.163.0-alpha.11.2.exe` 逐字节相同，所以新 `codex.exe` 很可能是 `codex-cli 0.163.0-alpha.11.2`。文件本身没有版本资源，版本以步骤 A 运行 `--version` 的结果为准。

## 3. 暂存结果

| 候选 | 用途 | 目录 | 文件 |
|---|---|---|---|
| `alpha.11.2` | **运行时**（准备重新固定） | `sha256-c81ed464e5e7cafd\` | `codex.exe` `c81ed464…122a`（336134960）；`codex-command-runner.exe` `ea7668d3…f4f3f`；`codex-windows-sandbox-setup.exe` `e2cec926…6a42`；`codex-code-mode-host.exe` `7200f4fe…9bf2`；`path\rg.exe` `8cb6fb5c…558a` |
| `alpha.11` | **只作存档**，不作为运行时，也不会被固定 | `sha256-00c0f1e4099246fd\` | `codex.exe` `00c0f1e4…d67c`（342187312）；`codex-command-runner-0.163.0-alpha.11.exe` `e95db223…ecfe7`。alpha.11 的 `codex-windows-sandbox-setup.exe` 和 `codex-code-mode-host.exe` 已无法找回 |

`provenance.json` 的哈希和暂存时的实际输出见第 6 节。

alpha.11 为什么只存档：它缺少同版本的沙箱安装程序和 code-mode host。在缺件的情况下，沙箱内命令会怎样执行无法确定，所以不拿它做运行时。保留这份副本，是为了让历史证据所对应的二进制有一份不会被更新删除的拷贝。

## 4. 新的步骤 A 申请（零模型）

**状态**：2026-10-23 经用户批准，在 `73da7dc` 上执行一次，结果 **IDENTICAL_SCHEMA**，版本为 `codex-cli 0.163.0-alpha.11.2`，与推断一致。实录见第 7 节。授权已用完。下文保留申请原文。

- **被测提交**：`feat/codex-runtime` 上包含本节的提交。
- **被测程序**：暂存的 `alpha.11.2`，即 `%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-c81ed464e5e7cafd\codex.exe`。
- **命令**：

  ```powershell
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\codex_runtime_step_a.py --capture alpha.11.2 --approve-zero-model-identity-check
  ```

- **执行前的被动检查**（任何一项不通过就停止，什么都不执行）：
  - 五个暂存文件的哈希和大小都与登记值一致，`provenance.json` 存在；
  - 五个文件的签名都是 `Valid`；
  - alpha.11 基线 `identity-hkzdmoqj\schema\` 的清单哈希仍为 `12c012a1…9ea6f`。
- **只执行三条本地命令**：`--version`、`app-server generate-json-schema --help`、`app-server generate-json-schema [--experimental] --out <run_dir>\schema`。
  - 每条时限 60 秒；
  - `CODEX_HOME` 指向一个新的空临时目录，不用长期 home，不读取凭据；
  - 不启动 app-server 会话，不建 thread，**模型回合为 0**。
- **记录**：
  - 运行前后名称含 codex 的进程；
  - 临时 home 是否为空并已删除；
  - 版本是否与推断一致。
- **比较**，与 alpha.11 基线对照：
  - S3 的 21 项、S4 和 S5 的协议事实，以及 app-server 适配器依赖的方法、通知、服务端请求和条目类型，必须全部存在；
  - 列出新增、删除、变化的 schema 文件，以及变化的定义名称。
- **判定**：
  - `IDENTICAL_SCHEMA`：事实齐全，schema 没有任何差异；
  - `COMPATIBLE_REVIEW_DIFF`：事实齐全，但有差异，需要逐项审查后才能重新固定；
  - `STOP`：缺少事实、前置检查失败、命令失败或超时。
- **停止规则**：任何异常立即停止，不重试，不换程序。
- **证据**：`var/codex-runtime/step-a-alpha.11.2-*/` 下的 `report.json`、三份输出和 schema 目录，原文件不入库，只登记 SHA-256。
- **只认定**：暂存的这份二进制能在新位置运行零模型命令，它报告的版本，以及协议 schema 与 alpha.11 的异同。
- **不认定**：任何模型回合中的行为、工具审批、中断、续接、沙箱、子 Agent 控制。

## 5. 步骤 A 之后的顺序（每个真实步骤都要单独批准）

1. **审查差异**：对 `COMPATIBLE_REVIEW_DIFF` 列出的变化逐项判断，必要时对照 alpha.11 与新版本的源码标签。对 S3、S4、S5 或适配器有影响的变化，写明影响和需要的复测。
2. **重新固定**（离线）：
   - `appserver_adapter.PINNED_APPSERVER` 的路径改为暂存目录，sha256、size 和 version 改为步骤 A 实测的值；
   - 同步 `tests/http/test_appserver_s6.py` 中的固定值断言，以及能力表和 S6 规划中的版本描述；
   - S3、S4、S5 的探针保留原来的 alpha.11 固定值，作为历史工具不改；
   - 运行受影响的离线测试：S6 相关、适配器、S6-2/S6-3/S6-4 收集与驱动。
3. **S6-2 冒烟**：在新运行时上做一次最小回合。依据是二进制和位置都变了。
4. **S6-4**：沿用第 12 节，更新被测提交和固定值。
5. **S6-5**：中断申请另行提交。

S1–S6-3 的现有证据保留为 alpha.11 的历史结果，不整体重跑；只有具体变化需要时，才做针对性复测。S6-V 和 S7 仍是单独的决定，不作为 S6-4、S6-5 的前提。

## 6. 暂存实录（2026-10-23，子 agent 执行，退出码均为 0）

```
python tools\probes\codex_runtime_stage.py --stage alpha.11.2   # 20:51:39，复制 5 个文件，signatures_valid true，problems []
python tools\probes\codex_runtime_stage.py --stage alpha.11     # 20:51:52，复制 2 个文件，signatures_valid true，problems []
python tools\probes\codex_runtime_stage.py --list               # 两个候选 problems 都为 []
```

| 记录 | 字节 | SHA-256 |
|---|---|---|
| `sha256-c81ed464e5e7cafd\provenance.json`（alpha.11.2） | 4596 | `b64d146cedb0cb9203d68e5c2c13d3480ec824d18a142f178abf5095e9598962` |
| `sha256-00c0f1e4099246fd\provenance.json`（alpha.11 存档） | 2055 | `99255c25a6430859c817b05b714f53c5606bd26497d2456cea4312bc9744c333` |

- 暂存的可执行文件都是只读的；`provenance.json` 本身没有设为只读，以上面登记的哈希为准。
- 两份记录的 `version_verified` 都保持 null，不回写（回写会改变上面登记的哈希）。实测版本记在第 7 节和代码常量 `PINNED_APPSERVER` 中。

## 7. 步骤 A 实录（2026-10-23 23:35，用户批准，执行一次）

- **授权**（用户原话）：“批准按顺序执行两项零模型检查，各一次：先 Codex 新步骤 A（feat/codex-runtime 73da7dc，docs/codex-runtime.md 第 4 节）……模型回合为 0；任何异常立即停止，不重试，不进入模型回合。”
- **执行前确认**：HEAD 为 `73da7dc`，工作树干净，进程环境中没有 `AGENT_HUB_*` 变量。
- **命令**：第 4 节的命令，退出码 0，只执行一次。
- **前置检查**：五个暂存文件的哈希与登记值一致，签名都是 `Valid`；基线清单未变。`problems` 为空。
- **三条命令**都返回 0：`--version` 0.44 秒，`generate-json-schema --help` 0.02 秒，`generate-json-schema --experimental --out` 0.45 秒。
  - stderr 只有一条已知警告：拒绝在临时目录下创建 PATH 别名，与 2026-10-22 的步骤 A 相同。
- **版本**：`codex-cli 0.163.0-alpha.11.2`，`version_matches_inference: true`。
- **协议**：
  - 71 项事实全部存在（S3、S4、S5 和适配器）；
  - schema 两边都是 452 个文件，没有新增、删除或变化；
  - 捕获的清单哈希就是 `12c012a1…9ea6f`，与 alpha.11 基线逐字节相同。
- **模型回合**：0。临时 `CODEX_HOME` 用后已删除。
- **进程**：运行前后名称含 codex 的进程完全相同，都是桌面应用原有的 5 个进程，本次没有残留进程。

| 证据（`var\codex-runtime\step-a-alpha.11.2-5t52dkuz\`，不入库） | 字节 | SHA-256 |
|---|---|---|
| `report.json` | 2456 | `f225e4199de57f26e01ab5a95deea5044da2bc6175d15c307ce762f5722a9944` |
| `version.txt` | 326 | `892a681f4583e953bcbac2c24c3809a8baa08857b42cf4e52bf05a98fbd48f1a` |
| `schema-help.txt` | 1398 | `75da31c8485ad36e3669ec3a31a8f9624529b8bdc7a54b1e311ebc67d65ea59e` |
| `schema-generate.txt` | 296 | `f1aaca72138d8dd71160a8b084c75143ed0706d4252ff9d2c2de6315fc036856` |
| `schema\`（452 个文件） | — | 清单哈希 `12c012a180048bbbff489295813f54c70130bf32edfe8394ffd3547b4e39ea6f` |

### 7.1 源码差异审查（离线）

方法：GitHub compare API 比较 `rust-v0.163.0-alpha.11...rust-v0.163.0-alpha.11.2`，一次读取；结果经网页抓取工具转述。两个标签已分叉，alpha.11.2 领先 8 个提交、落后 1 个。8 个提交是：

- 工具链与依赖：rustls-webpki / webpki-roots 锁文件、Rust 1.99.1、发布 alpha.11.1、热修复工作区与锁文件、发布 alpha.11.2；
- Windows MXC 沙箱迁移到 `mxc-sdk`（#55378），以及过滤无法检查的网络根目录（#54363）；
- `requirements.toml` 支持 Windows 注册表覆盖（#55456）。

结论：

- 2026-10-22 核对过的 17 个 S3 相关源文件（shell 检测与包装、审批、unified exec、事件映射、session 等）**都不在变更列表里**。
- 涉及 app-server 和执行策略的四个文件只是编译器升级后的写法调整，行为不变：`message_processor.rs` 的模式匹配写法、`exec_policy.rs` 的 `From` 实现、`executed_tool_calls.rs` 的 `fetch_update` 改为 `try_update`、`exec-server-protocol` 的闭包写法。
- **Windows 沙箱实现变了**。本项目本来就不声称沙箱可控，这条边界不变；但这是在新运行时上做一次 S6-2 冒烟的具体理由。
- 如果注册表覆盖改变了审批或沙箱策略，`thread/start` 之后的设置回读不符时，适配器不会开始回合（D2）。
- 局限：这只说明源码的预期行为，不能证明本机二进制就是从该标签构建的；没有逐一阅读的文件仍可能间接影响行为。

### 7.2 重新固定（离线，本提交）

- `PINNED_APPSERVER` 指向暂存目录 `sha256-c81ed464e5e7cafd\codex.exe`，sha256、size、version 都改为实测值。新调用的绑定身份随之变为 `codex-app-server@codex-cli 0.163.0-alpha.11.2;…`。
- S3、S4、S5 的探针保留 alpha.11 的历史固定值，不改。
- 受影响的离线测试共 324 项，全部通过：S6 HTTP、S6-2、S6-3、S6-4 收集、会话页、S3 身份、S4、S5、运行时。

## 8. S6-2 冒烟申请（新运行时，未执行）

**本节只是申请，不构成授权。** 用户单独批准后执行一次。

- **依据**：二进制和位置都变了，Windows 沙箱实现也变了（第 7.1 节）。S1–S6-3 的结论只属于 alpha.11，不整体重跑；S6-3 的续接逻辑与本次变化无关，不复测。
- **被测提交**：`feat/codex-runtime` 上包含本节的提交。执行前确认 HEAD 和工作树都没有变化。
- **固定程序**：`PINNED_APPSERVER`，即第 7.2 节的固定值。驱动执行前会再被动核验一次身份和大小。
- **其余与 S6 规划第 10 节相同**：
  - CODEX_HOME、证书包、执行前检查、路径（进程内 API → 审批并显式同意 → Worker → 适配器）；
  - 固定提示词 `Reply with exactly: OK. …`；
  - 1 个新 thread，最多 1 个模型回合，本地时限 300 秒；
  - PASS 条件、停止规则和风险。
- **命令**（一次）：

  ```powershell
  Set-Location E:\plug\agent-hub\.worktrees\codex-runtime
  & 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s6_2.py --mode real --approve-one-model-turn --acknowledge-supervised-boundary
  ```

- **只认定**：在 alpha.11.2 的 AgentHub 副本上，经过 Hub 的 supervised 最小回合可用一次。
- **不认定**：工具审批（S6-4）、中断（S6-5）、续接、网页路径、子 Agent 控制、沙箱。
- **证据**：登记 `var/appserver-s6-2/real-*/result.json`、stderr 日志和 rollout 的 SHA-256；打标签 `evidence/s6-2-real-run-2-<pass|fail|inconclusive>`。
- 结果为 PASS 后，S6-4（S6 规划第 12 节，更新被测提交和固定值）和 S6-5 分别申请。
