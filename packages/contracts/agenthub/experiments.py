"""User-recorded experiment snapshots; these records never execute code.

Metrics must be finite measured samples. provenance='reported' is a user's
claim, not independent verification; demonstration results cannot be mixed
with reported measurements in a comparison.
"""
import math


def validate_records(records):
    if not isinstance(records, list) or len(records) > 30:
        raise ValueError("最多保存 30 项实验记录")
    out, seen = [], set()
    for record in records:
        if not isinstance(record, dict) or set(record)-{"id","label","dataset","protocol","metric","direction","samples","provenance","note"}:
            raise ValueError("实验字段不正确")
        item = {}
        for key, bound in [("id",80),("label",160),("dataset",300),("protocol",1000),("metric",100)]:
            value = record.get(key)
            if not isinstance(value,str) or not value.strip() or len(value)>bound:
                raise ValueError(f"实验 {key} 必填且不超过 {bound} 字符")
            item[key] = value.strip()
        if item["id"] in seen: raise ValueError("实验 ID 不能重复")
        seen.add(item["id"])
        item["direction"] = record.get("direction")
        item["provenance"] = record.get("provenance")
        if item["direction"] not in ("min","max"): raise ValueError("指标方向必须为 min/max")
        if item["provenance"] not in ("reported","demo"): raise ValueError("请标明用户记录或演示数据")
        samples = record.get("samples")
        if not isinstance(samples,list) or not 1<=len(samples)<=1000:
            raise ValueError("每项实验需要 1–1000 个数值")
        if any(type(x) not in (int,float) or not math.isfinite(x) or abs(x)>1e100 for x in samples):
            raise ValueError("实验数值必须有限，不能包含布尔值、NaN 或无穷大")
        note = record.get("note", "")
        if not isinstance(note,str) or len(note)>3000: raise ValueError("备注过长")
        item.update(samples=list(samples),note=note)
        out.append(item)
    return out
