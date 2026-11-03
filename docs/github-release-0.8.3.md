# 0.8.3 GitHub 更新与发行

用户明确授权在线验证通过后合并 main 并更新 GitHub，另追加 40,000 Token 用于局部修复复核。源码构建提交 `d5b937eeffaa0769f9bae7640b4b105245fddecd`。实际在线结果与边界见 [在线实录](online-release-0.8.3.md)：原项目质量 needs_review 和原 passed=false 保留，后续局部修订/复核 passed=true，不称科学内容全部通过。

## 验证

- 最终论文、在线驱动与源码卫生回归 **245 passed in 62.09s**；最终指导/来源/预算专项 **38 passed in 8.90s**，有重叠。
- 从上述提交重新构建安装包/ZIP，构建退出 0。与前轮 `2774aeb` 的本地候选不同，本次包含材料缺口分支修复与模型上下文去重。
- 独立核对 208 份 manifest 文件、209 份 ZIP 文件、1032 个内嵌模块；新增来源模块存在，无开发认证、模拟适配器、pytest、账号或数据库。
- 真正冻结程序在新空目录、随机端口 54898 启动；科研接口未认证 401、单次配对票据、科研/设置页、数学资源、消息/SSE 往返、解析审计新表通过，自身 shutdown 退出 0。零调用，不用真实 profile。

产物位于 `E:\plug\agent-hub\.worktrees\online-release-083\dist\p6-release-0.8.3`：

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `AgentHub-0.8.3-windows-x64-setup.exe` | 22909141 | `32813bf44067387cecf209ecad2ce71872cbf683486d710e50fb12e0c5feba4a` |
| `AgentHub-0.8.3-windows-x64-portable.zip` | 22608854 | `768afcd0400cee644e6145dc3a3562690d4af6e6a8817d0549c24ef56af5ab89` |

## 上传范围和顺序

目标固定为已核对的公开仓库 `jackma752/multiple-agents`，main 工作区干净且远程仍为 `434e241`，v0.8.3 标签不存在。先快进本地 main，再普通推送 main；不强推，不推所有分支/标签。经审查的二进制只作为 v0.8.3 的三个附件上传（setup、portable ZIP、SHA256SUMS），先草稿上传核对大小与远程 SHA-256，再发布。

当前安装、用户研究及真实配置不变。原始模型输出、研究导出、PDF、运行数据库及凭据不入 Git 或发行包。上传成功后的远程 SHA、附件校验与状态在本记录补充。
