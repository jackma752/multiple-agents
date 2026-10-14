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

## 2. 本环境为什么不能替你建：实测结果

在受约束的执行环境里写入 `%LOCALAPPDATA%` 被拒绝（只允许工作区内写入）：

```
target: C:\Users\Administrator\AppData\Local\AgentHub\codex-home
CREATE: denied -> UnauthorizedAccessException: 对路径"AgentHub"的访问被拒绝。
```

因此这一步**必须由你在自己的终端里执行**（那里可以写 `%LOCALAPPDATA%`）。
这不是权限配置问题，也不是 Codex 的问题。

## 3. 你要执行的步骤

先看一眼当前状态（不会写任何东西，也不会读取凭证）：

```powershell
cd E:\plug\agent-hub
.\.venv\Scripts\python.exe tools\demo\codex_login.py
```

然后按它打印的步骤做（等价命令如下）：

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
4. 一次登录长期有效；需要撤销时删掉该目录下的 `auth.json` 并重新登录。

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
