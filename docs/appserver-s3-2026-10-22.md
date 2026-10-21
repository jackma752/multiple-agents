# S3 真实运行记录（2026-10-22）：FAIL——模型未发起审批请求，拒绝链未被触发

- 所测提交：`feat/appserver-s3-probe` @ `9b48db12d582de23b3852d1c420e9c912be132c5`
- 授权：用户在对话中批准执行“提交 9b48db1 所列的这一次方案”：先运行步骤 A；A 正常退出、结果为 COMPATIBLE 且身份一致时，再运行步骤 B，最多一个真实模型回合；不授权发送 accept；出现异常即停止，不重试、不换模型、不放宽权限。
- 执行通道：Agent 自己的 shell，**未经沙箱**，每条命令都经过权限确认。原计划是在用户的终端面板中运行，但该工具在 shell 启动较慢时会发送 Ctrl-C，可能打断进行中的回合，因此改用 Agent 自己的 shell；命令行与获批内容完全一致。
- 结论：**FAIL**，类别为“固定范围或协议校验失败”。**没有执行证据**。**拒绝链没有被触发**：没有收到审批请求，因此没有发送 decline。按方案，这种 FAIL 不能外推为“审批拒绝能力失效”；本次只说明了这个回合里模型没有走审批机制。

## 步骤 A：零模型身份与协议检查（通过）

`tools/probes/appserver_s3_identity.py --capture --approve-zero-model-identity-check`：退出码 0。

- `pinned_identity_matches: true`（`bin\896a0166b774037a\codex.exe`，SHA-256 `f6dbf48c…0312`，341312816 字节）；
- 版本 `codex-cli 0.163.0-alpha.10.2`（S1/S2 当时为 0.162.0-alpha.17.2）；
- 只执行了 `--version`、`app-server generate-json-schema --help`、`generate-json-schema --experimental --out …` 三条命令，退出码都是 0，模型回合 0 次；
- 21 项协议事实全部存在（COMPATIBLE）；
- 临时 `CODEX_HOME` 使用后为空，已删除。

## 步骤 B：一次 S3（FAIL）

`tools/probes/appserver_s3.py --mode real --approve-one-model-turn --acknowledge-unverified-native-control --codex-home C:\Users\Administrator\AppData\Local\AgentHub\codex-home`：退出码 1。

预检 11 项全部通过：身份未被覆盖、大小和 SHA-256 一致、home 位于仓库外且有凭据（只检查存在）、可写、CA 可读。

| 项目 | 结果 |
|---|---|
| 发出的消息 | `initialize → initialized → thread/start → turn/start`，共 4 条；**没有发送任何审批响应** |
| thread 实际设置 | 已核验（reviewer=user、policy=on-request、sandbox=readOnly 且禁止联网、cwd 一致） |
| 回合 | `turn_attempts=1`；thread `01a15fc8-1db1-7072-9530-53c2f4eae728`，turn `01a15fc8-26cd-7372-9a1e-354d778f07b1` |
| 收到的通知 | `thread/started`、`mcpServer/startupStatus/updated`×2、`thread/status/changed`、`turn/started`、`item/started`、`item/completed`、`item/started`、`item/agentMessage/delta`×2 |
| 审批请求 | **没有**（`approval: null`）；也没有 commandExecution item |
| 中止原因 | 模型在任何审批请求之前就开始输出文字回复。这段增量中含有 `DECLINED` 以外的字符（探针只允许回复由 `DECLINED` 的字母组成，以避免保存任意模型文本），被判为越界，探针随即中止（`client_violation`，`AppServerProtocolError`）。回复内容按设计**没有保存** |
| 执行证据 | 无：没有 processId、退出码或输出通知；工作目录为空（`workdir_empty: true`）且已删除；任何临时工作目录中都没有标记文件 |
| 终态 | 未确认（`terminal_confirmed: false`）：探针中止时还没有收到 `turn/completed`。本地自有进程 PID 17688 已退出，退出码 0；**远端回合是否结束未确认**。没有收到 usage 通知 |
| 其他进程 | 运行前后都存在的 PID 28416 和 33196 属于 `ChatGPT.exe`（app-server 和 exec-server），不是本次启动的，未触碰 |

stderr 中有 5 行相同的 `codex_skills_extension` 错误：“failed to install system skills: io error while remove existing system skills dir: 另一个程序…”，即长期 home 中的 `skills\.system` 被占用（该目录修改时间 02:45:12）。占用者无法确定。这是环境异常，没有证据表明它与本次结果有关，未处理。

## 这次运行证明了什么、没证明什么

- 已表明：对这个固定版本，按固定提示词、固定权限发起的这一个回合，模型**没有**使用命令审批机制，而是直接开始文字回复；探针按契约中止，没有发出任何响应，也没有观察到执行。
- 没有表明：审批拒绝链本身是否有效（它没有被触发）；审批机制是否失效；模型最后会说什么；远端回合是否已经结束。
- 与真实聊天功能无关：这不代表任何聊天功能已验收或失败。
- 本次一次性授权已经用完；**没有重试**，也没有运行 S4/S5。是否再申请一次 S3（例如调整提示词或判定规则），由用户另行决定。

## 证据（文件不入库，只登记哈希）

路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\`。

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `real-famna8n0\result.json` | 3139 | 2026-10-22 02:45:20 | `b5324a82010579410e8033ec61f876c7161c9fff52e14c23b9bc28c696f862f8` |
| `real-famna8n0\stderr.log` | 1150 | 2026-10-22 02:45:15 | `4667c1b497be7dc8f818202a6197d2a22b634b55cd191ddfe0be520903e26d29` |
| `identity-jlq59qic\report.json` | 1025 | 2026-10-22 02:44:07 | `3d44216cf625f0fa205bf6070a3e324f963ffd1876378ef49fbff9dbb01b66a4` |
| `identity-jlq59qic\version.txt` | 325 | 2026-10-22 02:44:06 | `09ec7643beca4e3d0f3587dd0b530e550c63d0bbfab42169f5b076b2f71dd5d0` |
| `identity-jlq59qic\schema-help.txt` | 1397 | 2026-10-22 02:44:06 | `f7bd6f2371ff8fc88a54f9cca1c97091bebdd4035facf62767df1443a246e002` |
| `identity-jlq59qic\schema-generate.txt` | 295 | 2026-10-22 02:44:07 | `540c10e5a55694fe27feeaa870b7ce1586f5f4e818cea33b9b0274c0503a8f8a` |
| `identity-jlq59qic\schema\`（452 个文件） | — | 2026-10-22 02:44:07 | 清单哈希 `908fd69a446b440c6df015b3cba956e25b087853170c6b09fbd12615d2b9eb39`（按相对路径排序后，对每行“文件 SHA-256 + 两个空格 + 路径”所组成文本计算的 SHA-256） |

本分支基于 S2，没有 `docs/evidence-manifest.md`；分支合并时，把上表并入证据清单。
