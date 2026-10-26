# 项目图标（2026-10-27）

用户指定图片作为项目图标。本轮保持原图完整内容及底部文字，不裁剪、不重绘，只转换尺寸和格式。

- 原图：`assets/branding/source.png`，逐字节与用户附件一致。
- 发布素材：`apps/api/agenthub_rt/static/brand/`，64/256/512 PNG 和 16/24/32/48/64/128/256 多尺寸 ICO。
- 应用 exe：PyInstaller `--icon`；安装器/卸载器：NSIS MUI_ICON/MUI_UNICON。
- 开始菜单使用应用 exe 内嵌图标。网页通过固定 `/favicon.ico` 和 `/assets/app-icon.png` 端点提供资源，页面标题与 README 使用同一张图。
- `make_icons.py` 为确定性格式转换工具，Pillow 仅用于重建资源，不是应用运行依赖。原始大图不进入安装包。
- 新包编号 0.5.1，0.5.0 旧包与原校验记录保留。新 ZIP 与 exe 都设置为正常 Medium 完整性，不修改系统安全策略。

验证：页面/控制台/桌面设置原有受影响测试 38 passed in 5.87s；ICO 7 个尺寸可读取，原图哈希一致。打包资源与最终产物检查在发行记录补充。

安装路径与数据路径仍分开：安装向导可改安装目录；AGENT_HUB_DESKTOP_HOME 可改数据目录，图形化迁移未实现。本轮不改这项行为，只在 README 补充说明。
