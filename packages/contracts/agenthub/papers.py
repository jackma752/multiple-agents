"""Paper research pipeline contracts (P6, ``docs/p6-paper-pipeline.md``).

A research task is a user-authorized stage (``agenthub.stage``, plan
``paper``): every model step is an ordinary stage call through the consume
gate, and every deterministic tool step (search, download, file read, export)
is checked against :class:`ResourceRules` at the moment it runs. The rules are
part of the stage rules, so they enter ``rules_hash``; paper content and model
output are data and can never widen them.

Nothing here performs I/O; the kernel (``agenthub_kernel.papers``) applies these
pure decisions to what the user set.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

PAPER_CONTRACT_VERSION = 1
RESOURCE_RULES_VERSION = 1

# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #

#: 方案一: ask once, continue with what exists if nobody chooses within the window.
FULLTEXT_CONTINUE = "continue_allowed"
#: 方案二: wait for the full text at the reading gate; never continue by silence.
FULLTEXT_WAIT = "wait_for_fulltext"
FULLTEXT_POLICIES = (FULLTEXT_CONTINUE, FULLTEXT_WAIT)
#: The only automatic continuation: 方案一's first question, unanswered for this long.
CHOICE_TIMEOUT_SECONDS = 30 * 60

LANGUAGES = {"zh": "简体中文", "en": "English"}
SOURCES = ("arxiv", "openalex", "crossref", "semantic_scholar")
SOURCE_LABELS = {"arxiv": "arXiv", "openalex": "OpenAlex", "crossref": "Crossref",
                 "semantic_scholar": "Semantic Scholar"}
MAX_TARGET_PAPERS = 30
MAX_TOPIC_CHARS = 500


@dataclass(frozen=True)
class ResearchRequest:
    topic: str
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    target_papers: int = 5
    language: str = "zh"
    fulltext_policy: str = FULLTEXT_CONTINUE
    sources: tuple = ("arxiv",)
    #: Candidates fetched per search query (bounded; arXiv returns at most this many).
    candidates_per_query: int = 15
    sections: int = 4
    # Legacy requests keep their exact hash and workflow. New UI explicitly opts in.
    mode: str = "review"
    core_papers: int = 1
    repair_rounds: int = 1

    def __post_init__(self) -> None:
        topic = " ".join((self.topic or "").split())
        if not topic or len(topic) > MAX_TOPIC_CHARS:
            raise ValueError(f"topic is required, at most {MAX_TOPIC_CHARS} characters")
        object.__setattr__(self, "topic", topic)
        if isinstance(self.sources, list):
            object.__setattr__(self, "sources", tuple(self.sources))
        for name in ("year_from", "year_to"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1990 <= value <= 2100):
                raise ValueError(f"{name} must be a year between 1990 and 2100")
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from cannot be after year_to")
        if type(self.target_papers) is not int or not 1 <= self.target_papers <= MAX_TARGET_PAPERS:
            raise ValueError(f"target_papers must be 1..{MAX_TARGET_PAPERS}")
        if self.language not in LANGUAGES:
            raise ValueError(f"language must be one of {sorted(LANGUAGES)}")
        if self.fulltext_policy not in FULLTEXT_POLICIES:
            raise ValueError(f"fulltext_policy must be one of {FULLTEXT_POLICIES}")
        if not self.sources or len(set(self.sources)) != len(self.sources) or set(self.sources) - set(SOURCES):
            raise ValueError(f"sources must be a subset of {SOURCES}")
        if type(self.candidates_per_query) is not int or not 5 <= self.candidates_per_query <= 50:
            raise ValueError("candidates_per_query must be 5..50")
        if type(self.sections) is not int or not 2 <= self.sections <= 6:
            raise ValueError("sections must be 2..6")
        if self.mode not in ("review", "study", "proposal", "project"):
            raise ValueError("mode must be review, study, proposal or project")
        if type(self.core_papers) is not int or not 1 <= self.core_papers <= min(3, self.target_papers):
            raise ValueError("core_papers must be 1..min(3, target_papers)")
        if type(self.repair_rounds) is not int or not 0 <= self.repair_rounds <= 2:
            raise ValueError("repair_rounds must be 0..2")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["sources"] = list(self.sources)
        if self.mode == "review" and self.core_papers == 1 and self.repair_rounds == 1:
            for name in ("mode", "core_papers", "repair_rounds"):
                out.pop(name)
        return out

    def request_hash(self) -> str:
        return _hash(self.as_dict())


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# resource authorization
# --------------------------------------------------------------------------- #

#: Hosts each source may contact (search API and open PDF links).
SOURCE_HOSTS = {"arxiv": ("export.arxiv.org", "arxiv.org"), "openalex": ("api.openalex.org",),
                "crossref": ("api.crossref.org",), "semantic_scholar": ("api.semanticscholar.org",)}
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
URL_SUPPLEMENT_OFF = "off"
URL_SUPPLEMENT_PUBLIC = "public_http"  # any public http(s) host on 80/443, checked per hop
URL_SUPPLEMENT_MODES = (URL_SUPPLEMENT_OFF, URL_SUPPLEMENT_PUBLIC)
MAX_REDIRECTS = 3

ACTION_SEARCH = "search"
ACTION_DOWNLOAD = "download"
ACTION_UPLOAD = "upload"
ACTION_READ_LOCAL = "read_local"
ACTION_FETCH_URL = "fetch_url"
ACTION_EXPORT = "export"
ACTION_SEND = "send_to_agent"
ACTIONS = (ACTION_SEARCH, ACTION_DOWNLOAD, ACTION_UPLOAD, ACTION_READ_LOCAL, ACTION_FETCH_URL,
           ACTION_EXPORT, ACTION_SEND)


@dataclass(frozen=True)
class ResourceRules:
    """What deterministic tool steps of one research task may touch.

    Approved once by the user together with the stage; part of the stage rules
    hash. ``local_roots`` are absolute, real paths the user named; files outside
    them are refused. ``send_to`` is every agent that may receive paper text.
    """

    sources: tuple = ("arxiv",)
    allow_pdf_download: bool = True
    max_download_bytes: int = 20 * 1024 * 1024
    allow_upload: bool = True
    local_roots: tuple = ()
    url_supplement: str = URL_SUPPLEMENT_PUBLIC
    send_to: tuple = ()
    version: int = RESOURCE_RULES_VERSION
    #: Absolute, real directory the user chose for a copy of the exports; "" = only
    #: the Hub's task directory. Left out of ``as_dict`` when empty so earlier tasks'
    #: hashes hold.
    export_dir: str = ""
    # Explicit opt-in for publisher/repository PDF URLs returned by approved sources.
    # Omitted when false so previously approved resource hashes remain identical.
    allow_public_fulltext: bool = False

    def __post_init__(self) -> None:
        if type(self.allow_public_fulltext) is not bool:
            raise ValueError("allow_public_fulltext must be boolean")
        for name in ("sources", "local_roots", "send_to"):
            if isinstance(getattr(self, name), list):
                object.__setattr__(self, name, tuple(getattr(self, name)))
        if set(self.sources) - set(SOURCES):
            raise ValueError("unknown source")
        if type(self.max_download_bytes) is not int or not 1024 <= self.max_download_bytes <= MAX_DOWNLOAD_BYTES:
            raise ValueError(f"max_download_bytes must be 1 KiB..{MAX_DOWNLOAD_BYTES}")
        if self.url_supplement not in URL_SUPPLEMENT_MODES:
            raise ValueError(f"url_supplement must be one of {URL_SUPPLEMENT_MODES}")
        for flag in ("allow_pdf_download", "allow_upload"):
            if type(getattr(self, flag)) is not bool:
                raise ValueError(f"{flag} must be a boolean")
        if len(self.local_roots) > 5 or not all(isinstance(r, str) and r for r in self.local_roots):
            raise ValueError("local_roots: at most 5 absolute directories")
        if not self.send_to or not all(isinstance(a, str) and a for a in self.send_to):
            raise ValueError("send_to must name the agents that receive paper text")
        if self.version != RESOURCE_RULES_VERSION:
            raise ValueError("unsupported resource rules version")
        if not isinstance(self.export_dir, str) or len(self.export_dir) > 500:
            raise ValueError("export_dir must be an absolute directory or empty")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for name in ("sources", "local_roots", "send_to"):
            out[name] = list(getattr(self, name))
        if not self.export_dir:
            out.pop("export_dir")
        if not self.allow_public_fulltext:
            out.pop("allow_public_fulltext")
        return out

    def rules_hash(self) -> str:
        return _hash(self.as_dict())

    def hosts(self) -> tuple:
        return tuple(h for s in self.sources for h in SOURCE_HOSTS[s])


def resource_rules_of(stored: dict[str, Any]) -> ResourceRules:
    return ResourceRules(**{k: v for k, v in (stored or {}).items() if k in ResourceRules.__dataclass_fields__})


def _blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped is not None
                                     and _blocked_ip(str(ip.ipv4_mapped))))


def check_url(url: str, *, hosts: Optional[Iterable[str]] = None,
              resolve: Optional[Any] = None) -> tuple[bool, str]:
    """``(ok, why)`` for one URL hop: http(s), default ports, no credentials.

    ``hosts`` (if given) is an allowlist of exact host names. ``resolve(host)``
    returns the addresses the host maps to; every one must be public, so a name
    pointing at loopback or a LAN address is refused. A literal IP host is
    checked directly. Pure apart from the injected resolver.
    """
    if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\r\n\t\0 "):
        return False, "malformed_url"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False, "malformed_url"
    if parts.scheme not in ("http", "https"):
        return False, "scheme_not_allowed"
    if parts.username or parts.password:
        return False, "credentials_in_url"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False, "malformed_url"
    if port not in (None, 80, 443):
        return False, "port_not_allowed"
    if hosts is not None and host not in {h.lower() for h in hosts}:
        return False, "host_not_allowed"
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return (False, "address_not_public") if _blocked_ip(str(literal)) else (True, "public_address")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        return False, "address_not_public"
    if resolve is not None:
        try:
            addresses = list(resolve(host))
        except Exception:  # noqa: BLE001
            return False, "unresolvable_host"
        if not addresses:
            return False, "unresolvable_host"
        if any(_blocked_ip(a) for a in addresses):
            return False, "address_not_public"
    return True, "ok"


def resource_decision(rules: ResourceRules, action: str, target: str = "", *,
                      resolve: Optional[Any] = None, realpath: Optional[Any] = None) -> tuple[bool, str]:
    """Whether one tool action of a task is inside its approved resource rules."""
    if action not in ACTIONS:
        return False, "unknown_action"
    if action == ACTION_SEARCH:
        return (True, "source approved") if target in rules.sources else (False, "source_not_approved")
    if action == ACTION_DOWNLOAD:
        if not rules.allow_pdf_download:
            return False, "download_not_approved"
        return check_url(target, hosts=None if rules.allow_public_fulltext else rules.hosts(), resolve=resolve)
    if action == ACTION_FETCH_URL:
        if rules.url_supplement != URL_SUPPLEMENT_PUBLIC:
            return False, "url_supplement_not_approved"
        return check_url(target, resolve=resolve)
    if action == ACTION_UPLOAD:
        return (True, "upload approved") if rules.allow_upload else (False, "upload_not_approved")
    if action == ACTION_READ_LOCAL:
        if not rules.local_roots:
            return False, "no_local_roots_approved"
        return _inside(target, rules.local_roots, realpath, "path_outside_approved_roots")
    if action == ACTION_SEND:
        return (True, "agent approved") if target in rules.send_to else (False, "agent_not_approved")
    # ACTION_EXPORT: no target = the Hub's own task directory; a target must be inside export_dir.
    if not target:
        return True, "export to task directory"
    if not rules.export_dir:
        return False, "no_export_dir_approved"
    return _inside(target, (rules.export_dir,), realpath, "path_outside_export_dir")


def _inside(target: str, roots: Iterable[str], realpath: Optional[Any], refusal: str) -> tuple[bool, str]:
    """``target`` strictly below one of ``roots`` after resolving links and ``..``."""
    import os

    real = realpath or os.path.realpath
    try:
        path = os.path.normcase(real(target))
    except (OSError, ValueError):
        return False, "unreadable_path"
    for root in roots:
        root_real = os.path.normcase(real(root))
        if path == root_real:
            continue
        try:
            if os.path.commonpath([path, root_real]) == root_real:
                return True, "inside approved root"
        except ValueError:
            continue
    return False, refusal


# --------------------------------------------------------------------------- #
# task, step and supplement states
# --------------------------------------------------------------------------- #

TASK_DRAFT = "draft"
TASK_RUNNING = "running"
TASK_WAITING_INPUT = "waiting_input"
TASK_PAUSED = "paused"
TASK_STOPPED = "stopped"
TASK_BLOCKED = "blocked"
TASK_COMPLETED = "completed"

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_BLOCKED = "blocked"
STEP_UNKNOWN = "unknown"
STEP_STATUSES = (STEP_PENDING, STEP_RUNNING, STEP_SUCCEEDED, STEP_FAILED, STEP_BLOCKED, STEP_UNKNOWN)

#: Pipeline phases, in order.
PHASES = ("plan", "search", "screen", "fetch", "fulltext_gate", "read", "synthesize", "outline", "write",
          "review", "abstract", "export", "done")

# Supplement request lifecycle (``docs/p6-fulltext-supplementation.md`` §5).
SUP_AWAITING_CHOICE = "awaiting_choice"
SUP_AWAITING_FILES = "awaiting_files"
SUP_VALIDATING = "validating_batch"
SUP_AWAITING_MORE = "awaiting_more_choice"
SUP_RESOLVED = "resolved"
SUP_OPEN = (SUP_AWAITING_CHOICE, SUP_AWAITING_FILES, SUP_VALIDATING, SUP_AWAITING_MORE)

CHOICE_SUPPLEMENT = "supplement"
CHOICE_CONTINUE = "continue"
#: How a request was closed.
DECISION_CONTINUE = "user_continue"
DECISION_TIMEOUT = "timeout_continue"
DECISION_COMPLETE = "all_supplied"

#: Which user choices each open state accepts.
CHOICES_IN = {
    SUP_AWAITING_CHOICE: (CHOICE_SUPPLEMENT, CHOICE_CONTINUE),
    SUP_AWAITING_FILES: (CHOICE_CONTINUE,),  # an explicit "stop waiting" decision
    SUP_AWAITING_MORE: (CHOICE_SUPPLEMENT, CHOICE_CONTINUE),
    SUP_VALIDATING: (),
}


def initial_supplement_state(policy: str) -> str:
    return SUP_AWAITING_CHOICE if policy == FULLTEXT_CONTINUE else SUP_AWAITING_FILES


def choice_transition(state: str, choice: str) -> tuple[str, Optional[str]]:
    """``(next_state, decision)`` for a user choice; ValueError when not allowed now."""
    if choice not in CHOICES_IN.get(state, ()):
        raise ValueError(f"choice {choice!r} not available in {state}")
    if choice == CHOICE_SUPPLEMENT:
        return SUP_AWAITING_FILES, None
    return SUP_RESOLVED, DECISION_CONTINUE


def timeout_due(state: str, deadline: Any, current: Any) -> bool:
    """Only 方案一's first question has a deadline; nothing else continues by silence."""
    return state == SUP_AWAITING_CHOICE and deadline is not None and current >= deadline


# Why a paper has no readable full text (shown with the reference).
MISSING_REASONS = {
    "no_fulltext_link": "来源没有提供开放全文链接",
    "download_not_approved": "本次授权未允许下载全文",
    "download_failed": "下载失败",
    "too_large": "文件超过允许大小",
    "not_pdf": "返回内容不是 PDF",
    "encrypted": "PDF 已加密，无法读取",
    "corrupt": "PDF 文件损坏或无法解析",
    "empty_text": "解析结果为空",
    "scanned": "疑似扫描件（首版不支持 OCR）",
    "host_not_allowed": "全文地址不在授权来源内",
}

# Supplement file outcomes.
FILE_RECEIVED = "received"
FILE_ACCEPTED = "accepted"
FILE_MISMATCH = "mismatch"
FILE_AMBIGUOUS = "ambiguous"
FILE_PARSE_FAILED = "parse_failed"
FILE_FETCH_FAILED = "fetch_failed"
FILE_DUPLICATE = "duplicate"
FILE_REFUSED = "refused"

# Reading depth and evidence levels.
DEPTH_FULLTEXT = "fulltext"
DEPTH_ABSTRACT = "abstract"
DEPTH_METADATA = "metadata_only"

# Execution and quality are recorded separately.
QUALITY_PENDING = "pending"
QUALITY_PASSED = "passed"
QUALITY_FAILED = "failed"
QUALITY_UNAVAILABLE = "unavailable"
QUALITY_NEEDS_REVIEW = "needs_review"
QUALITY_STATUSES = (QUALITY_PENDING, QUALITY_PASSED, QUALITY_FAILED, QUALITY_UNAVAILABLE, QUALITY_NEEDS_REVIEW)


# --------------------------------------------------------------------------- #
# call estimate (shown before the user authorizes)
# --------------------------------------------------------------------------- #


def estimate_calls(request: ResearchRequest, *, review: bool, revisions: int = 1, retry_margin: int = 3) -> dict:
    candidates = 3 * request.candidates_per_query * len(request.sources)
    screen = -(-candidates // 20)
    if request.mode != "review":
        n, rounds = request.core_papers, request.repair_rounds
        parts = {"plan": 1, "screen": screen, "extract_related": request.target_papers - n,
                 "study": n, "review": n if review else 0,
                 "targeted_read_or_repair": n * rounds,
                 "recheck": n * rounds if review else 0,
                 "directions": 0 if request.mode == "study" else 1}
        expected = sum(parts.values()) - parts["targeted_read_or_repair"] - parts["recheck"]
        return {"parts": parts, "expected": expected, "worst_case": sum(parts.values()) + retry_margin,
                "retry_margin": retry_margin}
    parts = {"plan": 1, "screen": screen, "extract": request.target_papers, "synthesize": 1, "outline": 1,
             "write": request.sections, "review": request.sections if review else 0,
             "revise": request.sections * revisions if review else 0, "abstract": 1}
    expected = sum(v for k, v in parts.items() if k != "revise")
    return {"parts": parts, "expected": expected, "worst_case": sum(parts.values()) + retry_margin,
            "retry_margin": retry_margin}


# --------------------------------------------------------------------------- #
# reference text from real metadata only
# --------------------------------------------------------------------------- #


def reference_text(number: Any, meta: dict[str, Any]) -> str:
    """``[n] Authors. Title. Year. DOI or link`` - missing fields are marked, never invented."""
    authors = [a for a in meta.get("authors") or [] if isinstance(a, str) and a.strip()]
    if not authors:
        who = "[作者未知]"
    elif len(authors) > 6:
        who = ", ".join(authors[:6]) + ", et al"
    else:
        who = ", ".join(authors)
    title = (meta.get("title") or "").strip() or "[题名未知]"
    parts = [f"[{number}] {who}.", f"{title}."]
    parts.append(f"{meta['year']}." if meta.get("year") else "[年份未知].")
    if meta.get("venue"):
        parts.append(f"{meta['venue']}.")
    if meta.get("doi"):
        parts.append(f"DOI: {meta['doi']}")
    elif meta.get("url"):
        parts.append(meta["url"])
    return " ".join(parts)


_TITLE_WORD = re.compile(r"[a-z0-9]+|[一-鿿]")


def title_tokens(text: str) -> list[str]:
    return _TITLE_WORD.findall((text or "").lower())


def normalize_title(text: str) -> str:
    return " ".join(title_tokens(text))
