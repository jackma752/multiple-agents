"""Model selection and model-budget keys (Agent is not a model)."""
import json
import re

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,79}$")
EFFORT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def validate_selection(model=None, reasoning_effort=None):
    if model is not None and (not isinstance(model, str) or not MODEL_ID.fullmatch(model)):
        raise ValueError("model must be a model ID, or null for provider default")
    if reasoning_effort is not None and (not isinstance(reasoning_effort, str)
                                         or not EFFORT.fullmatch(reasoning_effort)):
        raise ValueError("invalid reasoning_effort")


def budget_key(agent, provider, model):
    return json.dumps([agent, provider, model], ensure_ascii=False, separators=(",", ":"))


def validate_budgets(budgets):
    if not isinstance(budgets, dict) or len(budgets) > 100:
        raise ValueError("model_budgets must be an object with at most 100 entries")
    for key, value in budgets.items():
        try:
            agent, provider, model = json.loads(key)
            if not all(isinstance(x, str) and x and len(x) <= 200 for x in (agent, provider)):
                raise ValueError("invalid source")
            validate_selection(model)
            if not model or key != budget_key(agent, provider, model):
                raise ValueError("invalid key")
            if not isinstance(value, dict) or set(value) - {"total", "per_call", "warn_percent", "mode"}:
                raise ValueError("invalid fields")
            for field in ("total", "per_call"):
                if value.get(field) is not None and (type(value[field]) is not int or value[field] < 1):
                    raise ValueError("invalid amount")
            warn = value.get("warn_percent", 80)
            if type(warn) is not int or not 1 <= warn <= 100 or value.get("mode", "strict") not in ("strict", "soft"):
                raise ValueError("invalid mode or threshold")
            if value.get("total") and value.get("per_call") and value["per_call"] > value["total"]:
                raise ValueError("per-call exceeds total")
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid model budget") from None
