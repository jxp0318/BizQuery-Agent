"""用已记录的 pred_sql 重新在 dw 执行，并按最新 result_match 重判分。

不调用 LLM；只修报告分数。用法：
  python -m evals.rescore_from_sql
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evals.metrics import result_match

CASES_DIR = Path("evals/cases")
E2E = Path("evals/report/e2e-report.json")
FULL = Path("evals/report/e2e-full-100.json")
OUT = Path("evals/report/e2e-full-100.json")


def load_gold() -> dict[str, dict]:
    gold = {}
    for path in CASES_DIR.glob("*.jsonl"):
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                gold[row["id"]] = row
    return gold


async def main() -> None:
    db_url = "mysql+asyncmy://root:root_dev@localhost:3306/dw?charset=utf8mb4"
    engine = create_async_engine(db_url)
    gold_map = load_gold()
    e2e = json.loads(E2E.read_text(encoding="utf-8"))
    details = e2e.get("details") or []

    updated = []
    async with engine.connect() as conn:
        for d in details:
            row = dict(d)
            sql = d.get("pred_sql")
            g = gold_map.get(d.get("id") or "", {})
            if d.get("check") == "result" and sql:
                try:
                    result = await conn.execute(text(sql))
                    pred = [dict(r) for r in result.mappings().fetchall()]
                    if len(pred) == 1:
                        pred_val = pred[0]
                    else:
                        pred_val = pred
                    # JSON 可序列化
                    def _fix(x):
                        if isinstance(x, dict):
                            return {k: _fix(v) for k, v in x.items()}
                        if isinstance(x, list):
                            return [_fix(v) for v in x]
                        if hasattr(x, "__float__"):
                            try:
                                return float(x)
                            except Exception:  # noqa: BLE001
                                return x
                        return x

                    pred_val = _fix(pred_val)
                    row["result_match"] = result_match(g.get("gold_answer"), pred_val)
                    row["rescored"] = True
                except Exception as e:  # noqa: BLE001
                    row["result_match"] = False
                    row["rescore_error"] = str(e)[:160]
                    row["rescored"] = True
            updated.append(row)

    def rate(xs):
        return sum(1 for x in xs if x) / len(xs) if xs else 0.0

    by_cat = {}
    for r in updated:
        by_cat.setdefault(r.get("category") or "?", []).append(r)

    def cat_metrics(items):
        rf = [x["result_match"] for x in items if x.get("result_match") is not None]
        ef = [x["sql_executable"] for x in items if x.get("sql_executable") is not None]
        sf = [x["safety_ok"] for x in items if x.get("safety_ok") is not None]
        return {
            "total": len(items),
            "sql_executable_rate": rate([float(x) for x in ef]),
            "result_match_rate": rate([float(x) for x in rf]),
            "safety_violation_rate": rate([float(not x) for x in sf]) if sf else 0.0,
        }

    full = json.loads(FULL.read_text(encoding="utf-8")) if FULL.exists() else {}
    # 合并：仅更新有 pred_sql 的题
    merge_details = []
    for rec in full.get("details") or []:
        uid = rec["id"]
        hit = next((u for u in updated if u["id"] == uid), None)
        if hit and hit.get("rescored"):
            rec = {**rec, "result_match": hit.get("result_match"), "rescored": True}
        merge_details.append(rec)
    # 补上 updated 里不在 full 的
    seen = {r["id"] for r in merge_details}
    for u in updated:
        if u["id"] not in seen and u.get("rescored"):
            merge_details.append(
                {
                    "id": u["id"],
                    "category": u.get("category"),
                    "sql_executable": u.get("sql_executable"),
                    "result_match": u.get("result_match"),
                    "safety_ok": u.get("safety_ok"),
                    "rescored": True,
                }
            )

    rf = [r["result_match"] for r in merge_details if r.get("result_match") is not None]
    ef = [r["sql_executable"] for r in merge_details if r.get("sql_executable") is not None]
    sf = [r["safety_ok"] for r in merge_details if r.get("safety_ok") is not None]
    payload = {
        **full,
        "mode": "e2e-rescored",
        "sql_executable_rate": rate([float(x) for x in ef]),
        "result_match_rate": rate([float(x) for x in rf]),
        "safety_violation_rate": rate([float(not x) for x in sf]) if sf else 0.0,
        "counts": {
            "total": len(merge_details),
            "with_exec": len(ef),
            "with_result": len(rf),
            "with_safety": len(sf),
        },
        "by_category": {
            k: cat_metrics(v)
            for k, v in sorted(
                {
                    r.get("category") or "?": [
                        x for x in merge_details if x.get("category") == r.get("category")
                    ]
                    for r in merge_details
                }.items()
            )
        },
        "details": merge_details,
        "note": "result_match 已按新比对器对有 pred_sql 的题重判；1-33 题若无 pred_sql 仍保留原日志 match。",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("rescored details:", sum(1 for r in updated if r.get("rescored")))
    print("result_match_rate:", round(payload["result_match_rate"], 3))
    for cat, m in payload["by_category"].items():
        print(cat, m)
    fails = [r["id"] for r in merge_details if r.get("result_match") is False]
    print("still false:", fails)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
