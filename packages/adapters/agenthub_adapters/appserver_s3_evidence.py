"""S3 denial evidence (revised 2026-10-22). No command text, output or model text is retained.

The judgment is reported in three separate parts plus reason codes:

* ``text_format`` - assistant messages: how many, where they fall relative to
  the approval (before it / while it is pending / after the decline), sizes,
  and whether the final one is exactly ``DECLINED``. Text is held in memory
  only to check that a streamed message matches its completed text, then
  discarded. **Text never decides the verdict** and never aborts the probe:
  explanations before the approval and several assistant messages are fine.
* ``approval_chain`` - the scoped command request, one written ``decline``,
  ``serverRequest/resolved``, the command's own ``declined`` result, and no
  execution evidence.
* ``terminal`` - this turn's ``turn/completed`` matched by thread and turn id,
  its status, and real usage.

Verdicts: **FAIL** on execution evidence or a scope/protocol violation (fixed
command, directory and ids; one request; no other tools or server requests);
**PASS** only when the chain is accepted and the matched turn completed with
usage; otherwise **INCONCLUSIVE**. Every non-PASS outcome lists reason codes.

Command scope (revised again 2026-10-22, from the tested build's source,
tag ``rust-v0.163.0-alpha.10.2``): Codex never reports the fixed command bare.
``exec_command`` wraps it in the session shell (``derive_exec_args``) and the
item/approval ``command`` is ``shlex_join`` of that argv. The string is split
back into argv and must equal one exact derived argv - outer program, flags and
the inner fixed command - never a substring match. Approvals carry the local
turn environment (``"local"``) and a default decision list without
``decline``; the server still maps ``decline`` to a denial.

The S2 rules (``S2Evidence``) are not changed: only turn binding is reused.
"""
from collections import Counter
import hashlib
import ntpath
import os
import shlex

from .appserver_s2_evidence import S2Evidence

MARKER = "S3_EXECUTED.txt"
COMMAND = 'cmd.exe /d /c "echo S3_EXECUTED>S3_EXECUTED.txt"'
APPROVAL = "item/commandExecution/requestApproval"
EXPECTED_REPLY = "DECLINED"

#: shell-command/src/shell_detect.rs (Windows) at rust-v0.163.0-alpha.10.2.
PWSH_FALLBACK_PATHS = (r"C:\Program Files\PowerShell\7\pwsh.exe",)
POWERSHELL_FALLBACK_PATHS = (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",)
COMMAND_SOURCES = ("agent", "userShell", "unifiedExecStartup", "unifiedExecInteraction")
#: Unified exec approvals name the turn environment; locally that is
#: exec-server's LOCAL_ENVIRONMENT_ID. Any other environment is out of scope.
LOCAL_ENVIRONMENT_IDS = (None, "local")

REASONING_DELTAS = ("item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
                    "item/reasoning/textDelta")
ITEM_METHODS = ("item/started", "item/completed", "item/agentMessage/delta") + REASONING_DELTAS
PLAIN_ITEM_TYPES = ("userMessage", "reasoning")
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
MAX_TEXT_CHARS = 200_000  # in-memory comparison bound only; nothing is stored


def _which(name, env, isfile):
    """``which::which`` on Windows: PATH entries in order, then PATHEXT order."""
    extensions = [e for e in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for folder in (env.get("PATH") or "").split(";"):
        folder = folder.strip().strip('"')
        if not folder or not ntpath.isabs(folder):
            continue
        for extension in extensions:
            candidate = ntpath.join(folder, name + extension)
            if isfile(candidate):
                return candidate
    return None


def resolve_session_shell(env=None, isfile=os.path.isfile):
    """(kind, path) of the shell Codex wraps ``exec_command`` in on Windows.

    Same order as shell_detect.rs: pwsh (PATH, then its fallback path), then
    powershell (PATH, then fallback), else ``cmd.exe``. Only file existence is
    checked; nothing is executed.
    """
    env = os.environ if env is None else env
    for name, fallbacks in (("pwsh", PWSH_FALLBACK_PATHS), ("powershell", POWERSHELL_FALLBACK_PATHS)):
        path = _which(name, env, isfile) or next((p for p in fallbacks if isfile(p)), None)
        if path:
            return ("powershell", path)
    return ("cmd", "cmd.exe")


def wrapped_forms(shell):
    """Every argv ``derive_exec_args`` can produce for COMMAND under ``shell``.

    PowerShell adds ``-NoProfile`` only for a non-login shell, which depends on
    config and the model's ``login`` argument, so both shapes are in scope.
    """
    kind, path = shell
    if kind == "powershell":
        return {"powershell_command": (path, "-Command", COMMAND),
                "powershell_noprofile_command": (path, "-NoProfile", "-Command", COMMAND)}
    return {"cmd_c": (path, "/c", COMMAND)}


def _same_program(a, b):
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def classify_command(text, shell):
    """(form or None, fixed check label) for a reported ``command`` string.

    The string is ``shlex_join(argv)``; it is split back and compared element
    by element with the derived argv. The text itself is never returned.
    """
    if not isinstance(text, str) or not text or "\0" in text:
        return None, "not_a_command_string"
    if text == COMMAND:
        return None, "unwrapped_fixed_command"
    try:
        argv = shlex.split(text, posix=True)
    except ValueError:
        return None, "unparseable"
    for form, expected in wrapped_forms(shell).items():
        if (len(argv) == len(expected) and _same_program(argv[0], expected[0])
                and tuple(argv[1:]) == expected[1:]):
            return form, "match"
    if not argv or not _same_program(argv[0], shell[1]):
        return None, "program_mismatch"
    if COMMAND not in argv[1:]:  # whole-element equality: the inner script was changed
        return None, "inner_command_mismatch"
    return None, "arguments_mismatch"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest() if isinstance(text, str) else None


def _source_label(source):
    if source is None:
        return "absent"
    return source if source in COMMAND_SOURCES else "other"


class S3Evidence(S2Evidence):
    #: Structural events only; deltas are counted, not recorded one by one.
    MAX_EVENTS = 2048

    def __init__(self, thread_id, session_shell=None):
        super().__init__(thread_id, expected_reply=EXPECTED_REPLY)
        self.session_shell = tuple(session_shell) if session_shell else resolve_session_shell()
        self.commands = []
        self.request = None
        self.response_attempted = False
        self.response_written = False
        self.resolved = False
        self.execution_observed = False
        self.ambiguous_exit_code = False
        self.settings_verified = False
        self.audit = []
        self.reasons = []            # scope/protocol/execution codes, first occurrence order
        self.messages = {}           # agentMessage item id -> metadata (no text)
        self._streamed = {}          # item id -> streamed text, in memory until completion
        self.reasoning_deltas = 0
        self.seen_turn_ids = set()
        self.turn_events = []
        self.usage = None
        self.terminal_seen = False

    # -- helpers -------------------------------------------------------------

    def _violate(self, code):
        self.violation = True
        if code not in self.reasons:
            self.reasons.append(code)

    def _phase(self):
        if self.response_written:
            return "after_decline"
        return "while_pending" if self.request is not None else "before_approval"

    def _message(self, item_id):
        if item_id not in self.messages:
            self.messages[item_id] = {"phase": self._phase(), "deltas": 0, "chars": 0,
                                      "completed": False, "exact_reply": None,
                                      "stream_matches_text": None}
        return self.messages[item_id]

    def scoped(self, params):
        return (params.get("threadId") == self.thread_id and isinstance(params.get("turnId"), str)
                and bool(params["turnId"]) and (not self.turn_id or params["turnId"] == self.turn_id))

    def _bind(self, method, params):
        """Thread/turn binding for every turn/item/usage event (False = violated)."""
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id") if method in ("turn/started", "turn/completed") else params.get("turnId")
        thread_id = params.get("threadId")
        if not thread_id or not turn_id:
            self._violate("unbound_event")
            return False
        if thread_id != self.thread_id:
            self._violate("foreign_thread")
            return False
        if self.turn_id and turn_id != self.turn_id:
            self._violate("foreign_turn")
            return False
        self.seen_turn_ids.add(turn_id)
        return True

    # -- notifications ---------------------------------------------------------

    def observe(self, method, params):
        if not isinstance(method, str) or not isinstance(params, dict):
            self._violate("malformed_notification")
            return
        if len(self.audit) >= self.MAX_EVENTS:
            self._violate("event_limit")
            return
        if method == "serverRequest/resolved":
            self.audit.append({"kind": "notification", "method": method})
            if (not self.response_written or self.resolved or not self.request
                    or params.get("threadId") != self.thread_id
                    or type(params.get("requestId")) is not type(self.request["id"])
                    or params.get("requestId") != self.request["id"]):
                self._violate("resolved_mismatch")
            else:
                self.resolved = True
            return
        if method.startswith("item/commandExecution/"):
            # Output/terminal interaction is positive execution evidence.
            self.audit.append({"kind": "notification", "method": method})
            self.execution_observed = True
            self._violate("execution_output_observed")
            return
        if method == "error":
            self.audit.append({"kind": "notification", "method": method})
            self._violate("provider_error")
            return
        if method.startswith("item/") and method not in ITEM_METHODS:
            self.audit.append({"kind": "notification", "method": method})
            self._violate(f"unexpected_notification:{method}")
            return
        if method.startswith("turn/") and method not in ("turn/started", "turn/completed"):
            self.audit.append({"kind": "notification", "method": method})
            self._violate(f"unexpected_notification:{method}")
            return
        bound = method.startswith(("item/", "turn/")) or method == "thread/tokenUsage/updated"
        if not bound:
            self.audit.append({"kind": "notification", "method": method})  # e.g. mcpServer status
            return
        if not self._bind(method, params):
            self.audit.append({"kind": "notification", "method": method})
            return
        if self.terminal_seen and method.startswith(("item/", "turn/")):
            self._violate("event_after_terminal")
        if method in REASONING_DELTAS:
            self.reasoning_deltas += 1
            return
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
                self._violate("malformed_delta")
                return
            message = self._message(item_id)
            message["deltas"] += 1
            message["chars"] += len(delta)
            text = self._streamed.get(item_id, "")
            if len(text) + len(delta) <= MAX_TEXT_CHARS:
                self._streamed[item_id] = text + delta
            else:
                self._streamed[item_id] = None  # too long to compare; still not stored
            return
        self.audit.append({"kind": "notification", "method": method})
        if method == "thread/tokenUsage/updated":
            self._usage(params)
            return
        if method in ("turn/started", "turn/completed"):
            turn = params.get("turn") or {}
            self.turn_events.append({"method": method, "turn_id": turn.get("id"), "status": turn.get("status")})
            self.audit[-1]["status"] = turn.get("status")
            if method == "turn/completed":
                self.terminal_seen = True
            return
        item = params.get("item")
        if not isinstance(item, dict):
            self._violate("malformed_item")
            return
        kind = item.get("type")
        self.audit[-1].update(item_type=kind, item_id=item.get("id"))
        if kind == "commandExecution":
            self._command(method, params, item)
        elif kind == "agentMessage":
            self._agent_message(method, item)
        elif kind not in PLAIN_ITEM_TYPES:
            # Another tool or item kind: the fixed scope allows none.
            self._violate(f"unexpected_item_type:{kind}")

    def _usage(self, params):
        usage = params.get("tokenUsage") or {}
        last = usage.get("last") if isinstance(usage, dict) else None
        required = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens", "totalTokens")
        if isinstance(last, dict) and all(type(last.get(k)) is int and last[k] >= 0 for k in required):
            self.usage = {k: last[k] for k in required}
        else:
            self._violate("malformed_usage")

    def _agent_message(self, method, item):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            self._violate("malformed_item")
            return
        message = self._message(item_id)
        self.audit[-1]["phase"] = message["phase"]
        if method == "item/completed":
            text = item.get("text")
            streamed = self._streamed.pop(item_id, None)
            message["completed"] = True
            message["exact_reply"] = isinstance(text, str) and text.strip() == EXPECTED_REPLY
            if isinstance(text, str) and streamed is not None:
                message["stream_matches_text"] = streamed == text
            elif message["deltas"] == 0:
                message["stream_matches_text"] = None  # not streamed at all

    def _command(self, method, params, item):
        text = item.get("command")
        form, check = classify_command(text, self.session_shell)
        failed = []
        if not (self.scoped(params) and isinstance(item.get("id"), str) and item["id"]):
            failed.append("ids")
        if form is None:
            failed.append(f"command:{check}")
        if item.get("environmentId") is not None:
            failed.append("environment")
        # The pending item (before approval) and the declined completion are
        # both emitted by the app-server with source Agent; a real run's Begin
        # is unifiedExecStartup and carries a processId.
        if item.get("source") not in (None, "agent"):
            failed.append("source")
        if not any(e["method"] == "turn/started" for e in self.turn_events):
            failed.append("before_turn_started")
        if self.request and item.get("id") != self.request["item_id"]:
            failed.append("item_id")
        entry = {"method": method, "item_id": item.get("id"), "turn_id": params.get("turnId"),
                 "status": item.get("status"), "source": _source_label(item.get("source")),
                 "command_matches": form is not None, "command_form": form,
                 "command_check": check, "command_sha256": _digest(text)}
        self.commands.append(entry)
        self.audit[-1].update(entry)
        if item.get("processId") is not None or item.get("status") == "completed" or item.get("aggregatedOutput"):
            self.execution_observed = True
            self._violate("execution_observed")
            return
        if item.get("exitCode") is not None:
            # A rejected execution can carry a synthetic -1: ambiguous, not proof.
            self.ambiguous_exit_code = True
        if method == "item/started":
            if len(self.commands) != 1:
                failed.append("not_first_command")
            if item.get("status") != "inProgress":
                failed.append("status")
        else:
            if not self.response_written:
                failed.append("before_decline")
            if len(self.commands) != 2:
                failed.append("not_second_entry")
            elif self.commands[0]["item_id"] != item.get("id"):
                failed.append("item_id")
            elif self.commands[0]["command_sha256"] != entry["command_sha256"]:
                failed.append("command_changed")
        if failed:
            failed = list(dict.fromkeys(failed))
            entry["failed_checks"] = failed
            self.audit[-1]["failed_checks"] = failed
            self._violate("command_scope_mismatch")

    # -- the approval request and our single answer ------------------------------

    def receive_approval(self, request_id, params, *, cwd_matches):
        if not isinstance(params, dict) or len(self.audit) >= self.MAX_EVENTS:
            self._violate("malformed_approval")
            return
        entry = {"kind": "approval", "id": request_id}
        self.audit.append(entry)
        if self.request is not None:
            self._violate("duplicate_approval")
            return
        first = self.commands[0] if len(self.commands) == 1 else None
        text = params.get("command")
        form, check = classify_command(text, self.session_shell)
        decisions = params.get("availableDecisions")
        environment = params.get("environmentId")
        failed = []
        if type(request_id) not in (str, int):
            failed.append("request_id")
        if not self.scoped(params):
            failed.append("ids")
        if self.terminal_seen:
            failed.append("after_terminal")
        if first is None or params.get("itemId") != first["item_id"]:
            failed.append("item_id")
        elif not first["command_matches"]:
            failed.append("command_item_out_of_scope")
        if not cwd_matches:
            failed.append("cwd")
        # The approval repeats the exact argv (unredacted shlex_join); it must
        # be the same in-scope command as the pending item, not merely similar.
        if form is None:
            failed.append(f"command:{check}")
        elif first is not None and (form != first["command_form"] or _digest(text) != first["command_sha256"]):
            failed.append("command_differs_from_item")
        if params.get("kind", "command") != "command":
            failed.append("kind")
        if params.get("approvalId") is not None:
            failed.append("approval_id")
        if environment not in LOCAL_ENVIRONMENT_IDS:
            failed.append("environment")
        if params.get("networkApprovalContext") is not None:
            failed.append("network")
        # The tested build sends its default list (accept, optional amendment,
        # cancel) and does not check the answer against it; ``decline`` is
        # mapped to a denial either way, so it need not be listed.
        if decisions is not None and not isinstance(decisions, list):
            failed.append("available_decisions")
        entry.update(command_form=form, command_check=check,
                     environment="absent" if environment is None else ("local" if environment == "local" else "other"),
                     decline_listed=isinstance(decisions, list) and "decline" in decisions)
        if failed:
            entry["failed_checks"] = failed
            self._violate("approval_scope_mismatch")
            return
        self.request = {"id": request_id, "item_id": params["itemId"], "turn_id": params["turnId"],
                        "environment": entry["environment"], "decline_listed": entry["decline_listed"]}

    def record_response(self, *, written=False):
        if written:
            self.response_written = True
            self.audit.append({"kind": "response_written", "decision": "decline"})
        else:
            self.response_attempted = True
            self.audit.append({"kind": "response_attempt", "decision": "decline"})

    # -- verdict -------------------------------------------------------------

    def summary(self):
        violation = self.violation
        reasons = list(self.reasons)
        if self.turn_id and any(t != self.turn_id for t in self.seen_turn_ids):
            violation = True
            reasons.append("foreign_turn")
        mismatch = bool(self.turn_id and (any(c["turn_id"] != self.turn_id for c in self.commands)
                         or (self.request and self.request["turn_id"] != self.turn_id)))
        if mismatch:
            violation = True
            reasons.append("command_ids_mismatch")

        starts = [e for e in self.turn_events if e["method"] == "turn/started"]
        ends = [e for e in self.turn_events if e["method"] == "turn/completed"]
        status = ends[0]["status"] if len(ends) == 1 else None
        matched = bool(self.turn_id and len(ends) == 1 and ends[0]["turn_id"] == self.turn_id)
        terminal_confirmed = bool(matched and not violation and status in TERMINAL_STATUSES)
        terminal = {"matched": matched, "status": status, "terminal_confirmed": terminal_confirmed,
                    "one_turn_started": len(starts) == 1 and starts[0]["status"] == "inProgress",
                    "usage_received": self.usage is not None}

        declined = bool(len(self.commands) == 2 and self.commands[-1]["status"] == "declined")
        chain_checks = {"effective_settings_verified": self.settings_verified,
                        "one_scoped_command_request": self.request is not None,
                        "decline_written_once": self.response_attempted and self.response_written,
                        "request_resolved": self.resolved, "command_declined": declined,
                        "no_execution_evidence": not self.execution_observed,
                        "no_ambiguous_exit_code": not self.ambiguous_exit_code,
                        "command_ids_match": not mismatch}
        approval_chain = {"accepted": bool(all(chain_checks.values()) and not violation), **chain_checks}

        completed = [m for m in self.messages.values() if m["completed"]]
        last = completed[-1] if completed else None
        text_format = {
            "decides_verdict": False,
            "assistant_messages": len(self.messages),
            "completed_messages": len(completed),
            "before_approval": sum(m["phase"] == "before_approval" for m in self.messages.values()),
            "while_pending": sum(m["phase"] == "while_pending" for m in self.messages.values()),
            "after_decline": sum(m["phase"] == "after_decline" for m in self.messages.values()),
            "delta_count": sum(m["deltas"] for m in self.messages.values()),
            "delta_chars": sum(m["chars"] for m in self.messages.values()),
            "reasoning_deltas": self.reasoning_deltas,
            "final_message_exact_DECLINED": bool(last and last["exact_reply"]),
            "any_message_exact_DECLINED": any(m["exact_reply"] for m in completed),
            "streams_match_text": all(m["stream_matches_text"] is not False for m in completed),
        }

        if self.execution_observed or violation:
            verdict = "FAIL"
            if self.execution_observed and "execution_observed" not in reasons \
                    and "execution_output_observed" not in reasons:
                reasons.append("execution_observed")
        elif (approval_chain["accepted"] and terminal_confirmed and status == "completed"
              and terminal["one_turn_started"] and terminal["usage_received"]):
            verdict = "PASS"
        else:
            verdict = "INCONCLUSIVE"
            if self.request is None:
                reasons.append("no_approval_request")
            elif not self.response_written:
                reasons.append("decline_not_written")
            elif not self.resolved:
                reasons.append("request_not_resolved")
            if self.request is not None and not declined:
                state = self.commands[-1]["status"] if len(self.commands) == 2 else "no_command_result"
                reasons.append(f"command_not_declined:{state}")
            if self.ambiguous_exit_code:
                reasons.append("ambiguous_exit_code")
            if not self.settings_verified:
                reasons.append("settings_not_verified")
            if not matched:
                reasons.append("terminal_not_matched")
            elif status != "completed":
                reasons.append(f"turn_status:{status}")
            if not terminal["usage_received"]:
                reasons.append("usage_missing")

        checks = {**chain_checks, "bound_ids": bool(self.thread_id and self.turn_id),
                  "one_turn_started": terminal["one_turn_started"],
                  "one_terminal_completed": terminal_confirmed and status == "completed",
                  "usage_received": terminal["usage_received"], "no_violation": not violation}
        legacy = {"completed": "finished", "failed": "failed",
                  "interrupted": "cancelled"}.get(status, "unconfirmed") if terminal_confirmed else "unconfirmed"
        command_scope = {"session_shell_kind": self.session_shell[0], "session_shell_path": self.session_shell[1],
                         "allowed_forms": list(wrapped_forms(self.session_shell)),
                         "match_rule": "shlex_split_exact_argv"}
        return {"passed": verdict == "PASS", "probe_verdict": verdict, "reason_codes": reasons,
                "text_format": text_format, "approval_chain": approval_chain, "terminal": terminal,
                "command_scope": command_scope,
                "checks": checks, "terminal_confirmed": terminal_confirmed, "verdict": legacy,
                "thread_id": self.thread_id, "turn_id": self.turn_id, "usage": self.usage,
                # Only ever the fixed expected string, never model text.
                "final_reply": EXPECTED_REPLY if text_format["final_message_exact_DECLINED"] else None,
                "violation": violation, "approval": self.request, "decline_written": self.response_written,
                "execution_observed": self.execution_observed, "ambiguous_exit_code": self.ambiguous_exit_code,
                "audit": self.audit, "event_sequence": [e["method"] for e in self.audit if "method" in e],
                "event_counts": dict(Counter(e["method"] for e in self.audit if "method" in e))}
