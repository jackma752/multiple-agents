# 0.5.2：Windows 可写工作区修复

发行源码：`77d785bfc1dbd2ff91573a9b701ccdcdd711e991`，分支 `fix/desktop-workspace`。
未合并 main，未推送；没有覆盖用户正在运行的 0.5.1。

修复原因、边界与 205 项定向回归见 [问题记录](issue-desktop-workspace-write.md)。
包内嵌入模块已检查，包含 workspace_thread_config / settings_readback，开发认证与测试桩未混入。
可见文件清单 138 项。无账号临时环境中的本地协议和沙箱文件对照，不消耗模型额度；真实模型编辑未验收。

## 产物

保存位置：`E:\plug\agent-hub\dist\p5-release-0.5.2`。

| 文件 | SHA-256 |
|---|---|
| AgentHub-0.5.2-windows-x64-setup.exe | `f1bba309b09c38ff0d7bc90c80a1b42dca31a57efb5d7139e3c67ef86f0e42bb` |
| AgentHub-0.5.2-windows-x64-portable.zip | `c9f3d894ef829de3b464814110f787fd11d05c201bb2d417891c7a6d92112fe6` |

冻结包在独立空数据目录、18207 端口检查：启动、一次性配对、认证读取无 Agent 配置、
无开发令牌、退出码 0。检查不连接用户当前 8080 服务或已有配置。检查请求缺少 Origin 时确实返回 403；
补齐同源标头后正常退出，没有为测试修改认证约束。

本次未再运行安装器，避免修改用户现有安装的卸载注册信息；安装器模板沿用 0.5.1。

## 用户更新与下一次验收

1. 在当前网页点击“退出 Hub”，结束旧后台。
2. 运行新 setup，确认安装到原目录 `E:\multiple-agent\AgentHub`；不先卸载、不删除用户数据。
3. 重新打开，保持原工作区和“允许写入”。
4. 先在专门的样例工作区批准一次真实任务：创建文件、修改内容、运行一个简单检查。
   工具操作继续经过现有审批或阶段规则。该模型回合本次没有代用户执行。

## 本地诊断文件登记

仅登记非凭据文件，原文件留在被忽略的 worktree var 目录，不随包分发：

| 路径（相对 `.worktrees/desktop-workspace`） | SHA-256 |
|---|---|
| var/workspace-diagnostic/readback.json | `26830544eb7364e89cf8e8c88b42c298941d1dfc3dc07d3b5f5585c30d21377a` |
| var/workspace-diagnostic/package-smoke.json | `746a588e3f7d573535ab4c35dee533fcff373eb689cc481574e617d8e505608a` |
