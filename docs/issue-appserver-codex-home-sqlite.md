# 未决问题：app-server 无法在 `CODEX_HOME` 初始化 sqlite 状态库

**状态：未解决（OPEN）——现象在一次权限调整后不再出现，但根因未定位。**
**不得因为随后有一次成功调用，就视为已修复：本记录跟踪的是"为什么会被拒"，不是"现在能不能跑"。**

最后更新：2026-10-26。

---

## 1. 现象（实际观测）

`inv-89fb6ef66f72`（2026-10-26T02:58Z，0 token）在发出任何回合前即失败，服务端 stderr
（`var/p46-user-demo/data/codex-runs/_logs/inv-89fb6ef66f72-appserver-stderr.log`）两行：

```
WARNING: proceeding, even though we could not create PATH aliases: 拒绝访问。 (os error 5) at path
"C:\Users\Administrator\AppData\Local\AgentHub\codex-home\tmp\arg0-windows\codex-arg0th43aA"
Error: failed to initialize sqlite state runtime under C:\Users\Administrator\AppData\Local\AgentHub\codex-home:
failed to initialize state runtime at C:\Users\Administrator\AppData\Local\AgentHub\codex-home
```

内核结论：`server refused setup: disconnected`。

第 1 行（PATH alias）是**已知无害警告**，多处运行都出现过；致命的是第 2 行。

## 2. 触发条件的实测对照

| 前提 | 结果 |
|---|---|
| 起服务的 python 进程为 `Low` 完整性级别（`Mandatory Label\Low`） | app-server 初始化 sqlite 失败，连接断开 |
| 同一台机器、同一 `CODEX_HOME`，python 变为 `Medium` | 同一通道出真实回合：`inv-371025d822df`，`turn/completed with status completed`，stderr 0 字节 |

对照见 `docs/p47-codex-real-run-2026-10-26.md`。

**因果边界**：只观测到这一组对照，没有做多轮 A/B，不能宣称完整性级别是唯一原因。

## 3. 已确认与未确认

**已确认**

- 失败发生在**发回合之前**，属启动期失败；调用没有产生模型输出、没有消耗额度。
- 同一 `CODEX_HOME` 在进程拿到更高完整性级别后可以正常初始化。

**未确认**

- 具体是哪一条 ACL / 强制标签让 `Low` 令牌无法在 `%LOCALAPPDATA%\AgentHub\codex-home` 建库；
- 该目录是否曾被更高完整性级别的进程创建、从而带着不兼容的强制标签；
- 换一个全新的 `CODEX_HOME` 能否让 `Low` 进程也成功（未测）。

## 4. 影响

- 在多智能体（`multi`）模式下，codex 只能走 app-server（`adapters.py:58-64`），因此这条阻断会让
  **codex 的真实通道整体不可用**。
- 单 kind `codex` + `--codex-transport exec` 不走这条路径（但见
  `docs/issue-codex-exec-pin-removed.md` 的独立阻断）。

## 5. 关闭条件

1. 能说明 `Low` 级别为何无法初始化该目录（ACL 证据），并给出**不降低安全要求**的解决办法；或
2. 明确记录"本机运行 Hub 的进程必须 ≥ `Medium`"，并在启动文档/启动脚本里固定住这个前提。

在此之前，任何"app-server 可用"的说法都必须附带"服务进程完整性级别不低于 Medium"这个前提。
