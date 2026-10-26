# P5 设计：Agent 接入档案（discover / adopt / preflight / profile）

**状态：设计（未实现）。** 本文只描述方案，不改任何现有安全语义（审批、配额、执行准入一律不动）。

最后更新：2026-10-26。

---

## 1. 问题

现在每台机器上"哪个二进制算这个 Agent"是**写死在源码里的常量**：

| 位置 | 内容 |
|---|---|
| `apps/worker/agenthub_kernel/config.py:97-103` | `codex exec` 的 path / sha256 / size / version（`0.162.0-alpha.17.2`） |
| `packages/adapters/agenthub_adapters/appserver_adapter.py:114-119` | `PINNED_APPSERVER`（`0.163.0-alpha.11.2`，`%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-c81ed464e5e7cafd\codex.exe`） |
| `packages/adapters/agenthub_adapters/claude_code.py:83-90` | `PINNED_CLAUDE_CODE`（`2.1.308`，MSIX 包缓存路径） |

后果（都已在实机出现）：

1. Codex 自动更新删掉了 `...\OpenAI\Codex\bin\2e5e00daee91c61d\` → 真实调用被 `refused: executable missing` 挡住；
2. Claude Code 的路径只在这台机器的 MSIX 缓存里存在，**且没有任何 path/hash 覆盖入口** → 换台机器/别人安装不同版本，必然 `identity_not_pinned_constant` 或 `executable_missing`，且无法自我修复；
3. Codex 虽然能用 `AGENT_HUB_CODEX_PATH/SHA256/SIZE/VERSION`（`config.py:69-73`）覆盖，但**覆盖动作没有任何记录**——事后无法回答"这组哈希是谁批准的"。

也就是说：**当前形态可以在这一台机器上工作，但不可分发。**

## 2. 目标与非目标

**目标**

- 换一台机器，能在**不改源码**的前提下接入本机已安装的 Agent，并把"接受哪些字节"变成一次显式、可审计的动作。
- 任一通道不可用时，能给出**逐条**原因与修复建议，而不是一句 `blocked`。
- 严格模式仍然只接受"被 adopt 过的身份"，不因可移植性放松。

**非目标**

- 不下载、不安装、不升级任何 Agent 二进制。
- 不改 ACL、不绕过完整性级别/安全策略（见 `docs/issue-python-low-integrity.md`）。
- 不自动 adopt：没有显式命令 + 人工确认，永远不写入档案。
- 不放宽审批、配额、执行准入的任何判定。

## 3. 四个动作

```
discover  扫描本机，列出候选（路径/版本/sha256/大小/来源），只读
adopt     用户显式确认一个候选，写入本机档案（含 hash），记审计事件
preflight 逐项判定：身份/参数/目录/代理/CA/权限/可写性，输出可执行建议
profile   每台机器一份 JSON，承载 adopt 结果；仓库内路径一律拒绝
```

### 3.1 discover（只读）

输入：通道名（`codex` / `claude-code` / `all`）。
行为：

- 依次尝试：档案里已 adopt 的路径 → 环境变量指定 → 源码常量默认值 → 常见安装位置（`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`、`%LOCALAPPDATA%\AgentHub\runtimes\codex\*\codex.exe`、MSIX 包缓存、`where.exe codex` / `where.exe claude`）；
- 对每个存在的文件计算 `sha256` + `size` + 读 `--version`（超时 5 秒，失败只记"版本未知"）；
- 输出表：`status ∈ {pinned(命中源码常量), adopted(命中档案), candidate(发现但未接受), missing(找不到)}`。

**Codex 有两套评审常量**，`discover` 必须按"实际会用的传输"去比：

| transport | 常量 | 何时生效 |
|---|---|---|
| `exec` | `config.PINNED_CODEX_*`（0.162.0-alpha.17.2） | 单 `--adapter codex` 且未要求 app-server |
| `app-server` | `appserver_adapter.PINNED_APPSERVER`（0.163.0-alpha.11.2） | `--adapter multi`，或显式 `--codex-transport app-server` |

`--adapter multi` 会**无条件**走 app-server（`apps/worker/agenthub_kernel/adapters.py:58-64`），
所以同一个 0.163 运行时在 `multi` 下是 `pinned`、在单 `codex`+`exec` 下只是 `candidate`。
报告里必须同时打印实际 transport，否则"身份不匹配"会被误读成二进制有问题。

**不做**：不读取任何凭据文件内容；`discover` 的结果里不允许出现 token/API key。

### 3.2 adopt（显式、可审计）

```
python tools/doctor.py --adopt codex --path "C:\...\codex.exe" --i-understand-this-trusts-this-binary
```

行为：

1. 计算 hash/size/version；
2. 打印一行 `将信任：codex / <path> / <sha256> / <size> / <version>`；
3. 需要 `--i-understand-this-trusts-this-binary` 才继续（与现有探针的显式确认风格一致）；
4. 写入档案；追加审计事件 `adapter_adopted`（`host` / `channel` / `path` / `sha256` / `size` / `version` / `adopted_by` / `at`）。

冲突处理：若该通道已有 `adopted` 记录且 hash 不同，**默认拒绝**，需 `--replace` 才覆盖（覆盖同样记审计，新旧 hash 都留）。

### 3.3 preflight（逐项判定）

对每个通道输出一张检查表，每行含 `item / status / detail / hint`：

| 检查项 | 来源 |
|---|---|
| 身份可读、路径存在、大小与 hash 一致 | 新代码（`hashlib` + `os.stat`） |
| 启动参数未被改写 | 复用 `claude_code.supervised_problems()` / `build_argv` |
| 配置目录、工作目录在仓库之外 | 复用 `config._inside()` |
| 代理变量、CA 可读 | 复用 `claude_code` 的 `proxy_missing` / `ca_unreadable` 判定 |
| `CODEX_HOME` 可写（app-server 需要 sqlite） | 复用 `config.codex_home_write_check()` |
| 进程完整性级别 | 新增只读探测（`whoami /groups`），仅作提示，**不自动修复** |

`preflight` 的退出码：`0` 全部满足；`1` 有通道不可用（便于脚本/CI 判断）。

### 3.4 profile（本机档案）

默认位置：`%LOCALAPPDATA%\AgentHub\profiles\<host>.json`；可用 `AGENT_HUB_PROFILE` 覆盖；**仓库内路径拒绝**（与凭据目录同一规则）。

```json
{
  "schema_version": 1,
  "host": "DESKTOP-7CEN53I",
  "updated_at": "2026-10-26T06:00:00Z",
  "channels": {
    "codex": {
      "path": "C:\\...\\codex.exe",
      "sha256": "…",
      "size": 336134960,
      "version": "codex-cli 0.163.0-alpha.11.2",
      "transport": "app-server",
      "adopted_at": "2026-10-26T06:00:00Z",
      "adopted_by": "Administrator"
    },
    "claude-code": {
      "path": "C:\\...\\claude.exe",
      "sha256": "…",
      "size": 297376928,
      "version": "2.1.308",
      "adopted_at": "2026-10-26T06:01:00Z",
      "adopted_by": "Administrator"
    }
  }
}
```

解析优先级：**profile > 环境变量 > 源码常量**。任何一级命中都要在报告里标出来源（`profile` / `env` / `pinned`），不隐藏。

## 4. 与执行准入的关系（必须守住）

| 模式 | 接受的身份 | 说明 |
|---|---|---|
| 严格（supervised=false） | **只有** `status=adopted` 且磁盘 hash 与档案一致 | 源码常量命中也只算 `pinned`，需先 adopt 才能进严格模式 |
| 受监督（supervised=true） | `adopted` 或 `pinned`，或 `env` 指定 | 报告里必须写明来源；`candidate`（发现但未 adopt）仍拒绝 |

任何 hash 不一致（文件被替换/升级）→ 该通道回到 `candidate` 状态，严格模式立即拒绝，直到重新 adopt。

## 5. 实现切片（按顺序，每片都可独立验收）

1. **`tools/doctor.py`（只读）**：`discover` + `preflight` + `--json`；不改任何现有判定逻辑，不写档案。测试 8–10 项（用临时目录伪造二进制与档案，不得触碰真实 `%LOCALAPPDATA%`）。
2. **profile 读取与优先级**：在 `config.codex_settings()` 与 `claude_code` 的身份解析前插入一层；缺省行为与今天完全一致（没有 profile 就跑源码常量）。测试 6–8 项。
3. **`adopt` 写入 + 审计事件**：新增 DB 表/事件类型 `adapter_adopted`；测试 5–6 项。
4. **文档**：`docs/adapter-capabilities.md` 增加"接入一台新机器"章节；`docs/HANDOFF_CURRENT.md` 记一条。

第 1 片不涉及安全判定，**先做它**。

## 6. 风险与回滚

| 风险 | 处置 |
|---|---|
| 档案被误改导致跑到没评审的二进制 | adopt 必须显式确认 + 记审计；hash 不一致即拒绝；严格模式只认 adopted |
| 档案写进仓库被提交 | 路径在仓库内一律拒绝；`tests/test_hygiene.py` 增加断言 |
| 与现有 `AGENT_HUB_CODEX_*` 覆盖冲突 | 优先 profile，但两者都命中且不一致时**报错并停止**，不静默择一 |
| 回滚 | 删除档案文件即回到今天的纯常量行为；无需回滚代码 |

## 7. 关闭条件

- 在一台**没有 Claude Code MSIX 缓存、也没有 Codex 0.162 目录**的机器上，用 `doctor` + `adopt` 走通两条通道各一次真实受监督回合；
- 期间不修改源码常量、不放宽任何准入判定；
- 审计里能查到每次 adopt 的 hash 与操作者。
