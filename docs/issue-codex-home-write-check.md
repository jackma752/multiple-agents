# CODEX_HOME 写入预检长时间无输出（2026-10-26）

## 结论

`config.codex_home_write_check()` 原来使用 `tempfile.NamedTemporaryFile`。在 Windows 上，当真实创建文件抛出 `PermissionError`，但 `os.access(directory, os.W_OK)` 返回真时，Python 的 `_mkstemp_inner` 会认为只是临时名称冲突，换名继续。

本机 Python 3.12 的 `tempfile.TMP_MAX` 为 `2147483647`。因此执行沙箱拒绝写入时，预检可能持续占用 CPU，而不是及时报告权限不足。用户报告的“检查没有输出”与此路径吻合；已观察到同期 Python 进程持续耗用 CPU，但未取得该进程的运行中调用栈，不能把推断写成栈级确证。

这是**预检函数的实现缺陷**，与 Codex 的可执行文件缺失、app-server 初始化 SQLite 失败分别跟踪。修复预检不等于已经修复在线接入。

## 修复

生成 UUID 文件名，通过 `os.open(O_CREAT | O_EXCL | O_WRONLY)` 单次创建，再写入、关闭、删除。创建被拒立即返回 `(False, detail)`，不进行临时名称重试；不会覆盖已有文件或读取凭据。只清理本次成功创建的探测文件。

## 验证

三个用例位于 `tests/test_codex_home_write_check.py`：

- `os.access` 报可写但实际写入被拒，只尝试一次并返回原错误；旧 tempfile 的重试上限在用例中缩到 4，避免回归时跑大量循环。
- 写入失败后删除本次创建的文件，保留已有文件。
- 正常成功，只检查指定目录，保留已有文件且无残留。

实际结果：`3 passed in 0.07s`。

本轮项目 venv 在加载 `_ssl` 时另报 `ImportError: DLL load failed`，一次 pytest 启动返回 `0xc06d007e`，所以没有声称项目 Python 或完整相关套件通过。上述纯标准库函数用 Codex 捆绑 Python、现有 pytest 包和禁用第三方插件自动加载验证，未安装或更改依赖：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -u -c "import sys; sys.path.insert(0,r'E:\plug\agent-hub\.venv\Lib\site-packages'); import pytest; raise SystemExit(pytest.main(['-o','addopts=','-q','-s','tests/test_codex_home_write_check.py','--basetemp=E:/plug/.tmp/home-write-probe-bundled-20261026']))"
```

此 `_ssl` 错误是在助手执行环境观察到的，尚未证明用户自己的 PowerShell 存在同一问题。它与仓库已有的 DLL 初始化问题记录有关联线索，根因未定位。

## 用户侧下一步

先在原终端按 `Ctrl+C` 结束尚在运行的旧检查，不按名称关闭所有 Python。然后加载修复分支中的配置文件：

```powershell
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' -u -c "import runpy; print('CHECK_STARTED',flush=True); c=runpy.run_path(r'E:\plug\agent-hub\.worktrees\codex-home-write-probe\apps\worker\agenthub_kernel\config.py'); print(c['codex_home_write_check'](),flush=True)"
```

该命令不启动模型、不读取凭据。`True` 只证明这次 PowerShell 进程能写所选目录；后台也要在可写的启动环境中运行。`False` 按返回的实际错误处理。连 `CHECK_STARTED` 都没有时，先调查 Python 启动/输出，不继续推断目录权限。

当前在线接入的已知事实：exec 默认固定的旧文件不存在；app-server 的 AgentHub 独立运行时仍存在且哈希匹配，失败记录 `turn_requested=false`，尚未发起模型回合。真实 app-server 重试应等本地预检通过后，由用户操作。此次没有追加真实模型调用。

## 后续定位：项目解释器继承 Low 完整性标签

用户在自己的 PowerShell 报告：`whoami` 为 Administrator，原生 .NET 创建/删除测试文件得到 `WRITE_OK`，但同窗口的虚拟环境 Python 仍返回 `Permission denied`。因此不能将用户侧失败简单归因于助手的 CodexSandboxOffline 账户。

只读核查 `icacls` 得到：

- `E:\plug\agent-hub\.venv\Scripts\python.exe` 带 `Mandatory Label\Low Mandatory Level:(I)(NW)`；其 Scripts 父目录也带继承的 Low 标签。
- `D:\python\python.exe` 无 Low 标签。
- 长期 home 的 DACL 给 Administrator 完全控制，CodexSandboxUsers 只有读取/执行；没有把 home 改成可写给沙箱用户。
- 同一助手环境直接运行基础解释器，令牌查询得到 `S-1-16-8192`（Medium）。虚拟环境解释器的 `_ctypes` 导入失败，未取得其直接令牌输出，不能声称已取得 Low 令牌值。

Windows 官方规则：新进程完整性取用户与可执行文件完整性中的较低者；管理员启动 Low 文件，新进程仍按 Low 工作，不能写普通用户的 Medium 数据目录。来源：[Microsoft MIC](https://learn.microsoft.com/en-us/windows/win32/secauthz/mandatory-integrity-control)。上述文件标签可以解释用户两种写入结果的差异，也为虚拟环境 DLL 初始化异常提供线索；DLL 问题是否随标签修正消失仍待复测。

用户可在自己的 PowerShell 针对这一个解释器修正标签：

```powershell
icacls 'E:\plug\agent-hub\.venv\Scripts\python.exe' /setintegritylevel M
```

如果修改标签被拒，可使用管理员 PowerShell执行这一步。不要递归修改整个工作区、磁盘或凭据目录。标签变化只影响以后新启动的进程；成功后重新运行上文的修正版写入检查，并验证 `_ssl` 能否导入。此命令尚未由助手执行，用户操作结果待确认；在线恢复仍未认定。

### 用户修改标签后的复核

用户随后执行上面的单文件命令，报告成功处理 1 个文件。助手被动复核确认该解释器现在为 `Mandatory Label\Medium Mandatory Level:(NW)`。同一项目解释器的 `_ssl`、`ctypes` 均正常加载，退出码 0；不再需要备用解释器或禁用 pytest 插件来运行本次相关测试：

```text
E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest -o addopts='' -q tests/test_codex_home_write_check.py tests/test_appserver_s1.py tests/test_hygiene.py --basetemp=E:\plug\.tmp\home-write-probe-post-label-20261026
34 passed in 0.49s
```

这确认了本次观察到的解释器 DLL 加载失败在标签修正后消失；不据此关闭所有历史 DLL 问题。用户同一 PowerShell 下的 home 写入仍待重测，真实 Codex 回合尚未重试。

用户随后在同一 PowerShell 重测，返回 `(True, '...codex-home is writable by this process')`。**用户启动环境下的 Python 写入权限已恢复**。8080 当时仍是旧 Hub，8090 本地代理端口可连接；下一步需从用户这个已验证可写的窗口重启 Hub，明确选择 `--codex-transport app-server` 并清除 exec 身份覆盖及模拟器变量，再由用户在页面做真实最小回合。当前数据目录中的阶段仅有 blocked/completed/stopped，未见活动或额度等待阶段。在线运行是否恢复仍以新回合实际结果为准。

### 用户在线最小回合已完成

用户从修正后的 PowerShell 重启 8080 后，通过页面手动批准一个 Codex 用户监督回合，并提供成功截图。只读核对该次本地记录：

- 调用 `inv-371025d822df`，`status=succeeded`，错误为空，执行模式 `supervised`。
- 身份为固定的 `codex-app-server@codex-cli 0.163.0-alpha.11.2`（SHA-256 `c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a`，336134960 字节）。
- 适配器为 `finished`，`terminal=true`，`turn_requested=true`，`turn_status=completed`，`native_unfinished=[]`，`tool_process_state=none`。
- 用量来源 `thread_total_delta`，`completeness=complete`；输入 16787、输出 77，与用户截图相符。

本次确认 **解释器标签修正后，用户监督模式下的真实 Codex 单回合恢复可用**。这是用户操作、助手只读核对，不是助手追加真实运行；没有据此追加证据标签或宣称完成正式 P4.6d/真实自动阶段验收，也不升级 exec、严格模式、工具允许或额度恢复能力。

UI 待办：顶部 `renderAgentPosture()` 只展示 `execution_admission`（严格模式），所以成功监督回合旁仍出现红色 `blocked`。应明确区分“严格受限 / 监督可用”，避免将适配器能力边界误读为本次调用失败；本诊断分支未改该显示代码。
