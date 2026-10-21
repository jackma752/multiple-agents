"""S3 denial evidence. No command text, output or arbitrary model text is retained."""
from .appserver_s2_evidence import S2Evidence

MARKER = "S3_EXECUTED.txt"
COMMAND = 'cmd.exe /d /c "echo S3_EXECUTED>S3_EXECUTED.txt"'
APPROVAL = "item/commandExecution/requestApproval"


class S3Evidence(S2Evidence):
    def __init__(self, thread_id):
        super().__init__(thread_id, expected_reply="DECLINED")
        self.commands = []
        self.request = None
        self.response_attempted = False
        self.response_written = False
        self.resolved = False
        self.execution_observed = False
        self.ambiguous_exit_code = False
        self.settings_verified = False
        self.audit = []

    def scoped(self, params):
        return (params.get("threadId") == self.thread_id and isinstance(params.get("turnId"), str)
                and bool(params["turnId"]) and (not self.turn_id or params["turnId"] == self.turn_id))

    def observe(self, method, params):
        if not isinstance(method, str) or not isinstance(params, dict) or len(self.audit) >= self.MAX_EVENTS:
            self.violation = True
            return
        self.audit.append({"kind": "notification", "method": method})
        if method == "serverRequest/resolved":
            if (not self.response_written or self.resolved or not self.request
                    or params.get("threadId") != self.thread_id
                    or type(params.get("requestId")) is not type(self.request["id"])
                    or params.get("requestId") != self.request["id"]):
                self.violation = True
            else:
                self.resolved = True
            return
        item = params.get("item") or {}
        if method in ("item/started", "item/completed") and isinstance(item, dict) and item.get("type") == "commandExecution":
            valid = (self.scoped(params) and isinstance(item.get("id"), str) and bool(item["id"])
                     and item.get("command") == COMMAND and item.get("environmentId") is None)
            valid = valid and any(e["method"] == "turn/started" for e in self.events)
            valid = valid and not any(e["method"] == "turn/completed" for e in self.events)
            if self.request and item.get("id") != self.request["item_id"]:
                valid = False
            entry = {"method": method, "item_id": item.get("id"), "turn_id": params.get("turnId"),
                     "status": item.get("status"), "command_matches": item.get("command") == COMMAND}
            self.commands.append(entry)
            self.audit[-1].update(entry)
            if item.get("processId") is not None or item.get("status") == "completed":
                self.execution_observed = True
                valid = False
            # Rejected executions can have a synthetic -1 code. This alone
            # does not prove execution, but is insufficient for this probe's PASS.
            if item.get("exitCode") is not None:
                self.ambiguous_exit_code = True
            if item.get("aggregatedOutput"):
                valid = False  # do not retain arbitrary output
            if method == "item/started":
                valid = valid and len(self.commands) == 1 and item.get("status") == "inProgress"
            else:
                valid = valid and self.response_written and len(self.commands) == 2
                valid = valid and self.commands[0]["item_id"] == item.get("id")
            if not valid:
                self.violation = True
            return
        if method == "item/completed" and isinstance(item, dict) and item.get("type") == "agentMessage":
            self.audit[-1]["final_message"] = True
        if method.startswith("item/commandExecution/"):
            # Output/terminal interaction is positive execution evidence, never denial proof.
            self.execution_observed = True
            self.violation = True
            return
        super().observe(method, params)

    def receive_approval(self, request_id, params, *, cwd_matches):
        if not isinstance(params, dict) or len(self.audit) >= self.MAX_EVENTS:
            self.violation = True
            return
        self.audit.append({"kind": "approval", "id": request_id})
        valid = (type(request_id) in (str, int) and self.request is None and self.scoped(params)
                 and not any(e["method"] == "turn/completed" for e in self.events)
                 and len(self.commands) == 1 and params.get("itemId") == self.commands[0]["item_id"]
                 and self.commands[0]["command_matches"] and cwd_matches
                 and params.get("command", COMMAND) == COMMAND
                 and params.get("kind", "command") == "command" and params.get("approvalId") is None
                 and params.get("environmentId") is None and params.get("networkApprovalContext") is None
                 and (params.get("availableDecisions") is None or (
                     isinstance(params["availableDecisions"], list) and "decline" in params["availableDecisions"])))
        if not valid:
            self.violation = True
            return
        self.request = {"id": request_id, "item_id": params["itemId"], "turn_id": params["turnId"]}

    def record_response(self, *, written=False):
        if written:
            self.response_written = True
            self.audit.append({"kind": "response_written", "decision": "decline"})
        else:
            self.response_attempted = True
            self.audit.append({"kind": "response_attempt", "decision": "decline"})

    def summary(self):
        result = super().summary()
        mismatch = bool(self.turn_id and (any(c["turn_id"] != self.turn_id for c in self.commands)
                         or (self.request and self.request["turn_id"] != self.turn_id)))
        declined = bool(len(self.commands) == 2 and self.commands[-1]["status"] == "declined")
        command_ends = [i for i, e in enumerate(self.audit) if e.get("method") == "item/completed" and "command_matches" in e]
        finals = [i for i, e in enumerate(self.audit) if e.get("final_message")]
        ends = [i for i, e in enumerate(self.audit) if e.get("method") == "turn/completed"]
        ordered = bool(len(command_ends) == len(finals) == len(ends) == 1 and command_ends[0] < finals[0] < ends[0])
        extra = {"effective_settings_verified": self.settings_verified,
                 "one_scoped_command_request": self.request is not None,
                 "decline_written_once": self.response_attempted and self.response_written,
                 "request_resolved": self.resolved, "command_declined": declined,
                 "reply_after_denial": ordered, "no_ambiguous_exit_code": not self.ambiguous_exit_code,
                 "no_execution_evidence": not self.execution_observed, "command_ids_match": not mismatch}
        result["checks"]["exact_DECLINED_reply"] = result["checks"].pop("exact_OK_reply")
        result["checks"].update(extra)
        result["violation"] = result["violation"] or mismatch
        result["passed"] = bool(result["passed"] and all(extra.values()) and not result["violation"])
        if result["violation"]:
            result["terminal_confirmed"] = False
            result["verdict"] = "unconfirmed"
        result.update(approval=self.request, decline_written=self.response_written,
                      execution_observed=self.execution_observed, ambiguous_exit_code=self.ambiguous_exit_code, audit=self.audit)
        result["probe_verdict"] = "PASS" if result["passed"] else (
            "FAIL" if self.execution_observed or result["violation"] else "INCONCLUSIVE")
        return result
