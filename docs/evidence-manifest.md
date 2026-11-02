# 真实运行证据清单

## P6 0.8.0 真实在线验收（2026-11-02，部分通过）

结论与授权见 [在线验收](p6-online-080.md)。路径相对于 `.worktrees/`；不提交原始论文、模型输出、数据库或凭据。9 次真实调用，52,866 Token；多源筛选与核心引文校验未通过，未继续追加模型请求。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `p6-online-080/var/online-080/sources.json` | 1467 | `f5340855ffd1fbe549b96c6c91930c1f44a089bc56cc89df2a8b9953b72b382f` |
| `p6-online-080/var/online-080/pre-fix-task-view.json` | 4040 | `d68e3a670c2c00e9b7f449459f220a5f1100bc7f92b63fa9ea621834ecac26bd` |
| `p6-online-080/var/online-080/research-result.json` | 20110 | `9c8aabaa090e9908553d019b2ecdb56c68a25e416c2df488306fb851b9c1abb7` |
| `p6-online-080/var/online-080/small-study-result.json` | 784 | `914e6e08aa2a2ad3f392370b5b6fbea42efdba86d47c7eb4d8556703f771522f` |
| `p6-online-080/var/online-080/small-study-fallback-result.json` | 644 | `f46cd67e2524c5eb13f36b7a1a5b1c36c4feabc9484429a206c0e4245ad6148f` |
| `p6-online-080/var/online-080/quote-audit.json` | 1309 | `801cafe2b0caf0a6f7956b309cbf36686d1caa76e6271e92c55fc2cfbd63da53` |
| `p6-online-080/var/online-080/online-summary.json` | 3975 | `4f7314353b14c4fbacc5a2b6098c917534019caaa27ec09970103aa887d91770` |

## DeepSeek 用户批准的真实单次回复（2026-10-26）

`inv-630801980461` 由用户在页面批准执行；模型 `deepseek-flash`，provider 结束标记 `stop`，
输入34 / 输出79。只确认本次 HTTP 回复及用量；当时助手消息投影缺失，随后修复仅离线验。
下表文件由原适配器库只读提取数值、状态、回复字节数及哈希，不含密钥、提示词或回复原文。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `deepseek-http400/var/deepseek-real-success-metadata.json` | 650 | `ba0e553ed49e4f5f08e32cb5ccefedfbeadf89e14e292fa3338b0e7bd4ac4045` |

规则见 [Git 工作流](git-workflow.md) 第 4 节：证据文件不入库，只登记它们的路径、大小和 SHA-256。路径相对于 `E:\plug\agent-hub\.worktrees\`。2026-10-21 登记：只计算了哈希，没有读取文件内容。

## app-server S1：零模型握手（2026-10-17）

结论见 [S1 实录](appserver-s1-2026-10-17.md)，标签 `evidence/s1-real-pass`（`bdc6b40`）。

| 文件 | 字节 | 修改时间 | SHA-256 | 说明 |
|---|---|---|---|---|
| `appserver-s1-verification\var\appserver-s1\identity-review\help.txt` | 3412 | 2026-10-17 05:28 | `fe4b12d0f12108ba0f5ee54dba2f6e599216dff6dd7fd00a3c6eb591941d0d76` | 现装可执行文件身份核验 |
| `appserver-s1-verification\var\appserver-s1\identity-review\version.txt` | 227 | 2026-10-17 05:28 | `687958b15a91811199341feb07f6ad93b91a1d857e4e3c699e5bd98fbbded75f` | 同上 |
| `appserver-s1-verification\var\appserver-s1\s1-real-eq9q7hxt\result.json` | 187 | 2026-10-17 05:26 | `1e5b5f45a20c2b5b286b264fbc938d6bd8b0e37c54c148136ae34f25cd506931` | 第一次：预检失败，启动次数 0 |
| `appserver-s1-verification\var\appserver-s1\s1-real-euxv5bb_\result.json` | 1083 | 2026-10-17 05:29 | `3f3b468f587a77d824ee854a8845dca7ac7264a509bfade7fa9cb7a604ff540b` | 真实 S1 PASS |
| `appserver-s1-verification\var\appserver-s1\s1-real-euxv5bb_\appserver-stderr.log` | 216 | 2026-10-17 05:29 | `34480755111ff54e01142e919d02e6489815422fb2f4e025aff09525e96b966f` | 同上 |

## app-server S2：单个真实模型回合（2026-10-17）

结论见 [S2 实录](appserver-s2-2026-10-17.md)，标签 `evidence/s2-real-pass`（`023d0ab`）。

| 文件 | 字节 | 修改时间 | SHA-256 | 说明 |
|---|---|---|---|---|
| `appserver-s2-probe\var\appserver-s2\real-5j0vung8\result.json` | 2159 | 2026-10-17 22:14 | `e66e470ced37f013d5bdd31a7fa9908bdb2cc3a2c4376446500fb22e585ce7bd` | 真实 S2 PASS |
| `appserver-s2-probe\var\appserver-s2\real-5j0vung8\stderr.log` | 216 | 2026-10-17 22:14 | `8a8870e48580faa050df227bb12e778374d3c055c4cec47887bbb5a7f06540ea` | 同上 |

## P3 只读拒写探测（2026-10-14，结论 INCONCLUSIVE）

结论见 [探测记录](p3-probe-readonly-write-2026-10-14.md)，标签 `evidence/p3-readonly-write-inconclusive`（`bd0f47f`）。

| 文件 | 字节 | 修改时间 | SHA-256 |
|---|---|---|---|
| `p3-wrapup\var\probes\readonly-write-real\codex-executions.db` | 12288 | 2026-10-14 23:02 | `7fd7cb80b0f201f8aaea3c5327bca62358251b123fcd635f8d1c52b9ac230da7` |
| `p3-wrapup\var\probes\readonly-write-real\probe-readonly-write-real-1791988791.db` | 4096 | 2026-10-14 22:39 | `d78c40febe1ba7ec83660b78e3f6ab7bc45ab822b8f21b03b16b9cb4f3b3a259` |
| `p3-wrapup\var\probes\readonly-write-real\probe-readonly-write-real-1791988791.db-wal` | 251352 | 2026-10-14 22:40 | `a54c210833864b4be06f64745e01e9497ea30c1cfa9a1b232ad855c5ca57578d` |
| `p3-wrapup\var\probes\readonly-write-real\probe-readonly-write-real-1791990152.db` | 4096 | 2026-10-14 23:02 | `d78c40febe1ba7ec83660b78e3f6ab7bc45ab822b8f21b03b16b9cb4f3b3a259` |
| `p3-wrapup\var\probes\readonly-write-real\probe-readonly-write-real-1791990152.db-wal` | 251352 | 2026-10-14 23:02 | `e282a34cb114cb9348c5a86bb7057c1e31e21bdeb540e2282e06366d64141dce` |
| `p3-wrapup\var\probes\readonly-write-real\codex-runs\_scratch\codex-1791988794744.jsonl` | 391 | 2026-10-14 22:40 | `16289918e112d2dba09442433122568c29b53b53d44adb047ae35f6a12363ba8` |
| `p3-wrapup\var\probes\readonly-write-real\codex-runs\_scratch\codex-1791990155837.jsonl` | 510 | 2026-10-14 23:02 | `8e7aae8170dcc1fc89e06df20edc7b0cc0d75b6dda511e7e92a7ff823594f5eb` |

两个 `.db-shm` 文件是 SQLite 共享内存索引，最后修改时间为 2026-10-15 00:40，晚于探测本身，说明探测之后被再次打开过，因此不作为证据登记。

## 尚未登记

- 2026-10-14 经批准的真实 Codex 调用（[记录](p3-codex-real-run-2026-10-14.md)，标签 `evidence/p3-real-codex-run`，`f7b2291`）：证据很可能在主目录 `var/codex-verify` 或 `var/codex-runs`。这两个目录里混有其他运行，本轮没有逐一甄别，所以暂未登记。
- 主目录 `var/codex-home-dbg` 可能含凭据，按规则不计算哈希。
