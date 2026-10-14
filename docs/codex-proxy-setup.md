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
