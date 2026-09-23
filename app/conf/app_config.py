"""
应用主配置

定义 conf/app_config.yaml 在程序中的结构化配置对象
项目启动后会在这里一次性完成配置文件加载和类型化转换，其他模块只需要导入 app_config
就可以按属性方式读取日志 MySQL Qdrant Embedding Elasticsearch 和 LLM 配置
"""

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from omegaconf import OmegaConf


@dataclass
class File:
    """文件日志配置"""

    # 是否把日志写入磁盘文件。
    enable: bool
    # 文件日志最低级别。
    level: str
    # 日志文件输出路径。
    path: str
    # 单个日志文件的轮转条件，例如按大小或时间切分。
    rotation: str
    # 历史日志保留时长或数量。
    retention: str


@dataclass
class Console:
    """控制台日志配置"""

    # 是否向标准输出打印日志。
    enable: bool
    # 控制台日志最低级别。
    level: str


@dataclass
class LoggingConfig:
    """日志总配置"""

    # 文件日志子配置。
    file: File
    # 控制台日志子配置。
    console: Console


@dataclass
class DBConfig:
    """MySQL 连接配置"""

    # MySQL 服务主机名；Docker 内通常使用服务名，本机运行通常使用 localhost。
    host: str
    # MySQL TCP 端口。
    port: int
    # 数据库登录用户。
    user: str
    # 数据库登录密码，实际值应由环境变量注入。
    password: str
    # 连接后默认使用的数据库名称。
    database: str


@dataclass
class QdrantConfig:
    """Qdrant 连接与向量维度配置"""

    # Qdrant 服务主机名。
    host: str
    # Qdrant HTTP 端口。
    port: int
    # Collection 向量维度，必须与 Embedding 模型输出维度一致。
    embedding_size: int


@dataclass
class EmbeddingConfig:
    """Embedding 服务配置"""

    # Embedding 推理服务主机名。
    host: str
    # Embedding 推理服务端口。
    port: int
    # 当前使用的向量模型名称，用于客户端初始化和排查维度不匹配。
    model: str


@dataclass
class ESConfig:
    """Elasticsearch 配置"""

    # Elasticsearch 服务主机名。
    host: str
    # Elasticsearch HTTP 端口。
    port: int
    # 存放字段真实值、供 WHERE 条件召回使用的索引名称。
    index_name: str


@dataclass
class RedisConfig:
    """LangGraph Redis Checkpointer 配置。"""

    # Redis 连接串；数据库编号也包含在 URL 中。
    url: str
    # Checkpoint 默认存活分钟数，过期后可从 MySQL 历史重新水合。
    checkpoint_ttl_minutes: int
    # 读取 Thread 时是否续期，活跃会话因此不会在对话过程中突然过期。
    refresh_on_read: bool
    # 建立 Redis TCP 连接允许等待的最长秒数。
    connect_timeout_seconds: float
    # 单次 Redis 读写允许等待的最长秒数。
    operation_timeout_seconds: float
    # 应用启动时初始化 Saver/索引的最大尝试次数。
    startup_retries: int
    # 两次启动重试之间的等待秒数。
    retry_delay_seconds: float
    # Redis State 中保留的近期原始消息数量，超出部分进入滚动摘要。
    recent_message_limit: int
    # Checkpoint miss 时最多从 MySQL 读取多少条消息用于懒恢复。
    hydration_message_limit: int
    # 较早对话摘要的最大字符数，防止短期记忆无限增长。
    summary_max_chars: int


@dataclass
class LLMConfig:
    """大模型调用配置"""

    # 聊天模型名称。
    model_name: str
    # 模型服务鉴权密钥，实际值由环境变量注入。
    api_key: str
    # OpenAI 兼容模型服务的 API 根地址。
    base_url: str


@dataclass
class AppConfig:
    """项目级总配置入口"""

    # 日志输出配置。
    logging: LoggingConfig
    # Meta MySQL：保存 Schema/指标元数据和 P1 完整会话历史。
    db_meta: DBConfig
    # DW MySQL：承载最终问数 SQL 的执行数据。
    db_dw: DBConfig
    # 字段和指标向量检索配置。
    qdrant: QdrantConfig
    # 自然语言向量化服务配置。
    embedding: EmbeddingConfig
    # 字段真实值全文检索配置。
    es: ESConfig
    # P2 LangGraph 短期记忆配置。
    redis: RedisConfig
    # SQL 生成、改写和过滤使用的大模型配置。
    llm: LLMConfig


# 从当前文件位置回到项目根目录，再定位到 conf/app_config.yaml
project_root = Path(__file__).parents[2]
config_file = project_root / "conf" / "app_config.yaml"

# 先读取本地 .env，让 YAML 中的 ${oc.env:...} 可以解析到敏感配置
load_dotenv(project_root / ".env")

# 读取 YAML 配置内容
context = OmegaConf.load(config_file)

# 根据 AppConfig 生成结构化配置 schema
schema = OmegaConf.structured(AppConfig)

# 把配置结构和配置值合并，再转换成可以直接按属性访问的对象
app_config: AppConfig = OmegaConf.to_object(OmegaConf.merge(schema, context))

if __name__ == "__main__":
    # 简单测试：验证配置是否能正常读取
    print(app_config.es.host)
