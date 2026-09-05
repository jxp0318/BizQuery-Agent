"""
元数据库 MySQL 仓储

这一层对应文档里的 Meta Repository，负责接收业务实体并落到 Meta MySQL
Repository 自身只关心“如何写入”，而“哪些写操作要放在同一笔事务里”，由 Service 层统一决定

表 字段 指标和字段指标关系都会先以业务实体流转，再在这里统一转成 ORM 模型
问数链路运行时也会从这里读取元数据，用来把召回到的 id 补齐成完整实体
"""

from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.entities.column_info import ColumnInfo
from app.entities.column_metric import ColumnMetric
from app.entities.metric_info import MetricInfo
from app.entities.table_info import TableInfo
from app.models.column_info import ColumnInfoMySQL
from app.models.column_metric import ColumnMetricMySQL
from app.models.metric_info import MetricInfoMySQL
from app.models.table_info import TableInfoMySQL
from app.repositories.mysql.meta.mappers.column_info_mapper import ColumnInfoMapper
from app.repositories.mysql.meta.mappers.column_metric_mapper import ColumnMetricMapper
from app.repositories.mysql.meta.mappers.metric_info_mapper import MetricInfoMapper
from app.repositories.mysql.meta.mappers.table_info_mapper import TableInfoMapper


class MetaMySQLRepository:
    """负责把元数据业务实体持久化到 Meta MySQL"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def save_table_infos(self, table_infos: list[TableInfo]):
        """批量保存表元数据。支持冲突更新"""
        if not table_infos:
            return

        models = [TableInfoMapper.to_model(ti) for ti in table_infos]
        stmt = mysql_insert(TableInfoMySQL).values(
            [
                {
                    "id": m.id,
                    "name": m.name,
                    "role": m.role,
                    "description": m.description,
                }
                for m in models
            ]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            role=stmt.inserted.role,
            description=stmt.inserted.description,
        )
        await self.session.execute(stmt)

    async def save_column_infos(self, column_infos: list[ColumnInfo]):
        """批量保存字段元数据。支持冲突更新"""
        if not column_infos:
            return

        models = [ColumnInfoMapper.to_model(ci) for ci in column_infos]
        stmt = mysql_insert(ColumnInfoMySQL).values(
            [
                {
                    "id": m.id,
                    "name": m.name,
                    "type": m.type,
                    "role": m.role,
                    "examples": m.examples,
                    "description": m.description,
                    "alias": m.alias,
                    "table_id": m.table_id,
                }
                for m in models
            ]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            type=stmt.inserted.type,
            role=stmt.inserted.role,
            examples=stmt.inserted.examples,
            description=stmt.inserted.description,
            alias=stmt.inserted.alias,
            table_id=stmt.inserted.table_id,
        )
        await self.session.execute(stmt)

    async def save_metric_infos(self, metric_infos: list[MetricInfo]):
        """批量保存指标元数据。支持冲突更新"""
        if not metric_infos:
            return

        models = [MetricInfoMapper.to_model(mi) for mi in metric_infos]
        stmt = mysql_insert(MetricInfoMySQL).values(
            [
                {
                    "id": m.id,
                    "name": m.name,
                    "description": m.description,
                    "relevant_columns": m.relevant_columns,
                    "alias": m.alias,
                }
                for m in models
            ]
        )
        stmt = stmt.on_duplicate_key_update(
            name=stmt.inserted.name,
            description=stmt.inserted.description,
            relevant_columns=stmt.inserted.relevant_columns,
            alias=stmt.inserted.alias,
        )
        await self.session.execute(stmt)

    async def save_column_metrics(self, column_metrics: list[ColumnMetric]):
        """批量保存字段与指标的关联关系。支持冲突忽略"""
        if not column_metrics:
            return

        models = [ColumnMetricMapper.to_model(cm) for cm in column_metrics]
        stmt = mysql_insert(ColumnMetricMySQL).values(
            [
                {
                    "column_id": m.column_id,
                    "metric_id": m.metric_id,
                }
                for m in models
            ]
        )
        stmt = stmt.on_duplicate_key_update(
            column_id=stmt.inserted.column_id
        )
        await self.session.execute(stmt)

    async def get_column_info_by_id(self, id: str) -> ColumnInfo | None:
        """按字段 id 查询字段元数据，供召回信息合并阶段补齐字段上下文"""

        column_info: ColumnInfoMySQL | None = await self.session.get(
            ColumnInfoMySQL, id
        )
        if column_info:
            return ColumnInfoMapper.to_entity(column_info)
        else:
            return None

    async def get_table_info_by_id(self, id: str) -> TableInfo | None:
        """按表 id 查询表元数据，最终组装成提示词里的表结构信息"""

        table_info: TableInfoMySQL | None = await self.session.get(TableInfoMySQL, id)
        if table_info:
            return TableInfoMapper.to_entity(table_info)
        else:
            return None

    async def get_key_columns_by_table_id(self, table_id: str) -> list[ColumnInfo]:
        """查询指定表的主外键字段，避免 Join 关键字段被向量召回漏掉"""

        # 主外键字段用于后续生成 join 条件，不能完全依赖向量召回命中
        sql = "select * from column_info where table_id = :table_id and role in ('primary_key','foreign_key')"
        # :table_id 是 SQLAlchemy text SQL 的占位符，实际值通过第二个参数传入
        result = await self.session.execute(text(sql), {"table_id": table_id})
        # mappings() 会把结果行转成类似字典的结构，便于解包成 ColumnInfo
        return [ColumnInfo(**dict(row)) for row in result.mappings().fetchall()]
