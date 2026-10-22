# S3 重新固定后的步骤 A（2026-10-22）：COMPATIBLE，版本变为 `0.163.0-alpha.11`

- 所测提交：`feat/appserver-s3-probe` @ `0cea3382de2b7a927307557ec04ce55fe831b837`（阶段 0：重新固定 `S3_CODEX_IDENTITY`）
- 授权（用户原话）：“批准阶段 0 和阶段 A，暂不批准阶段 B。……A 完成后，无论版本是否变化，都不要自动进入 B。”
- 执行通道：Agent 自己的 shell，未经沙箱；命令行与申请内容一致；只执行了一次，没有重试。
- **结论**：
  - 程序身份与固定值一致；
  - 21 项协议事实全部存在（COMPATIBLE）；
  - 模型回合 0 次；
  - **版本由 `0.163.0-alpha.10.2` 变为 `0.163.0-alpha.11`**。
  - 离线源码核对表明，修订二所依据的源码在 alpha.11 上没有影响结论的变化。
  - **阶段 B 没有运行**，真实拒绝链仍未验证。

## 执行

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' tools\probes\appserver_s3_identity.py --capture --approve-zero-model-identity-check
```

退出码 0，2026-10-22 20:41。

| 项目 | 结果 |
|---|---|
| 固定文件 | `bin\6257d9e8f63c0f2b\codex.exe`，342187312 字节，SHA-256 `00c0f1e4…d67c`；`pinned_identity_matches: true` |
| 执行的命令 | 共三条，退出码都是 0：`--version`（0.03 秒）、`app-server generate-json-schema --help`（0.03 秒）、`app-server generate-json-schema --experimental --out <dir>`（0.44 秒） |
| 版本 | `codex-cli 0.163.0-alpha.11` |
| 协议事实 | 21/21，`missing_in_old` 和 `missing_in_new` 都为空 |
| 模型回合 | 0 |
| 临时 `CODEX_HOME` | 使用后为空，已删除（`scratch_home_removed: true`）。stderr 中只有一条已知警告：拒绝在临时目录下创建 PATH 别名 |
| 进程 | 运行后只剩运行前就存在的 PID 1628 和 10904（ChatGPT 应用，19:50 启动），本次没有触碰它们 |

### 与上一次捕获的 schema 比较

两次捕获都是 452 个文件，没有增删。只有 4 个文件不同：`ClientRequest.json`、两个汇总文件和 `v2/ThreadRealtimeStartParams.json`。在定义层面，唯一的变化是 `ThreadRealtimeStartParams` 的 `properties`，属于 realtime 功能，与 S3 无关。S3 用到的审批请求与响应、ThreadItem、通知、thread 和 turn 参数等 schema 全部相同。

## 修订二的源码依据：在 alpha.11 上重新核对（离线）

方法：两个标签不在同一条线上（alpha.11 相对 alpha.10.2 领先 23 个、落后 5 个提交），所以不用 compare 的文件列表。改为用 GitHub contents API 逐个比较两个标签下同一文件的 git blob SHA；SHA 相同即内容相同。SHA 是经网页抓取工具转述得到的。

| 文件（`codex-rs/` 下） | blob SHA（alpha.10.2 / alpha.11） |
|---|---|
| shell-command/src/shell_detect.rs | `3aecd03d…572c`，相同 |
| shell-command/src/parse_command.rs | `01768d0a…d740`，相同 |
| shell-command/src/powershell.rs | `0b668eb4…b82c`，相同 |
| core/src/shell.rs | `da0fbbea…9406`，相同 |
| core/src/tools/handlers/unified_exec.rs | `6eeaf861…2bea`，相同 |
| app-server-protocol/src/protocol/item_builders.rs | `1ace0e8a…f2d7`，相同 |
| app-server/src/bespoke_event_handling.rs | `5e543082…20aa`，相同 |
| core/src/tools/runtimes/unified_exec.rs | `61d66c69…101e`，相同 |
| core/src/unified_exec/process_manager.rs | `cda8c59d…5fb4`，相同 |
| core/src/tools/approvals.rs | `16197b56…c1bf`，相同 |
| core/src/tools/sandboxing.rs | `d49273be…ff69`，相同 |
| core/src/tools/orchestrator.rs | `e7f54445…6baf`，相同 |
| protocol/src/approvals.rs | `09b71158…d684`，相同 |
| exec-server/src/environment.rs | `8c3d89b8…6e81`，相同 |
| core/src/tools/code_mode/delegate.rs | `9e4429a4…9d3d`，相同 |
| core/src/event_mapping.rs | `c0fe8308…2173`，相同 |
| core/src/session/mod.rs | `a541c552…1d15` / `9b00655e…40a9`，**不同** |

对不同的那个文件，直接在 alpha.11 上核对了相关函数：[core/src/session/mod.rs @ alpha.11](https://github.com/openai/codex/blob/rust-v0.163.0-alpha.11/codex-rs/core/src/session/mod.rs)。`request_command_approval` 的行为没有变：参数仍为 `environment_id: Option<String>` 和 `available_decisions: Option<Vec<ReviewDecision>>`；为 None 时调用 `ExecApprovalRequestEvent::default_available_decisions`，事件中写入 `available_decisions: Some(...)`；`environment_id` 原样写入事件。

结论：

- 修订二依据的各项事实在 alpha.11 上仍然成立：
  - 会话 shell 的选取与包装 argv；
  - `command` 为 shlex 拼接，且先发待决 item、再发审批请求；
  - decline 映射为拒绝；
  - `environmentId` 为 `"local"`；
  - 默认决定列表不含 decline；
  - 先审批，后运行。
- **不需要修改探针代码。**
- 局限：
  - 这只说明源码的预期行为；版本号与标签一致，不能证明本机二进制就是从该标签构建的。
  - 其余未逐一比较的文件可能间接影响行为；schema 层面已经确认 S3 用到的部分没有变化。

## 证据（文件不入库，只登记哈希）

路径相对于 `E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\`。

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `identity-hkzdmoqj\report.json` | 1023 | 2026-10-22 20:41:32 | `804e69a73a748460d5da71d0a8aeacfd344da2c9eb5db74e5e2352255bf55d3b` |
| `identity-hkzdmoqj\version.txt` | 323 | 2026-10-22 20:41:32 | `dc9c48e8df0f971704b631388bab3efdee37acee691d5c9c52e26f98c898327a` |
| `identity-hkzdmoqj\schema-help.txt` | 1397 | 2026-10-22 20:41:32 | `04da81f9355663c241033fbc6f35dce295d708be5640ace216b3ff7398361db8` |
| `identity-hkzdmoqj\schema-generate.txt` | 295 | 2026-10-22 20:41:32 | `e195f83622198da4917d3c25dac39d5e3ee3aa6ff03783d8fa686409b18ebda9` |
| `identity-hkzdmoqj\schema\`（452 个文件） | — | 2026-10-22 20:41:32 | 清单哈希 `12c012a180048bbbff489295813f54c70130bf32edfe8394ffd3547b4e39ea6f` |

清单哈希的计算方法：按 POSIX 风格的相对路径排序；每行为“小写十六进制 SHA-256 + 两个空格 + 路径”；行之间用 LF 连接，末尾也有一个 LF；对 UTF-8 编码的全文计算 SHA-256。

按同样的方法，上一次捕获 `identity-jlq59qic\schema\` 的清单为 `eb127dd720b831db52f46fc27a09bd950933d0581c8737d669f624d4d1949fb2`。第一次实录中登记的 `908fd69a…` 用的拼接方式无法确定，这次没能复现；两次捕获之间的比较以上面的逐文件比较为准。

## 没有做的事

- 没有运行阶段 B，没有模型回合，没有读取长期 home 或凭据，没有换程序，没有重试。
- 真实拒绝链：**未验证**。
