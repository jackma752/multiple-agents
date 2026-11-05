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

## 0.8.3 引用与 Codex 钩子修复（2026-11-03）

用户要求“按照这份调研结果，拟定好方案后去执行修好bug”。结论见 [修复记录](citation-codex-0.8.3.md)。真实 DeepSeek 新调用一次，12,741 Token，38/38 引文通过；Codex 为真实固定 CLI + 本机合成模型，在线调用 0。正常拒绝钩子通过，损坏返回与超时放行的失败证据保留，严格入口仍阻止。首次大回归用量迟到项失败，修复后另存最终回归；不覆盖旧证据。下表路径相对于 `.worktrees/`；仅登记已知测试文件，不读取或哈希任何登录目录。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `citation-codex-083/var/citation-083/offline-regression.json` | 1645 | `2d93d9b2f790a065d8209a807012879296e419827b12941bd9180bd467e7f915` |
| `citation-codex-083/var/citation-083/online-result.json` | 957 | `bbe1cfd3b83bfadb63404fae675707623391f5f12681abbc8dcc40b4c181823c` |
| `citation-codex-083/var/citation-083/online-0-started.json` | 431 | `4f11fd289fe55828c792f69a32734f099f97fda665552b4bd548be2df0bc36d6` |
| `citation-codex-083/var/citation-083/validated-study.json` | 41726 | `4b207a3d1dd3fb97bc268cd9f65ef279501899c947827e3a4859d9a8d37739d7` |
| `citation-codex-083/var/citation-083/online-hub.db` | 741376 | `26cce4bf9f83bf2a3ca92a404870d4726cf12e267c7c9084f11fea16cd647f9c` |
| `citation-codex-083/var/citation-083/online-executions.db` | 53248 | `428950b082eeede1d4e864a9d8802eaafce9d950dea21f4f2828bdbca01cc95e` |
| `citation-codex-083/var/citation-083/regression.xml` | 200408 | `ba15be32b6f93e4b772f202d46cf3f295007c49ef5a98127ad5b1f67c0640deb` |
| `citation-codex-083/var/citation-083/regression-final.xml` | 199477 | `bd6940f99ba46324c7f38cf3b63b9ebf8de51375ffede7c98f58c5c9dfdba6e1` |
| `citation-codex-083/var/citation-083/final-citation-tests-v2.xml` | 6960 | `db40f4142111c56cebc60f0346362223f5120ba9192ce5c8fc3c7543347f6cbb` |
| `citation-codex-083/var/codex-hooks-083/trusted-deny.json` | 1602 | `d94002199f6b630ce5a98e69ceaf7bf7ee85934fba7225d3c9978089a09b3d12` |
| `citation-codex-083/var/codex-hooks-083/trusted-deny-v2.json` | 2843 | `17703995ba1b093238cf8f5f7927906f154fde6c37421f48d1793e0d282cbaf5` |
| `citation-codex-083/var/codex-hooks-083/invalid-json.json` | 2852 | `ec1e8066e03cea92c5d4b72d39cce926dafc7c74037aa1bd660b51eca6b0d518` |
| `citation-codex-083/var/codex-hooks-083/timeout.json` | 2847 | `f358768d07fe24dc7081d62e3b1a8122467b72e55b85dcc080e4e94154c8544c` |
| `citation-codex-083/var/codex-hooks-083/trusted-deny-v2/guard.py` | 403 | `8d9f3ca585e34f90d8e26feb0924add9adb60b9d5f02690a87ec77c6a2a0fd1c` |
| `citation-codex-083/var/codex-hooks-083/trusted-deny-v2/guard-events.jsonl` | 611 | `24aa3ddcb9bf17e2180c87b61134ba6b13554d76349bc6336ddaecb334393b25` |
| `citation-codex-083/var/codex-hooks-083/trusted-deny-v2/notifications.json` | 9542 | `69f1118f67e1ebe87471180afcf1ead71370972cafe61e8b62e00eb81b44e4b7` |
| `citation-codex-083/var/codex-hooks-083/invalid-json/guard.py` | 250 | `490e49c702af88bf803f5736d354cc444c3479f2f9f96977c5eea4264794d81b` |
| `citation-codex-083/var/codex-hooks-083/invalid-json/guard-events.jsonl` | 611 | `5bbb96ba06d2e9ecefede3383bab1c3b6a7a0b84ec36a92495738d2e36a19f22` |
| `citation-codex-083/var/codex-hooks-083/invalid-json/notifications.json` | 13983 | `dc68e21da55786e8efc0adc5eef3dbc20a744077aeae0746a8db6c245fd40a9c` |
| `citation-codex-083/var/codex-hooks-083/timeout/guard.py` | 259 | `8f76c9ca91a22cc2c1649c0e09358addcf059440572f0638e5d88cc104807c21` |
| `citation-codex-083/var/codex-hooks-083/timeout/guard-events.jsonl` | 611 | `d771ba940cf0c779b823de8141acbf6f1349732162d93f3724984ec5ddaa6c27` |
| `citation-codex-083/var/codex-hooks-083/timeout/notifications.json` | 14094 | `0bee5c00a74201ff52dd0519a6e53c39018710829cdc08622a035af686a26d24` |

### 0.8.3 本地候选及冻结程序检查

源码 `2774aeb`，仅本地构建，未安装或发布。最终冻结程序检查通过；两次检查脚本误判的报告与脚本保留。路径相对于 `.worktrees/`，未哈希任何 profile/账号目录。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `citation-codex-083/dist/p6-release-0.8.3/AgentHub-0.8.3-windows-x64-setup.exe` | 22908705 | `a1659bba6e415d65fec2c324560100498f98713af538293440ab14aa0e8ad005` |
| `citation-codex-083/dist/p6-release-0.8.3/AgentHub-0.8.3-windows-x64-portable.zip` | 22607378 | `9762d5e90cf786f6a916b9ccc24777d5b96f82cb25c8bc3489af072571cbd542` |
| `citation-codex-083/dist/p6-release-0.8.3/SHA256SUMS.txt` | 211 | `84031983ef07f2ffd56130ee32bad1a6d9a31355544e8057971ee6103b042a88` |
| `citation-codex-083/dist/p6-release-0.8.3/payload/AgentHub/MANIFEST.json` | 40300 | `5fc85d9178357ad3c56a9ddceb6ce55ad083ad5b80950cd7c55b6ebf829fe9bc` |
| `citation-codex-083/dist/p6-release-0.8.3/payload/AgentHub/PACKAGE_AUDIT.json` | 383 | `8645170b4a225880460fc8f6559eca1f9184eb4a28511fe6e9765f93e9ff39bd` |
| `citation-codex-083/var/citation-083/release-smoke-original-driver.json` | 507 | `e5dd5ed5bb4c276e9f7566698ea1d364144114dee2a6d4a85aff1cd1d843e638` |
| `citation-codex-083/var/citation-083/release-smoke-driver-assets.json` | 567 | `20dabb019178e908d5a40fd62d7868cf97fc7cb95405632464c7f9b7fa3626e0` |
| `citation-codex-083/var/citation-083/release-smoke.json` | 702 | `44b7ed2dbe0026ab85e3ed7b41b99624f99b0fcb8852288b344400c9d49c340c` |
| `citation-codex-083/var/release_smoke_083_v1.py` | 6641 | `ca36d55c0554225153e7f0ce514787999b984962607e7117c452cbefcb747969` |
| `citation-codex-083/var/release_smoke_083_v2.py` | 6758 | `0770f58eb1571822c18f2711656f22e3149839f545c55d23d2f7bb7a3d8b4374` |
| `citation-codex-083/var/release_smoke_083.py` | 6751 | `9c76e3b965ad8bf3762c51eaed340e88ed4b64cd8c44a58e4cab5eacc84682c0` |

## 0.8.3 在线项目执行与局部修复（2026-11-03）

用户明确授权通过后合并 main 并更新 GitHub，并追加 40,000 Token 作局部复核。结果见 [在线实录](online-release-0.8.3.md)：原项目 97,683 Token、33/33 引文，质量 needs_review，原 passed=false 保留；局部修订/复核另用 37,685 Token，46/46 引文、0 个实质未决问题，局部 passed=true。源缺口仍保留，不能称论文内容全部通过。路径相对于 `.worktrees/`；仅登记已知测试文件，不哈希任何 profile 或凭据目录。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `online-release-083/var/project-online-083/project-started.json` | 243 | `ee115bf7ef18f1d048f61194dcdda7ea50d06cbbf322d9ff903b930462e67f61` |
| `online-release-083/var/project-online-083/original-driver-result.json` | 189 | `8a8397beb6b98c6c8f102714def855c5dfeaebda7b48d43f1890233bb170ca55` |
| `online-release-083/var/project-online-083/original-driver-view.json` | 49007 | `fe42b3e985d55f24cb656aa89c8246563b8e2652f100a283a2ffddb8ea635285` |
| `online-release-083/var/project-online-083/original-driver.py` | 15224 | `a418113566feed085329bb0cd436db89eaa112a7355bd069e27c4b5cb41fc268` |
| `online-release-083/var/project-online-083/resume-1-started.json` | 107 | `740a16276ec86cdb30b5b084639804f562de4b9001075dfac1e7b3d740e0014f` |
| `online-release-083/var/project-online-083/resume-2-started.json` | 128 | `8f0c62d6b9c6b0d4cd9ffc75a5e6c2ea3b91e3acc4e4d7b7bb756e2e203402fe` |
| `online-release-083/var/project-online-083/before-fulltext-supplement.json` | 6275 | `ca0a897794161f14bbde66fa5e23e3e7bde0412563a56de112ff621d43d3a5e2` |
| `online-release-083/var/project-online-083/before-fulltext-supplement-view.json` | 57611 | `a07e15a81ec7ff709c31d1b3b76119ecf9e980e8f83d16dbc6861b22da64c25e` |
| `online-release-083/var/project-online-083/pdf-retry.json` | 199 | `13ec3f40ac5ea01a0cb7a97a9e3f1efaa141f90e1f3ee78164c6425407ab77f8` |
| `online-release-083/var/project-online-083/fresh-public-ragas.pdf` | 231984 | `6c24762a3f277fd120ed1adf34b01420ecf92f18ff6d1b5435fa0a09cb804ecb` |
| `online-release-083/var/project-online-083/preflight.json` | 4584 | `fabae028ab3e69890d5921ff39312736ef5ec017a70a07a8b2ffb6c1892e08f9` |
| `online-release-083/var/project-online-083/result.json` | 6533 | `0a5acfd985d5c1efe3673b4e9264d64a8f7d0fc4bbe8a1d56fa5f0eebb8f6529` |
| `online-release-083/var/project-online-083/task-view.json` | 154921 | `5771e29b97c2123f21b4073215b5b436e988cea7cbfbf1cfc17d6c514a46d243` |
| `online-release-083/var/project-online-083/hub.db` | 2129920 | `72483b688b5402decccdf37189968e564e08c6689b5fe278120ea16afe305aae` |
| `online-release-083/var/project-online-083/executions.db` | 167936 | `aea83ecd280fe1b7a3c446ce0e58f20ac8c89ddcba3e6e39fa72fa1560ee7102` |
| `online-release-083/var/project-online-083/downloads/research-bundle.zip` | 167657 | `bc689f1cea1b00fb8c06ef41930067eb28962aa2286cd2a7843293d8ba2b9c36` |
| `online-release-083/var/project-repair-083/call-0-started.json` | 226 | `8a73de37a5ac99a54a1d3ed6659f261cda2485622c26206a5e6e38634a7dcb4b` |
| `online-release-083/var/project-repair-083/call-1-started.json` | 227 | `adec9c0622144e8ffb18b701c8ff868c1828efbaf51fe94fedbca67806745ba0` |
| `online-release-083/var/project-repair-083/corrected-study.json` | 56487 | `acdedbd2b650bbcbc1cb4fbd601f3a1aeb49a2b43bc8c5272888df56c726277c` |
| `online-release-083/var/project-repair-083/review.json` | 20 | `2d15e095185b0e0de330b31cffab7e1011a72a489d3520d2522636b82f73ade7` |
| `online-release-083/var/project-repair-083/result.json` | 1130 | `bc59fa6917b048b580d77c235a59b4064a1f204656ac7b36831bdc65b734a042` |
| `online-release-083/var/project-repair-083/hub.db` | 884736 | `3d5cf288134ae5000a9c66841444acee14b4e930bede638b4bec9b4a6d776f3f` |
| `online-release-083/var/project-repair-083/executions.db` | 61440 | `22eff9aeb02fb779e108d25488424e1778e9e5f78260e6cdf659241d34dd7f4d` |
| `online-release-083/var/material-repair-regression.xml` | 10921 | `73c45000e6b0bff35ccfc2612295c50bafd62f33f297fcfedd76c0f107f4429f` |
| `online-release-083/var/material-repair-final.xml` | 38293 | `8e2b9ba1258e5f8222e75b5abd4a5b61d2baf60544527b29931555bea7e53959` |
| `online-release-083/var/final-guidance-payload.xml` | 5660 | `e504ae55ea7abda400da613c96bc40a528b1166c60e148a5fe927c61578a0e27` |
| `online-release-083/var/release-083-checks.xml` | 10828 | `f3e9db50fdc55d39f928ec573fc4caa43221da73fa3f4bcaf38658bc79757bf1` |
| `online-release-083/var/release-final-regression.xml` | 38444 | `55de4ca2c31d1630ef26f838bb050d89d5407ad3fda804437d848aea27bc1034` |
| `online-release-083/var/publication-source-audit.json` | 132 | `c5db106095b8b1d784eff6eb5388b74ef03839cb91b58b4a79bf16aa157b8caf` |

### 0.8.3 实际发布构建与冻结程序检查

构建源码 d5b937e，真实冻结程序新空目录检查通过。下表不含任何用户配置或登录文件。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `online-release-083/var/release-smoke.json` | 702 | `965ee90397af8edd95cb720ab1e08705cc289fb6bbae0e515aad7c24a8e501e8` |
| `online-release-083/var/release_smoke_083.py` | 6879 | `15f9320c5da135489a2a0ce506192a3a5725ca1e5efc94cb1b8999ea36858c23` |
| `online-release-083/var/publication-source-audit-final.json` | 132 | `c2be3c6a6e7b8c8391ba806740efa6197f2539876033f346823e95d8a646f2ef` |
| `online-release-083/dist/p6-release-0.8.3/AgentHub-0.8.3-windows-x64-setup.exe` | 22909141 | `32813bf44067387cecf209ecad2ce71872cbf683486d710e50fb12e0c5feba4a` |
| `online-release-083/dist/p6-release-0.8.3/AgentHub-0.8.3-windows-x64-portable.zip` | 22608854 | `768afcd0400cee644e6145dc3a3562690d4af6e6a8817d0549c24ef56af5ab89` |
| `online-release-083/dist/p6-release-0.8.3/SHA256SUMS.txt` | 211 | `c41549b0253f68d215a49b9fb0c926ccc627d6e3f8b427c94f39374cad513beb` |
| `online-release-083/dist/p6-release-0.8.3/payload/AgentHub/MANIFEST.json` | 40300 | `2f780ea5296d2a4a44aa27fd224266bec0e87e54dd28b0172f118c155d66fb9a` |
| `online-release-083/dist/p6-release-0.8.3/payload/AgentHub/PACKAGE_AUDIT.json` | 383 | `8645170b4a225880460fc8f6559eca1f9184eb4a28511fe6e9765f93e9ff39bd` |

### 0.8.4 Claude 更新后路径修复（2026-11-04）

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `claude-path-recovery/var/path-recovery-tests.xml` | 21140 | `caabcd49a4bdf6ecdb41af60136a609eeeff829b1a12e716bc543f6af0a42679` |
| `claude-path-recovery/var/path-recovery-final.xml` | 26591 | `c245039f9128f0ab6fbd99e958b80eddca8300d4cfa007dcd46ad2fcf80135ba` |

后续真实本机配置恢复、2.1.317 严格拦截与停止回归；原失败保留。各测试范围重叠，不累加。仅哈希登记，不上传原始记录或账号配置。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `claude-path-recovery/var/queued-stop-final.xml` | 31083 | `fed815691dcdf5583b7fe738283e7f31998367cfdd9cc3eaf15811fc15dc09f0` |
| `claude-path-recovery/var/paper-budget-isolated.xml` | 932 | `cf2ce21ef7271544ddd6d5fc142c5d061c7929234665db4d54fd46463bd8e6e8` |
| `claude-path-recovery/var/queued-stop-packaged-deps.xml` | 65432 | `18f267114dd2a4d62aac97eba8ce0bbb6921695ae4c529eb783d6981ebd71b6b` |
| `claude-path-recovery/var/claude-2317-final.xml` | 26642 | `b701734eab6ad8fb4b30a25245184750998925f84d10eda14f5830f6c61a75c1` |
| `claude-path-recovery/var/local-adoption.json` | 438 | `d10b0b4868e370ceae8e06c2389705a7cc6867c562e49c3dcaa9f68524a1d8b2` |
| `claude-path-recovery/var/claude-2317-loopback.json` | 22066 | `29465c6fd1a88f5dcfcc955673f76dde6c6c63eea35bc250ac74ff6a5d86c9ba` |
| `claude-path-recovery/var/claude-2317-worker-allow.json` | 2316 | `7fb75206936cf33e32e897bad432142f548b8b84cb96fffab4be7cd0340d1a73` |
| `claude-path-recovery/var/claude-2317-worker-deny.json` | 2153 | `3024166acf9983df2820e74c67edd797a3fedbbaebd663951c531645544223b0` |
| `online-release-083/var/github-publish.json` | 227 | `9b87c8da5daa272fdc266d6529f9c8383949a81d2a9f9e31708c5aac67893d4d` |
| `online-release-083/var/github-verify.json` | 227 | `9b87c8da5daa272fdc266d6529f9c8383949a81d2a9f9e31708c5aac67893d4d` |
| `online-release-083/var/github-finish-assets.jsonl` | 422 | `8d6b1709cbeca055d0ceef4b7d387ae59ec1db7903dc07c3a98e7fa652e63760` |

### 0.8.4 最终发行构建与本机升级

源码 `0a35deb`，与早期 `4eca7a3` 本地候选区分；本机程序升级的报告不含配置正文或凭证。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `claude-path-recovery/var/release-smoke.json` | 702 | `163ac2d6db7658004d4c553c20a98f73e7dec2b7482e5a3b7cda6ade47311638` |
| `claude-path-recovery/var/local-upgrade-084.json` | 389 | `cab6b5b2e8691ccd40c4e7e0dab6341c1e621963bbcaf4d4b60343fa77f3e5df` |
| `claude-path-recovery/dist/p6-release-0.8.4/AgentHub-0.8.4-windows-x64-setup.exe` | 22912106 | `a5357d9264ba00aaf3b01dc77110d8448acf3b47837ec77dff29b96ca0f5d38c` |
| `claude-path-recovery/dist/p6-release-0.8.4/AgentHub-0.8.4-windows-x64-portable.zip` | 22610726 | `3e74cefb2fc864951a1146260002100cb2f06cc299623e70e83f914f6ae2a710` |
| `claude-path-recovery/dist/p6-release-0.8.4/SHA256SUMS.txt` | 211 | `b81dff99fc1399006958144516764f16b4b1d9e803d91f58ebffc653202af9cb` |
| `claude-path-recovery/dist/p6-release-0.8.4/payload/AgentHub/MANIFEST.json` | 40300 | `9fabe77811c6602d3198e03e5a095414933e022a524216e9f62f2a8265e0b9a3` |
| `claude-path-recovery/var/release_smoke_084_final.py` | 6878 | `30cfe0c78332884252ae42fbe31d5c00523217cf8ab202a0307e2ba3ddc2339a` |
| `claude-path-recovery/var/build-0.8.4.log` | 11713 | `7bdc2809351da718571a143de57ea9f6a442708f5155c91b4ef3b6dadf0ec05d` |
| `claude-path-recovery/var/github-finish-assets.jsonl` | 492 | `7fd779b07e69616b88cf863704a696385112889c727217a32da1ea50cb163264` |
| `claude-path-recovery/var/github-publish.json` | 227 | `817e8b85e212c750f1dfd376267bff3f4fc279bf2228bd182bfb2f2b72f0bece` |
| `claude-path-recovery/var/github-verify.json` | 227 | `817e8b85e212c750f1dfd376267bff3f4fc279bf2228bd182bfb2f2b72f0bece` |

### 0.8.5 原生子任务前置检查（2026-11-04）

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `native-preflight-085/var/native-preflight-first.xml` | 9893 | `459965068ab84c244c6186afa97f1238221129a7e69167db42aa289c0d540f19` |
| `native-preflight-085/var/native-preflight-final.xml` | 22501 | `47e42293b1612c1c016ee24c6862be3e1a022328dcc4d5bfbd4aefa937ebf7ab` |
| `native-preflight-085/var/native-preflight-last.xml` | 7697 | `f1a0db3144a3a2df6d63eaa5edc65d1a744c1393f0d1717ee61cd03ab939bd4e` |
| `native-preflight-085/var/composer-check.json` | 66 | `e00bf96269e20686e4550574e713617eb9fd301905fc187a822cb2494bcc0c5b` |

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `native-preflight-085/var/release-smoke.json` | 739 | `1dc18c6f867c177c97848a84059d2459e8773836b5f4d30aaf2c1a8d09c3fb62` |
| `native-preflight-085/var/release_smoke_085.py` | 7698 | `d651235e78108409a75249ff3b1081a88ed3d1f28a91cb52751ff07ec7ddd345` |
| `native-preflight-085/var/build-0.8.5.log` | 11598 | `bc8c57b7c1113612960b3038e6961fc93301a4af553186182df31ef60ef811e9` |
| `native-preflight-085/dist/p6-release-0.8.5/AgentHub-0.8.5-windows-x64-setup.exe` | 22911309 | `2ff944bec5c5c81f8c0c376a57b7e3a85a519670415dc1ca04eca75fa9340321` |
| `native-preflight-085/dist/p6-release-0.8.5/AgentHub-0.8.5-windows-x64-portable.zip` | 22610165 | `3e6ac97ad82f4bb221e3af786263ccc733aa3d64921ec8719bce9fd9e2461338` |
| `native-preflight-085/dist/p6-release-0.8.5/SHA256SUMS.txt` | 211 | `f5e55a080ae699763ce56f96633692798453e13f38b241dbd4563fd61f37e47f` |
| `native-preflight-085/dist/p6-release-0.8.5/payload/AgentHub/MANIFEST.json` | 40300 | `255f33d5e9a2fce0f40ff0be8650679ea18309ee75163205f1611b113552fa7b` |
| `native-preflight-085/var/github-finish-assets.jsonl` | 492 | `36e235f8fec54178bc1642cfbf99190ae1f09303f2cf631991dde252463c36d9` |
| `native-preflight-085/var/github-publish.json` | 227 | `01e417c1b029c2677bd6bfd2cc10d2452649dc770f47c277d5a0e7e7f34c4f0f` |
| `native-preflight-085/var/github-verify.json` | 227 | `01e417c1b029c2677bd6bfd2cc10d2452649dc770f47c277d5a0e7e7f34c4f0f` |

### 0.8.5 本机安装完成（2026-11-05）

用户明确确认后执行；指定阶段核对时已暂停，升级后保持暂停。仅登记脱敏安装报告与维护脚本，不读取或计算登录目录、账号文件及配置正文的哈希。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `native-preflight-085/var/local-upgrade-085.json` | 522 | `93f5d037a1f311a4f112999de3c20992a03e9e03a42af378c367b96450f95f99` |
| `native-preflight-085/var/upgrade_local_085.py` | 8526 | `6ec87b7bb995d7203cfa36c6a86866de1742050c83a2ca638ab394caf9e1e013` |

### 空正文导出诊断与独立草稿恢复（2026-11-05）

真实用户数据仅在本地只读检查，不复制账号、凭据或数据库。原失败状态、审批、用量与导出不改；恢复内容是独立、未审阅且未完成的草稿。原文、提示词和模型正文不入库，本轮真实模型调用 0。下列诊断、恢复报告和离线测试记录仅登记长度及哈希。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `paper-empty-export/var/recovery-result.json` | 1123 | `1d924639edc4a6a27e596033aef77424679fd045596c8984ab76babf9efe9492` |
| `paper-empty-export/var/recover_saved_study.py` | 3808 | `9af4d7121a2be316fe5c7c09b26e1155f2fd5a7309a44c55187c3f51935006a9` |
| `paper-empty-export/var/failed-reply-diagnostics.json` | 302 | `bb4618ccdf811083ce01008e009955c72585f53c5a9d9a36bbcddac4c93c4cf0` |
| `paper-empty-export/var/export-guidance-tests.xml` | 6854 | `00d4654978ece28639713263b4125ed27e04cbe178cd7e5349fbbecb533eff84` |
| `paper-empty-export/var/remaining-papers-tests.xml` | 28239 | `8d02e728785d093f6942cb4a61107832d87b25c4a1b859ed016c4cd87527dd7c` |
| `paper-empty-export/var/guidance-final-tests.xml` | 3027 | `9b3c3647145c6180511a9fe87e30ff400b60bd8073249e9bab6f7b2c017c119a` |
| `paper-empty-export/var/check_result_ui.js` | 1865 | `f3f9d5d6b82e5d4c12f3245d6a8e855273a6954c8c0ab3b2741a1d3bb9d07ef2` |
| `paper-empty-export/var/result-ui-check.json` | 82 | `47431339ec0ec920bd93f7dd912c8f8f5b31cb17b241483a5cd734166327ff94` |


### 0.8.6 单次美元上限默认关闭与本机更新（2026-11-05）

真实模型调用 0；本机安装只备份旧程序清单内文件，配置、旧排队记录及已完成阶段保留。首个系统 Python 候选因依赖偏离未采用；隔离环境最终包单独核验，原文/登录材料/配置正文均不入库。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `claude-usd-default-off/var/usd-cap-regression.xml` | 30275 | `273ad13f3c3300e142897261b451a5ce6aba341f07ee88797d3d94dae0b98686` |
| `claude-usd-default-off/var/build-0.8.6.log` | 12369 | `2e9f68e7f0d3fb557d844b5884044e13dc880bacfd452dba5b53c3214fe32edb` |
| `claude-usd-default-off/var/build-0.8.6-isolated.log` | 11630 | `088b19bfe7422f39c3c3c7b82eaa888a4a6000caed3a1bf69ee07c8ea1b602aa` |
| `claude-usd-default-off/var/release_smoke_086.py` | 8458 | `011cadb44e2e6d01bbf39c050f0f17319c7c36d3e6d642ae125768b2bede2784` |
| `claude-usd-default-off/var/release-smoke.json` | 814 | `f08085bbe36043faf764e99edb6cd90c42455c778c86d7adddc47894b6dc0e28` |
| `claude-usd-default-off/var/upgrade_local_086.py` | 9105 | `535de57fd0c15ff6a7ebe5e94be2cf2354d22c6ac59ac9d28dfd25e297405e71` |
| `claude-usd-default-off/var/local-upgrade-086.json` | 633 | `f1435e92753c55d6e5bce78a3704abbe79ceac20a0277ce8b667565b8f375eef` |
| `claude-usd-default-off/dist/p6-release-0.8.6/AgentHub-0.8.6-windows-x64-setup.exe` | 22914671 | `24df1f908b9690be9714c8b8e7fc956c3464e7530c35500ace96fb3199cb99af` |
| `claude-usd-default-off/dist/p6-release-0.8.6/AgentHub-0.8.6-windows-x64-portable.zip` | 22614733 | `79c432cbec159572a9d7e2494aba6802a37e0a720a21f3512818d5729a9666f1` |
| `claude-usd-default-off/dist/p6-release-0.8.6/SHA256SUMS.txt` | 211 | `29a50f5c24981aafe325266a138f5f998e14a8134f118467c09c10686a4a2d2e` |
| `claude-usd-default-off/dist/p6-release-0.8.6/payload/AgentHub/MANIFEST.json` | 40300 | `0afb61bf51a83ff5789a1e973ea26eda69fd29b2685d6864400a4e0d199704cf` |
| `claude-usd-default-off/var/github-verify-086.json` | 783 | `91620b7a62bdf0e3a54f5d3bf25afa7dad2093022d2e38e78edeca14734b4035` |


## 2026-11-05：0.8.7 失败生成断点接续

来源 efbfb609bb1604a3ceaa494aa115e82244562ffe；真实模型调用 0，未启动研究接续。仅登记本地证据哈希，不上传用户研究或数据库。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `paper-failed-checkpoint/var/checkpoint-final-tests.xml` | 43469 | `40400e5e1b16999fa4c739afbfd7fb110b116ad843dd0e017cc875d67e258376` |
| `paper-failed-checkpoint/var/checkpoint-ui-result.json` | 56 | `a2919b07d91f010c0a43cc24a2c4a20df37f7422127471bdaf21b177aa189850` |
| `paper-failed-checkpoint/var/actual-checkpoint-preflight.json` | 362 | `9575981c0542c06d9ec7fb37ea81406f9dcd2f693b3decb45a0b03fde0a8fec4` |
| `paper-failed-checkpoint/var/continuation-proof-preflight.json` | 112 | `df569fae2edf07a36515d512eb6564f0220cc3468966987afd78dfffd94400a2` |
| `paper-failed-checkpoint/var/release-smoke.json` | 892 | `def3ee6b1f3d8df380b982c0fdd93d7df6bb6acfc64f4c7482fcb5df922b9622` |
| `paper-failed-checkpoint/var/local-upgrade-087.json` | 752 | `66b409e285d98a0be06941ba6fdfbd53d310b5570f4a2e9954c47d3f9d3544aa` |
| `paper-failed-checkpoint/dist/p6-release-0.8.7/AgentHub-0.8.7-windows-x64-setup.exe` | 22919921 | `1fddc5da4b30243a967b0bb5d715eac035f84584151bca18a7b6e867b2f57881` |
| `paper-failed-checkpoint/dist/p6-release-0.8.7/AgentHub-0.8.7-windows-x64-portable.zip` | 22619434 | `70c81358cd450573162a56fe0d68e21381ba7b5b22a727cef2873bb1c4fd6d35` |
| `paper-failed-checkpoint/dist/p6-release-0.8.7/SHA256SUMS.txt` | 211 | `f48a4dff8b7f4ce1c5d316e6095892256d2ecac224c489f60cadfbaef31759a7` |
| `paper-failed-checkpoint/dist/p6-release-0.8.7/payload/AgentHub/MANIFEST.json` | 40300 | `3d3394f7759129c287d34523fd8b07cd7627a813d8dd402fe0b5ddc69df04241` |
| `paper-failed-checkpoint/var/github-verify-087.json` | 807 | `5db2e09470ddabfc9f31ac2891879a68225c644d74556e320a563bcda2b38523` |


## 2026-11-05：0.8.8 修订耗量保护

来源 08a376dbe668ccaaa886a23700f0457a70986d53；真实模型调用 0，用户阶段保持暂停，研究/聊天不入库。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `paper-repair-cost/var/repair-cost-final.xml` | 93186 | `1fa39e50db4b43bfbeb7a99ed79327fbff0df8fe4961569b95eff12535817d91` |
| `paper-repair-cost/var/repair-usage-contracts.xml` | 9307 | `e19b4eb763dd4b7288e2cafba2e7a61eeeb4f36247850b7e57aed330d27a5245` |
| `paper-repair-cost/var/repair-material-final.xml` | 6655 | `8bd995e4dab604079b9f4aaff2c7fb5a1a6bff3a163500b1548a175f12f353a4` |
| `paper-repair-cost/var/checkpoint-ui-result.json` | 56 | `fd713757d44535b6672fdee4f06d0692ddc290817477190108b7eeab3ae99bbb` |
| `paper-repair-cost/var/actual-cost-replay.json` | 789 | `ca32daa9b1d14c94c13a6bda58b60849ecfbaa99783d02b9c0353d413ffacfb5` |
| `paper-repair-cost/var/release-smoke.json` | 979 | `ed2c7f26cd5b642ddb8848402055e7a3054d2afe9ddc2b2b89cc36bd14c569f9` |
| `paper-repair-cost/var/local-upgrade-088.json` | 858 | `c1d1d5c850bb7b4d00b63ad4b5c7542c4bf0d5621bc09e7f719c4a87c6d6ba48` |
| `paper-repair-cost/dist/p6-release-0.8.8/AgentHub-0.8.8-windows-x64-setup.exe` | 22926446 | `81f07c8e4914c9b9932a0b4a749e1348ef799c79b2e1c3a754efacd1795eaac6` |
| `paper-repair-cost/dist/p6-release-0.8.8/AgentHub-0.8.8-windows-x64-portable.zip` | 22626617 | `61baec3d86bd7d4a4efc7cfa7c963d2d5f845c2db33431777ee90969ccfd8071` |
| `paper-repair-cost/dist/p6-release-0.8.8/SHA256SUMS.txt` | 211 | `33acfbab9dd0910cb07694a45664aa5c75c53954efa39a68d7fa1373a4bffc09` |
| `paper-repair-cost/dist/p6-release-0.8.8/payload/AgentHub/MANIFEST.json` | 40300 | `89eaf37e3dc3114a612953113b3d095b6f7736505dcef379b8a41ca68af4bea2` |
| `paper-repair-cost/var/github-verify-088.json` | 807 | `64d8080e427efecff660132f66eea2f2ce76efdd8f8180cbecf7e68322ccb461` |

## 2026-11-05：半途研究离线恢复

来源 0fb6445；本轮新增 Hub 模型调用 0，原阶段仍 blocked，不改变审批、预算、研究或账本。不发布用户研究正文、实际回复、PDF 或私有恢复脚本，仅登记证据哈希。恢复文件独立于原调度器，数学复审与恢复入口未完成。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `paper-recover-paid/var/reference-recovery-preflight.json` | 939 | `5940f84d84013fbc5f2308fd6ef4a727b34619f355f9df96eb4224484548fb74` |
| `paper-recover-paid/var/export-result.json` | 554 | `3f1e9303201132a7b905cdd2bc0cf0a3e1e7caeefe75ab2e6050a8b49c9732c9` |
| `paper-recover-paid/var/export-verification.json` | 276 | `521d7c553fc00c292078b4d8bbf68f5f03151738b8184c6367dc00a56b247a3d` |
| `paper-recover-paid/var/math-render-result.json` | 70 | `78d0cf56bf9c3cc2a76625bbfd4c6f1597a44986a132983bb7c46cba201b8691` |
| `paper-recover-paid/var/recovery-final-tests.xml` | 7039 | `148a8d83590baa3e526e3e1e16526f913140f1569e9e44e23e5a8ab88de7d37a` |
| `paper-recover-paid/var/export_recovery.py` | 21358 | `0ff738321b8379c0c7c7680616febb41657d8998808b92ed22e3679148027111` |
| `paper-recover-paid/var/verify_export.py` | 3165 | `052e2bb23f2270915cc307c434497aaa68ec0e499ef55db54d47337136b48dc3` |
| `paper-recover-paid/var/verify_math.cjs` | 756 | `26a556eacf54cdef3b044f8949e9f3559d931842ca5f33faedc32911e893a14b` |
| `paper-recover-paid/var/final-state.json` | 128 | `adf60af647a9b0dd0b65eb29b090390df9138c3c43b90d26e254fbbe569f21c1` |

## 2026-11-05：0.8.9 项目内恢复与本机回接

来源 fc72bcab55685ec25f2fae33a55ef4379502c7af。本机原任务已通过安装版 HTTP 接回 19 单元/81 条精确来源；数学未复审，阶段仍 blocked，原调用及账本不变，本轮 Hub 模型调用 0。研究正文、模型回复、私有 DB/PDF/恢复脚本内容不入库，仅登记哈希。

| 工作树内路径 | 字节 | SHA-256 |
|---|---:|---|
| `paper-recover-paid/var/runtime-final-tests.xml` | 56725 | `0e89c1f1d94ccc8960362428e05d29a015facefc39e0f40e3c5b717f1da1f788` |
| `paper-recover-paid/var/runtime-affected-final.xml` | 8852 | `6a1f7596f60971e8b23ce0215c17525cf05b11191a895a65abc70d9a6c637747` |
| `paper-recover-paid/var/recovery-package-tests.xml` | 6022 | `959b4a04b04449adb1444760f613889d587f8d6df06452070e0a485ec8caf893` |
| `paper-recover-paid/var/recovery-ui-result.json` | 276 | `98639f74d134d347a1c20db24f586acd803c5875e80e5fa46af3c6cc5bbf5506` |
| `paper-recover-paid/var/checkpoint-ui-result.json` | 56 | `fd713757d44535b6672fdee4f06d0692ddc290817477190108b7eeab3ae99bbb` |
| `paper-recover-paid/var/runtime-preflight.json` | 922 | `49e2d7cde13e0f8a7ecece2d2409156d619da8e460790281faed4da10fa5c6f5` |
| `paper-recover-paid/var/runtime-db-replay.json` | 367 | `7385ef50cc77381d195b8ec4248dc8cdc551e04f47766f6f605ecadbf9e9aadf` |
| `paper-recover-paid/var/build-0.8.9.log` | 11567 | `1354d1e513f9415abe5678f5469c116fb179f79f514290d1773f2fef3f6d9614` |
| `paper-recover-paid/var/release-smoke.json` | 1072 | `7255daea0832102a3f9dfa0cd09dfb389c824fbdd54163dc8c55dad8056d73ba` |
| `paper-recover-paid/var/frozen-recovery-replay.json` | 347 | `13a501ccbe65c4682129c493ec99ddbd52903ec388558a3a41895e18610e96f7` |
| `paper-recover-paid/var/local-upgrade-089.json` | 860 | `63a513d6325af9b2f3075d5306b79a8d991f3877599b856cd9f28ba6210f13e6` |
| `paper-recover-paid/var/local-recovery-089.json` | 502 | `a2484ef4397622b860d5d5f0770d1329f1d545d1ddc58fb53f1f5e9d9ef3bf2d` |
| `paper-recover-paid/var/release_smoke_089.py` | 9699 | `b828725d30cb344247b35ac90b9c53040d03614ba0c29a96805bd3da90e579f1` |
| `paper-recover-paid/var/frozen_recovery_replay.py` | 4438 | `d00112a6ddf4f172ee9eeeb119b2fa2bea8c3fa463cabb3dab8386c0c914d175` |
| `paper-recover-paid/var/upgrade_local_089.py` | 10616 | `cd42fad8f5d19c6f04b0037a6184ea6712b0a08456eaeb82df11900e6a8adec4` |
| `paper-recover-paid/var/restore_local_089.py` | 3262 | `44c96a61f65c3351c05d231cf66b5122da5e226aaf3a4412a71a2bff86f62704` |
| `paper-recover-paid/dist/p6-release-0.8.9/AgentHub-0.8.9-windows-x64-setup.exe` | 22955591 | `139770e3491ea1f928b52d1fcd6e5b860f04209a5e676460b1c7597a5f90a502` |
| `paper-recover-paid/dist/p6-release-0.8.9/AgentHub-0.8.9-windows-x64-portable.zip` | 22655265 | `1b92e71743e9f99dd1baaec90a89e54d4b13969c5cf0c73550b73c457f87933f` |
| `paper-recover-paid/dist/p6-release-0.8.9/SHA256SUMS.txt` | 211 | `5c73df0c624ff1f126637c3830a73fb6a21caafe8e2b3965d28516e433c2fa0c` |
| `paper-recover-paid/dist/p6-release-0.8.9/payload/AgentHub/MANIFEST.json` | 40300 | `72c0bd573a7e68d8ac078e0cf439a280960abb5988c6fe6cc80c3a7baea5a11d` |
