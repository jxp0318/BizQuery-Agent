"""
数仓 MySQL 仓储

这一层对应文档里的 DW Repository，职责是到真实数仓中补齐配置文件里
没有显式维护的信息，例如字段类型和字段示例值。Service 层只关心
“需要哪些信息”，具体怎样查数仓由仓储层统一封装
SQL 生成闭环中的数据库环境读取 SQL 校验和最终查询执行也集中放在这里
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class DWMySQLRepository:
    """负责查询数仓真实表结构和字段样例值"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_column_types(self, table_name: str) -> dict[str, str]:
        """查询整张表的字段类型，作为 ColumnInfo.type 的真实来源"""
        sql = f"show columns from {table_name}"
        result = await self.session.execute(text(sql))
        result_dict = result.mappings().fetchall()
        return {row["Field"]: row["Type"] for row in result_dict}

    async def get_column_values(
        self, table_name: str, column_name: str, limit: int = 10
    ) -> list:
        """抽样查询字段示例值，供元数据入库和后续检索链路复用"""
        sql = f"select distinct {column_name} from {table_name} limit {limit}"
        result = await self.session.execute(text(sql))
        return [row[0] for row in result.fetchall()]

    async def get_db_info(self):
        """读取当前数仓数据库的方言和版本，供 SQL 生成提示词使用"""

        sql = "select version()"
        result = await self.session.execute(text(sql))
        version = result.scalar()

        # dialect 来自 SQLAlchemy 当前绑定的数据库方言，例如 mysql
        dialect = self.session.bind.dialect.name
        return {"dialect": dialect, "version": version}

    async def validate(self, sql: str):
        """用 EXPLAIN 让数据库提前解析 SQL，发现语法 表名 字段名等错误"""
        sql = f"explain {sql}"
        await self.session.execute(text(sql))

    async def run(
        self,
        sql: str,
        *,
        max_rows: int = 1000,
        timeout_seconds: float | None = None,
    ) -> dict:
        """受控执行最终 SQL：超时 + 行数截断，不改写原始 SQL。

        Args:
            sql: 已通过 sql_guard 与 EXPLAIN 的候选查询。
            max_rows: 最多返回的业务行数；超出截断并标记 truncated。
            timeout_seconds: 优先写入会话 MAX_EXECUTION_TIME，由 MySQL 侧超时。

        Returns:
            {"rows": list[dict], "row_count": int, "truncated": bool}

        实现说明:
            不用 SELECT * FROM (...) LIMIT 外包，避免改写 CTE/排序等语义；
            在游标上 fetchmany(max_rows+1) 多取一行用于判断是否截断。
        """

        if timeout_seconds:
            # MySQL 8 的 MAX_EXECUTION_TIME 单位是毫秒，仅对 SELECT 生效。
            await self.session.execute(
                text("SET SESSION MAX_EXECUTION_TIME = :ms"),
                {"ms": int(timeout_seconds * 1000)},
            )
        result = await self.session.execute(text(sql))
        chunk = result.mappings().fetchmany(max_rows + 1)
        truncated = len(chunk) > max_rows
        rows = [dict(row) for row in chunk[:max_rows]]
        return {"rows": rows, "row_count": len(rows), "truncated": truncated}

    async def connection_id(self) -> int:
        """读取当前连接 CONNECTION_ID，供取消时 KILL。"""

        result = await self.session.execute(text("SELECT CONNECTION_ID()"))
        return int(result.scalar())

    async def kill_connection(self, connection_id: int) -> bool:
        """尽力终止指定连接；失败返回 False，由调用方记日志。

        使用独立 execute：KILL 需要足够权限；开发环境可用同账号。
        生产应改用只读账号 + 管理账号执行 KILL（P8）。
        """

        try:
            await self.session.execute(text(f"KILL {int(connection_id)}"))
            return True
        except Exception:
            return False
