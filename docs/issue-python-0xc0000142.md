# 未决问题：python.exe 的 `0xc0000142`（DLL 初始化失败）

**状态：未解决（OPEN）。**
**不得因为测试通过、或本文件之外的工作完成，而视为已修复。**

最后更新：2026-10-14。

**姊妹记录**：`docs/issue-codex-0xc0000142.md`（codex.exe 的同一错误码）。
两者**分开跟踪**：可执行文件、DLL 加载路径与启动方式都不同，混在一起无法收敛根因。
**真实调用成功、测试通过、文档更新，都不构成对这两条记录中任何一条的修复证据。**

---

## 1. 问题定义

`0xc0000142` = `STATUS_DLL_INIT_FAILED`（进程在 DLL 初始化阶段失败），
出现在 `python.exe` 启动时。

**重要来源说明**：该错误码由**用户报告**。在本会话中，我**没有在自己捕获到的任何输出里
直接观察到 `0xc0000142`**——我捕获到的失败是别的形态（见 §4）。因此本记录按
"用户报告 + 我未能复现" 如实归档，**不假装已定位**。

---

## 2. 涉及的可执行文件（本机实测）

| 路径 | 版本 | 用途 | 备注 |
|---|---|---|---|
| `D:\python\python.exe` | 3.12.0 | 基础解释器 | 大小 103192，mtime 2023-10-02 |
| `E:\plug\agent-hub\.venv\Scripts\python.exe` | 3.12.0 | **本项目所有测试与工具** | 大小 270616，mtime 2026-10-13（venv 启动器） |
| `E:\plug\agent-hub\.venv\Scripts\pythonw.exe` | 3.12.0 | 无控制台变体 | 大小 259352 |

**待确认（阻塞诊断）**：用户报告的 `0xc0000142` 发生在**哪一个**可执行文件上？
两者行为可能不同：venv 的 `python.exe` 是一个**启动器**，它会再去加载基础解释器，
多一层 DLL 加载路径。**在得到确认为哪一个之前，根因无法收敛。**

---

## 3. 启动方式清单（本机实测结果）

| 启动方式 | 结果 |
|---|---|
| 前台直接调用、**不接管道、不重定向**：`& python.exe -c "pass"` | ✅ 可用（退出码 0） |
| 前台直接调用并输出到控制台 | ✅ 可用 |
| 由 Python 启动子进程、stdout 指向**文件句柄** | ✅ 可用 |
| 后台任务（harness job 机制） | ✅ 可用（可被 `job_kill` 干净结束） |
| `tools/demo/serve.py` + `stop.py` 优雅关闭 | ✅ 可用（退出码 0） |
| 接 PowerShell 管道（`\| cmdlet`、`2>&1`） | ❌ `Access is denied`（沙箱拒绝管道） |
| PowerShell 重定向到文件（`> file`、`*> file`） | ❌ 文件为空，输出静默丢失 |
| `Start-Process -Wait -NoNewWindow` | ❌ `Access is denied` |
| `Start-Process -PassThru`（无控制台，无 `-Wait`） | ❌ 子进程挂住，`WaitForExit(15000)` 返回 False |

**复现尝试**：单次启动、**12 次连续**启动、**12 次并发**启动（基础与 venv 解释器各测），
**均未产生 `0xc0000142`**。

---

## 4. 错误日志核查结果（**未找到任何 `0xc0000142` 记录**）

| 来源 | 检查结果 |
|---|---|
| Windows 应用程序事件日志（近 2 天，最多 200 条） | 仅 1 条**无关**条目：`Perflib` / `rasctrs.dll`（RemoteAccess 性能计数器，Windows 常见无害告警）。**无 `0xc0000142`、无 python 崩溃事件** |
| `C:\ProgramData\Microsoft\Windows\WER\ReportArchive` | 存在 10 份 `Report.wer`（2025-11 ~ 2026-10-12）。**逐份检索 `c0000142`：0 命中**；且**最新一份为 2026-10-12**，早于本会话（2026-10-13 起），会话时间窗内**没有**对应报告 |
| `C:\ProgramData\Microsoft\Windows\WER\ReportQueue` | 存在，**0 个文件** |
| `%LOCALAPPDATA%\CrashDumps` | 9 个转储，**全部是 `ctfmon.exe`（7 个，2026-05）与 `explorer.exe`（1 个，2026-07）**；**无 python.exe 转储** |
| `%LOCALAPPDATA%\Microsoft\Windows\WER\*` | 目录不存在 |

**结论：本机系统层没有任何 `0xc0000142` 或 python.exe 失败的留存记录。**
这意味着：要么当时未生成事件/WER 记录（例如进程在极早期失败、或 WER 被禁用），
要么该码来自本机之外/更早的时间点。**待确定。**

---

## 5. 会话中我实际观察到的失败（与 `0xc0000142` 不同，勿混淆）

| 现象 | 实测成因 | 与 `0xc0000142` 的关系 |
|---|---|---|
| `程序"python.exe"无法运行: Access is denied` | 沙箱拒绝**管道** | **不同**：这是策略拒绝，不是 DLL 初始化失败 |
| 重定向到文件得到**空文件** | PowerShell 对原生进程重定向不生效 | **不同** |
| 退出码 `1073807364` | `0x40010004` = `DBG_TERMINATE_PROCESS`，系**我自己**执行 `Stop-Process -Force` 造成 | **完全不同**（`0xc0000142` = `3221225794`） |
| Codex 的 `os error 5` | 沙箱拒绝写 `~/.codex/tmp` | **不同**（那是 Codex，不是 python） |

---

## 6. 现行处置（已落实）

1. **不再按名称批量终止 Python 进程。** 已废止
   `Get-Process python | Stop-Process -Force`（它会终止机器上所有 python 进程，
   且会产生 `0x40010004`，容易被误读为启动失败）。
2. **改为按 PID 精确管理**：`tools/demo/serve.py` 写 `var/serve-<port>.pid`，
   `tools/demo/stop.py` 先做**非破坏性归属校验**（`/openapi.json` 的 `title` 必须是
   `Agent Hub`），再请求**优雅退出**；校验不通过时**拒绝终止**。
3. 禁止 `os.kill(pid, 0)` 作为存活探测（Windows 上可能**直接杀死**目标；
   沙箱下又会抛 `OSError` 而误报"进程不存在"——本会话实测到该假阴性）。

---

## 7. 下次复现时需要抓取的信息（诊断清单）

请**在出现 `0xc0000142` 时立即**收集（不要事后补）：

1. **可执行文件全路径**：是 `D:\python\python.exe` 还是
   `E:\plug\agent-hub\.venv\Scripts\python.exe`？（这是当前最大的未知）
2. **完整命令行**（含全部参数）与**启动方式**（直接调用 / `Start-Process` /
   计划任务 / 服务 / 由其他程序拉起）。
3. **退出码的原始来源**：是进程自身退出码，还是调用方（PowerShell /
   我们的工具 / harness）报告的值？
4. **同时刻的进程数**与系统资源状况（是否伴随大量并发启动）。
5. Windows 应用程序事件日志中该时间点的条目：
   ```powershell
   Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-1)} -MaxEvents 50 |
       Where-Object { $_.Message -match 'python|0xc0000142|3221225794' } |
       Format-List TimeCreated, ProviderName, Id, Message
   ```
6. WER 报告与转储是否新增：
   ```powershell
   Get-ChildItem 'C:\ProgramData\Microsoft\Windows\WER\ReportArchive' |
       Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name, LastWriteTime
   Get-ChildItem "$env:LOCALAPPDATA\CrashDumps" | Sort-Object LastWriteTime -Descending |
       Select-Object -First 5 Name, LastWriteTime
   ```
7. 若可复现，用 `where.exe python` 与 `python -c "import sys; print(sys.executable, sys.version)"`
   确认实际被加载的解释器。
8. **是否与代码库本身的启动路径有关**：确认它是
   `.venv\Scripts\python.exe`（启动器，会再加载基础解释器）还是 `D:\python\python.exe`，
   以及**由谁拉起**（本项目工具 / harness 后台任务 / 直接前台）。
   本仓库的工具都用绝对路径调用 venv 解释器，这会改变 DLL 搜索顺序，属可疑变量。
9. **当时是否伴随大量并发启动**：本仓库的探测/联调会连续启动多个 Python 进程
   （例如"正对照"会再起一个服务端）；请记录当时的进程数与是否同时运行了多个服务。
10. **与 `0xc0000142` 同时是否存在 stdout 管道**：本环境的管道必然被拒，
    两种失败必须分开记录，否则会把策略拒绝误当成 DLL 失败。

---

## 8. 明确不做的事

- **不**把"当前测试数量全部通过"（现在是 289）当作本问题的解决证据——两者无关。
- **不**把真实调用成功、文档定稿、阶段验收当作已修复。
- **不**在没有新证据的情况下宣称已定位根因。
- **不**通过放宽沙箱权限来"绕过"该错误（那是掩盖，不是修复）。
- **不**批量终止 Python 进程。

在用户提供 §7 的信息之前，本问题**保持 OPEN**。
