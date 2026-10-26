"""Authenticated model metadata; GET never starts a provider process."""
from fastapi import Depends, HTTPException
from agenthub_kernel.adapters import adapter_for
from agenthub_kernel.model_usage import provider_of
from agenthub_kernel.usage_ledger import instance_usage
from agenthub_rt.auth import Principal, authenticate, require_approver


def register_models(app, db):
    def read(agent_id, principal, refresh=False):
        require_approver(principal)
        adapter = adapter_for(app.state.worker.adapter, agent_id)
        if adapter is None or agent_id not in getattr(adapter, 'AGENT_IDS', ()):
            raise HTTPException(404, detail="agent not served")
        method = getattr(adapter, 'model_catalog', None)
        try:
            data = method(refresh=refresh) if callable(method) else {}
        except Exception as exc:
            # No arbitrary provider stderr or config content in HTTP responses.
            raise HTTPException(409, detail={"code": "model_catalog_failed", "message": type(exc).__name__})
        choices = {m['id']: m for m in data.get('models', [])}
        with db.session() as session:
            history = instance_usage(session, principal.identity).get('by_model', [])
        for row in history:
            if row['agent_id'] == agent_id and row['model']:
                choices.setdefault(row['model'], {"id": row['model'], "display_name": row['model'],
                    "reasoning_efforts": [], "source": "historical_usage"})
        return {"agent_id": agent_id, "provider": provider_of(agent_id), "default_model": None,
                "selection": bool(getattr(adapter, 'supports_model_selection', False)),
                "effort_selection": bool(getattr(adapter, 'supports_effort_selection', False)),
                "refresh_available": callable(method), **data, "models": list(choices.values()),
                "catalog_note": "候选和历史记录不保证账号当前可用；实际结果以接入方报告为准"}

    @app.get('/api/agents/{agent_id}/models')
    def get_models(agent_id: str, principal: Principal = Depends(authenticate)):
        return read(agent_id, principal)

    @app.post('/api/agents/{agent_id}/models/refresh')
    def refresh_models(agent_id: str, principal: Principal = Depends(authenticate)):
        """Explicit metadata refresh, possibly a local CLI process, never turn/start."""
        return read(agent_id, principal, refresh=True)
