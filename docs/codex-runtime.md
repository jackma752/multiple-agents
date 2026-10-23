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

## 4. 新的步骤 A 申请（零模型，未执行）

**本节只是申请，不构成授权。** 用户批准后执行一次。

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
- 两份记录的 `version_verified` 都为 null，要等步骤 A 实测之后才有值。
