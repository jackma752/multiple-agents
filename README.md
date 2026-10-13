# Agent Hub

把 Codex、Claude Code 等 Agent 接入一个统一聊天入口的私人控制台。核心约束：**每次 Agent 调用先经用户同意，子 Agent 数量由用户限制**。

方案与验收标准见 `E:\plug\AGENT_HUB_PLAN.md`。

## 当前状态

- 阶段：**P0 接入能力验证**（进行中）。审计对象：AionUi / AionCore 源码。
- 已产出：`docs/adapter-capabilities.md`（能力矩阵）、`docs/audit-aionui.md`（三个硬条件验证报告）。
- 尚未实现任何功能代码；首个可演示目标是 Mock 版审批与限额闭环。

## 仓库约定

见 `AGENTS.md`。

## 目录结构

```text
agent-hub/
  README.md
  AGENTS.md
  .gitignore
  .gitattributes
  docs/          能力矩阵、架构、审计报告、Git 流程
  apps/          api / worker / web（待建）
  packages/      contracts / adapters（待建）
  tests/         policy / adapters / e2e（待建）
```

## 本地开发

P1 阶段完成后，新机器按本节可启动 Mock 环境（无需真实 Agent 账号）：

```powershell
# 依赖锁定文件随仓库提交；真实配置放 .env，不提交
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# pip install -e ".[dev]"   （apps/api 落地后启用）
```
