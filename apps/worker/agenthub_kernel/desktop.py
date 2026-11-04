"""Per-user desktop configuration. Discovery never runs a model or reads login files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

CHANNELS = ("codex", "claude-code", "deepseek")


def enabled() -> bool:
    return os.environ.get("AGENT_HUB_DESKTOP") == "1"


def home() -> Path:
    return Path(os.environ.get("AGENT_HUB_DESKTOP_HOME") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentHub" / "desktop")


def load() -> dict:
    path = home() / "profile.json"
    if not path.exists():
        return {"schema": 1, "agents": {}, "workspace": "", "workspace_access": "read-only",
                "proxy": "", "ca_bundle": "", "codex_timeout_seconds": 0, "execution_timeout_seconds": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError("Unsupported profile version; keep a backup before upgrading")
    data.setdefault("execution_timeout_seconds", data.get("codex_timeout_seconds", 0))
    data.setdefault("codex_timeout_seconds", data["execution_timeout_seconds"])
    return data


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def identity(path: str) -> dict:
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_file() or (os.name == "nt" and p.suffix.lower() != ".exe"):
        raise ValueError("请选择原生 Agent exe，不能使用 cmd/bat 快捷包装器")
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(p), "sha256": digest.hexdigest(), "size": p.stat().st_size}


def discover() -> list[dict]:
    """Bounded common-install scan, not recursive drive or credential scanning."""
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    appdata = Path(os.environ.get("APPDATA", str(Path.home())))
    patterns = {
        "codex": [local / "OpenAI/Codex/bin/*/codex.exe",
                  local / "AgentHub/runtimes/codex/*/codex.exe",
                  appdata / "npm/node_modules/@openai/codex*/vendor/*/codex/codex.exe",
                  local / "Programs/codex/codex.exe"],
        "claude-code": [Path.home() / ".local/bin/claude.exe",
                        local / "Packages/Claude_*/LocalCache/Roaming/Claude/claude-code/*/*/claude.exe",
                        appdata / "Claude/claude-code/*/*/claude.exe",
                        local / "Microsoft/WindowsApps/*Claude*/app/resources/*/claude.exe",
                        appdata / "npm/node_modules/@anthropic-ai/claude-code*/vendor/*/claude.exe"],
    }
    found = []
    seen = set()
    profile = load()
    for channel, globs in patterns.items():
        paths = []
        for name in (channel if channel == "codex" else "claude",):
            p = shutil.which(name)
            if p:
                paths.append(Path(p))
        if profile.get("agents", {}).get(channel, {}).get("path"):
            paths.append(Path(profile["agents"][channel]["path"]))
        for pattern in globs:
            import glob
            paths.extend(Path(p) for p in glob.iglob(str(pattern)))
            paths = paths[:128]
        for p in paths[:128]:
            try:
                resolved = p.resolve()
                key = os.path.normcase(str(resolved))
                if key in seen or not resolved.is_file() or (os.name == "nt" and resolved.suffix.lower() != ".exe"):
                    continue
                size = resolved.stat().st_size
            except OSError:
                # Updates may remove a candidate between glob/stat; one stale
                # or inaccessible file must not hide other installed versions.
                continue
            seen.add(key)
            found.append({"agent": channel, "path": str(resolved), "size": size})
    return found


def workspace_permissions(agent_id=None) -> dict:
    if not enabled():
        return {}
    profile = load()
    path = profile.get("workspace")
    if not path:
        raise ValueError("请先在设置中选择工作区")
    permissions = {"workspace": path, "workspace_access": profile["workspace_access"]}
    if agent_id:
        permissions["execution_timeout_seconds"] = profile["execution_timeout_seconds"]
    return permissions


def save_configuration(body: dict) -> dict:
    """Explicit user's adoption; version query is local, never an inference."""
    current = load()
    if body.get("confirm") is not True:
        raise ValueError("请确认采用这些程序与工作区权限")
    workspace = str(Path(body.get("workspace", "")).expanduser().resolve(strict=True))
    if not Path(workspace).is_dir():
        raise ValueError("工作区必须为现有目录")
    protected = [home().resolve(), Path(os.environ.get("WINDIR", "C:/Windows")).resolve()]
    if (Path(workspace) == Path(workspace).parent or Path(workspace) == Path.home()
            or any(Path(workspace) == p or p in Path(workspace).parents
                   or Path(workspace) in p.parents for p in protected)):
        raise ValueError("请选择项目目录，不要选择系统或 Hub 数据目录")
    access = body.get("workspace_access", "read-only")
    if access not in ("read-only", "workspace-write"):
        raise ValueError("工作区权限只能为只读或允许写入")
    from agenthub.workspace import validate_execution_timeout
    timeout = validate_execution_timeout(body.get("execution_timeout_seconds",
        body.get("codex_timeout_seconds", current["execution_timeout_seconds"])))
    proxy = str(body.get("proxy", "")).strip()
    if proxy and (urlsplit(proxy).scheme not in ("http", "https") or not urlsplit(proxy).hostname
                  or urlsplit(proxy).username or urlsplit(proxy).password):
        raise ValueError("代理需要 http(s) 地址，不接受内嵌账号密码")
    ca = str(body.get("ca_bundle", "")).strip()
    if ca and not Path(ca).is_file():
        raise ValueError("证书文件不存在")
    agents = {}
    pending_key = None
    for channel, record in body.get("agents", {}).items():
        if channel not in CHANNELS:
            raise ValueError("未知 Agent")
        if not record.get("enabled"):
            continue
        if channel == "deepseek":
            endpoint = str(record.get("base_url") or "https://api.deepseek.com").rstrip("/")
            u = urlsplit(endpoint)
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError("API 地址需要不含凭据的 HTTPS 地址")
            key = str(record.get("api_key", "")).strip().rstrip("\x16").strip()
            if key and (any(ord(c) < 33 or ord(c) > 126 for c in key)):
                raise ValueError("密钥含无效字符")
            credential = home() / "credentials/deepseek.json"
            old = current.get("agents", {}).get(channel, {})
            if not key and (not credential.is_file() or old.get("base_url") != endpoint):
                raise ValueError("启用 API 或更换服务地址时请填写密钥")
            if key:
                pending_key = (credential, key)
            agents[channel] = {"enabled": True, "base_url": endpoint,
                               "model": record.get("model") or "deepseek-flash"}
            continue
        item = identity(str(record.get("path", "")))
        old = current.get("agents", {}).get(channel, {})
        if all(old.get(k) == item[k] for k in ("path", "sha256", "size")):
            version = old["version"]
        else:
            result = subprocess.run([item["path"], "--version"], capture_output=True,
                                    timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            version = result.stdout.decode("utf-8", "replace").strip().splitlines()
            if result.returncode or not version or len(version[0]) > 150:
                raise ValueError(f"{channel} 版本检查失败；请选择可用的原生 CLI")
            version = version[0]
            expected = "codex" if channel == "codex" else "claude"
            if expected not in version.lower():
                raise ValueError(f"版本结果不属于 {channel}")
        login_home = str(Path(record.get("config_dir") or
                        (Path.home() / (".codex" if channel == "codex" else ".claude"))).expanduser().resolve())
        if not Path(login_home).is_dir():
            raise ValueError(f"{channel} 配置目录不存在，请先在该 Agent 中登录")
        agents[channel] = {**item, "enabled": True, "version": version, "config_dir": login_home}
    saved = {"schema": 1, "agents": agents, "workspace": workspace, "workspace_access": access,
             "proxy": proxy, "ca_bundle": ca, "codex_timeout_seconds": timeout,
             "execution_timeout_seconds": timeout}
    # No prompts, credentials or raw CLI output in audit records.
    from datetime import datetime, timezone
    audit = home() / "adoptions.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "agents": {
            k: {x: v.get(x) for x in ("path", "sha256", "size", "version")} for k, v in agents.items()},
            "workspace": workspace, "access": access, "codex_timeout_seconds": timeout}, ensure_ascii=False) + "\n")
    if pending_key:
        atomic_json(pending_key[0], {"api_key": pending_key[1]})
    atomic_json(home() / "profile.json", saved)
    return saved


def build_router():
    from agenthub_adapters.routing import RoutingAdapter
    from agenthub_kernel.adapters import _build_appserver, _build_claude_code, _build_deepseek
    from agenthub_kernel import config
    profile = load()
    adapters = {}
    for channel, item in profile.get("agents", {}).items():
        if not item.get("enabled"):
            continue
        settings = {"proxy": profile.get("proxy", ""), "ca_bundle": profile.get("ca_bundle", ""),
                    "timeout_seconds": profile["execution_timeout_seconds"], "adopted": item}
        if channel == "codex":
            settings.update(codex_home=item["config_dir"], workdir_root=str(home() / "runs/codex"),
                            appserver_store_path=str(home() / "executions/codex.db"))
            adapters[channel] = _build_appserver(settings)
        elif channel == "claude-code":
            settings.update(config_dir=item["config_dir"], workdir_root=str(home() / "runs/claude"),
                            store_path=str(home() / "executions/claude.db"))
            adapters[channel] = _build_claude_code(settings)
        elif channel == "deepseek":
            settings.update(timeout_seconds=120, base_url=item["base_url"], model=item["model"],
                            credentials_path=str(home() / "credentials/deepseek.json"),
                            store_path=str(home() / "executions/deepseek.db"), stub="")
            adapters[channel] = _build_deepseek(settings)
    return RoutingAdapter(adapters, allow_empty=True)
