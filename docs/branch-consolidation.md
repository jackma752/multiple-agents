# 分支整理方案（2026-10-23，仅方案，未执行）

> 历史提案：下文“现状”仅指 2026-10-23，不能用于判断当前 main 或再次授权。2026-10-30 用户已明确授权整合所有开发流程；本提案随历史分支纳入，当前结果见 [完整整合记录](development-integration-2026-10-30.md)。未据此执行删除、推送、证据迁移或 CI 建设。

下面每一步都会改变仓库或备份状态，必须由用户在对话中逐项批准（[Git 工作流](git-workflow.md)第 5 节）。本文件只记录核对结果和建议顺序，没有执行任何合并、推送、删除或清理。

## 1. 现状（2026-10-23 只读核对）

- **远程仓库**：没有。`git remote -v` 输出为空，所有分支都没有上游。仓库目前只有本机这一份。
- **CI**：没有。`feat/appserver-s6` 和 `main` 上都不存在 `.github/`、`.gitlab-ci.yml`、`azure-pipelines.yml`、`.pre-commit-config.yaml`；本机没有安装 `gh`；git 为 2.56.0（MinGit）。
- **`main`**：停在 `1d4a303`（2026-10-14，P2）。它完全包含在整合基线里：基线领先 74 个提交，落后 0 个，可以直接快进。
- **整合基线**：`feat/appserver-s6` @ `2ccae55`。
- **本地分支共 30 个**，其中 28 个已是基线的祖先，内容都已在基线中。没有并入的只有 2 个：
  - `feat/p3-wrapup` 的 `42a61bb`：
    - 只改了 `docs/p3-probe-readonly-write-2026-10-14.md`（+20/−7），内容是 exec 事件分类的说明；
    - 它的父提交已在基线，但基线上的该文件仍是父提交的版本。
  - `fix/approved-only` 的 `071c918`：
    - 是 2026-10-14 留下的 22 个未跟踪文件的 wip 快照，从 P0 的 `15eedbc` 分出；
    - 其中 21 个路径在基线上已有相同或更新的版本，只有 `repair-baseline.json` 不在基线；
    - 提交说明自己写明“很可能已被取代，提交只为溯源”。
- **工作树**：
  - 有两个失效的登记，`E:/ds_agent/workplace/agent-hub/.worktrees/api` 和 `.../web`：目录已不存在，git 标为 `prunable`。
  - 其余工作树都没有未提交的改动；也没有 stash。
- **标签**：14 个，其中 13 个 `evidence/*`，1 个 `milestone/p2-kernel`。
- **主检出**：`E:\plug\agent-hub` 停在 `feat/p3-codex-probe`（`8b24d0a`，已并入基线）。
- **今天新开的分支**，都从 `2ccae55` 开出：
  - `feat/codex-runtime`（工作流 A：Codex 运行时）；
  - `feat/claude-code-min`（工作流 B：Claude Code 最小接入）；
  - `plan/branch-consolidation`（本文件）。

## 2. 删除工作树之前必须先迁走证据

真实运行证据只登记哈希，原文件留在各工作树的 `var/` 下（[证据清单](evidence-manifest.md)，路径相对于 `.worktrees\`）。例如，新步骤 A 的比较基准就是 `appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\schema\`，清单哈希为 `12c012a1…`。

`git worktree remove` 会连同这些未跟踪文件一起删除。所以**任何工作树在删除之前，都要先把它的 `var/` 复制到仓库外的证据目录，并按清单核对哈希**。

## 3. 建议顺序

每一步都需要单独批准。

1. **迁移证据**（不涉及 git）：
   - 把各工作树 `var/` 中的证据复制到 `%LOCALAPPDATA%\AgentHub\evidence\<工作树名>\`，逐个核对 SHA-256；
   - 清单里还“尚未登记”的部分先补登记；
   - 更新清单中的路径。
2. **补上 `42a61bb`**：在整合分支上 `git cherry-pick 42a61bb`。只涉及一个文档，预计没有冲突。
3. **归档 `fix/approved-only`，不合并**：
   - 打带注释的标签 `archive/approved-only-snapshot`，指向 `071c918`；
   - 之后删除这个分支及其工作树，快照仍可以通过标签找到。
4. **合并今天的两条工作流**：
   - A 和 B 各自评审通过后，用 `--no-ff` 合并进整合分支，保留分支历史；
   - 冲突预计只出现在 `docs/HANDOFF_CURRENT.md`、`docs/adapter-capabilities.md`，可能还有 `apps/worker/agenthub_kernel/adapters.py`（两条工作流都会在 `build_adapter()` 里登记）。
5. **快进 `main`**：P3 双 Agent 基线验收后，把 `main` 快进到整合分支。两者没有分叉，不需要合并提交。然后打标签 `milestone/p3-two-agent`。
6. **清理**：
   - 删除已是 `main` 祖先的分支，所有证据点都由标签保留；
   - 第 1 步完成后，再删除对应的工作树；
   - 用 `git worktree prune` 清除两个失效登记；
   - 主检出切换到 `main`。
7. **远程仓库与备份**（需要用户决定）：
   - 新建一个私有远程仓库，例如 GitHub 私有库；添加 `origin` 后，首次推送 `main`、整合分支和全部标签；
   - 远程建立之前，至少先在仓库外做一份离线备份：`git bundle create <仓库外路径>\agent-hub-<日期>.bundle --all`，并用 `git bundle verify` 核对。
8. **CI**（远程建立之后）：
   - 一个只跑离线测试的工作流：Windows runner，Python 版本与 `.venv` 一致，按仓库的依赖文件安装，然后运行 `python -m pytest -q`；
   - 不注入任何凭据，不运行真实探测（真实探测都需要逐次批准）；
   - 工作流文件等远程方案确定后再提交。

## 4. 仍缺的工程基础

- 远程仓库和备份：没有；第 7 步需要用户决定。
- CI：没有；在第 7 步之后做。
- `main` 落后基线 74 个提交；在第 5 步、P3 验收之后快进。
