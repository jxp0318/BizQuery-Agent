/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
export type ProgressStatus = "running" | "success" | "error";

/** 后端节点开始、完成或失败时发送的进度事件。 */
export type ProgressEvent = {
  /** SSE 判别字段，用于 AgentEvent 联合类型的分支收窄。 */
  type: "progress";
  /** 面向用户展示的流程步骤名称，例如「生成SQL」。 */
  step: string;
  /** 当前步骤的最新状态；同名步骤事件会在前端覆盖合并。 */
  status: ProgressStatus;
  /** P5.2：相对本轮开始的毫秒数，便于前端展示步骤耗时。 */
  elapsedMs?: number;
  /** P5.2：本轮 request_id，用于排障。 */
  requestId?: string;
};

/** SQL 执行结束后返回完整结果的数据事件。 */
export type ResultEvent = {
  type: "result";
  /** 结果结构取决于查询列，因此在展示前保持 unknown，避免前端错误假设字段。 */
  data: unknown;
  elapsedMs?: number;
  requestId?: string;
};

/** 结果超过 max_rows 被截断时的提示事件（P3）。 */
export type TruncatedEvent = {
  type: "truncated";
  message: string;
  max_rows: number;
  elapsedMs?: number;
  requestId?: string;
};

/** 流已经开始后发生异常时由后端发送的错误事件。 */
export type ErrorEvent = {
  type: "error";
  /** 可直接展示给当前会话用户的错误说明。 */
  message: string;
  /** 稳定原因码（P3），与后端 sql_errors 对齐；便于前端映射与排查。 */
  code?: string;
  /** P5.2：本轮 request_id，失败卡片可复制给开发者查日志。 */
  requestId?: string;
  elapsedMs?: number;
};

/** SQL 生成或校正完成后发送的事件。 */
export type SqlEvent = {
  type: "sql";
  sql: string;
  elapsedMs?: number;
  requestId?: string;
};

/** 上下文问题改写完成后的独立问题事件。 */
export type ResolvedQueryEvent = {
  type: "resolved_query";
  query: string;
  elapsedMs?: number;
  requestId?: string;
};

/** MySQL 成功创建本轮 user/assistant 两条消息后首先发送的关联事件。 */
export type TurnEvent = {
  type: "turn";
  /** 当前产品会话 ID，后端同时把它作为 LangGraph thread_id。 */
  conversationId: string;
  /** 已持久化 user 消息 ID，用于替换页面提交时创建的临时 ID。 */
  userMessageId: string;
  /** assistant 占位消息 ID，后续所有流式事件都归并到这条消息。 */
  assistantMessageId: string;
  /** P5.2：本轮 request_id。 */
  requestId?: string;
  elapsedMs?: number;
};

/** P5.2：LLM 长等待期间的空闲保活事件，UI 可忽略。 */
export type HeartbeatEvent = {
  type: "heartbeat";
  elapsedMs?: number;
  requestId?: string;
};

/** P5.1：一轮结束后的耗时/token 指标事件，UI 可忽略或做调试展示。 */
export type MetricsEvent = {
  type: "metrics";
  request_id?: string;
  total_ms?: number;
  llm_ms?: number;
  tokens_in?: number;
  tokens_out?: number;
  nodes?: RunMetricsPayload["nodes"];
  explain?: QueryExplain;
  elapsedMs?: number;
  requestId?: string;
};

/** P5.3：结果解释事件，早于 metrics 时也可先渲染面板。 */
export type ExplainEvent = {
  type: "explain";
  data: QueryExplain;
  elapsedMs?: number;
  requestId?: string;
};

/** streamQuery 可能收到的全部 SSE 事件联合类型。 */
export type AgentEvent =
  | ProgressEvent
  | ResultEvent
  | TruncatedEvent
  | ErrorEvent
  | SqlEvent
  | ResolvedQueryEvent
  | TurnEvent
  | HeartbeatEvent
  | MetricsEvent
  | ExplainEvent;

export type StepState = {
  /** Agent 流程步骤名称，是同一步骤多次状态更新时的合并键。 */
  step: string;
  /** 当前步骤最后一次收到的状态。 */
  status: ProgressStatus;
  /** 前端接收事件的毫秒时间戳，用于恢复和展示步骤更新时间。 */
  updatedAt: number;
};


/** 左侧历史列表的轻量会话摘要（与后端 ConversationSummarySchema 对齐）。 */
export type ConversationSummary = {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount?: number;
};

/** 恢复历史会话时的单条消息（与 ConversationMessageSchema 对齐，camelCase）。 */
export type ConversationMessage = {
  id: string;
  conversationId: string;
  role: "user" | "assistant" | string;
  content: string;
  status: string;
  resolvedQuery?: string | null;
  sql?: string | null;
  result?: unknown;
  steps?: StepState[];
  error?: string | null;
  createdAt: string;
  metrics?: RunMetricsPayload | null;
};

/** 会话详情：摘要 + 完整消息。 */
export type ConversationDetail = ConversationSummary & {
  messages: ConversationMessage[];
};


/** P5.3：结果解释上下文（后端 explain 事件 data）。 */
export type QueryExplain = {
  query?: string;
  tables?: Array<{
    name?: string;
    role?: string;
    description?: string;
    columns?: Array<{
      name?: string;
      description?: string;
      examples?: unknown[];
    }>;
  }>;
  metrics?: Array<{
    name?: string;
    description?: string;
    relevant_columns?: string[];
  }>;
  date_info?: { date?: string; weekday?: string; quarter?: string };
  db_info?: { dialect?: string; version?: string };
};

/** P5.1/P5.3：一轮耗时与 token 摘要（SSE metrics / 消息 metrics）。 */
export type RunMetricsPayload = {
  request_id?: string;
  total_ms?: number;
  llm_ms?: number;
  tokens_in?: number;
  tokens_out?: number;
  nodes?: Array<{
    node: string;
    node_ms: number;
    llm_ms: number;
    llm_calls?: number;
    tokens_in?: number;
    tokens_out?: number;
  }>;
  explain?: QueryExplain;
};

/** 当前页面渲染使用的消息状态，可能仍处于 SSE 流式执行中。 */
export type ChatMessage = {
  /** 消息稳定 ID；提交初期可能是前端临时 ID，收到 TurnEvent 后替换为 MySQL ID。 */
  id: string;
  /** 决定消息气泡方向和是否展示 Agent 执行证据。 */
  role: "user" | "assistant";
  /** 用户问题或 Agent 最终摘要正文。 */
  content: string;
  /** 前端统一使用的毫秒时间戳。 */
  createdAt: number;
  /** 该 assistant 消息的执行状态。 */
  status?: "streaming" | "done" | "error" | "cancelled";
  /** 从 progress 事件逐步合并得到的执行轨迹。 */
  steps?: StepState[];
  /** 本轮完整查询结果，只在当前流或 MySQL 历史恢复后存在。 */
  result?: unknown;
  /** 结果是否因超过 max_rows 被截断（P3）。 */
  truncated?: boolean;
  /** 生成或校正后的 SQL 文本，便于结果区展示与复制。 */
  sql?: string | null;
  /** 上下文补全后的独立问题。 */
  resolvedQuery?: string | null;
  /** P5.2：本轮 request_id，失败时展示便于排障。 */
  requestId?: string | null;
  /** 失败时的内部错误说明，可与 content 同时存在。 */
  error?: string;
  /** P5.3：结果解释（表/指标口径/环境）。 */
  explain?: QueryExplain | null;
  /** P5.1：分节点耗时与 token。 */
  metrics?: RunMetricsPayload | null;
};
