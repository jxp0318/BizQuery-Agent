"""用 gold_sql 在冻结数仓上刷 gold_answer（禁止手填）。"""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _jsonable(value):
    """Decimal/日期等转换为 JSON 可序列化值。"""

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value

CASES_DIR = Path("evals/cases")


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db-url",
        default="mysql+asyncmy://root:root_dev@localhost:3306/dw?charset=utf8mb4",
    )
    args = parser.parse_args()
    engine = create_async_engine(args.db_url)

    for path in sorted(CASES_DIR.glob("*.jsonl")):
        rows = load_rows(path)
        async with engine.connect() as conn:
            for row in rows:
                sql = row.get("gold_sql")
                if not sql:
                    continue
                try:
                    result = await conn.execute(text(sql))
                    mapped = [dict(r) for r in result.mappings().fetchall()]
                    mapped = _jsonable(mapped)
                    if len(mapped) == 1:
                        row["gold_answer"] = mapped[0]
                    else:
                        row["gold_answer"] = mapped
                except Exception as e:  # noqa: BLE001
                    row["gold_answer"] = None
                    row["gold_error"] = str(e)[:200]
        save_rows(path, rows)
        filled = sum(1 for r in rows if r.get("gold_answer") is not None)
        print(f"{path.name}: {filled}/{len(rows)} gold_answer filled")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
