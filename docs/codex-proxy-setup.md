# Codex 代理与证书配置（P3 受控接入）

日期：2026-10-14。**本文件全部结论来自不调用模型的检查**：
`codex doctor`（本地诊断）、`tools/proxy_check.py`（经代理的 HTTP 探测）、
以及对 Codex 二进制的只读字符串检索。

---

## 1. 代理端口核对

| 端口 | 监听 | 经其访问 `chatgpt.com` | 经其访问 `api.openai.com/v1/models` | 经其访问 `pypi.org` |
|---|---|---|---|---|
| **8090**（用户告知的混合代理） | **OPEN** | **403**（server: cloudflare） | **401**（cloudflare） | 200 |
| 10090（Windows 用户级代理设置指向此端口） | **OPEN** | **403** | **401** | 200 |

两者行为**完全一致**。另注：

- **无任何 `*_PROXY` 环境变量**（这解释了此前 Codex 为何"直连"失败）
- Windows 用户级（WinINET）代理配置为 `127.0.0.1:10090`、`ProxyEnable=1`；
  但 **Codex 是 Rust 程序，不读 WinINET 设置**，只认环境变量/自身配置
- 直连对照：`chatgpt.com` 连接超时、`api.openai.com` DNS 解析失败、`pypi.org` 200

> 关于状态码差异：我请求的是 `/v1/models`，得到 **401**（未带凭据，属**可达**的正常响应）；
> 用户报告的 **421** 应来自不同路径或请求头。**如实记录各自的确切 URL 与状态，不假设一致。**

**结论：连接失败的直接原因是"未走代理"。** 经代理后连接可完成并返回真实 HTTP 响应。

---

## 2. 核对 Codex 启动进程是否真的使用代理（实测）

`codex doctor` 本身会报告代理状态。三种变体对比：

| 变体 | proxy env | doctor 报告 | 握手结果 |
|---|---|---|---|
| 无代理 | 未设 | `proxy env vars  none`、`respect system proxy  disabled`、`managed proxy  not configured` | `network error ... (os error 11002)` = **DNS 解析失败** |
| 设 `HTTPS_PROXY`/`HTTP_PROXY` | 已设 | **`proxy env vars present  HTTP_PROXY, HTTPS_PROXY, http_proxy, https_proxy`** | 错误**前移**为 `invalid peer certificate: UnknownIssuer` |
| 设 `-c network.proxy_url=...` | 不生效 | **`session-flags: 'network' is ignored.`** | 与"无代理"相同 |

**由此确认**：

1. **Codex 会读取代理环境变量**（doctor 明确报告 env 已present，且失败点随之前移）。
2. **`network.proxy_url` 配置键在 exec 路径下被忽略**，不能作为代理配置手段。
3. 设代理后剩余障碍是**证书信任**（`UnknownIssuer`）。

### 二进制只读检索（旁证）

`codex.exe` 中含 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`（含小写）、
`CODEX_CA_CERTIFICATE`、`SSL_CERT_FILE`、`Proxy-Authorization`、
`NetworkToml{proxy_url, enable_socks5, socks_url, allow_upstream_proxy,
dangerously_allow_non_loopback_proxy, ...}`。
注意 **`dangerously_allow_non_loopback_proxy`** 说明该机制**默认只允许 loopback 代理**——
`127.0.0.1:8090` 正是 loopback，符合要求。

---

## 3. 证书信任：`UnknownIssuer` 的解法

给 Codex 一个**可读的 PEM 包**即可。实测两个变量都有效：

| 变量 | 值 | doctor 结果 |
|---|---|---|
| （不设） | — | `handshake transport error network error: invalid peer certificate: UnknownIssuer` |
| **`CODEX_CA_CERTIFICATE`** | `.venv\Lib\site-packages\certifi\cacert.pem` | `CODEX_CA_CERTIFICATE  readable file ...` → 握手**通过** |
| **`SSL_CERT_FILE`** | 同上 | `SSL_CERT_FILE  readable file ...` → 握手**通过** |

设 CA 后（当时尚未恢复 auth）错误变为应用层
`http 401 Unauthorized: Missing bearer or basic authentication in header`，
且 `reachability active provider endpoints are reachable over HTTP` —— **TLS 已通**。

---

## 4. 最终预检（全绿）

恢复登录副本后（**内容从未打印**），以
`CODEX_HOME` + `HTTPS_PROXY` + `HTTP_PROXY` + `NO_PROXY` + `CODEX_CA_CERTIFICATE` 运行 `codex doctor`：

```
stored auth mode         chatgpt
auth mode                chatgpt
proxy env vars present   HTTP_PROXY, HTTPS_PROXY, NO_PROXY, http_proxy, https_proxy, no_proxy
handshake result         HTTP 101 Switching Protocols        <-- TLS + WebSocket 握手成功
reachability             active provider endpoints are reachable over HTTP
ChatGPT inference URL    https://chatgpt.com/backend-api/<redacted> reachable (HTTP 405)
desktop assets CDN       https://persistent.oaistatic.com/codex-app-prod/<redacted> reachable (HTTP 200)
```

**结论：代理、证书、登录态、可达性四项预检全部通过。**
（其间曾观察到一次 `request timed out`，复跑后为 `reachable (HTTP 405)` —— 提示该端点的探测
**存在间歇性**，但因未做多次受控重复，**不能断言其稳定性**。）

---

## 5. 本次进程专用的配置（不影响系统，不改全局设置）

**全部通过环境变量注入子进程，不写注册表、不改系统代理、不动 `~/.codex/config.toml`。**

| 变量 | 值 | 作用 |
|---|---|---|
| `CODEX_HOME` | `E:\plug\agent-hub\var\codex-home` | 专用、可写、**Gi已忽略**；避免写 `~/.codex/tmp` 被拒 |
| `HTTPS_PROXY` / `https_proxy` | `http://127.0.0.1:8090` | 走混合代理 |
| `HTTP_PROXY` / `http_proxy` | `http://127.0.0.1:8090` | 同上 |
| `NO_PROXY` / `no_proxy` | `localhost,127.0.0.1` | 本机流量不走代理 |
| `CODEX_CA_CERTIFICATE` | `E:\plug\agent-hub\.venv\Lib\site-packages\certifi\cacert.pem` | 解决 `UnknownIssuer` |

> 凭据说明：`var/codex-home/auth.json` 是从默认 home **本地复制**的登录凭据副本。
> **内容从未打印**。验证完成后应删除（`Remove-Item var\codex-home\auth.json`）。

---

## 6. 更新的结论

- 原先记录为"**连接失败，原因待确定**"。现在**原因已定位到可操作的层面**：
  **Codex 未走代理（无代理环境变量）**，因此直连失败。
- 经代理并配置可读 CA 后，**握手与端点可达性预检全部通过**。
- 仍需注意：**尚未完成任何成功的真实模型调用**。
  预检通过 ≠ 真实调用成功；端点在探测中曾出现一次超时，稳定性未验证。
- 因此**真实适配器状态仍为"未验证"**，需一次成功的真实调用才能改变。

---

## 7. pip、certifi 与代理证书信任（2026-10-22 复核与处理）

全程不调用模型，不下载任何包，只查询了索引元数据。证书验证始终开启。

**事实**：

| 检查 | 结果 |
|---|---|
| 经 `127.0.0.1:8090` 访问 pypi.org 和 files.pythonhosted.org | TLS 验证通过，证书由 GlobalSign 签发 |
| 经同一代理访问 chatgpt.com | TLS 验证通过，证书由 Google Trust Services WE1 签发 |
| 验证所用的 CA | venv 的 certifi（2024.08.30）、pip 自带的 certifi（2023.05.07）、系统证书库，三者结果一致 |
| 直连上述三个站点 | 都超时 |
| `certifi/cacert.pem`、`pip/_vendor/certifi/cacert.pem` | 都与安装时 RECORD 中的哈希一致，**没有被追加过任何证书** |
| 不带代理环境变量时运行 `pip index versions certifi` | 约 41 秒后报 “No matching distribution found”，这就是此前的“卡住” |
| 带代理环境变量时同样运行 | 约 1 秒返回 |

**结论**：

- 8090 是普通的 CONNECT 隧道，**不做 TLS 拦截**，所以不存在需要信任的私有代理 CA。
- pip 的问题是没有走代理，不是证书问题。

**处理**（只影响项目的 venv，不改系统和用户级设置）：

- 新增 `tools/bootstrap/pip_trust.py`：
  - 默认只读检查：两个 certifi 包与 RECORD 是否一致；venv 的 `pip.ini` 是否设置了代理、是否不含 `trusted-host` 或任何关闭验证的设置；经代理的 TLS 是否通过主机名和证书链验证。
  - `--write-venv-config`：写入 `E:\plug\agent-hub\.venv\pip.ini`（site 级，只对这个 venv 生效），内容为 `proxy = http://127.0.0.1:8090`、`cert = <venv certifi>`、`timeout = 30`、`retries = 2`。
  - 如果将来代理改为拦截 TLS，可用 `--extra-ca <代理CA.pem>`：把 certifi 与该 CA 合并成单独的 `.venv\agenthub-ca-bundle.pem` 再引用。**certifi 自己的文件不会被修改。**
- 已写入。之后在不带代理环境变量的 shell 中，同一条 pip 查询约 1 秒成功；`pip config debug` 显示 site 级配置生效。
- Codex 子进程仍然需要 `CODEX_CA_CERTIFICATE=<venv certifi>`，否则报 `UnknownIssuer`（见第 3 节），S1–S5 的预检会检查它。这一项**保留，不做改动**。
- 没有升级 certifi（当前 2024.08.30，索引上最新为 2026.10.19），以免在 S2–S5 证据序列中途改变环境。是否升级需另行决定；升级后 `pip.ini` 引用的路径不变。
- 如果代理不在运行，pip 会立即报连接失败，而不是卡住；这时需要先启动代理。

### 7.1 升级 pip 与 certifi 并核验（2026-10-22 晚，用户要求）

升级命令为 `.\.venv\Scripts\python.exe -m pip install --upgrade pip certifi`。安装经 venv 的 `pip.ini` 走代理，验证保持开启，没有使用 trusted-host。之后运行 `pip check`：No broken requirements found。

| 项目 | 升级前（S1–S4 的所有运行都在这个环境下） | 升级后（S5 起） |
|---|---|---|
| pip | 23.2.1（内置 certifi 2023.05.07，139 张证书，SHA-256 `b301535dca491d9814ea28faa320ac7a19d0f5d94237996fa0a3b5a936432514`） | **26.2.2**（内置 truststore 0.10.4；内置 certifi 2026.10.19，与下一行逐字节相同） |
| certifi | 2024.08.30，151 张，`cacert.pem` 299427 字节，SHA-256 `94edeb66e91774fcae93a05650914e29096259a5c7e871a1f65d461ab5201b47` | **2026.10.19**，120 张，238856 字节，SHA-256 `50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785` |
| `certifi-*.dist-info/RECORD` | — | `fc9e47b66b0cbfc88a6c5b07400bffdb5ca6b4dfd10713c263b77ac4374ff1a8` |
| `pip-*.dist-info/RECORD` | — | `7d9aed153153ec74bf3dfee8152ef9c3dd8e6587aa5301700fe355590750dd28` |
| `.venv\pip.ini` | `9bc2a2c04e5ba29c0a241decefc60ebe2eb98d1f14a42f83f5021ebb6528cdf8` | 未变（cert 路径不变） |

**核验**：运行 `tools/bootstrap/pip_trust.py --negative-control`，退出码 0：

- 两个证书包都与各自的 RECORD 一致（未被修改）；`pip.ini` 检查通过；经代理访问 pypi.org 和 files.pythonhosted.org 的 TLS 验证通过（GlobalSign）。
- **Codex 使用的证书包**（`CODEX_CA_CERTIFICATE`，即新的 certifi）：经代理访问 chatgpt.com，验证通过（Google Trust Services）。
- **反向对照**：用 IP 而不是域名访问 PyPI，pip 拒绝连接，报 “There was a problem confirming the ssl certificate”，随后是本地化的系统消息“证书 CN 名不匹配”。这说明主机名验证确实开启，并由系统证书库执行。
- **cert 的含义已经改变**：只含 Amazon Root CA 1 的证书包仍然能连上 PyPI。原因是 pip 24.2 起默认通过 truststore 使用 **Windows 系统证书库**，`cert` 只是在其基础上**追加** CA，不再替换。第 7 节“`cert` = certifi 即 pip 的信任源”的说法，只对升级前的 pip 23.2.1 成立。
- 正向对照：按现有配置运行成功；不带代理环境变量时，约 1 秒成功。

**影响**：

- pip 现在信任“系统证书库 + certifi”，验证保持开启。
- Codex 子进程只信任 `CODEX_CA_CERTIFICATE` 指向的新 certifi。
- S1–S4 的证据是在旧环境下取得的，保持原样；S5 的申请和运行记录会写明新环境。
- `tools/bootstrap/fetch_pinned.py` 中 certifi 的版本固定为 2026.10.19。
