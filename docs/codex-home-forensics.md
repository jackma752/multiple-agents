# 脱敏诊断摘要：早期遗留的仓库内 `var/codex-home/`（已删除）

日期：2026-10-14。性质：**运行期产物清理记录**，不是功能设计文档。

---

## 1. 它是什么、为什么在仓库里

P3 早期的两次真实探测（`tools/codex_one_call.py`）把 `CODEX_HOME` 放在了
`var/codex-home/`（当时该目录已被 `.gitignore` 忽略）。理由是那时需要一个
**专用的、可写**的 home：Codex 若去写 `~/.codex/tmp` 会因为权限被拒而报
`os error 5`。

后来发现这个位置不妥：`CODEX_HOME` 里可以放一份 `auth.json` 登录凭证副本，
而**任何持有凭证的目录都不应位于仓库树内**——一次 `git add -f`、一次打包、
一次备份就可能泄露。因此：

* 默认位置已改为仓库之外（`%LOCALAPPDATA%\AgentHub\codex-home`，可用
  `AGENT_HUB_CODEX_HOME` 覆盖）；
* 代码层回归测试见 `tests/test_hygiene.py`（`codex_home` / `credential_location`
  必须在仓库之外，且解析路径不得产生写副作用）。

## 2. 清理前采集的脱敏清单

采集方式：只统计文件数量、大小、时间戳与目录结构；**没有读取任何文件内容**，
**没有把任何文件复制出去**。

| 项 | 值 |
|---|---|
| 路径 | `E:\plug\agent-hub\var\codex-home\`（仓库内，已被 Git 忽略） |
| 文件数 | 371 |
| 总大小 | 33,268,832 字节（约 31.7 MiB） |
| `auth.json` | **不存在**（凭证副本此前已被单独删除） |
| 顶层目录 | `cache/`(2)、`plugins/`(304)、`sessions/`(2)、`skills/`(49)、`thread-writer-locks/`(1)、`tmp/`(0) |
| 顶层文件（数据） | `state_5.sqlite`(+`-wal`/`-shm`)、`logs_2.sqlite`、`goals_1.sqlite`、`memories_1.sqlite`、`queue_1.sqlite`、`thread_history_1.sqlite`(+`-wal`/`-shm`)、`installation_id`、`models_cache.json`、`.sandbox_migration` |
| 体积构成 | `.pptx/.docx/.xlsx` 共 20 个文件合计约 26.4 MB，来自 Codex 的远程插件 `openai-templates` 缓存；其余为 `.md/.tsx/.json/.yaml/.mjs/.py/.png` 等插件与技能资源 |
| 会话记录 | `sessions/` 下 2 个 rollout 文件，写入时间 `2026-10-14 14:47:36` 与 `2026-10-14 19:37:55`（分别对应早期失败的一次探测与成功的那一次） |

**为什么必须删除**：两个 rollout 文件是**会话记录**（含当时对话内容），
而仓库（即使是 gitignore 目录）不应保留用户聊天内容与运行期数据库。

> 本清单刻意只保留"有多少、多大、什么时候"，不保留会话内容、thread_id、
> token、cookie、机器标识等可关联信息。

## 3. 已执行的清理

```
Remove-Item -Recurse -Force E:\plug\agent-hub\var\codex-home
```

删除后 `var/codex-home` 不再存在；仓库工作树保持干净（该目录从未被提交）。

## 4. 这次清理不影响什么

* 与 Codex 真实调用有关的**结论**都记录在别处，且不依赖这个目录：
  `docs/agent-codex-capabilities.md`（能力与实测）、`docs/codex-proxy-setup.md`
  （代理与 CA）、`docs/p3-codex-adapter.md`（适配器与安全姿态）。
* 再次做真实调用时用一个**新的、可写的、位于仓库之外**的 `CODEX_HOME` 即可；
  新 home 是空目录也能正常工作（早期那次成功的调用就是在新建的 home 上完成的）。
* `0xc0000142` 仍是**未定位的 OPEN 项**（见 `docs/issue-python-0xc0000142.md`）。
  它与本目录无关：该错误发生在进程创建阶段，当时的现象与 `CODEX_HOME` 位置无关。
