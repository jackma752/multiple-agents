# 真实运行证据清单

## 原生严格模式：真实 CLI 与合成模型（2026-11-03）

用户要求“继续”实施。均为新建空配置、本机合成模型/零模型配置读取，不读真实登录文件，不调用在线模型。路径相对 `.worktrees/`；原始输出、数据库及诊断仅留在忽略目录。结论和失败边界见 [原生严格模式](native-strict.md)。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `native-strict/var/native-runtime-release.json` | 22035 | `4345d83a8f7bdad92a6f594a709135d6c7bea5a66a685ef181febcb67ec7a9ad` |
| `native-strict/var/native-worker-release.json` | 2289 | `abff69f3325f6c3da8eb7e45b21bd4ab22f33f9dfa6ef6472b786a59b5b26c6d` |
| `native-strict/var/codex-native-disabled.json` | 312 | `86e531271e90bda906344b3488063ce9fe69de4535cb1a1ea78d7e347b9d81fc` |
| `native-strict/var/codex-disabled-turn.json` | 1100 | `7a4444e439d83e2bef47a7cc82c47b95d6de9f7c623e3630ee028147cee12cd7` |
| `native-strict/var/codex-pretool-spawn-failed.json` | 1130 | `f4cc32f1fcde6e20475e1e902d0033ff0396c51b52dd1722966f19c57c52b5e9` |
| `native-strict/var/package-082-check.json` | 655 | `86a0a1f892e421a9b8c11b6745fae40bc0efe5f39a39b80c06d77a6b9ff97ad4` |
| `native-strict/var/runs/result-empty-66c53869-f3f5-46f2-9fa6-6c670d25b10b.json` | 549 | `47e7bf475eee3d6b797e4dca1357d1175ef4fc6347b016a755497d3270e4b056` |

## 严格模式零模型核验与传输基准（2026-11-03）

结果见 [0.8.1 记录](strict-transmission-0.8.1.md)。程序身份核验只读采用文件，不启动 CLI/模型或读登录文件；真实元数据查询无模型；本机基准只写专用测试消息。下列文件均只含状态/数字，原件保留 var。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `strict-transmission/var/transmission-benchmark.json` | 491 | `120a5f7c479a6390ce45e4fe05a927644ebf14bd12b558a36ac74430eec5f7c1` |
| `strict-transmission/var/live-compression.json` | 187 | `3e85d28c3d13029aabe65b01d5c6eee266696b2c7fd136423e78850cc065c1bb` |
| `strict-transmission/var/adopted-mode-check/result.json` | 2160 | `72f068e1dca1b8102ba70c571dcfa263c340fe00536d558fc41a0c0e1daaf623` |

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

## 0.8.2 真实在线严格模式与科研定点复测（2026-11-03）

用户“在线模型测试做一下”并回复“继续”。结果见 [在线实录](online-strict-0.8.2.md)。Claude 两次顶层 CLI 调用（含一个原生子任务）与文件审批通过；DeepSeek 20/20 筛选通过，24/28 引文匹配，科研仍未通过。本轮 DeepSeek 17,505 Token；CLI 返回费用估算 $0.0710005，不以 CLI 统计声称原生子任务独立用量已验证。路径相对于 `.worktrees/`；仅哈希本轮已知测试证据文件，不读取/哈希凭据或用户配置目录。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `online-strict-082/var/online-082/environment.json` | 847 | `afa9d9b6c4bfa7247aa97d9853a5fdb2ab3020b4f4ae7dc94886c69782fb90c4` |
| `online-strict-082/var/online-082/claude-original-driver-verdict.json` | 1466 | `8400d0a387952edf5f4f1a364ceed619e9f4b044c4adcbe3faac918b649b53fc` |
| `online-strict-082/var/online-082/claude-files-started.json` | 468 | `d48bbbe8dd422e5934d89f3bce7214052298b80b5defd229f6077b8ef51662b4` |
| `online-strict-082/var/online-082/claude-child-started.json` | 468 | `fdf9267cb8cee3273af10a5ef84ef172d880a2bd3443ab8abfef7b43da9ba3e2` |
| `online-strict-082/var/online-082/claude-result.json` | 2796 | `1cba343889cb3b1f04d431225d9f3c756e7663750e0116496820c339bb52f885` |
| `online-strict-082/var/online-082/claude-hub.db` | 684032 | `227ef5a721b8adc4688d4271732cab5748714c0bd7307b6485cc126d51d2abdc` |
| `online-strict-082/var/online-082/claude-executions.db` | 20480 | `f0867ddd74f807c5875574c4470b9b44d175be3bbf848700b4b37da362888caf` |
| `online-strict-082/var/online-082/paper-screen-started.json` | 544 | `1155fd65ff808154fa606a832294843f2377d8f53023c3b1b1a88d2cabde97b0` |
| `online-strict-082/var/online-082/paper-study-started.json` | 582 | `0e2c837a470ba127d3de2f6e9c994af04247c7e4858aec0154e8dedda6e6e41b` |
| `online-strict-082/var/online-082/paper-result.json` | 1855 | `e29396d0314a045fa0e94e20097949b0747a54418a8b1cf3b8d943a015bf8dfd` |
| `online-strict-082/var/online-082/paper-hub.db` | 753664 | `cdbf50a86c265b35196ce6616e02d41615b453ec5d1434e6ae4c0877b5c9994b` |
| `online-strict-082/var/online-082/paper-executions.db` | 57344 | `eedf3130b96fdd0b1fa08c539bdb37b3b15c9221bea288f40e0954f6b7f04bf7` |
| `online-strict-082/var/online-082/quote-audit.json` | 1188 | `710aae88ead76100c75dc68f6ed5db0e4b42e4363aa7fff878d4fba17a1f5b2d` |
| `online-strict-082/var/online-082/online-summary.json` | 850 | `c52eb91528ccb41ac30b0db3dd026463d9907248c4f308414125ba00aac80fb8` |
| `online-strict-082/var/online-082/fixtures/claude-child-child.txt` | 24 | `cf995dd902b9923175a8a94712f4a22ed8b51b88e99b510dcc91197142b9c49e` |
| `online-strict-082/var/online-082/fixtures/claude-child-fixture.txt` | 27 | `9940abbcf8d21832bacc1d25a872e8d604b1abc2b7dfeb93d5a05abf7f2f200e` |
| `online-strict-082/var/online-082/fixtures/claude-files-allowed.txt` | 26 | `8e9a5f36d683716ea2abb35a255b088c85997bb7c67f0a538b68dd9bd10dc176` |
| `online-strict-082/var/online-082/fixtures/claude-files-fixture.txt` | 27 | `9940abbcf8d21832bacc1d25a872e8d604b1abc2b7dfeb93d5a05abf7f2f200e` |

## 尚未登记

- 2026-10-14 经批准的真实 Codex 调用（[记录](p3-codex-real-run-2026-10-14.md)，标签 `evidence/p3-real-codex-run`，`f7b2291`）：证据很可能在主目录 `var/codex-verify` 或 `var/codex-runs`。这两个目录里混有其他运行，本轮没有逐一甄别，所以暂未登记。
- 主目录 `var/codex-home-dbg` 可能含凭据，按规则不计算哈希。
