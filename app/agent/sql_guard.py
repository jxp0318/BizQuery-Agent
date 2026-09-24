"""SQL 程序化安全闸（sqlglot AST）。

职责：在 EXPLAIN 与执行之前，用确定性规则判断「这条 SQL 是否允许进入数据库」。
不调用大模型；解析失败 fail-closed。规则分层：
1. 语句级：单条 SELECT / WITH…SELECT，禁止写操作与危险子句。
2. 对象级：物理表白名单（对齐 Meta 元数据），系统库默认拒绝。
3. 函数级：危险函数黑名单。

设计取舍见 docs/improvement-roadmap.md P3；面试口径见 docs/interview-qa.md Q2～Q5。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import sqlglot
from sqlglot import exp

from app.agent.sql_errors import (
    FORBIDDEN_FUNCTION,
    PARSE_FAILED,
    TABLE_NOT_ALLOWED,
    UNSAFE_STATEMENT,
    SqlCheckError,
)

# 仅允许的顶层只读查询形态；WITH…SELECT 在 sqlglot 中仍是 Select（含 with）。
_ALLOWED_TOP_TYPES = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# 系统/元数据库：业务问数不应直接探查，防绕过 Meta RAG 拖库结构。
_BLOCKED_SCHEMAS = {
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
}

# 危险函数黑名单（大小写不敏感）。陌生函数一期放行，靠只读账号与超时纵深防御。
_FORBIDDEN_FUNCTIONS = {
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "GET_LOCK",
    "RELEASE_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "MASTER_POS_WAIT",
    "WAIT_FOR_EXECUTED_GTID_SET",
    "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS",
    "SYS_EVAL",
    "SYS_EXEC",
}

# 写文件等子句形态：可能以 INTO OUTFILE / INTO DUMPFILE 出现，不单靠函数名。
_FORBIDDEN_CLAUSE_SNIPPETS = (
    "INTO OUTFILE",
    "INTO DUMPFILE",
)


def _strip_trailing_semicolon(sql: str) -> str:
    """去掉末尾若干空白与单个分号；中间仍有分号则留给多语句检查拒绝。"""

    return sql.strip().rstrip(";").strip()


@lru_cache(maxsize=1)
def load_allowed_table_names() -> frozenset[str]:
    """从 Meta 配置加载允许访问的物理表名，与元数据 RAG 同源。

    Returns:
        小写表名集合。配置缺失时返回空集（fail-closed：未登记表一律拒绝）。
    """

    config_path = Path("conf") / "meta_config.yaml"
    if not config_path.exists():
        return frozenset()
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    tables = data.get("tables") or []
    names = {str(item.get("name", "")).lower() for item in tables if item.get("name")}
    return frozenset(names)


def _collect_physical_tables(statement: exp.Expression) -> list[exp.Table]:
    """收集语句中的物理表节点；CTE 别名不当作物理表。

    sqlglot 的 Table 节点对应 FROM/JOIN 后的真实或派生表名，CTE 引用通常带 alias，
    通过 with_ 的 cte 名称集合做差集，避免 WITH t AS (...) 里的 t 误判。
    """

    cte_names: set[str] = set()
    for with_ in statement.find_all(exp.With):
        for cte in with_.expressions:
            if cte.alias:
                cte_names.add(cte.alias.lower())

    physical: list[exp.Table] = []
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        physical.append(table)
    return physical


def _reject(code: str, detail: str) -> SqlCheckError:
    return SqlCheckError(code=code, detail=detail)


def check_sql(sql: str) -> SqlCheckError | None:
    """对候选 SQL 做 AST 安全检查。

    Args:
        sql: generate_sql / correct_sql 产出的候选查询文本。

    Returns:
        None 表示通过，可进入 EXPLAIN；否则返回含 code/detail 的错误对象。

    规则顺序：形态与单语句 → 语句类型 → 危险子句/函数 → 表白名单。
    解析失败一律拒绝（fail-closed），不猜测作者意图。
    """

    raw = (sql or "").strip()
    if not raw:
        return _reject(PARSE_FAILED, "SQL 为空")

    # 多语句：去掉末尾单个分号后若仍含分号，视为多条。
    body = _strip_trailing_semicolon(raw)
    if ";" in body:
        return _reject(UNSAFE_STATEMENT, "禁止多语句；只允许单条 SELECT 或 WITH…SELECT")

    upper = raw.upper()
    for snippet in _FORBIDDEN_CLAUSE_SNIPPETS:
        if snippet in upper:
            return _reject(
                UNSAFE_STATEMENT,
                f"禁止子句 {snippet}；问数场景只读，不允许导出文件",
            )

    if " FOR UPDATE" in upper or upper.rstrip().endswith("FOR UPDATE"):
        return _reject(UNSAFE_STATEMENT, "禁止 FOR UPDATE；分析查询不得加锁")

    try:
        # MySQL 方言解析；失败 fail-closed。
        statements = sqlglot.parse(raw, read="mysql")
    except Exception as error:  # noqa: BLE001 - 解析器异常类型不稳定，统一归为 parse_failed
        return _reject(PARSE_FAILED, f"SQL 无法解析：{error}")

    if len(statements) != 1 or statements[0] is None:
        return _reject(UNSAFE_STATEMENT, "禁止多语句；只允许单条查询")

    statement = statements[0]
    if not isinstance(statement, _ALLOWED_TOP_TYPES):
        return _reject(
            UNSAFE_STATEMENT,
            f"仅允许 SELECT / WITH…SELECT，检测到 {type(statement).__name__}",
        )

    # 函数黑名单
    for func in statement.find_all(exp.Func):
        name = (func.sql_names()[0] if func.sql_names() else type(func).__name__).upper()
        # Anonymous 与具体子类都按名称匹配
        candidate = (getattr(func, "this", None), name)
        text_name = name
        if isinstance(func, exp.Anonymous):
            text_name = (func.name or "").upper()
        if text_name in _FORBIDDEN_FUNCTIONS or name in _FORBIDDEN_FUNCTIONS:
            return _reject(
                FORBIDDEN_FUNCTION,
                f"禁止危险函数 {text_name or name}；请改写为只读分析函数",
            )
        _ = candidate  # 保留结构可读性

    # 表白名单（库名+表名；无库名时只看表名）
    allowed = load_allowed_table_names()
    for table in _collect_physical_tables(statement):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        if schema in _BLOCKED_SCHEMAS:
            return _reject(
                TABLE_NOT_ALLOWED,
                f"禁止访问系统库 {schema}",
            )
        if schema and schema not in {"dw", "shopkeeper"}:
            # 默认仅允许无库名或业务库前缀；其它显式库名需在允许集合中出现表名且库为 dw
            return _reject(
                TABLE_NOT_ALLOWED,
                f"禁止跨库访问 {schema}.{name}",
            )
        if name not in allowed:
            return _reject(
                TABLE_NOT_ALLOWED,
                f"表 {name} 不在可分析数据范围内",
            )

    return None
