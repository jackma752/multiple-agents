r"""S1: app-server handshake verification (no model turn, ever).

    # review what it will do, and the exact plan
    .\.venv\Scripts\python.exe tools\probes\appserver_s1.py --list

    # offline: drives the scripted server in-process (no Codex, no process)
    .\.venv\Scripts\python.exe tools\probes\appserver_s1.py --mode stub

    # real: starts ONE pinned app-server, handshakes, creates at most one EMPTY thread
    .\.venv\Scripts\python.exe tools\probes\appserver_s1.py --mode real `
        --i-understand-this-starts-an-app-server-process `
        --codex-home "%LOCALAPPDATA%\AgentHub\codex-home"

WHAT S1 IS ALLOWED TO DO
------------------------
* start one app-server process with the PINNED executable (identity checked first);
* send `initialize`, then the `initialized` notification;
* create AT MOST ONE thread via `thread/start` and stop there.

WHAT S1 MUST NEVER DO (enforced BEFORE sending, plus transcript assertions)
------------------------------------------------------------------------
* no `turn/start` - therefore no prompt, no model turn, no quota;
* no `turn/resume`, `turn/steer`, `thread/fork`;
* no command execution and no approval request of any kind;
* never a second thread.

IT KEEPS EVIDENCE AND FAILS CLOSED
----------------------------------
* the child's stderr is written to a file (a handle works where pipes are denied);
* the wire transcript and the checks are printed and saved;
* the FIRST failed check stops the run - no retry, no relaxation;
* only the process THIS script started is terminated (by its own handle), and only
  the run directory THIS script created is removed. The long-term CODEX_HOME and the
  operator's own ~/.codex are never deleted. Codex can write runtime state in
  the selected home; the write probe touches only its own random temporary file.

NEGATIVE CONTROLS (offline, `--mode stub`)
------------------------------------------
* a thread/turn request attempted before the handshake must be refused LOCALLY and
  must never appear on the wire;
* a client that sends `initialize` but SKIPS the `initialized` notification must be
  refused BY THE SERVER (-32002) - which proves the gate exists on both sides.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [
    os.path.join(REPO, "packages", "contracts"),
    os.path.join(REPO, "packages", "adapters"),
    os.path.join(REPO, "apps", "worker"),
]

from agenthub_adapters import appserver as A  # noqa: E402
from agenthub_adapters import appserver_stub as S  # noqa: E402

RUN_ROOT = os.path.join(REPO, "var", "appserver-s1")
# Reviewed 2026-10-17 with explicit user authorization for ZERO-TURN S1 only.
# The exec adapter's historical identity and blocked admission are unchanged.
S1_CODEX_PATH = r"C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\9614f1d9b40d9c4f\codex.exe"
S1_CODEX_SHA256 = "e98c516f1c8935789aabe9122ee7e05ec4c3efd1ba6acf179242e031c495b99e"
S1_CODEX_SIZE = 334121776
S1_CODEX_VERSION = "codex-cli 0.162.0-alpha.17.2"
#: Methods S1 must never send. Checked against the raw transcript.
FORBIDDEN_METHODS = (
    "turn/start", "turn/steer", "turn/interrupt", "review/start",
    "thread/fork", "command/exec", "process/spawn",
)
APPROVAL_METHOD_SUBSTRINGS = ("requestApproval", "Approval", "requestUserInput")


class CheckFailed(Exception):
    """A failed check: stop immediately (fail closed), do not retry."""


class S1Client(A.AppServerClient):
    """S1 has a pre-send allowlist, not a retrospective ban on model calls.

    No prompt, resumed session, tool response or second thread can leave this
    client. Evidence contains envelopes only, never server payloads or errors.
    """

    def __init__(self, transport, *, workdir: str, request_timeout: float = 20.0):
        super().__init__(transport, request_timeout=request_timeout)
        self.workdir = workdir
        self.sent: list[dict] = []
        self.received: list[dict] = []
        self._s1_lock = threading.Lock()
        self.violation = False

    def _send(self, message: dict) -> None:
        with self._s1_lock:
            index = len(self.sent)
            method = message.get("method")
            valid = (
                index == 0 and method == A.INITIALIZE and "id" in message
                and message.get("params") == {
                    "clientInfo": {"name": "agent-hub-s1", "version": "0.1.0"}}
            ) or (
                index == 1 and method == A.INITIALIZED and "id" not in message
                and message.get("params") == {} and self._initialization_received()
            ) or (
                index == 2 and method == A.THREAD_START and "id" in message
                and message.get("params") == {
                    "cwd": self.workdir, "sandbox": "read-only", "approvalPolicy": "never"}
            )
            if not valid:
                self.violation = True
                raise CheckFailed("S1 rejected outgoing message before transmission")
            super()._send(message)
            self.sent.append({"method": method, "id": message.get("id")})

    def _initialization_received(self) -> bool:
        return any(m.get("id") == self.sent[0]["id"] and m["kind"] == "result"
                   for m in self.received)

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        kind = ("request" if "id" in message else "notification") if method else (
            "error" if "error" in message else "result")
        self.received.append({"kind": kind, "method": method, "id": message.get("id")})
        if kind == "request" or (isinstance(method, str) and
                                  (method.startswith(("turn/", "item/")) or
                                   "Approval" in method)):
            self.violation = True
            raise A.AppServerProtocolError("unexpected activity during zero-turn S1")
        super()._dispatch(message)

    def close(self) -> None:
        super().close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            if self._reader.is_alive():
                raise A.AppServerTimeout("S1 reader did not stop")


def exercise_s1(client: S1Client, child) -> str:
    client.start()
    session = A.AppServerSession(client)
    info = session.initialize(client_name="agent-hub-s1", version="0.1.0")
    report("initialize 有响应", isinstance(info, dict))
    report("handshake_complete = True", session.handshake_complete)
    result = client.request(A.THREAD_START, {
        "cwd": client.workdir, "sandbox": "read-only", "approvalPolicy": "never"})
    thread = result.get("thread") or {}
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    report("thread id 非空", isinstance(thread_id, str) and bool(thread_id))
    report("framing 为 JSONL", client.detected_framing == A.FRAMING_LINES)
    report("无异常服务端活动", not client.violation and not client.disconnected)
    check_transcript(client.sent, "real")
    report("进程仍存活", child.alive and child.returncode is None)
    return thread_id


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def report(check: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {check}" + (f" -- {detail}" if detail else ""))
    if not ok:
        raise CheckFailed(f"{check}: {detail}")


def print_plan() -> None:
    print("=" * 78)
    print("S1: APP-SERVER HANDSHAKE VERIFICATION")
    print("=" * 78)
    print("  目的      : 确认 stdio 传输、framing（官方 JSONL）、握手两步（initialize +")
    print("              initialized）、以及 thread/start 是否被接受")
    print("  允许      : 启动 1 个 app-server 进程；最多创建 1 个**空** thread；然后停止")
    print("  禁止      : 任何 turn/start（因此没有任何模型回合、不消耗配额）、")
    print("              thread/fork、命令执行、任何审批请求、第二个 thread")
    print("  真实回合  : 0 次（S1 不产生模型输出）")
    print("  进程      : 仅使用固定路径 + sha256/size 校验通过的可执行文件")
    print("  证据      : stderr 留在被忽略的本地目录；仅消息类型/id 写入 result.json")
    print("  失败处理  : 第一项检查失败即停止；不重试、不放宽")
    print("  清理范围  : 只终止**本脚本启动**的进程，只删除 var/appserver-s1/<本次运行>/")
    print("              **长期 CODEX_HOME 与用户 ~/.codex 一律保留**（含登录凭证）")
    print()


def check_transcript(transcript: list[dict], mode: str) -> None:
    """Assert over what we actually sent: no turns, no commands, at most one thread."""
    methods = [str(m.get("method") or "<response>") for m in transcript]
    for bad in FORBIDDEN_METHODS:
        report(f"未发送 {bad}", bad not in methods, f"methods={sorted(set(methods))}")
    approvals = [m for m in methods if any(s in m for s in APPROVAL_METHOD_SUBSTRINGS)]
    report("未发送任何审批/命令相关请求", not approvals, f"found={approvals}")
    threads = [m for m in methods if m == A.THREAD_START]
    report("最多创建一个 thread", len(threads) <= 1, f"thread/start x{len(threads)}")
    report("握手包含 initialize 请求", A.INITIALIZE in methods)
    report("握手包含 initialized 通知", A.INITIALIZED in methods)
    if mode == "real":
        report("没有第二个 thread 请求", len(threads) <= 1, f"thread/start x{len(threads)}")


# --------------------------------------------------------------------------- #
# stub mode: in-process, no Codex, and two negative controls
# --------------------------------------------------------------------------- #


def run_stub() -> int:
    print("STUB MODE - scripted server in-process; no Codex process, no network\n")

    # --- positive: the full allowed sequence ---------------------------------
    server, streams = S.make_pair("happy")
    transport = _QueueTransport(streams["reader"], streams["writer"])
    client = A.AppServerClient(transport, request_timeout=5.0)
    session = A.AppServerSession(client)
    client.start()
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        print("[1] 正常路径：握手 + 一个空 thread")
        info = session.initialize(client_name="agent-hub-s1", version="0.1.0")
        report("initialize 返回 serverInfo", "serverInfo" in info, json.dumps(info))
        report("handshake_complete = True", session.handshake_complete is True)
        thread_id = session.start_thread(cwd=os.path.join(RUN_ROOT, "stub-workdir"))
        report("thread/start 返回 thread id", bool(thread_id), thread_id)
        _wait_until(lambda: server.initialized_seen, timeout=2.0)
        report("服务端收到了 initialized 通知", server.initialized_seen is True)
        report("服务端未记录握手违规", server.handshake_violations == [],
               f"{server.handshake_violations}")
        check_transcript(server.received, "stub")
    finally:
        server.stop()
        client.close()

    # --- negative control 1: request before the handshake ---------------------
    print("\n[2] 反面对照：握手前请求 thread/start")
    server2, streams2 = S.make_pair("happy")
    client2 = A.AppServerClient(_QueueTransport(streams2["reader"], streams2["writer"]),
                               request_timeout=5.0)
    session2 = A.AppServerSession(client2)
    client2.start()
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        refused = False
        try:
            session2.start_thread()
        except A.HandshakeRequired:
            refused = True
        report("本地拒绝（HandshakeRequired）", refused)
        report("该请求从未上线", server2.received_methods() == [],
               f"received={server2.received_methods()}")
    finally:
        server2.stop()
        client2.close()

    # --- negative control 2: initialize without the notification --------------
    print("\n[3] 反面对照：只发 initialize，不发 initialized")
    server3, streams3 = S.make_pair("happy")
    client3 = A.AppServerClient(_QueueTransport(streams3["reader"], streams3["writer"]),
                               request_timeout=5.0)
    client3.start()
    threading.Thread(target=server3.serve_forever, daemon=True).start()
    try:
        client3.request(A.INITIALIZE, {"clientInfo": {"name": "s1", "version": "0"}})
        code = None
        try:
            client3.request(A.THREAD_START, {})
        except A.AppServerResponseError as exc:
            code = exc.code
        report("服务端拒绝（-32002）", code == -32002, f"code={code}")
        report("服务端记录了违规", server3.handshake_violations == ["thread/start"],
               f"{server3.handshake_violations}")
    finally:
        server3.stop()
        client3.close()

    print("\nSTUB MODE COMPLETE - 正面对照通过，两个反面对照都被拒绝")
    print("（这验证的是**脚本与客户端**的顺序约束；真实 app-server 行为仍未验证）")
    return 0


class _QueueTransport:
    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self._writer = writer

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    def close(self) -> None:
        self._writer.close()
        self.reader.close()


def _wait_until(predicate, *, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# real mode: one pinned process, handshake, at most one empty thread
# --------------------------------------------------------------------------- #


def build_real_environment(codex_home: str) -> dict:
    from agenthub_kernel import config

    env = dict(os.environ)
    env["CODEX_HOME"] = codex_home
    proxy = os.environ.get(config.ENV_PROXY) or "http://127.0.0.1:8090"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[key] = proxy
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = "localhost,127.0.0.1"
    ca = os.environ.get(config.ENV_CA_BUNDLE) or config._default_ca_bundle()
    if ca:
        report("CA bundle 可读", os.path.isfile(ca))
        env["CODEX_CA_CERTIFICATE"] = ca
    return env


def preflight(codex_home: str, identity: "tuple[str, str, int] | None" = None) -> tuple[list[str], dict]:
    from agenthub_kernel import config

    # This probe validates the reviewed binary, never an environment-selected
    # replacement. A new version requires a separately reviewed identity update,
    # passed explicitly by the probe that reviewed it (S1 keeps its own).
    codex_path, expected_sha, expected_size = identity or (S1_CODEX_PATH, S1_CODEX_SHA256, S1_CODEX_SIZE)
    for key, expected in ((config.ENV_CODEX_PATH, codex_path),
                          (config.ENV_CODEX_SHA256, expected_sha),
                          (config.ENV_CODEX_SIZE, str(expected_size))):
        report("固定身份未被覆盖: " + key,
               os.environ.get(key, str(expected)) == str(expected))
    report("固定可执行文件存在", os.path.isfile(codex_path), codex_path)
    report("size 与固定值一致", os.path.getsize(codex_path) == expected_size)
    report("sha256 与固定值一致", sha256(codex_path) == expected_sha)
    # The home must be outside the repository AND outside the workspace that holds
    # it: a credential beside the repo is one careless `git init` away from being
    # committed.
    #
    # The workspace used to be written as os.path.join(REPO, "..", ".."), which
    # normalises to the DRIVE ROOT (E:\) - so `_inside()` was true for *every* path
    # on the repo's drive and this check could never pass there, even for a
    # perfectly good location like %TEMP% on the same drive. One level up is what
    # was meant.
    workspace = os.path.dirname(os.path.abspath(REPO))
    report("CODEX_HOME 位于仓库之外", not _inside(codex_home, REPO)
           and not _inside(codex_home, workspace))
    report("CODEX_HOME 是目录", os.path.isdir(codex_home))
    report("CODEX_HOME 有登录凭证", os.path.isfile(os.path.join(codex_home, "auth.json")))
    writable, detail = config.codex_home_write_check(codex_home)
    report("本进程可写所选 CODEX_HOME", writable, detail)
    return [codex_path, "app-server", "--stdio"], build_real_environment(codex_home)


def run_real(codex_home: str, clean: bool) -> int:
    return run_process_probe(codex_home=codex_home, clean=clean, stub=False)


def run_process_probe(*, codex_home: str = "", clean: bool = False, stub: bool = True) -> int:
    os.makedirs(RUN_ROOT, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix="s1-stub-" if stub else "s1-real-", dir=RUN_ROOT)
    workdir = os.path.join(run_dir, "thread-cwd")
    os.mkdir(workdir)
    stderr_path = os.path.join(run_dir, "appserver-stderr.log")
    result = {"mode": "process-stub" if stub else "real", "passed": False,
              "process_started": False, "process_stopped": None, "thread_id": None,
              "sent": [], "received": [], "error_type": None}
    child = client = None
    protocol_ok = False
    try:
        if stub:
            argv = [sys.executable, os.path.join(REPO, "tools", "demo", "stub_appserver.py")]
            env = dict(os.environ)
            # The stub has no authentication path; isolate all incidental state.
            env["CODEX_HOME"] = workdir
        else:
            argv, env = preflight(codex_home)
        child = A.StdioTransport(argv, env=env, cwd=workdir, stderr_path=stderr_path)
        result.update(process_started=True, pid=child.pid)
        client = S1Client(child, workdir=workdir)
        result["thread_id"] = exercise_s1(client, child)
        protocol_ok = True
    except (CheckFailed, A.AppServerError, OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, CheckFailed):
            result["failed_check"] = str(exc)
        # Do not print peer error text: it can contain user content or secrets.
        print(f"S1 FAILED: {type(exc).__name__}; no retry")
    finally:
        if child is not None:
            try:
                if client is not None:
                    client.close()
                else:
                    child.close()
                result["process_stopped"] = not child.alive and child.returncode is not None
                result["returncode"] = child.returncode
            except (A.AppServerError, OSError, TimeoutError) as exc:
                result["process_stopped"] = False
                result["cleanup_error_type"] = type(exc).__name__
            print(f"Owned process stopped: {result['process_stopped']}")
        if client is not None:
            result["sent"] = client.sent
            result["received"] = client.received
            result["protocol_violation"] = client.violation
            result["reader_error"] = type(client._error).__name__ if client._error else None
        result["passed"] = bool(protocol_ok and result["process_stopped"]
                                and not result.get("protocol_violation")
                                and not result.get("reader_error"))
        with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"Evidence: {run_dir}")
        print("S1 PASSED" if result["passed"] else "S1 FAILED")
        print("No model turn sent. Long-term homes are preserved; Codex may write runtime state there.")
        if clean and result["passed"]:
            _clean_own_run_dir(run_dir, protected_home=codex_home)
        elif clean:
            print("Failure evidence retained; cleanup not attempted.")
    return 0 if result["passed"] else 1


def _inside(path: str, root: str) -> bool:
    p, r = (os.path.normcase(os.path.realpath(v)) for v in (path, root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different Windows drives
        return False


def _clean_own_run_dir(run_dir: str, *, protected_home: str = "") -> bool:
    root = os.path.realpath(RUN_ROOT)
    target = os.path.realpath(run_dir)
    if not _inside(target, root) or os.path.normcase(target) == os.path.normcase(root):
        print("REFUSING cleanup outside a run directory")
        return False
    for home in (protected_home, os.environ.get("LOCALAPPDATA"), os.environ.get("USERPROFILE")):
        if home and (_inside(target, home) or _inside(home, target)):
            print("REFUSING cleanup overlapping a protected home")
            return False
    # Resolving first catches links/junctions escaping the run root. Never cross
    # a directory reparse point inside the owned run either.
    for directory, children, _ in os.walk(target, followlinks=False):
        for name in [directory, *(os.path.join(directory, c) for c in children)]:
            import stat
            attrs = getattr(os.lstat(name), "st_file_attributes", 0)
            if os.path.islink(name) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                print("REFUSING cleanup through a link/junction")
                return False
    shutil.rmtree(target)
    return not os.path.exists(target)


def main() -> int:
    ap = argparse.ArgumentParser(description="S1: app-server handshake verification.")
    ap.add_argument("--mode", choices=["stub", "process-stub", "real"], default="stub")
    ap.add_argument("--list", action="store_true", help="print the plan and exit")
    ap.add_argument("--codex-home", default=None)
    ap.add_argument("--i-understand-this-starts-an-app-server-process", action="store_true",
                    help="required for --mode real")
    ap.add_argument("--clean", action="store_true",
                    help="after the run, delete ONLY this run's own directory "
                         "(never CODEX_HOME, never ~/.codex); default keeps evidence")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    print_plan()
    if args.list:
        print("Nothing was executed.")
        return 0

    if args.mode == "process-stub":
        return run_process_probe(clean=args.clean)

    if args.mode == "stub":
        os.makedirs(RUN_ROOT, exist_ok=True)
        return run_stub()

    if not args.i_understand_this_starts_an_app_server_process:
        print("REFUSING: --mode real starts a real app-server process.")
        print("It sends NO prompt and creates at most one EMPTY thread, but it does start")
        print("the pinned executable; pass --i-understand-this-starts-an-app-server-process.")
        return 2
    home = os.path.abspath(args.codex_home or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AgentHub", "codex-home"))
    return run_real(home, args.clean)


if __name__ == "__main__":
    sys.exit(main())
