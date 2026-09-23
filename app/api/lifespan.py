"""
FastAPI 应用生命周期管理

负责在服务启动时初始化外部客户端，在服务关闭时释放连接资源。
这些客户端是应用级资源，适合在 lifespan 中创建一次并复用，而不是每个请求
重复初始化。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.clients.redis_checkpoint_manager import redis_checkpoint_manager
from app.models.base import Base
from app.models.conversation import ConversationMessageMySQL, ConversationMySQL


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理 FastAPI 启动和关闭阶段的应用级外部资源。

    Args:
        app: 当前 FastAPI 应用实例；参数由框架传入，本函数不直接修改应用状态。

    Yields:
        控制权交还 FastAPI 处理请求。退出 yield 后统一释放所有异步连接。

    Redis Saver、向量库和数据库连接池都具有初始化成本并需要显式关闭，因此在
    lifespan 中创建一次并由请求复用，而不是在 Depends 中反复建立网络连接。
    """

    # 启动阶段先建立各类外部服务客户端，后续 Depends 只组装轻量 Repository。
    # 任一关键依赖初始化失败都会阻止应用进入服务状态，避免请求运行到中途才失败。
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()
    try:
        await redis_checkpoint_manager.init()

        # 使用 create_all 非破坏性补齐 P1 会话表，让已有 Docker volume 升级后
        # 无需清库；复杂字段迁移仍应在生产环境改用正式 migration 工具。
        _ = (ConversationMySQL, ConversationMessageMySQL)
        async with meta_mysql_client_manager.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        # yield 之前是启动逻辑，yield 之后是关闭逻辑；中间阶段由 FastAPI 处理请求。
        yield
    finally:
        # finally 保证正常退出和启动后异常都执行清理，避免开发热重载留下连接池。
        await redis_checkpoint_manager.close()
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()
