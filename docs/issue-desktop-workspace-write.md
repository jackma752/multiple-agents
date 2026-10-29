# 安装版 Codex 可写工作区初始化失败

状态：修复，待真实模型编辑验收。授权：2026-10-27，用户要求保留多 Agent 在项目工作区实际开发的核心能力。

## 现象与范围

0.5.1 安装版选择“允许写入”后，Codex 启动被 `appserver_settings_mismatch: sandbox` 拒绝。
执行记录确认没有发送 `turn/start`，本次模型用量为 0。Claude Code 的周额度耗尽是另一问题。

相关契约：`packages/contracts/agenthub/workspace.py`；路径和访问级别属于已批准的快照。
当前实现：`appserver_adapter.py` 在 thread/start / thread/resume 后回读 sandbox，逐回合再传 sandboxPolicy。

## 修复标准

- 用户批准的工作区可使用 workspace-write，不能用只读代替验收。
- 初始化配置与回合策略一致，继续拒绝范围扩大、网络权限扩大和审批设置不符。
- 权限回读失败保留必要的配置诊断，不保存提示词或认证内容。
- 针对性回归验证 workspace-write 的线程启动、续接、回合配置和越界拒绝。
- 本地无账号的协议检查与真实模型编辑验收分开记录；不自行追加真实模型调用。
- 安装包修复作为新产物交付，保留现有安装、用户数据和工作区。

## 调查记录

- 现有 stub 工作区测试只覆盖只读完整路径；可写回读仅有函数级测试。
- 失败记录没有保留实际 sandbox 回读值，尚不能根据旧记录确定具体不匹配字段。

## 原因与修复

无账号临时 CODEX_HOME、相同本机 CLI 的本地协议检查复现：请求 `sandbox=workspace-write`，
Windows 沙箱后端未配置时回报 `readOnly`。这是 Codex 的平台回退行为，Hub 的拒绝并非代理故障。
官方来源：[平台回退实现](https://github.com/openai/codex/blob/main/codex-rs/config/src/config_toml.rs)、
[Windows 配置说明](https://developers.openai.com/codex/windows/)。本机二进制的回报另行核对，未把最新文档当成本机实测。

- 在可写线程启动和续接前读取对应 cwd 的有效配置。仅当 Windows 没有配置后端时，
  在本线程的 config override 选择无需管理员安装的 `unelevated`；保留已有 elevated/MXC 配置，
  不修改用户 config.toml，不修改系统策略或账户，也不切换到 full-access。
- 线程与回合均限制工作区写入，去掉继承的额外可写根、临时目录写权限和工具网络权限。
  根列表为空表示 Codex 隐含的 cwd；每回合的 writableRoots 显式写入已批准的工作区。
- 回读仍校验 sandbox、cwd、审批模式和审阅人，额外校验临时目录排除项。
  配置诊断只保存相关字段，不保存提示词或登录材料。
- Windows unelevated 使用受限令牌，网络隔离能力弱于 elevated；仍属用户监督通道。
  本改动没有把原生子 Agent、无需审批的工具或进程后代描述为 Hub 可完整控制。

## 验证

- 本地 CLI：只初始化线程，不发送 turn/start；无账号、无凭据，外部代理指向关闭端口。
  加入线程配置后回报 workspaceWrite、networkAccess=false、两个临时目录排除项为 true。
- 同一 CLI 使用临时全局后端配置，通过 standalone command/exec 做本地文件对照
  （该接口不继承某个线程的配置，因此显式在临时进程配置中选择相同后端）：
  工作区内 Set-Content exit=0、文件存在；工作区外 exit=1、PermissionDenied、文件不存在。
  此对照验证本机后端的文件边界，不等于真实模型编辑回合验收。真实模型调用 0 次。
- `python -m pytest tests/adapters/test_desktop_workspace.py tests/adapters/test_appserver_client.py tests/http/test_appserver_s6.py tests/http/test_async_and_tool_approval.py tests/http/test_desktop.py tests/test_appserver_s3.py -q -o addopts=''`
  → **205 passed in 27.49s**。包括可写启动/续接、只读保留、默认后端选择、已有后端保留、
  降级回读在模型回合前拒绝、额外根/网络/临时目录边界拒绝。
- 新安装包编号 0.5.2，旧 0.5.1 产物保留；没有自动覆盖正在运行的用户安装。
