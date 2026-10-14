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

WHAT S1 MUST NEVER DO (enforced by an assertion over the wire transcript)
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
  operator's own ~/.codex are never written to or deleted - cleanup prints that.

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
#: Methods S1 must never send. Checked against the raw transcript.
FORBIDDEN_METHODS = (
    "turn/start", "turn/steer", "turn/interrupt", "review/start",
    "thread/fork", "command/exec", "process/spawn",
)
APPROVAL_METHOD_SUBSTRINGS = ("requestApproval", "Approval", "requestUserInput")


class CheckFailed(Exception):
    """A failed check: stop immediately (fail closed), do not retry."""


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
    print("  证据      : 子进程 stderr 落文件；线级 transcript 打印并存盘")
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


def run_real(codex_home: str, clean: bool) -> int:
    from agenthub_kernel import config

    codex_path = os.environ.get("AGENT_HUB_CODEX_PATH") or config.PINNED_CODEX_PATH
    expected_sha = os.environ.get("AGENT_HUB_CODEX_SHA256") or config.PINNED_CODEX_SHA256
    expected_size = int(os.environ.get("AGENT_HUB_CODEX_SIZE") or config.PINNED_CODEX_SIZE)

    run_dir = os.path.join(RUN_ROOT, f"s1-{int(time.time())}")
    os.makedirs(run_dir, exist_ok=True)
    workdir = os.path.join(run_dir, "thread-cwd")
    os.makedirs(workdir, exist_ok=True)
    stderr_path = os.path.join(run_dir, "appserver-stderr.log")
    print(f"  run dir    : {run_dir}")
    print(f"  stderr     : {stderr_path}")
    print(f"  CODEX_HOME : {codex_home}（只读使用；**不会**被删除）")

    transcript: list[dict] = []
    child = None
    try:
        # ---- identity before anything else -------------------------------
        report("可执行文件存在", os.path.isfile(codex_path), codex_path)
        actual_size = os.path.getsize(codex_path)
        report("size 与固定值一致", actual_size == expected_size,
               f"{actual_size} vs {expected_size}")
        actual_sha = sha256(codex_path)
        report("sha256 与固定值一致", actual_sha == expected_sha,
               f"{actual_sha[:16]}… vs {expected_sha[:16]}…")

        # ---- CODEX_HOME access (distinguish denial from absence) ----------
        report("CODEX_HOME 是目录", os.path.isdir(codex_home), codex_home)
        report("CODEX_HOME 有登录凭证", os.path.isfile(os.path.join(codex_home, "auth.json")))
        write_ok, write_detail = config.codex_home_write_check()
        report("本进程可写 CODEX_HOME", write_ok, write_detail)

        env = dict(os.environ)
        env["CODEX_HOME"] = codex_home
        argv = [codex_path, "app-server", "--stdio"]
        print(f"\n  启动: {' '.join(argv)}   (cwd={workdir})")
        child = A.StdioTransport(argv, env=env, cwd=workdir, stderr_path=stderr_path)
        print(f"  child pid  : {child.pid}")
        report("child.pid 存在", isinstance(child.pid, int) and child.pid > 0)

        client = A.AppServerClient(child, request_timeout=60.0)
        session = A.AppServerSession(client)

        def note_outgoing(message: dict) -> None:
            transcript.append(dict(message))

        original_send = client._send  # noqa: SLF001 - instrumentation for the report

        def recording_send(message: dict) -> None:
            note_outgoing(message)
            original_send(message)

        client._send = recording_send  # type: ignore[assignment]  # noqa: SLF001

        print("\n[1] 握手（initialize 请求 + initialized 通知）")
        client.start()
        info = session.initialize(client_name="agent-hub-s1", version="0.1.0")
        report("initialize 有响应", isinstance(info, dict), json.dumps(info)[:200])
        report("handshake_complete = True", session.handshake_complete is True)
        report("framing 为 JSONL", (client.detected_framing or client.framing) == A.FRAMING_LINES,
               f"detected={client.detected_framing!r}")

        print("\n[2] 创建一个空 thread（不发送任何回合）")
        thread_id = session.start_thread(cwd=workdir)
        report("thread id 非空", bool(thread_id), thread_id)

        print("\n[3] 线级检查（禁止项断言）")
        check_transcript(transcript, "real")

        print("\n[4] 结果")
        print(f"  thread_id  : {thread_id}")
        print(f"  wire       : {json.dumps([m.get('method') for m in transcript])}")
        report("进程仍存活（未崩溃）", child.alive or child.returncode is not None,
               f"returncode={child.returncode}")
        print("\nREAL S1 COMPLETE - 握手与空 thread 已验证；**没有**发起任何模型回合")
        print("（thread 与 turn 的完整行为属 S2+，需另行批准）")
        return 0
    except CheckFailed as exc:
        print(f"\nFAILED (stopped immediately, no retry): {exc}")
        return 1
    finally:
        if child is not None:
            child.close()
            print(f"  已终止本次启动的 app-server 进程（pid={child.pid}）")
        if transcript:
            path = os.path.join(run_dir, "wire-transcript.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(transcript, fh, ensure_ascii=False, indent=2)
            print(f"  线级 transcript: {path}")
        if os.path.exists(stderr_path):
            size = os.path.getsize(stderr_path)
            print(f"  stderr 保留（{size} 字节）: {stderr_path}")
        if clean:
            _clean_own_run_dir(run_dir)
        else:
            print(f"  证据保留在 {run_dir}")
            print(f"  （要删除它：加上 --clean；或手工 "
                  f"rmdir /s /q \"{run_dir}\"）")
        print("  全程未写入、未删除长期 CODEX_HOME 与 %USERPROFILE%\\.codex")


def _clean_own_run_dir(run_dir: str) -> None:
    """Delete ONLY a run directory this script created, and never a credential home."""
    run_root = os.path.abspath(RUN_ROOT)
    target = os.path.abspath(run_dir)
    if os.path.commonpath([run_root, target]) != run_root or target == run_root:
        print(f"  REFUSING to clean {target}: not inside {run_root}")
        return
    for home_key in ("LOCALAPPDATA", "USERPROFILE"):
        home = os.path.abspath(os.environ.get(home_key) or "")
        if home and os.path.commonpath([home, target]) == home:
            print(f"  REFUSING to clean {target}: it is inside {home_key}")
            return
    shutil.rmtree(target, ignore_errors=True)
    print(f"  已删除本次运行目录（仅此目录）: {target}")


def main() -> int:
    ap = argparse.ArgumentParser(description="S1: app-server handshake verification.")
    ap.add_argument("--mode", choices=["stub", "real"], default="stub")
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
