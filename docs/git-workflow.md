# Git 工作流与溯源规则

2026-10-21 起执行，经仓库所有者同意。目标是让每次改动、每次授权、每份真实运行证据都能在 git 中追溯到。

## 1. 每轮结束即提交

- 一轮工作对应一个分支和一个 `.worktrees/<name>` 工作树，沿用 AGENTS.md 规则 2。
- 交接之前必须提交。没有完成验证的内容用 `wip(...)` 前缀提交，并在说明里写清“未验证”的部分；不能让改动停留在工作区或暂存区就交给下一轮。
- 交接文档 `docs/HANDOFF_CURRENT.md` 要引用本轮的提交号。
- 一个提交只放一轮的内容。两轮混在同一工作区、已经无法拆开的，提交说明里要逐轮写明（例如 2026-10-21 的 chat-workbench 提交）。

## 2. 提交说明格式

```text
<type>(<scope>): <一句话说明>

<做了什么、为什么；未验证的部分要写明>

验证：<实际命令> -> <实际输出摘要>

Agent: <执行者，例如 Claude Code (claude-opus-5-5) / Codex / 人工>
Authorized-by: <授权来源：谁、何时、在哪里同意的>
```

- `Agent` 记录实际改代码的执行者；同一提交有多个执行者时逐一列出，未知时写 `unknown`。
- `Authorized-by` 记录用户授权。涉及真实模型回合（S3/S4/S5 等）时，必须写明对应的单次授权。
- 提交前给出 `git diff --cached --stat` 和测试的实际输出（AGENTS.md 规则 6）。

## 3. 身份

- 仓库 `.git/config` 的作者和提交者身份是仓库所有者本人：`PENDING-NAME <haoph710@gmail.com>`。姓名暂时空着。
- 确定姓名后，在仓库根目录新增 `.mailmap`，写一行 `真实姓名 <haoph710@gmail.com>`，并把 `user.name` 改成真实姓名。`.mailmap` 会让 `git log` / `git shortlog` 显示真实姓名，不改写历史。
- 2026-10-21 之前的提交身份是占位的 `Agent Hub Dev <dev@example.invalid>`，看不出执行者，以当时的交接文档为准。

## 4. 真实运行证据

- 证据文件（`result.json`、wire 记录、stderr、探测数据库）**不入库**：其中可能有会话 ID、模型输出或环境信息。它们留在各工作树被忽略的 `var/` 下。
- 每份证据的路径、大小和 SHA-256 登记在 [证据清单](evidence-manifest.md)，随提交一起入库。以后重新取得或核对证据时，以哈希为准。
- 记录真实运行结论的提交打带注释的标签 `evidence/<名称>`，阶段验收打 `milestone/<名称>`。标签一旦打上就不移动，需要更正时新打一个。
- 不对 `codex-home`、`auth.json` 等可能含凭据的目录计算哈希，也不读取它们。

## 5. 需要用户另行批准的操作

推送、合并到 `main`、改写或删除已有提交与标签、新建远程仓库，都需要用户在对话里明确同意（AGENTS.md：批准 Agent 运行 ≠ 批准 `git push` / 合并）。

当前整合：2026-10-30 用户已明确要求把所有开发流程合并到本地 `main`，本轮按该授权整合；早期 main 停在 P2 的记录已过期。最新情况见 [完整整合记录](development-integration-2026-10-30.md)。此次授权不包含推送、删除历史或配置远程仓库。

后续发布授权：同日用户另行要求上传 GitHub，并明确选择公开仓库 `jackma752/multiple-agents`。该授权允许创建该仓库、配置对应远程和上传 main；不扩展到删除历史、上传凭据或自动发布安装包。公开范围与检查见 [GitHub 发布记录](github-publication.md)。

## 6. 本机环境

- git：`C:\Users\Administrator\AppData\Local\Programs\MinGit\cmd\git.exe`（Git for Windows MinGit 2.56，签名有效），已加入当前用户的 PATH，新开的终端生效。
- **不要使用** `E:\ds_agent\workplace\tools\MinGit` 下的原件。那个目录带 Low 完整性标签（子项继承），git 会以低完整性运行，写任何正常目录都返回 “could not lock … Permission denied”。上面的副本内容与原件哈希一致，但不带该标签。2026-10-21 曾把这个现象误判为“Agent 沙箱禁止写 .git”，在此更正。
- 大多数工作树属于 `CodexSandboxOffline` 账户。`Administrator` 使用前，要先把它们加入全局 `safe.directory`；新建工作树时同样补上：`git config --global --add safe.directory E:/plug/agent-hub/.worktrees/<name>`。
- Agent 无法提交时（例如工具受限），只准备好提交说明和脚本，放在仓库外或 `var/` 下，由用户在自己的终端执行；不要把“无法提交”留到下一轮。
