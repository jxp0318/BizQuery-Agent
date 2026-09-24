"""评测指标：Recall@k、SQL 可执行率、结果一致率、安全违规率。"""

from __future__ import annotations

from typing import Any, Iterable


def recall_at_k(gold: Iterable[str], retrieved: Iterable[str], k: int) -> float:
    """金标对象出现在检索 top-k 中的比例。"""

    gold_set = {g.strip().lower() for g in gold if g}
    if not gold_set:
        return 1.0
    top = [r.strip().lower() for r in list(retrieved)[:k] if r]
    hit = sum(1 for g in gold_set if g in top)
    return hit / len(gold_set)


def normalize_rows(data: Any) -> list[tuple]:
    """把结果规整为可比对的行元组集合（忽略行序与列名别名差异时可再扩展）。"""

    if data is None:
        return []
    if isinstance(data, dict):
        # 单对象答案：{col: val} 或 {label: val}
        return [tuple(sorted((str(k), _norm_val(v)) for k, v in data.items()))]
    if isinstance(data, list):
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append(tuple(sorted((str(k), _norm_val(v)) for k, v in item.items())))
            elif isinstance(item, (list, tuple)):
                rows.append(tuple(_norm_val(v) for v in item))
            else:
                rows.append((_norm_val(item),))
        return rows
    return [(_norm_val(data),)]


def _norm_val(v: Any) -> Any:
    """数值统一为两位小数 float，兼容字符串数字（SSE 可能把 Decimal 变成 str）。"""

    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return round(float(v), 2)
    if isinstance(v, str):
        try:
            return round(float(v.strip()), 2)
        except ValueError:
            return v
    return v


def result_match(gold_answer: Any, pred_answer: Any) -> bool:
    """结果一致：忽略行序、列名别名；允许预测多带常量维度列。

    匹配顺序：
    1. 键值行集合完全一致
    2. 仅数值集合一致（列名不同）
    3. 金标行的值是某预测行值的子集（预测多 select 年份等冗余列）
    """

    gold_rows = set(normalize_rows(gold_answer))
    pred_rows = set(normalize_rows(pred_answer))
    if gold_rows == pred_rows:
        return True

    def row_values(row: tuple) -> set:
        if all(isinstance(item, tuple) and len(item) == 2 for item in row):
            return {_norm_val(v) for _k, v in row}
        return {_norm_val(v) for v in row}

    def values_only(rows: set[tuple]) -> set[tuple]:
        def _sort_key(v: Any) -> str:
            return f"{type(v).__name__}:{v}"

        return {tuple(sorted(row_values(row), key=_sort_key)) for row in rows}

    if values_only(gold_rows) == values_only(pred_rows):
        return True

    pred_value_sets = [row_values(row) for row in pred_rows]
    for grow in gold_rows:
        gv = row_values(grow)
        if not any(gv.issubset(pv) or pv.issubset(gv) for pv in pred_value_sets):
            return False
    return True


def aggregate_metrics(records: list[dict]) -> dict:
    """汇总 6 个一期指标。records 为逐题评测结果。"""

    col_scores = [r["recall_columns"] for r in records if r.get("recall_columns") is not None]
    met_scores = [r["recall_metrics"] for r in records if r.get("recall_metrics") is not None]
    val_scores = [r["recall_values"] for r in records if r.get("recall_values") is not None]
    exec_flags = [r["sql_executable"] for r in records if r.get("sql_executable") is not None]
    result_flags = [r["result_match"] for r in records if r.get("result_match") is not None]
    safety_flags = [r["safety_ok"] for r in records if r.get("safety_ok") is not None]

    def avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "field_recall_at_10": avg(col_scores),
        "metric_recall_at_5": avg(met_scores),
        "value_recall_at_5": avg(val_scores),
        "sql_executable_rate": avg([float(x) for x in exec_flags]),
        "result_match_rate": avg([float(x) for x in result_flags]),
        "safety_violation_rate": avg([float(not x) for x in safety_flags]) if safety_flags else 0.0,
        "counts": {
            "total": len(records),
            "with_exec": len(exec_flags),
            "with_result": len(result_flags),
            "with_safety": len(safety_flags),
        },
    }
