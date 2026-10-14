# 工具链与进程启动诊断记录

日期：2026-10-14。范围：Python 启动失败排查、进程启动/停止方式核对、PID 管理工具。

本文件记录**实测到的事实**与**未能复现的部分**，不把推测写成结论。

---

## 1. `0xc0000142`（STATUS_DLL_INIT_FAILED）：未能复现

### 状态：**根因未定位，如实保留**

`0xc0000142` = `STATUS_DLL_INIT_FAILED`（进程在加载/初始化 DLL 阶段失败）。

**本次会话未能复现该错误码。** 我做了以下尝试，均未产生 `0xc0000142`：

| 尝试 | 结果 |
|---|---|
| 单次直接启动 base python（`D:\python\python.exe`） | 退出码 0 |
| 单次直接启动 venv python（`.venv\Scripts\python.exe`） | 退出码 0 |
| 12 次连续启动 venv python | 全部成功（无失败码） |
| 12 次并发启动 venv python | 12 个 PID 均被创建（`10472, 18252, 29904, 25476, 10464, 9080, 23164, 28500, 12376, 5492, 2020, 680`） |

因此**本条不能宣称已修复**，只能记录观察到的相关事实与可用启动方式。

### 观察到的真实失败（有明确成因，均非 DLL 初始化失败）

排查中出现的失败都另有原因，可与 `0xc0000142` 区分：

| 现象 | 实测成因 | 证据 |
|---|---|---|
| `程序"python.exe"无法运行: Access is denied` | **管道被沙箱拒绝**（原生进程 stdout 经 PowerShell 管道/`2>&1` 被拒） | `& $exe -c pass 2>$null \| Out-Null` → Access is denied |
| 输出文件为**空** | PowerShell 把原生进程输出重定向到文件**不生效**，内容静默丢失 | `& $exe -c "print('hi')" *> _l.txt` → `_l.txt` 长度 0 |
| `Start-Process ... -Wait -NoNewWindow` → Access is denied | 沙箱拒绝该启动组合 | 12 次连续启动全部报错 |
| `Start-Process -PassThru`（无控制台）子进程**不退出** | 子进程 stdio 未接控制台时挂住 | `WaitForExit(15000)` 返回 `False`；只杀该 PID 后恢复 |
| 后台任务退出码 `1073807364` | **`0x40010004` = `DBG_TERMINATE_PROCESS`**，是我自己执行 `Get-Process python \| Stop-Process -Force` 造成的强制终止 | 与 `0xc0000142`（`0xC0000142` = `3221225794`）**完全不同**，不可混为一谈 |
| `os.kill(pid, SIGTERM)` → `PermissionError WinError 5` | 沙箱禁止跨进程终止 | 见 §3 |

### 待验证的假设（**未证实，不得当作结论**）

`0xc0000142` 常见于**进程在 DLL 初始化阶段被强制终止**，或桌面堆/句柄资源紧张时。
本次会话早期确实多次执行过 `Get-Process python | Stop-Process -Force`（见 §3），
而当时正有 python 进程处于启动/初始化阶段。**二者相关但未证明因果**，
需要在下一次复现时抓取以下信息才能定位：

- 失败进程的**完整路径**与命令行
- 启动方式（直接调用 / `Start-Process` / 服务 / 计划任务）
- 失败时刻的系统资源与同时运行的进程数
- Windows 应用程序事件日志中该 PID 的条目

---

## 2. 实测可用的启动方式（本环境）

| 启动方式 | 是否可用 | 备注 |
|---|---|---|
| 前台直接调用，**不接管道、不重定向**：`& $py -c "pass"` | ✅ 可用 | 退出码可靠 |
| 前台直接调用，输出到控制台 | ✅ 可用 | 本会话大量使用 |
| 由 Python 启动子进程，stdout 指向**文件句柄** | ✅ 可用 | 跨进程持久化测试即用此法 |
| 后台任务（harness 管理的 job） | ✅ 可用 | 可 `job_kill` 干净结束 |
| `serve.py` 启动服务 + `stop.py` 优雅关闭 | ✅ 可用 | 退出码 **0**（干净退出） |
| 接 PowerShell 管道（`\| cmdlet`、`2>&1`） | ❌ 不可用 | Access is denied |
| PowerShell 重定向到文件（`> file`、`*> file`） | ❌ 不可用 | 文件为空，内容丢失 |
| `Start-Process -Wait -NoNewWindow` | ❌ 不可用 | Access is denied |
| `Start-Process -PassThru`（无 `-Wait`，无控制台） | ❌ 不可用 | 子进程挂住，`WaitForExit` 返回 False |

**可用的 Python 解释器**

| 路径 | 版本 | 用途 |
|---|---|---|
| `E:\plug\agent-hub\.venv\Scripts\python.exe` | 3.12.0（venv） | **本项目所有测试与工具** |
| `E:\plug\agent-hub\.venv\Scripts\pythonw.exe` | 3.12.0 | 无控制台变体 |
| `D:\python\python.exe` | 3.12.0（base） | 建 venv / 引导工具（`tools/bootstrap`） |

---

## 3. 进程管理：改为按 PID，取消按名批量强杀

### 问题

此前使用：

```powershell
Get-Process python | Stop-Process -Force     # ❌ 危险
```

这会终止机器上**所有** python 进程，包括无关工作与其他 agent 的进程；
且如上表所示，它产生的 `0x40010004` 曾被误读为启动失败。

### 现状：显式 PID 记录与核对

`tools/demo/serve.py` 启动时把**自己的 PID** 写入 `var/serve-<port>.pid`
（退出时自动清除），并打印：

```
  PID      : 15988  (recorded in var\serve-8080.pid)
  stop with: python tools/demo/stop.py --port 8080
```

`tools/demo/stop.py` 按 PID 精确停止，且**失败即拒绝（fail-closed）**：

1. 从 PID 文件取 PID；
2. **非破坏性归属校验**：向该端口请求 `GET /openapi.json`，确认 `info.title == "Agent Hub"`；
   不是本项目则**拒绝终止**；
3. 通过校验后，优先请求**优雅关闭**（见下），失败才回退到进程信号。

### 为什么需要优雅关闭

本环境**禁止跨进程终止**：

```
failed to signal pid 30200: PermissionError: [WinError 5] Access is denied
```

因此新增开发用端点 `POST /api/dev/shutdown`（**仅 user 角色、仅 Mock/开发用途、只绑 localhost**），
由 `serve.py` 注入 `server.should_exit = True` 钩子实现优雅退出。
`stop.py` 默认走这条路；`--no-graceful` 可跳过。

### 实测结果

```
> python tools\demo\stop.py --status
PID file : pid 15988
identity : http://127.0.0.1:8080/openapi.json reports title='Agent Hub'
HEALTHY                                        (exit 0)

> python tools\demo\stop.py
PID file : var\serve-8080.pid -> pid 15988
identity : http://127.0.0.1:8080/openapi.json reports title='Agent Hub'
graceful : {'stopping': True}
stopped pid 15988 (port 8080 closed, graceful)  (exit 0)
```

服务进程与后台任务均**以退出码 0 干净结束**（非强制终止）。

### 注意：`os.kill(pid, 0)` 不能用作存活探测

Windows 上 `os.kill` 对普通信号会走 `TerminateProcess`，**可能直接杀死目标**；
而在受限沙箱下它抛 `OSError`，表现为"进程不存在"——本次就出现过
"服务明明可达（HTTP 200）却报 process is not running"的**假阴性**。
`stop.py` 因此**完全不用它**做存活判断，改用 TCP 连接 + HTTP 归属校验。

---

## 4. 可复现的启动验证

```powershell
# 1) 启动（会打印 PID 并写 PID 文件）
.\.venv\Scripts\python.exe tools\demo\serve.py --port 8080

# 2) 核对：PID 文件内容 / 归属 / 健康
.\.venv\Scripts\python.exe tools\demo\stop.py --status
#    期望：identity 报告 title='Agent Hub'，输出 HEALTHY，退出码 0

# 3) 核对 PID 与进程一致（按 PID，不按名）
Get-Content var\serve-8080.pid
Get-Process -Id (Get-Content var\serve-8080.pid) |
    Select-Object Id, ProcessName, StartTime

# 4) 功能验证
.\.venv\Scripts\python.exe tools\demo\demo_flow.py
#    期望：DEMO_EXIT=0

# 5) 优雅停止（退出码 0）
.\.venv\Scripts\python.exe tools\demo\stop.py
```

若 PID 文件缺失或端口无人监听，`stop.py` 会清除陈旧 PID 文件并返回 0/2，不会误杀。

---

## 5. 结论与未决项

**已解决**

- 进程停止改为 **PID 精确管理 + 归属校验 + 优雅关闭**，不再按名批量强杀。
- 明确记录本环境**可用/不可用**的启动方式（§2），后续工具按可用方式编写。
- 区分了三类曾混淆的现象：沙箱 I/O 拒绝、`0x40010004` 强制终止、`0xc0000142` DLL 初始化失败。

**未决（如实保留）**

- `0xc0000142` **未复现、根因未定位**。上文"强制终止打断 DLL 初始化"只是**假设**，
  需要在下一次复现时按 §1 末尾列出的信息抓取后才能确认或排除。
- 本环境禁止跨进程终止，故 `--no-graceful` 路径在此不可用（在普通机器上可用）。
