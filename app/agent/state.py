"""
电商问数 Agent 状态定义

State 是 LangGraph 各节点之间传递和更新的共享数据
状态在用户原始问题之外维护关键词列表和三路召回结果
并把召回到的实体整理成后续提示词更容易消费的表信息和指标信息
SQL 生成闭环会继续写入候选 SQL 以及校验错误信息，用于控制校正或执行分支
"""

from typing import TypedDict

from app.entities.column_info import ColumnInfo
from app.entities.metric_info import MetricInfo
from app.entities.value_info import ValueInfo


class MetricInfoState(TypedDict):
    """面向 SQL 生成提示词的指标信息"""

    # 指标规范名称，例如 GMV；SQL 生成应优先使用该名称对应的业务口径。
    name: str
    # 指标计算含义和适用范围，用于约束模型不要只根据字段名猜测。
    description: str
    # 指标依赖的字段 id，用来提示模型不要脱离业务口径随意计算
    relevant_columns: list[str]
    # 用户可能使用的别名或同义表达，帮助模型对应自然语言问题。
    alias: list[str]


class ColumnInfoState(TypedDict):
    """表上下文中的字段信息"""

    # 数据库真实字段名，生成 SQL 时必须使用该名称。
    name: str
    # 字段数据库类型，用于判断聚合、比较和日期函数是否适用。
    type: str
    # 字段在业务模型中的角色，例如维度、度量或主外键。
    role: str
    # 字段真实样例值，尤其用于辅助 where 条件里的枚举值选择
    examples: list
    # 字段业务含义，弥补物理列名无法表达的语义。
    description: str
    # 字段别名，用于把用户表达与真实列名对应起来。
    alias: list[str]


class TableInfoState(TypedDict):
    """SQL 生成阶段真正传给模型的表结构上下文"""

    # 数据库真实表名。
    name: str
    # 表在业务模型中的用途，例如事实表或维度表。
    role: str
    # 表级业务说明，帮助模型选择正确数据域。
    description: str
    # 过滤后保留下来的相关字段，而不是整库所有列。
    columns: list[ColumnInfoState]


class DateInfoState(TypedDict):
    """SQL 生成阶段使用的当前日期上下文"""

    # 服务运行当天日期，用于解析“本月”“上季度”等相对时间表达。
    date: str
    # 当前星期信息，辅助自然语言日期换算。
    weekday: str
    # 当前季度信息，辅助季度类相对时间换算。
    quarter: str


class DBInfoState(TypedDict):
    """SQL 生成阶段使用的数据库环境信息"""

    # 目标数仓 SQL 方言，决定日期函数、分页等语法写法。
    dialect: str
    # 目标数据库版本，避免生成仅在其他版本可用的函数或语法。
    version: str


class DataAgentState(TypedDict):
    """一次问数链路中的临时状态，不由 Redis Checkpointer 持久化。"""

    # 经过上下文改写后的独立问题，是召回和 SQL 生成实际消费的查询。
    query: str
    # 用户本轮原始输入，保留它才能判断是否确实发生了上下文补全。
    original_query: str
    # 外层会话图提供的消息窗口与摘要，只在 resolve_query 节点中使用。
    conversation_context: str
    # 从独立问题中抽取的检索词，驱动字段、指标和真实值三路召回。
    keywords: list[str]
    # Qdrant 召回的原始字段实体，尚未完成表级聚合和过滤。
    retrieved_column_infos: list[ColumnInfo]
    # Qdrant 召回的原始指标实体，尚未经过业务相关性过滤。
    retrieved_metric_infos: list[MetricInfo]
    # Elasticsearch 召回的真实字段取值，用于约束 WHERE 条件。
    retrieved_value_infos: list[ValueInfo]

    # 将三路召回合并、补齐并过滤后得到的表结构 Prompt 上下文。
    table_infos: list[TableInfoState]
    # 过滤后的指标口径 Prompt 上下文。
    metric_infos: list[MetricInfoState]
    # 解析相对日期表达时使用的当天日期、星期和季度。
    date_info: DateInfoState
    # 目标数仓方言和版本，约束模型生成可执行 SQL。
    db_info: DBInfoState

    # 当前候选 SQL；生成节点写入，校验失败时由修正节点覆盖。
    sql: str

    # SQL 校验错误；None 表示进入执行分支，非空表示先进入修正节点。
    error: str
