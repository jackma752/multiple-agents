"""Persistent Hub chat contracts; local history is not provider session resume."""

from dataclasses import asdict, dataclass
import hashlib
import json


CONTEXT_MODE = "independent"
MAX_MESSAGE_CHARS = 100_000


@dataclass(frozen=True)
class ConversationTurn:
    text: str
    agent_id: str
    client_request_id: str
    execution_mode: str = "strict"

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.text, MAX_MESSAGE_CHARS),
            (self.agent_id, 200),
            (self.client_request_id, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError("turn fields must be nonblank strings within size limits")
        if self.execution_mode not in {"strict", "supervised"}:
            raise ValueError("execution_mode must be strict or supervised")

    def request_hash(self) -> str:
        """Deduplication hash, separate from the policy-bound approval hash."""
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
