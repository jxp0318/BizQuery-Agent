"""合并两次 e2e 结果为 100 题汇总报告。

前 33 条来自第一次全量运行的终端日志（崩溃前未写盘）；
34～100 来自 evals/report/e2e-report.json。
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.metrics import aggregate_metrics

REPORT = Path("evals/report/e2e-report.json")
OUT = Path("evals/report/e2e-full-100.json")

# 第一次运行日志（A-001～B-003）：exec / match
LOG_FIRST_33 = [
    ("A-001", "A", True, True),
    ("A-002", "A", True, True),
    ("A-003", "A", True, True),
    ("A-004", "A", True, True),
    ("A-005", "A", True, True),
    ("A-006", "A", True, True),
    ("A-007", "A", True, True),
    ("A-008", "A", True, True),
    ("A-009", "A", True, True),
    ("A-010", "A", True, True),
    ("A-011", "A", True, True),
    ("A-012", "A", True, True),
    ("A-013", "A", True, True),
    ("A-014", "A", True, True),
    ("A-015", "A", True, True),
    ("A-016", "A", True, True),
    ("A-017", "A", True, True),
    ("A-018", "A", True, True),
    ("A-019", "A", True, True),
    ("A-020", "A", True, True),
    ("A-021", "A", True, True),
    ("A-022", "A", True, True),
    ("A-023", "A", True, True),
    ("A-024", "A", True, True),
    ("A-025", "A", True, True),
    ("A-026", "A", True, True),
    ("A-027", "A", True, True),
    ("A-028", "A", True, False),
    ("A-029", "A", True, True),
    ("A-030", "A", True, True),
    ("B-001", "B", True, True),
    ("B-002", "B", True, True),
    ("B-003", "B", True, True),
]


def main() -> None:
    rest = json.loads(REPORT.read_text(encoding="utf-8"))
    rest_details = rest.get("details") or []
    by_id = {d["id"]: d for d in rest_details}

    records = []
    for cid, cat, exec_ok, match_ok in LOG_FIRST_33:
        detail = by_id.get(cid, {})
        records.append(
            {
                "id": cid,
                "category": cat,
                "source": "log-first-run",
                "recall_columns": detail.get("recall_columns"),
                "recall_metrics": detail.get("recall_metrics"),
                "recall_values": detail.get("recall_values"),
                "sql_executable": exec_ok,
                "result_match": match_ok,
                "safety_ok": detail.get("safety_ok"),
            }
        )

    # 后 67 条用盘上详细报告（若与前 33 重叠则跳过，避免重复计）
    seen = {cid for cid, *_ in LOG_FIRST_33}
    for d in rest_details:
        if d["id"] in seen:
            continue
        records.append(
            {
                "id": d["id"],
                "category": d.get("category"),
                "source": "log-second-run",
                "recall_columns": d.get("recall_columns"),
                "recall_metrics": d.get("recall_metrics"),
                "recall_values": d.get("recall_values"),
                "sql_executable": d.get("sql_executable"),
                "result_match": d.get("result_match"),
                "safety_ok": d.get("safety_ok"),
            }
        )

    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    records.sort(key=lambda r: (order.get(r["category"], 9), r["id"]))

    metrics = aggregate_metrics(records)
    by_cat: dict[str, list] = {}
    for rec in records:
        by_cat.setdefault(rec["category"], []).append(rec)
    metrics["by_category"] = {k: aggregate_metrics(v) for k, v in sorted(by_cat.items())}
    metrics["mode"] = "e2e-merged"
    metrics["note"] = (
        "1-33 来自第一次运行日志（仅 exec/match）；34-100 来自 e2e-report.json。"
        "Recall 仅在有 pred 细节的题上统计。"
    )
    metrics["details"] = records

    OUT.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print("merged cases:", len(records))
    for k in (
        "field_recall_at_10",
        "metric_recall_at_5",
        "value_recall_at_5",
        "sql_executable_rate",
        "result_match_rate",
        "safety_violation_rate",
    ):
        print(f"{k}: {metrics[k]:.3f}")
    print("counts:", metrics["counts"])
    print("by_category:")
    for cat, m in metrics["by_category"].items():
        c = m.get("counts") or {}
        print(
            f"  {cat}: n={c.get('total')} exec={m.get('sql_executable_rate', 0):.2f} "
            f"match={m.get('result_match_rate', 0):.2f} "
            f"safety_violation={m.get('safety_violation_rate', 0):.2f}"
        )
    fails = [r for r in records if r.get("result_match") is False]
    print("result mismatches:", len(fails))
    for r in fails:
        print(" -", r["id"])
    print("report:", OUT)


if __name__ == "__main__":
    main()
