# bootstrap 工具（依赖离线下载）

> **2026-10-22 更新**：卡住的原因已经查明。直连 PyPI 会超时，而 pip 没有走本机代理；问题不在证书。
>
> 解决办法：运行 `.\.venv\Scripts\python.exe tools\bootstrap\pip_trust.py --write-venv-config`，给 venv 写入 `pip.ini`（代理加 certifi 包，验证保持开启）。之后直接 `pip install` 即可。
>
> 详见 `docs/codex-proxy-setup.md` 第 7 节。下面的 wheelhouse 方案保留，用于没有代理的环境。

（历史说明）本机（WORKSPACE）pip 的 downloader 在网络 TLS 上会卡住（见交接包 HANDOFF §1），
但 Python 的 `urllib` 走 OpenSSL 可正常访问 `pypi.org` / `files.pythonhosted.org`。
这几个脚本用 `urllib` 把 wheel 下载到仓库级 `wheelhouse/`（已 gitignore，不入库），
再让 `pip install --no-index --find-links wheelhouse` 从本地完成安装。

## 为什么不用 `pip install` 直接装

`pip` 在本机 `python -m pip install` 下载元数据/whl 时会长时间无响应（连接被卡住），
故改用 `urllib` 直连下载，规避该问题。托管机器若网络正常，直接用
`pip install -r` / `pip install -e ".[dev]"` 即可，无需这些脚本。

## 各脚本

| 脚本 | 用途 |
|---|---|
| `fetch_wheels.py` | 按包名列表下载"最新"兼容 wheel（cp312/win_amd64 或纯 py）到 wheelhouse |
| `fetch_pinned.py` | 按显式版本号下载 wheel，替换"最新"（用于把 fastapi 定在 0.115.x 以减小依赖树） |
| `bootstrap_deps.py` | 迭代循环：`pip install --no-index` 失败时解析缺包名 → `urllib` 补下载 → 重试，直至全部装成功 |

## 路径约定

三个脚本都从自身位置向上回溯出仓库根（`tools/bootstrap/ -> repo root`），
把 `wheelhouse` 与 `.venv` 定位到仓库根下。因此更换目录到任何位置都能复现。

## 用法

```powershell
cd <repo-root>
# 1) 建虚拟环境
python -m venv .venv
# 2) 下载所需 wheel（可任选其一，视需要"最新"还是"固定版本"）
python tools/bootstrap/fetch_pinned.py
# 3) 从本地 wheelhouse 安装（bootstrap_deps 会自动补缺包并重试）
python tools/bootstrap/bootstrap_deps.py
# 4) 跑测试
.\.venv\Scripts\python.exe -m pytest tests/policy -v
```

## 注意

`wheelhouse/`、`.venv/`、`*.db`、`*.sqlite*`、`__pycache__/` 均为生成物，
已在 `.gitignore` 中排除，不随仓库提交。提交的只是脚本本身。
