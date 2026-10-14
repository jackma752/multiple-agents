# 长期凭据目录与用户自行登录（Codex）

日期：2026-10-14。目的：把 Codex 的登录凭证固定放在**仓库之外的一个长期目录**，
并给出**由你自己执行**的登录步骤。本文件不含任何凭证内容。

---

## 1. 位置与优先级

| 项 | 值 |
|---|---|
| 默认长期目录 | `%LOCALAPPDATA%\AgentHub\codex-home`（Windows：`C:\Users\<你>\AppData\Local\AgentHub\codex-home`） |
| 覆盖方式 | 环境变量 `AGENT_HUB_CODEX_HOME`（优先级最高） |
| 凭证文件 | `<CODEX_HOME>\auth.json`（**只报告是否存在，从不读取内容**） |
| 代码 | `apps/worker/agenthub_kernel/config.py`：`codex_home()`（纯路径解析）、`ensure_codex_home()`（真正启动前才创建）、`credential_location()`、`codex_home_status()` |
| 工具 | `tools/demo/codex_login.py`（查看状态 + 打印登录步骤） |

**为什么必须放在仓库之外**：`CODEX_HOME` 里会放一份 `auth.json`。即使被
`.gitignore` 忽略，一次 `git add -f`、一次打包或一次备份就会泄露。所以：

* 默认位置在仓库之外（`%LOCALAPPDATA%`，不是 `%TEMP%`）；
* `CodexAdapter.start()` 在启动前才创建该目录，读设置本身没有写副作用；
* 回归测试：`tests/test_hygiene.py` 断言解析出的 `CODEX_HOME` 与凭证路径
  **不在 `REPO_ROOT` 之下**，且解析不创建目录。

**为什么不放在 `%TEMP%`**：2026-10-14 的实测显示，当 `CODEX_HOME` 位于临时目录时，
Codex 会**拒绝**创建 helper binaries（PATH 别名）：

```
WARNING: proceeding, even though we could not create PATH aliases: Refusing to
         create helper binaries under temporary dir "...\Temp\..."
```

它只是警告并继续执行（那次真实调用仍然成功），但这是能力降级。临时目录只适合
一次性验证；长期使用请用普通目录。

## 2. 当前实际状态（2026-10-14 核对，元数据 + 登录状态）

| 检查项 | 结果 |
|---|---|
| 目录存在 | **存在**：`C:\Users\Administrator\AppData\Local\AgentHub\codex-home` |
| 目录内容（仅元数据） | `auth.json`、`sessions/`、`log/`、`tmp/`、`state_5.sqlite` 等（**未读取任何文件内容**） |
| 凭证文件 | `auth.json` 存在，3918 字节，mtime `2026-10-14 21:28:13`（**只读元数据**） |
| 登录状态 | `codex login status` → **`Logged in using ChatGPT`**，退出码 0 |
| 本进程能否**写**该目录 | **不能**：`codex login status` 输出 `WARNING: … could not create PATH aliases: 拒绝访问。(os error 5) at path "...\codex-home\tmp\arg0\..."` |

**关键区分（曾写错，特此更正）**：

* 这里的"访问被拒"是**本执行沙箱拒绝我的进程写入 `%LOCALAPPDATA%`**，
  **不是**"目录不存在"，也**不是**登录失效。
* 早期文档根据一次失败的 `New-Item` 写了"该目录不存在/需由你创建"。**那是错的**：
  失败的是*我的*写入尝试，而目录与凭证都一直在。
* 结论：**目录与登录都有效**；只是我这一侧的进程没有写权限。
  真实探测需要写 `CODEX_HOME`（`logs/state/tmp`），因此要么在你的终端里运行，
  要么为该命令单独批准更宽的写权限——**不要**因此去改目录位置或重建凭证。

工具 `tools/demo/codex_login.py` 现在会分别报告"存在/凭证存在"与"本进程可写"，
并且**从不读取凭证内容**。

## 3. 你要执行的步骤（如已登录可跳过）

先看一眼当前状态（不会写任何东西，也不会读取凭证）：

```powershell
cd E:\plug\agent-hub
.\.venv\Scripts\python.exe tools\demo\codex_login.py
```

若上面显示"目录存在 / 凭证存在 / credential set: yes"，**你不需要再登录**。
若确实未登录，再按下面步骤做（等价命令）：

```powershell
# 1) 建目录并登录一次（Codex 自己会走浏览器/设备码流程）
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\AgentHub\codex-home"
$env:CODEX_HOME = "$env:LOCALAPPDATA\AgentHub\codex-home"
& "$env:LOCALAPPDATA\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe" login

# 2) 不发模型调用，只确认登录状态（期望输出 Logged in using ChatGPT）
& "$env:LOCALAPPDATA\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe" login status

# 3) 让 Agent Hub 指向同一个 home（默认就是这个路径，只有你换了目录才需要设）
$env:AGENT_HUB_CODEX_HOME = "$env:LOCALAPPDATA\AgentHub\codex-home"

# 4) 复查：`credential set` 应显示 yes
.\.venv\Scripts\python.exe tools\demo\codex_login.py
```

工具也提供了这两步的封装（会用固定的 `codex.exe` 并设置好 `CODEX_HOME`）：

```powershell
.\.venv\Scripts\python.exe tools\demo\codex_login.py --status   # 只查状态，不发模型调用
.\.venv\Scripts\python.exe tools\demo\codex_login.py --login    # 交互式登录
```

### 规则（务必遵守）

1. **绝不**把 `auth.json`、token、cookie 粘贴到聊天、Issue、日志或文档中。
2. **绝不**把凭证复制进仓库（包括 `.gitignore` 忽略的目录）。
3. 登录是**你本人**的动作：本工具不会替你登录，也不会读取凭证内容。
4. **绝不**把长期 `CODEX_HOME` 当作"本次运行的临时目录"删除；探测的清理范围只包含
   探测自己创建的 `var/probes/<probe>-<mode>*` 运行目录。
5. 一次登录长期有效；需要撤销时删掉该目录下的 `auth.json` 并重新登录。

## 4. 与真实调用验证的关系

`tools/demo/codex_verify.py --mode real` 会在**发起任何调用之前**检查：

* `CODEX_HOME` 在仓库之外；
* 该目录**已有** `auth.json`（否则拒绝执行，避免白花一次真实调用）。

所以：**先按上面步骤登录一次**，之后每次真实调用仍**逐次审批**。

## 5. 已知边界

* 凭证的**内容**从不被 Agent Hub 读取；只检查文件是否存在（`codex_home_status()`）。
* 登录态过期、被远程吊销、需要重新验证时，只有 Codex 自己的 `login status` 能回答；
  Agent Hub 不猜测登录是否有效——它只在启动失败时如实报告退出码与错误。
* 同一个 `CODEX_HOME` 同时被多个 Codex 进程使用是 Codex 自身的行为，未做额外串行化。
