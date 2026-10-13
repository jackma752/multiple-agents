"""Authentication & authorization for the Agent Hub HTTP API (P2).

This is the backend-enforced approval boundary required by AGENT_HUB_PLAN §4.1:
"只有登录的用户通过审批端点才能批准。Agent、Worker、插件和模型输出没有审批权限".

P2 uses a deliberately small principal model:

* A request carries ``Authorization: Bearer <token>`` (or ``X-Agent-Hub-Token``).
* Tokens are mapped to a :class:`Principal` with a role.
* Only ``user`` role may approve / reject / revoke an approval or change policy.
* ``agent`` / ``worker`` / ``service`` roles are valid identities but are
  explicitly forbidden from the approval endpoints, so an agent that learns a
  service token still cannot approve its own invocation.

The dev token is read from ``AGENT_HUB_USER_TOKEN`` (defaults to a clearly-marked
development value). Real deployments must inject it via the environment; see
AGENTS.md rule 4 (never commit credentials).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

#: ``auto_error=False`` so a missing header reaches our own handler, which
#: returns a structured 401 instead of FastAPI's bare default. Using the standard
#: HTTPBearer scheme also makes the interactive docs page show an "Authorize"
#: button, so a trial user pastes the token once instead of per request.
_bearer_scheme = HTTPBearer(auto_error=False)


ROLE_USER = "user"
ROLE_AGENT = "agent"
ROLE_WORKER = "worker"
ROLE_SERVICE = "service"

#: Roles allowed to act as the approving human.
APPROVER_ROLES = frozenset({ROLE_USER})


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    identity: str
    role: str

    @property
    def can_approve(self) -> bool:
        return self.role in APPROVER_ROLES


def _dev_tokens() -> dict[str, Principal]:
    """Token -> principal map.

    The user token comes from the environment so it is never hardcoded in the
    repository. The agent/worker tokens exist so tests (and a real deployment)
    can prove those identities are refused by the approval endpoints.
    """
    user_token = os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token")
    agent_token = os.environ.get("AGENT_HUB_AGENT_TOKEN", "dev-agent-token")
    worker_token = os.environ.get("AGENT_HUB_WORKER_TOKEN", "dev-worker-token")
    return {
        user_token: Principal(identity="local-user", role=ROLE_USER),
        agent_token: Principal(identity="agent", role=ROLE_AGENT),
        worker_token: Principal(identity="worker", role=ROLE_WORKER),
    }


def authenticate(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    x_agent_hub_token: Optional[str] = Header(default=None),
) -> Principal:
    """Resolve the caller from a bearer token. 401 when absent/unknown.

    No anonymous fallback: an unauthenticated request can never reach an
    approval endpoint, and cannot approve by omission.
    """
    token: Optional[str] = None
    if credentials is not None and credentials.credentials:
        token = credentials.credentials.strip()
    elif x_agent_hub_token:
        token = x_agent_hub_token.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "unauthenticated",
                "message": "missing bearer token; approval requires a logged-in user",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = _dev_tokens().get(token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unknown_token", "message": "unknown token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_approver(principal: Principal) -> Principal:
    """403 unless the principal is the human user.

    Agents, workers and services are authenticated identities but are NOT
    permitted to approve; this is what stops an agent from approving itself.
    """
    if not principal.can_approve:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "not_permitted",
                "message": (
                    f"role '{principal.role}' may not approve invocations; "
                    "only a logged-in user can"
                ),
            },
        )
    return principal
