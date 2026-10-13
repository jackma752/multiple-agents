# P2 统一持久化数据库

日期：2026-10-14。范围：API 与独立 Worker 共用同一文件型 SQLite。
仅使用 Mock Agent，不调用真实产品接口。

## 1. 问题

修复前：`create_app()` 默认 `sqlite:///:memory:`，而 Worker `main()` 用
`sqlite:///./agent-hub.db`（相对**当前工作目录**）。

后果有两个，都属安全问题：

1. **API 与 Worker 看到不同的库**：用户在 API 批准的调用，Worker 永远看不到，
   于是"批准后不会执行"或"换了进程就丢状态"。
2. **路径依赖 cwd**：从不同目录启动会得到不同的库文件，重启后"记录消失"。

## 2. 方案

新增 `apps/worker/agenthub_kernel/config.py`，数据库 URL 解析顺序：

1. 环境变量 `AGENT_HUB_DB_URL`（完整 SQLAlchemy URL，最高优先级）。
2. `<repo_root>/var/agent-hub.db`。

其中 `repo_root` 由**本文件位置**推导（`config.py -> kernel -> worker -> apps -> repo root`），
因此**不依赖当前工作目录**。`var/` 已加入 `.gitignore`（运行时状态不入库）。

API 与 Worker 都改为读取该配置：

- `create_app(db=None, db_url=None)`：未显式传入时使用 `config.default_db_url()`；
  测试可传 `db=` 或 `db_url=` 隔离。
- `worker.main()`：使用同一 `config.default_db_url()`。

## 3. 文件型 SQLite 的并发设置

`Database` 对文件型 SQLite 启用：

```sql
PRAGMA journal_mode=WAL;     -- 读不阻塞写，支持并发 Worker
PRAGMA busy_timeout=30000;   -- 第二个写者等待而非立即 "database is locked"
PRAGMA synchronous=NORMAL;
```

内存库仍用 `StaticPool`（共享单连接）便于顺序测试，但**不能**表达多连接并发——
因此并发用例一律使用文件型库。

## 4. 验证结果

命令：`python -m pytest -v`（仓库根目录，使用 `.venv`）

```
tests\http\test_auth.py ...........              11
tests\persistence\test_shared_db.py ......        6
tests\policy\test_approval.py .......             7
tests\policy\test_consumable_approval.py .......  7
tests\policy\test_quota.py ......                 6
tests\policy\test_stop_and_authz.py ....          4
tests\policy\test_worker_atomicity.py ...         3
44 passed in 3.86s   exit code 0
```

本次新增的 6 个持久化用例（`tests/persistence/test_shared_db.py`）：

| 验收点 | 用例 |
|---|---|
| 库路径不依赖工作目录 | `test_default_db_path_is_cwd_independent`（切换到两个不同 cwd 后 URL 必须相同） |
| 第二条独立连接可见首条连接的写入 | `test_second_connection_sees_records_from_first` |
| **真实独立进程**读取本进程写入的记录 | `test_separate_process_reads_api_written_records`（`subprocess` 启动新 python，`cwd` 故意设为临时目录，断言子进程输出 `APPROVALS=1 INVOCATIONS=1 STATUS=pending`） |
| 未批准 0 次启动，批准后执行 1 次 | `test_unapproved_zero_starts_then_approved_executes` |
| 重启保留记录且已消费审批不重复启动 | `test_restart_preserves_records_and_does_not_restart_consumed`（新建 `Database`+`Worker`+`MockAdapter`，同一文件，断言新适配器启动 0 次、调用仍为 `running`、审批仍为 `consumed`、名额预留恰好 1 条） |
| 不同调用争抢最后一个配额不突破上限 | `test_concurrent_distinct_invocations_do_not_exceed_total_cap`（4 条不同调用、4 线程 + barrier 同抢 `max_subagents_total=1`，断言恰好 1 成功、预留恰好 1 条、落败者只能是配额/审批拒绝码） |

## 5. 仍未验证（不得当作已通过）

- **进程被强杀（非优雅退出）后的恢复**：本次验证的是"新进程读同一文件不重复启动"，
  未模拟 `kill -9` 中途写入、WAL 未 checkpoint 时的收敛行为，也未实现
  `starting` 状态下崩溃的 `unknown` 收敛与重查策略。
- **策略版本变化使旧审批失效**：`update_policy` 仍未持久化策略，
  `_maybe_invalidate_if_task_changed` 仍是空实现。
- **停止流程端到端**：停止后不新调度已验证；**取消正在运行的调用**未实现。
- **长事务/锁竞争下的超时行为**：`busy_timeout=30s` 是有意设定，未测试超过该阈值的行为。
- 真实适配器接入（下一阶段 P3）。

以上需单独实现与验证后才能宣称 P2 完整验收。
