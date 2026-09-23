/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
export type ProgressStatus = "running" | "success" | "error";

/** 后端节点开始、完成或失败时发送的进度事件。 */
export type ProgressEvent = {
  /** SSE 判别字段，用于 AgentEvent 联合类型的分支收窄。 */
  type: "progress";
  /** 面向用户展示的流程步骤名称，例如“生成 SQL”。 */
  step: string;
  /** 当前步骤的最新状态；同名步骤事件会在前端覆盖合并。 */
  status: ProgressStatus;
};

/** SQL 执行结束后返回完整结果的数据事件。 */
export type ResultEvent = {
  type: "result";
  /** 结果结构取决于查询列，因此在展示前保持 unknown，避免前端错误假设字段。 */
  data: unknown;
};

/** 流已经开始后发生异常时由后端发送的错误事件。 */
export type ErrorEvent = {
  type: "error";
  /** 可直接展示给当前会话用户的错误说明。 */
  message: string;
};

/** SQL 生成或校正完成后发送的事件。 */
export type SqlEvent = { type: "sql"; sql: string };

/** 上下文问题改写完成后发送的独立问题事件。 */
export type ResolvedQueryEvent = { type: "resolved_query"; query: string };

/** MySQL 成功创建本轮 user/assistant 两条消息后首先发送的关联事件。 */
export type TurnEvent = {
  type: "turn";
  /** 当前产品会话 ID，后端同时把它作为 LangGraph thread_id。 */
  conversationId: string;
  /** 已持久化 user 消息 ID，用于替换页面提交时创建的临时 ID。 */
  userMessageId: string;
  /** assistant 占位消息 ID，后续所有流式事件都归并到这条消息。 */
  assistantMessageId: string;
};

/** streamQuery 可能收到的全部 SSE 事件联合类型。 */
export type AgentEvent =
  | ProgressEvent
  | ResultEvent
  | ErrorEvent
  | SqlEvent
  | ResolvedQueryEvent
  | TurnEvent;

export type StepState = {
  /** Agent 流程步骤名称，是同一步骤多次状态更新时的合并键。 */
  step: string;
  /** 当前步骤最后一次收到的状态。 */
  status: ProgressStatus;
  /** 前端接收事件的毫秒时间戳，用于恢复和展示步骤更新时间。 */
  updatedAt: number;
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
  /** 仅 assistant 消息需要的执行状态。 */
  status?: "streaming" | "done" | "error" | "cancelled";
  /** 从 progress 事件逐步合并得到的执行轨迹。 */
  steps?: StepState[];
  /** 本轮完整查询结果，只在当前流或 MySQL 历史恢复后存在。 */
  result?: unknown;
  /** 后端返回的失败或取消原因。 */
  error?: string;
  /** 本轮最终执行 SQL。 */
  sql?: string;
  /** 结合短期记忆补全后的独立问题。 */
  resolvedQuery?: string;
};

/** 左侧历史列表使用的轻量会话信息，不包含完整 messages。 */
export type ConversationSummary = {
  /** MySQL 会话主键，也是继续问数时携带的 conversationId。 */
  id: string;
  /** 历史列表展示标题。 */
  title: string;
  /** 后端返回的 ISO 创建时间。 */
  createdAt: string;
  /** 后端返回的 ISO 最近更新时间，用于列表排序和展示。 */
  updatedAt: string;
  /** user 与 assistant 消息总数。 */
  messageCount: number;
};

/** 从 MySQL 历史接口恢复的一条完整持久化消息。 */
export type PersistedMessage = {
  id: string;
  /** 用于确认该消息属于当前请求的会话。 */
  conversationId: string;
  role: "user" | "assistant";
  content: string;
  /** 数据库保存的最终或中间执行状态。 */
  status: "streaming" | "done" | "error" | "cancelled";
  resolvedQuery?: string | null;
  sql?: string | null;
  /** 完整结果由 MySQL 提供；Redis 只保存行数摘要。 */
  result?: unknown;
  steps: StepState[];
  error?: string | null;
  /** ISO 时间字符串，进入 ChatMessage 时转换为毫秒时间戳。 */
  createdAt: string;
};

/** 打开某个历史会话时返回的摘要与完整消息集合。 */
export type ConversationDetail = ConversationSummary & {
  messages: PersistedMessage[];
};
