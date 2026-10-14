# 未决问题：codex.exe 的 `0xc0000142`（DLL 初始化失败）

**状态：未解决（OPEN），独立于 python.exe 的记录。**
**不得因为"真实调用成功过"而视为已修复。**

最后更新：2026-10-14。

姊妹记录：`docs/issue-python-0xc0000142.md`（python.exe 的同一错误码）。
两者**分开跟踪**：可执行文件、加载路径、启动方式都不同，混在一起会让根因永远收敛不了。

---

## 1. 问题定义

`0xc0000142` = `STATUS_DLL_INIT_FAILED`（进程在 DLL 初始化阶段失败，十进制 `3221225794`），
出现在**启动 `codex.exe`** 时。

**来源与诚实说明**：本仓库的会话记录中，**没有任何一次我们自己捕获到的输出里出现
`0xc0000142`**（`var/` 下的全部捕获已检索，`c0000142` 0 命中）。因此本记录按
"用户报告 + 我方未复现"归档，**不假装已定位**。

涉及的二进制（本机实测）：

| 路径 | 版本 | 大小 |
|---|---|---|
| `%LOCALAPPDATA%\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe` | `codex-cli 0.162.0-alpha.17.2` | 334,121,776 |
| 同目录 `codex-command-runner.exe` / `codex-code-mode-host.exe` / `codex-windows-sandbox-setup.exe` | 同版本 | — |

sha256（固定值，改动即视为换版）：
`d13914ced6c7af174d8d638db938284231227312104b06a25e9608c632a7daec`

## 2. 已知的启动方式与结果（本机实测）

| 启动方式 | 结果 |
|---|---|
| 由我们的适配器 `subprocess.Popen` 启动，**stdout 指向文件句柄**、`stdin=DEVNULL` | ✅ 成功（真实调用已达多次：退出码 0） |
| 前台直接调用、输出到控制台（`--help` / `--version` / `features list` / `generate-json-schema`） | ✅ 成功 |
| 自描述命令（无模型调用）`codex debug prompt-input`、`codex app-server generate-json-schema` | ✅ 成功（退出码 0） |
| 原生 stdout 接 PowerShell 管道 | ❌ 沙箱拒绝管道（`Access is denied`）——**与应用层无关** |
| 长驻后台 daemon（`daemon_auto_start stable true`，会**自动重启**） | 存在且常驻；**未**对其做任何干预 |

**复现尝试 0 次成功**：本机对 codex.exe 的多次启动（含一次真实模型调用）都正常，
**没有**出现 `0xc0000142`。

## 3. 与 python.exe 记录的区分（易混淆点）

| 现象 | 归属 | 关系 |
|---|---|---|
| `程序"python.exe"无法运行: Access is denied` | 运行环境的管道策略 | 与 `0xc0000142` **不同**（策略拒绝 vs DLL 初始化失败） |
| `0x40010004`（`DBG_TERMINATE_PROCESS`） | 我们自己执行 `Stop-Process -Force` | 与 `0xc0000142` **完全不同**（且已禁止按名批量强杀） |
| Codex 的 `os error 5`（写 `$CODEX_HOME/tmp` 被拒） | 目录权限 | 与 `0xc0000142` **不同** |
| `codex doctor` 报 `sandbox backend disabled` | 沙箱后端状态 | 与 `0xc0000142` **不同**，但同样未被解释，需留档 |

## 4. 下次复现时必须采集的信息（codex.exe 专用）

出现该错误码时**立即**采集（不要事后补）：

1. **可执行文件全路径**：是 `codex.exe`，还是 `codex-command-runner.exe` /
   `codex-code-mode-host.exe` / `codex-windows-sandbox-setup.exe`？（子进程同样可能失败）
2. **完整命令行**（含全部参数）与**启动方式**：直接调用 / `Start-Process` /
   计划任务 / 服务 / **由我们的适配器 Popen** / 由 Codex 自身 daemon 拉起。
3. **退出码来源**：进程自身退出码，还是调用方（PowerShell / 我们的 Python / harness）报告的？
4. **同时刻是否有多个 codex 进程**（daemon 常驻 + 子进程）与系统资源状况。
5. **是否与 stdout 目标有关**：本次是文件句柄、控制台，还是管道？（管道在本环境必然被拒，
   必须与 DLL 失败区分）
6. **Windows 事件日志**（该时间点）：
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-1)} -MaxEvents 100 |
       Where-Object { $_.Message -match 'codex|0xc0000142|3221225794' } |
       Format-List TimeCreated, ProviderName, Id, Message
   ```
7. **WER 报告与转储是否新增**：
   ```powershell
   Get-ChildItem 'C:\ProgramData\Microsoft\Windows\WER\ReportArchive' |
       Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name, LastWriteTime
   Get-ChildItem "$env:LOCALAPPDATA\CrashDumps" -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name, LastWriteTime
   ```
8. **我们的捕获是否为空**：对应 invocation 的 `codex-executions.db` 里 `exit_code` /
   `errors` / `unparsed_lines`，以及 `var/codex-runs/<invocation>/_scratch/*.jsonl` 是否为空文件。
9. **可否用同一 argv 手工复跑一次**（不改参数、不加权限）——记录是否可复现。

## 5. 明确不做的事

* **不**把"真实调用成功过"当作已修复：成功只能说明那些运行没触发该错误。
* **不**按进程名批量强杀（会杀掉机器上所有相关进程，并产生 `0x40010004` 之类易混淆的退出码）。
* **不**干预共享的 Codex daemon（它常驻并自动重启；停掉它会掩盖或改变现象）。
* **不**通过放宽沙箱/权限来"绕开"该错误。
* **不**在没有新证据时宣称根因。

在收集到 §4 的信息之前，本问题**保持 OPEN**。
