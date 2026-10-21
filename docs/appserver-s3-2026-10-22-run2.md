# S3 真实运行记录·第二次（2026-10-22）：FAIL——命令 item 与固定命令不符，拒绝链未被触发

- 所测提交：`feat/appserver-s3-probe` @ `58e4a7acf31fc243c6d0c9738dbe7548bbf7ebac`（判定修订后的版本）
- 授权（用户原话）：“批准提交 58e4a7a 的这一次 S3：复用已通过的步骤 A，不重新导出 schema；一个新 thread、最多一个模型回合、只发送 decline。接受申请中的限制，异常即停、不重试。”
- 执行通道：与第一次相同，在 Agent 自己的 shell 中运行，**未经沙箱**，每条命令都经过权限确认；命令行与获批内容完全一致。
- 结论：**FAIL**，原因代码 `command_scope_mismatch`、`reader_error:AppServerProtocolError`。类别为“越出固定范围”，不是执行证据。**拒绝链没有被触发**：没有收到审批请求，也没有发送 decline。

## 步骤 A：复用，未重新运行

按授权复用第一次的步骤 A 证据（`identity-jlq59qic`，COMPATIBLE，见 [第一次实录](appserver-s3-2026-10-22.md)），没有重新导出 schema。运行前被动复核：HEAD 为 `58e4a7a`、工作树干净；固定文件 `bin\896a0166b774037a\codex.exe` 为 341312816 字节，SHA-256 `f6dbf48c…0312`，与固定值一致。

## 步骤 B：一次 S3（FAIL）

`tools/probes/appserver_s3.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home C:\Users\Administrator\AppData\Local\AgentHub\codex-home`：退出码 1，运行时间约为 2026-10-22 03:02（本地时间）。

预检 11 项全部通过。

| 项目 | 结果 |
|---|---|
| 发出的消息 | `initialize → initialized → thread/start → turn/start`，共 4 条；**没有发送任何审批响应** |
| thread 实际设置 | 已核验（`effective_settings_verified: true`） |
| 回合 | `turn_attempts=1`；thread `01a15fd7-ba60-7b01-9a22-aef7fe05772e`，turn `01a15fd7-c30c-7f90-a7fe-595c94cb98bc` |
| 收到的通知 | `thread/started`、`mcpServer/startupStatus/updated`×2、`thread/status/changed`×2、`turn/started`（inProgress）、`item/started` 与 `item/completed`（userMessage）、`item/started`（commandExecution） |
| 助手文字 | 没有：助手消息 0 条，增量 0，推理增量 0。修订后的文字规则这次**没有被用到** |
| 审批请求 | **没有**（`approval: null`） |
| 中止原因 | 收到 commandExecution 的 `item/started`：item `exec-c7ef8bd6-02cc-40b5-b671-7df14479ecc2`，turn ID 与本回合一致，状态 `inProgress`，但 `command` 字段与固定命令字符串**不完全相同**（`command_matches: false`）。按规则判为 `command_scope_mismatch`，探针立即中止（`client_violation`，读取线程报 `AppServerProtocolError`）。这个 item 出现在任何审批请求之前 |
| 命令原文 | 按设计**没有保存**，只记录了是否完全一致。因此无法区分三种情况：固定命令的包装形式（例如被外层 shell 包了一层）、模型改写过的命令、完全不同的命令 |
| 执行证据 | 没有正面证据：该 item 的 `processId` 为空，没有输出，`exitCode` 为空，状态不是 completed；也没有 `item/commandExecution/*` 输出通知。进程停止后检查，工作目录为空（`workdir_empty: true`），并已删除。事后在工作树、`AppData\Local\AgentHub`、`AppData\Local\Temp` 和用户目录（深度 3）中搜索 `S3_EXECUTED*`，没有找到；也没有命令行含标记名的 cmd/powershell 进程残留 |
| 执行情况的界限 | 中止时该 item 仍是 `inProgress`。在 on-request 策略下，Codex 可能不经审批就在只读沙箱中运行命令；schema 中 `processId` 的说明是“如有则提供”，为空不能证明没有运行。由于命令内容未知，**这条命令在沙箱中是否已开始运行，无法确定**；上面的检查只能说明标记文件没有出现 |
| 终态 | 未确认（`terminal_confirmed: false`）：中止时还没有收到 `turn/completed`，也没有 usage。本地自有进程 PID 4040 已退出，退出码 0；**远端回合是否结束未确认** |
| 其他进程 | PID 28416 和 33196（`codex.exe`，01:33 启动）在运行前已存在，不是本次启动的，没有触碰，运行后仍在；没有新增的 codex 进程 |

stderr 中有 5 行与第一次相同的 `codex_skills_extension` 错误（安装系统技能时目录被占用，os error 32）。占用者仍无法确定。没有证据表明它与本次结果有关，未处理。

## 这次运行证明了什么、没证明什么

- 已表明：对这个固定版本，按固定提示词和固定权限发起的这一个回合，模型没有先说话，而是直接发起了一个命令 item，其 `command` 字符串与固定命令不完全一致，而且这个 item 出现在任何审批请求之前。探针按契约中止，没有发出任何响应，也没有观察到正面的执行证据。
- 没有表明：
  - 审批拒绝链是否有效：它**仍未被触发，仍未验证**；
  - 这条命令之后是否会进入审批；
  - 命令的实际内容，以及它是否已在沙箱中开始运行；
  - 远端回合是否已经结束。
- 第一次运行的远端终态同样仍未确认。
- 与真实聊天功能无关：这次结果不代表任何聊天功能已验收或失败。
- 本次一次性授权已经用完；**没有重试**，没有换模型，没有放宽权限，也没有运行 S4/S5。

## 对下一步的影响（只是记录，未实施）

现有证据无法判断不一致的原因，所以在判定规则不变的情况下再跑一次，大概率还会停在同一个位置，而且同样无法诊断。如果要继续 S3，需要先离线决定：

1. 是否在不保存原文的前提下记录更多命令元数据，例如命令字符串的 SHA-256 和长度、是否包含固定命令、外层程序属于固定列表中的哪一类；
2. 固定命令的包装形式是否算作范围内；
3. 对于不经审批、直接在沙箱里运行的命令 item，探针应如何对待。

这三点都需要用户决定，本次没有改动任何代码。

## 证据（文件不入库，只登记哈希）

路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\`。

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `real-v3qrkhhc\result.json` | 4333 | 2026-10-22 03:02:24 | `2ae1cefb58233b57a321fc511ff9737adea9d103dc17e57f1f507e76b726be9e` |
| `real-v3qrkhhc\stderr.log` | 1150 | 2026-10-22 03:02:18 | `ae2799e7b51703cabc2f5432ecdd8c351a4f291212f06c8b22616ba6d820dffc` |
| `identity-jlq59qic\report.json`（复用的步骤 A） | 1025 | 2026-10-22 02:44:07 | `3d44216cf625f0fa205bf6070a3e324f963ffd1876378ef49fbff9dbb01b66a4` |

分支合并时，把上表并入证据清单。
