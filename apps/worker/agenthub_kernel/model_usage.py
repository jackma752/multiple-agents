"""Model components supplement the per-call ledger; totals still use that ledger."""
from sqlalchemy import delete, select
from agenthub.usage import normalize_usage, MAPPING_CLAUDE_CODE
from agenthub.models import MODEL_ID
from agenthub_kernel.db import ModelUsageRow


def provider_of(agent_id):
    # Interface source, not a claim about a gateway's underlying model vendor.
    return "claude-code" if agent_id in ("claude-code", "cc") else "codex" if agent_id == "codex" else agent_id


def write_components(session, inv, rec, report):
    provider = provider_of(inv.snapshot.get("agent_id", "unknown"))
    components = []
    details = (report or {}).get("model_usage") or {}
    if rec.mapping == MAPPING_CLAUDE_CODE and isinstance(details, dict):
        for model, raw in details.items():
            if not isinstance(model, str) or not MODEL_ID.fullmatch(model) or not isinstance(raw, dict):
                continue
            translated = {key: raw.get(source) for key, source in {
                "input_tokens": "inputTokens", "output_tokens": "outputTokens",
                "cache_read_input_tokens": "cacheReadInputTokens",
                "cache_creation_input_tokens": "cacheCreationInputTokens"}.items()}
            record = normalize_usage({"mapping": rec.mapping, "usage": translated,
                                      "model": model, "source": "model_usage",
                                      "notes": [n for n in rec.notes if n != "multi_model_usage_unverified"]})
            components.append({**record.to_dict(), "model_source": "provider_usage"})
        # Component totals are a breakdown only if they reconcile with the call.
        reconciled = components and all(
            all(c.get(k) is not None for c in components)
            and sum(c[k] for c in components) == getattr(rec, k)
            for k in ("input_total", "output_total"))
        if not reconciled:
            components = []
    if not components:
        # Legacy single-model results remain readable; unknown models stay unknown.
        components = [{**rec.to_dict(), "model_source": (report or {}).get("model_source", "provider_report")}]
    session.execute(delete(ModelUsageRow).where(ModelUsageRow.invocation_id == inv.id))
    for component in components:
        session.add(ModelUsageRow(invocation_id=inv.id, provider=provider,
                                  model=component.get("model") or "", payload=component))
    session.flush()


def components(session, views):
    rows = session.scalars(select(ModelUsageRow).where(
        ModelUsageRow.invocation_id.in_([v["invocation_id"] for v in views]))).all() if views else []
    by_inv = {}
    for row in rows:
        by_inv.setdefault(row.invocation_id, []).append({**row.payload, "provider": row.provider})
    out = []
    for view in views:
        for part in by_inv.get(view["invocation_id"], [view]):
            out.append({**view, **part, "agent_id": view["agent_id"],
                        "provider": part.get("provider") or provider_of(view["agent_id"])})
    return out


def group_models(session, views):
    from agenthub_kernel.usage_ledger import summarize
    groups = {}
    for view in components(session, views):
        key = (view["agent_id"], view["provider"], view.get("model") or "")
        groups.setdefault(key, []).append(view)
    return [{"agent_id": agent, "provider": provider, "model": model or None,
             "total": summarize(group), "model_source": sorted({v.get("model_source", "legacy") for v in group})}
            for (agent, provider, model), group in sorted(groups.items())]
