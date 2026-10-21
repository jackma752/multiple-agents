# 会话后端（2026-10-21）

本轮只实现后端，前端最后再接。会话和消息写入运行数据库，不能把运行库打进源码发布包。无需读取凭据或发起模型调用。

## HTTP 契约

以下接口均须用户 Bearer token；未知会话或非本人的会话返回 404。

| 接口 | 输入 | 效果 |
|---|---|---|
| POST `/api/conversations` | 可省略；`title` 可选 | 创建持久化会话，不创建任务或审批 |
| GET `/api/conversations` | `offset=0, limit=50`（最大 100） | 按创建时间倒序列出本人的会话 |
| POST `/api/conversations/{id}/messages` | `text` | 只保存用户消息；保留旧页面的 `acked/text` 返回值，不隐式创建调用 |
| GET `/api/conversations/{id}/messages` | `after_id=0, limit=100`（最大 200） | 按递增消息 ID 读取；返回 `next_after_id`、`has_more`、关联调用/审批状态 |
| POST `/api/conversations/{id}/invocations` | `text, agent_id, client_request_id, execution_mode=strict` | 在一个事务中保存用户消息、调用快照及待审批对象；不批准、不执行 |
| POST `/api/invocations/{id}/run` | 无 | 只尝试执行指定调用；仍经过既有准入、consume、审批 CAS、配额及停机检查 |

消息与任务输入保留原文；空白文本、超长输入、不认识的输入字段被拒绝。提交调用的 `client_request_id` 必填，同会话同键同内容返回原对象；同键改内容/Agent/执行模式返回 409。幂等重放不重新审批、不重置策略、不启动调用。

`run` 未就绪/准入或配额拒绝返回 409，未知调用返回 404；已启动的调用不能通过重复请求再启动。进入适配器后的失败结果返回 200，并以 `ok/terminal/status` 表达，不能只凭 HTTP 200 判断执行成功。`adapter_start_attempted=true` 只说明调用了适配器的 start 方法；是否真正启动外部进程须另看适配器执行证据。

## 事务与配额

会话第一次提交调用时创建一个根任务；后续调用复用同一个根任务及累计配额，快照绑定当前策略版本。每一条调用都有独立审批。暂停时可保存待审批调用，执行仍被阻止；已停止的会话拒绝创建新调用。新建会话属于新任务树，不提供全账号总额限制。

`conversations` 和 `chat_messages` 为新增表，启动时增量建表，不改写旧任务/审批。旧页面先保存消息再调旧 `/api/invocations` 的流程继续可用，但这些旧调用不会自动关联到会话；不可猜测历史配对。

人工裁决重试仍须先确认旧执行已停止。重试从被重试调用的快照保留原文、Agent、权限等，绑定当前策略版本；在同一会话新增一条关联用户消息和全新待审批调用。不得从根任务最初的目标重建后续消息，亦不得沿用旧批准。

## 回复与恢复边界

只把已确认终态调用的适配器 `final_message` 保存为助手消息，原样保存，不把状态说明、Mock 状态或错误文本编造成模型回复。每个调用最多一条助手消息。回复保存与内核终态结算同事务；若适配器记录暂不可读，结算仍完成，下一次 `worker/recover` 可从已有适配器记录补回，绝不重新调用模型。unknown 调用不自动补成成功回复或释放名额。

`context_mode=independent`：这里恢复的是 Hub 的本地历史，**没有接入原生 thread/resume**，也不自动把旧聊天内容转发给下一次调用或另一个 Agent。页面增量输出、原生上下文续接及第二个真实 Agent 留待后续。

目前仍为本机单用户产品；既有任务管理/全队列 drain 是用户级管理入口，不宣称多租户隔离。真实控制边界、监督模式、两项 DLL OPEN 状态不变。

## 参考与验证

按用户要求，在检查重试与消息对应关系时查看了 GitHub 原项目：

- [Paseo provider 契约](https://github.com/getpaseo/paseo/blob/main/docs/providers.md) 用 `clientMessageId` 对应唯一用户消息，避免 provider 回显再生成一条消息。这里只参考消息身份分离原则，未复制实现。
- [Orca orchestration 文档](https://github.com/stablyai/orca/blob/main/docs/site/content/docs/cli/orchestration.mdx) 区分任务与执行派发，并显式记录 retry-of。这里只参考显式执行身份；本项目仍逐次审批，不自动重试不确定调用。

本轮验收入口（全离线，不发模型请求）：

```powershell
cd E:\plug\agent-hub\.worktrees\chat-workbench
& 'E:\plug\agent-hub\.venv\Scripts\python.exe' -m pytest tests/http/test_conversations.py -o addopts='' -q
```

覆盖文件库重开、消息分页、原文保持、权限与输入拒绝、事务回滚、并发幂等、精确调度、所有旧门禁、配额连续累计、重试快照/历史、回复落盘与恢复、unknown 保留、Codex 离线捕获、旧库增量建表。现有前端未接入新会话调用端点，8080 服务未在本轮替换。

最终全量验证：同一目录执行 `E:\plug\agent-hub\.venv\Scripts\python.exe -m pytest -o addopts='' -q`，实际输出 **550 passed in 30.00s**。本轮新增 37 项会话后端测试；没有真实模型回合。
