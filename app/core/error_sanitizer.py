"""业务异常与基础设施异常的脱敏映射（P5.4）。

同一失败拆两份：
1. 用户：稳定 code + 可操作短句，禁止带库账号、主机、SQL 片段或堆栈。
2. 日志/内部：完整异常，用 request_id 关联。

在 sql_errors 的 P3 原因码之上，补充 LLM / 数据库 / 依赖服务等运行期错误码，
供 QueryService 在统一出口做脱敏，而不是把 str(error) 直接甩给前端。
"""

from __future__ import annotations

from app.agent.sql_errors import (
    CANCELLED,
    QUERY_TIMEOUT,
    USER_MESSAGES,
    SqlCheckError,
)

# ---- 运行期原因码（P5.4 最小集）----
DB_ERROR = "db_error"
LLM_ERROR = "llm_error"
DEPENDENCY_ERROR = "dependency_error"
INTERNAL_ERROR = "internal_error"

# 用户可见短句：只描述现象与建议，不含内网地址、账号或原始驱动报错。
USER_MESSAGES.update(
    {
        DB_ERROR: "数据服务暂时不可用，请稍后重试。",
        LLM_ERROR: "模型服务暂时不可用，请稍后重试。",
        DEPENDENCY_ERROR: "检索服务暂时不可用，请稍后重试。",
        INTERNAL_ERROR: "系统内部错误，请稍后重试。",
    }
)


def sanitize_exception(error: BaseException) -> SqlCheckError:
    """把任意异常映射为可对用户展示的脱敏错误载荷。

    Args:
        error: 捕获到的原始异常；完整内容只应进入日志。

    Returns:
        SqlCheckError：`for_user()` 为安全短句，`for_model()`/detail 不含密钥。
    """

    if isinstance(error, SqlCheckError):
        return error

    name = type(error).__name__
    text = str(error)
    lowered = f"{name} {text}".lower()

    if "cancel" in lowered or name in {"CancelledError", "KeyboardInterrupt"}:
        return SqlCheckError(
            code=CANCELLED,
            detail="请求被取消",
            user_message=USER_MESSAGES[CANCELLED],
        )

    if "timeout" in lowered or "timed out" in lowered:
        return SqlCheckError(
            code=QUERY_TIMEOUT,
            detail="操作超时",
            user_message=USER_MESSAGES[QUERY_TIMEOUT],
        )

    # 数据库驱动/连接类错误：细节进日志，对外统一「数据服务不可用」
    if (
        "operationalerror" in lowered
        or "asyncmy" in lowered
        or "sqlalchemy" in lowered
        or "access denied" in lowered
        or "can't connect" in lowered
        or "pymysql" in lowered
    ):
        return SqlCheckError(
            code=DB_ERROR,
            detail=f"database_error: {name}",
            user_message=USER_MESSAGES[DB_ERROR],
        )

    # 向量/全文/Embedding 等旁路依赖
    if (
        "qdrant" in lowered
        or "elastic" in lowered
        or "huggingface" in lowered
        or "embedding" in lowered
        or "connection error" in lowered
        or "connecterror" in lowered
        or "connectionerror" in lowered
        or "redis" in lowered
    ):
        return SqlCheckError(
            code=DEPENDENCY_ERROR,
            detail=f"dependency_error: {name}",
            user_message=USER_MESSAGES[DEPENDENCY_ERROR],
        )

    # 模型服务
    if (
        "openai" in lowered
        or "deepseek" in lowered
        or "llm" in lowered
        or "rate limit" in lowered
        or "api key" in lowered
        or "authentication" in lowered
    ):
        return SqlCheckError(
            code=LLM_ERROR,
            detail=f"llm_error: {name}",
            user_message=USER_MESSAGES[LLM_ERROR],
        )

    return SqlCheckError(
        code=INTERNAL_ERROR,
        detail=f"internal_error: {name}",
        user_message=USER_MESSAGES[INTERNAL_ERROR],
    )
