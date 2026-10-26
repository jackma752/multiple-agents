# 未决问题：`python.exe` 被降到 `Low` 完整性级别（读不到 MSIX 包目录、写不了高位目录）

**状态：未解决（OPEN）——用户手工调整后进程变为 `Medium`，但降权来源未定位。**
**不得因为随后 `codex` app-server 成功，就视为已修复。**

最后更新：2026-10-26。

---

## 1. 观测到的事实（同一次会话、同一台机器）

在**同一个控制台**里，父子进程的强制完整性级别不一致：

| 检查对象 | 结果 |
|---|---|
| `whoami /groups \| Select-String "Mandatory Level"`（PowerShell 自己） | `High Mandatory Level` |
| `cmd /c whoami /groups`（cmd 子进程） | `High Mandatory Level` |
| `python.exe -c "...subprocess.run(['whoami','/groups'])"`（python 子进程） | **`Low Mandatory Level`** |

`E:\plug\agent-hub\.venv\Scripts\python.exe` 为 3.12.0 64-bit；`os.getlogin()` = `Administrator`。
也就是说：**同一个父进程下 `cmd` 继承 High，`python.exe` 掉到 Low** —— 限制是按可执行文件下发的，
不是按窗口、不是按用户、不是按启动参数。

调整权限之后（用户操作，具体做法未记录在案），同一个探测脚本报 `Medium Mandatory Level`。

## 2. 造成的实际后果（三处，均已单独留痕）

| 后果 | 表现 | 记录 |
|---|---|---|
| 读不到 Claude Code 的 MSIX 包目录 | `GetFileAttributesW` / `os.stat` 在 `...\LocalCache\Roaming` 处 `winerror=5`；适配器报 `executable_missing` 并拒绝执行 | 见 §3 |
| 写不了 `CODEX_HOME` 的 sqlite 状态库 | app-server 启动期内退出，`server refused setup: disconnected` | `docs/issue-appserver-codex-home-sqlite.md` |
| 更早的临时目录不可写 | `%TEMP%` 不可用时 `tempfile.gettempdir()` 回退到仓库根，造成 5 个"路径必须在仓库外"的假失败 | `docs/HANDOFF_CURRENT.md` 相关段落 |

## 3. Claude Code 路径的详细取证

目标路径：

```
C:\Users\Administrator\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.308\c17a819afb40\claude.exe
```

`Low` 进程下的逐层结果（脚本 `var/probe_path2.py`）：

| 层 | `...\Claude_pzs8sxrjxfjjc` | `...\LocalCache` | `...\LocalCache\Roaming` | `...\claude.exe` |
|---|---|---|---|---|
| `GetFileAttributesW` | OK | OK | **FAIL err=5** | **FAIL err=5** |
| `CreateFileW` | OK | **FAIL err=5** | **FAIL err=5** | **FAIL err=5** |
| `os.stat` | OK | OK | **FAIL errno=13 winerror=5** | **FAIL errno=13 winerror=5** |
| `\\?\` 长路径形式 | — | — | — | **FAIL errno=13 winerror=5** |

同一路径的 ACL（`icacls ...\LocalCache`）：

```
S-1-15-2-3557520199-3666692283-3112367039-3524159787-2791857073-3163583606-3692855932:(F)
S-1-15-2-3557520199-3666692283-3112367039-3524159787-2791857073-3163583606-3692855932:(OI)(CI)(IO)(F)
DESKTOP-7CEN53I\Administrator:(OI)(CI)(F)
DESKTOP-7CEN53I\CodexSandboxUsers:(I)(OI)(CI)(RX)
NT AUTHORITY\SYSTEM:(I)(OI)(CI)(F)
BUILTIN\Administrators:(I)(OI)(CI)(F)
```

即：**Administrator 在该目录上有完全控制权**，但 `Low` 令牌仍然被拒——所以这不是普通 ACL 配置问题，
而是**完整性级别**（或按进程下发的策略）在起作用。旁证：普通（非管理员）PowerShell 里
`Test-Path <claude.exe>` = `True`，说明文件确实存在且高位进程可读。

适配器的行为是**正确的**：它看不见二进制就拒绝执行，并把原因归为 `executable_missing`
（`packages/adapters/agenthub_adapters/claude_code.py:296-297`）。**不能**把这条记为适配器 bug。

## 4. 已确认与未确认

**已确认**

- 同一时刻、同一控制台：`cmd` = High，`python.exe` = Low。
- `Low` 下读写该 MSIX 目录均被拒（`winerror=5`），`Medium`/`High` 下 `Test-Path` = True。
- 调整后 python 变为 `Medium`，`codex` app-server 随即出真实回合。

**未确认**

- **是谁把 `python.exe` 降到 Low**（安全软件？AppLocker / WDAC？某个兼容性 shim？DSH 的沙箱钩子？）——
  只知道 ACL 里存在 `CodexSandboxUsers` 组，疑似与机器上的沙箱工具相关。
- `Medium` 下 Claude Code 的 MSIX 目录能否被读（**未复验**；`Medium` 下 app-server 已可用是另一件事）。
- 该降权是否会再次出现（用户调整的持久性未知）。

## 5. 明确不采用的做法

- **不**通过复制/改名可执行文件、或用别的解释器名字，去规避机器上的安全策略；
- **不**为该目录放宽 ACL 以便 `Low` 进程通过；
- **不**在文档里写"已修复"——只写"观测到一次变化后恢复"。

## 6. 关闭条件

1. 能定位降权来源（具体策略/工具/规则），并说明为何只作用于 `python.exe`；或
2. 明确记录并固定"运行 Hub 的服务进程必须 ≥ `Medium` 完整性级别"，在启动说明里作为前提写出，
   同时**分别复验** codex app-server 与 claude-code 两条通道在该前提下是否可用（后者目前未复验）。
