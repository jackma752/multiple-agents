"""Source-development authentication only. Excluded from the desktop release."""
import os

DEFAULT_USER_TOKEN = 'dev-user-token'


def development_tokens():
    from agenthub_rt.auth import Principal, ROLE_USER, ROLE_AGENT, ROLE_WORKER
    return {os.environ.get('AGENT_HUB_USER_TOKEN', DEFAULT_USER_TOKEN):Principal('local-user',ROLE_USER),
            os.environ.get('AGENT_HUB_AGENT_TOKEN','dev-agent-token'):Principal('agent',ROLE_AGENT),
            os.environ.get('AGENT_HUB_WORKER_TOKEN','dev-worker-token'):Principal('worker',ROLE_WORKER)}
