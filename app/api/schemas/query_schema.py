"""
问数接口请求体定义

集中声明 API 层输入输出的数据结构，让路由函数只处理业务流程，
字段校验和 OpenAPI 文档生成交给 Pydantic 与 FastAPI 完成。
"""

from pydantic import BaseModel, Field, field_validator


class QuerySchema(BaseModel):
    """问数请求体，承载用户本轮原始自然语言问题。"""

    # 前端请求体中的 query 字段，例如 {"query": "统计华北地区销售额"}
    query: str = Field(min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        """去除首尾空白，避免创建只有空白内容的 MySQL 消息和 Checkpoint。"""

        query = value.strip()
        if not query:
            raise ValueError("查询内容不能为空。")
        return query
