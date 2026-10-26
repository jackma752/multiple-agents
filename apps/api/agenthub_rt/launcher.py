"""Self-contained Windows desktop entry. No development tokens or demo adapters."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import socket
import sys
import threading
import time
import webbrowser


def notify(message: str) -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Agent Hub", 0x10)
    else:
        print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Agent Hub Desktop")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--headless", action="store_true", help="启动后台，不自动打开浏览器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    # A release cannot be redirected to old demo databases, stubs or credentials
    # by inherited developer environment variables.
    data_override = os.environ.get("AGENT_HUB_DESKTOP_HOME")
    for key in list(os.environ):
        if key.startswith("AGENT_HUB_"):
            del os.environ[key]
    os.environ["AGENT_HUB_DESKTOP"] = "1"
    if data_override:
        os.environ["AGENT_HUB_DESKTOP_HOME"] = data_override
    from agenthub_kernel import desktop
    data = desktop.home()
    data.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Per-user state can contain API credentials. Restrict this directory,
        # never the user's existing Agent homes or workspace.
        import subprocess
        import getpass
        result = subprocess.run(["icacls", str(data), "/inheritance:r", "/grant:r",
            f"{getpass.getuser()}:(OI)(CI)F", "SYSTEM:(OI)(CI)F"], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            notify("无法限制本机数据目录权限，请选择当前用户可管理的目录。")
            return 1
    os.environ["AGENT_HUB_DATA_DIR"] = str(data)
    # Import only after configuration. Python frozen layouts are independent
    # of the build machine and the install working directory.
    from agenthub_kernel import config
    config.DATA_DIR = str(data)
    config.ensure_data_dir()
    try:
        sock = socket.socket()
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", args.port))
        sock.listen(128)
    except OSError:
        sock.close()
        # An existing instance can mint a fresh single-use browser ticket only
        # for the local user's launcher. Never put a permanent token in a URL.
        try:
            import json
            import urllib.request
            instance = json.loads((data/'instance.json').read_text(encoding='utf-8'))
            if instance.get('port') != args.port:
                raise ValueError('another port')
            request=urllib.request.Request(f'http://127.0.0.1:{args.port}/api/desktop/reopen',
                data=json.dumps({'control':instance['control']}).encode(),
                headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=3) as response:
                ticket=json.load(response)['ticket']
            if not args.headless:
                webbrowser.open(f'http://127.0.0.1:{args.port}/chat#pair={ticket}')
            return 0
        except (OSError, ValueError, KeyError):
            pass
        if not args.headless:
            notify(f"端口 {args.port} 已被占用。请关闭之前的 Hub，或使用 --port 指定其他端口。不会停止其他服务。")
        return 1
    try:
        import uvicorn
        from agenthub_rt.api import create_app
        app = create_app(files_root=desktop.load().get("workspace") or None, autopilot_interval=2.0)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
            log_config=None, access_log=False, loop="asyncio", http="h11", ws="none"))
        app.state.desktop_shutdown = lambda: setattr(server, "should_exit", True)
        desktop.atomic_json(data/'instance.json',{'pid':os.getpid(),'port':args.port,
                                                 'control':app.state.desktop_control})
        if not args.headless:
            def open_page():
                for _ in range(100):
                    if server.started:
                        webbrowser.open(f"http://127.0.0.1:{args.port}/chat#pair={app.state.desktop_ticket}")
                        return
                    if server.should_exit:
                        return
                    time.sleep(.1)
            threading.Thread(target=open_page, daemon=True).start()
        server.run(sockets=[sock])
        return 0
    except Exception as exc:
        # No traceback/environment/credential logging in the user launcher.
        import json
        import traceback
        (data / "startup-error.json").write_text(json.dumps({"type":type(exc).__name__,
            "module":getattr(exc,"name",None), "frames":[{"file":Path(f.filename).name,
            "line":f.lineno,"function":f.name} for f in traceback.extract_tb(exc.__traceback__)]},indent=2))
        if not args.headless:
            notify(f"启动失败：{type(exc).__name__}。请检查本机配置目录或将 profile.json 备份后重新配置。")
        return 1
    finally:
        sock.close()
        try:
            import json
            instance_file=data/'instance.json'
            if json.loads(instance_file.read_text(encoding='utf-8')).get('pid') == os.getpid():
                instance_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
