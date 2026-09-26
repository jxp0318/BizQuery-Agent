/**
 * 前端应用主组件
 * 负责聊天会话状态、SSE 事件消费、MySQL 历史恢复和跨标签页同步。
 * Redis 短期记忆完全由后端按 conversation_id 管理，前端只维护当前选中会话 ID。
 */
import {
  Activity,
  BarChart3,
  Eraser,
  History,
  Leaf,
  MessageSquarePlus,
  Server,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { ConversationList } from "./components/ConversationList";
import { DeleteConversationDialog } from "./components/DeleteConversationDialog";
import { EmptyState } from "./components/EmptyState";
import { MessageBubble } from "./components/MessageBubble";
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  streamQuery,
} from "./lib/agentApi";
import { cn, summarizeResult } from "./lib/format";
import type {
  AgentEvent,
  ChatMessage,
  ConversationDetail,
  ConversationSummary,
  StepState,
} from "./types/agent";

const examples = [
  "统计 2025 年第一季度各大区的 GMV，并按 GMV 从高到低排序",
  "统计 2025 年 3 月各商品品类的销量和销售额",
  "查询华东地区 2025 年第一季度销售额最高的前 5 个商品",
  "按会员等级统计 2025 年第一季度的订单数和销售额",
];

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "Vite /api proxy";
// 多标签页只通过该频道广播“会话发生变化”，真实消息始终重新从 MySQL 接口读取。
const CONVERSATION_CHANNEL = "bizquery-conversations";

function makeId() {
  return crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function upsertStep(steps: StepState[] = [], event: Extract<AgentEvent, { type: "progress" }>) {
  /** 同一步骤可能多次发送 running/success，按名称覆盖可避免刷新后出现重复节点。 */
  const next = steps.filter((item) => item.step !== event.step);
  next.push({
    step: event.step,
    status: event.status,
    updatedAt: Date.now(),
  });
  return next;
}

function restoreMessages(conversation: ConversationDetail): ChatMessage[] {
  /**
   * 把 MySQL 历史响应转换为页面消息状态。
   * 服务端记录是刷新后的事实来源，因此会覆盖流式阶段使用的本地临时消息 ID 和状态。
   */
  return conversation.messages.map((message) => ({
    id: message.id,
    role: message.role === "user" ? ("user" as const) : ("assistant" as const),
    content: message.content,
    createdAt: new Date(message.createdAt).getTime(),
    status:
      message.role === "assistant"
        ? (message.status as ChatMessage["status"])
        : undefined,
    steps: message.steps ?? [],
    result: message.result ?? undefined,
    error: message.error ?? undefined,
    sql: message.sql ?? undefined,
    resolvedQuery: message.resolvedQuery ?? undefined,
    explain:
      (message as { metrics?: { explain?: ChatMessage["explain"] } }).metrics?.explain ??
      undefined,
    metrics: (message as { metrics?: ChatMessage["metrics"] }).metrics ?? undefined,
  }));
}

export default function App() {
  // 主聊天区的页面状态：messages 既包含历史恢复消息，也包含当前 SSE 正在更新的消息；
  // draft 只保存尚未发送的输入框内容，不属于持久化会话或短期记忆。
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  // 非空表示当前存在流式请求；同一个对象同时用于“停止生成”和禁用会话切换。
  const [activeController, setActiveController] = useState<AbortController | null>(null);
  // 左侧列表只保存轻量摘要；activeConversationId 会随每次查询传给后端，
  // 后端再把它作为 LangGraph thread_id 恢复 Redis State。
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  // 历史列表加载状态独立于问数流，避免一次刷新失败清空当前聊天内容。
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  // 删除采用“候选项 -> 用户确认 -> 请求中/失败”的显式状态机，防止列表按钮直接误删。
  const [deleteCandidate, setDeleteCandidate] = useState<ConversationSummary | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // 消息容器引用只用于新事件到达后滚动到底部，不保存任何业务数据。
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // activeController 是流请求是否存在的唯一前端判断，避免再维护一份容易不同步的布尔值。
  const isStreaming = Boolean(activeController);
  const canSubmit = draft.trim().length > 0 && !isStreaming;

  // “完成”统计只计算成功结束的 assistant Turn，失败和取消不会被算作已完成查询。
  const completedCount = useMemo(
    () => messages.filter((message) => message.role === "assistant" && message.status === "done").length,
    [messages],
  );

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  const refreshConversations = async () => {
    /** 只刷新轻量会话摘要，不加载所有消息，避免历史数量增长后放大轮询开销。 */
    try {
      const items = await listConversations();
      setConversations(items);
      setHistoryError(null);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : String(error));
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    // 定时轮询是 BroadcastChannel 的兜底：其他标签页、后端任务或不支持该 API 的
    // 浏览器发生变化时，左侧历史列表仍会最终收敛到 MySQL 状态。
    void refreshConversations();
    const timer = window.setInterval(() => void refreshConversations(), 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!("BroadcastChannel" in window)) return;
    // BroadcastChannel 只同步“哪个会话发生变化”的通知，不复制消息内容；
    // 收到通知后重新读取 MySQL，避免多个标签页维护不同的客户端真值。
    const channel = new BroadcastChannel(CONVERSATION_CHANNEL);
    channel.onmessage = (
      event: MessageEvent<{ conversationId?: string; deleted?: boolean }>,
    ) => {
      void refreshConversations();
      if (event.data?.deleted && event.data.conversationId === activeConversationId) {
        setActiveConversationId(null);
        setMessages([]);
        setDraft("");
        return;
      }
      if (
        event.data?.conversationId &&
        event.data.conversationId === activeConversationId &&
        !isStreaming
      ) {
        void getConversation(activeConversationId)
          .then((conversation) => setMessages(restoreMessages(conversation)))
          .catch(() => undefined);
      }
    };
    return () => channel.close();
  }, [activeConversationId, isStreaming]);

  const selectConversation = async (conversationId: string) => {
    // 流式请求期间禁止切换会话，避免正在到达的 SSE 事件写入另一个会话的界面。
    if (isStreaming) return;
    setHistoryLoading(true);
    try {
      const conversation = await getConversation(conversationId);
      setActiveConversationId(conversationId);
      setMessages(restoreMessages(conversation));
      setDraft("");
      setHistoryError(null);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : String(error));
    } finally {
      setHistoryLoading(false);
    }
  };

  const requestDeleteConversation = (conversation: ConversationSummary) => {
    // 删除属于不可恢复操作，先保存候选项并打开站内确认框，不在列表按钮上直接执行。
    if (isStreaming) return;
    setDeleteCandidate(conversation);
    setDeleteError(null);
  };

  const cancelDeleteConversation = () => {
    if (isDeleting) return;
    setDeleteCandidate(null);
    setDeleteError(null);
  };

  const confirmDeleteConversation = async () => {
    if (!deleteCandidate || isDeleting) return;
    const conversation = deleteCandidate;
    setIsDeleting(true);
    setDeleteError(null);
    try {
      await deleteConversation(conversation.id);
      // 后端 204 表示 MySQL 删除已经成立；先更新当前标签页，再通知其他标签页刷新。
      setConversations((current) => current.filter((item) => item.id !== conversation.id));
      if (activeConversationId === conversation.id) {
        setActiveConversationId(null);
        setMessages([]);
        setDraft("");
      }
      setHistoryError(null);
      setDeleteCandidate(null);
      if ("BroadcastChannel" in window) {
        const channel = new BroadcastChannel(CONVERSATION_CHANNEL);
        channel.postMessage({ conversationId: conversation.id, deleted: true });
        channel.close();
      }
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsDeleting(false);
    }
  };

  const startQuery = async (rawQuery = draft) => {
    /**
     * 创建本地流式占位消息，必要时创建会话，然后消费后端 SSE 更新同一消息。
     * 流结束后再次读取 MySQL，用服务端稳定 ID、完整步骤和最终状态替换临时 UI 状态。
     */
    const query = rawQuery.trim();
    if (!query || isStreaming) return;

    const userMessage: ChatMessage = {
      id: makeId(),
      role: "user",
      content: query,
      createdAt: Date.now(),
    };

    const assistantId = makeId();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "正在连接问数智能体...",
      createdAt: Date.now(),
      status: "streaming",
      steps: [],
    };

    const controller = new AbortController();
    setActiveController(controller);
    setDraft("");
    setMessages((current) => [...current, userMessage, assistantMessage]);

    let conversationId = activeConversationId;

    const onEvent = (event: AgentEvent) => {
      // SSE 事件只更新本轮助手占位消息，避免历史消息因后续节点事件被误改。
      setMessages((current) =>
        current.map((message) => {
          if (message.id !== assistantId) return message;

          if (event.type === "progress") {
            return {
              ...message,
              content: event.status === "running" ? `正在执行：${event.step}` : message.content,
              steps: upsertStep(message.steps, event),
              requestId: event.requestId ?? message.requestId,
            };
          }

          if (event.type === "truncated") {
            return {
              ...message,
              truncated: true,
              content: message.content
                ? `${message.content}\n${event.message}`
                : event.message,
            };
          }
          if (event.type === "result") {
            return {
              ...message,
              status: "done",
              content: summarizeResult(event.data),
              result: event.data,
            };
          }

          if (event.type === "sql") {
            return { ...message, sql: event.sql };
          }

          if (event.type === "resolved_query") {
            return { ...message, resolvedQuery: event.query };
          }

          // P5.2：heartbeat 仅保活，不改写正文。
          if (event.type === "heartbeat") {
            return event.requestId
              ? { ...message, requestId: event.requestId }
              : message;
          }

          // P5.1/P5.3：metrics 落到消息上，供「本次查询说明」面板渲染耗时。
          if (event.type === "metrics") {
            return {
              ...message,
              requestId: event.requestId ?? event.request_id ?? message.requestId,
              metrics: {
                request_id: event.request_id,
                total_ms: event.total_ms,
                llm_ms: event.llm_ms,
                tokens_in: event.tokens_in,
                tokens_out: event.tokens_out,
                nodes: event.nodes,
                explain: event.explain ?? message.explain ?? undefined,
              },
            };
          }

          // P5.3：explain 提前到达时先缓存，metrics 事件会再带上 explain。
          if (event.type === "explain") {
            return {
              ...message,
              explain: event.data,
            };
          }

          // turn 事件提供后端消息 ID；当前 UI 继续使用临时 ID 保持渲染稳定，
          // 流结束后的 MySQL 回刷会一次性替换成服务端真值。同时记录 requestId。
          if (event.type === "turn") {
            return event.requestId
              ? { ...message, requestId: event.requestId }
              : message;
          }

          if (event.type === "error") {
            return {
              ...message,
              status: "error",
              content: "这次查询没有成功。",
              error: event.message,
              requestId: event.requestId ?? message.requestId,
            };
          }

          return message;
        }),
      );
    };

    try {
      if (!conversationId) {
        // 新会话必须先持久化，随后同一个 ID 会贯穿 MySQL、Redis thread_id 和 SSE 请求。
        const title = query.length > 60 ? `${query.slice(0, 60)}…` : query;
        const conversation = await createConversation(title);
        conversationId = conversation.id;
        setActiveConversationId(conversation.id);
        setConversations((current) => [conversation, ...current]);
      }

      await streamQuery(conversationId, query, { signal: controller.signal, onEvent });
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId && message.status === "streaming"
            ? { ...message, status: "done", content: "流程已结束，后端未返回查询结果。" }
            : message,
        ),
      );
    } catch (error) {
      const isAbort = error instanceof DOMException && error.name === "AbortError";
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: isAbort ? "done" : "error",
                content: isAbort ? "已停止本次查询。" : "无法连接问数接口。",
                error: isAbort ? undefined : error instanceof Error ? error.message : String(error),
              }
            : message,
        ),
      );
    } finally {
      setActiveController(null);
      await refreshConversations();
      if (conversationId) {
        try {
          // 本地 SSE 状态用于即时反馈，MySQL 历史才是刷新与跨标签页共享的最终真值。
          const conversation = await getConversation(conversationId);
          setMessages(restoreMessages(conversation));
        } catch {
          // 当前流式结果仍然可用，后台历史刷新失败不覆盖本地消息。
        }
        if ("BroadcastChannel" in window) {
          const channel = new BroadcastChannel(CONVERSATION_CHANNEL);
          channel.postMessage({ conversationId });
          channel.close();
        }
      }
    }
  };

  const stopQuery = () => {
    activeController?.abort();
  };

  const beginNewConversation = () => {
    if (isStreaming) return;
    setMessages([]);
    setDraft("");
    setActiveConversationId(null);
  };

  return (
    <div className="h-dvh overflow-hidden bg-parchment text-ink">
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(90deg,rgba(32,32,29,0.045)_1px,transparent_1px),linear-gradient(rgba(32,32,29,0.035)_1px,transparent_1px)] bg-[size:48px_48px]" />
      <div className="pointer-events-none fixed inset-0 grain" />

      <div className="relative grid h-full min-h-0 overflow-hidden lg:grid-cols-[300px_minmax(0,1fr)]">
        <aside className="hidden min-h-0 border-r border-ink/10 bg-[#efe6d8]/85 backdrop-blur lg:flex lg:flex-col">
          <div className="border-b border-ink/10 px-5 py-5">
            <div className="flex items-center gap-3">
              <div className="grid h-10 w-10 place-items-center bg-ink text-parchment">
                <BarChart3 className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <div className="text-base font-semibold tracking-[0.02em]">电商问数</div>
                <div className="text-xs text-ink/50">shopkeeper-agent</div>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-hidden px-4 py-4">
            <button
              type="button"
              onClick={beginNewConversation}
              disabled={isStreaming}
              className="flex h-11 w-full items-center justify-center gap-2 bg-ink text-sm font-semibold text-parchment transition hover:bg-soot disabled:cursor-not-allowed disabled:bg-ink/35"
            >
              <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
              新会话
            </button>

            <section className="shrink-0">
              <div className="mb-2 flex items-center gap-2 px-1 text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">
                <History className="h-3.5 w-3.5" aria-hidden="true" />
                样例
              </div>
              <div className="space-y-2">
                {examples.map((example) => (
                  <button
                    key={example}
                    type="button"
                    disabled={isStreaming}
                    onClick={() => startQuery(example)}
                    className="w-full border border-ink/10 bg-white/42 px-3 py-3 text-left text-sm leading-5 text-ink/75 transition hover:border-moss/35 hover:bg-white/75 disabled:cursor-not-allowed disabled:opacity-55"
                  >
                    {example}
                  </button>
                ))}
              </div>
            </section>

            <ConversationList
              conversations={conversations}
              activeId={activeConversationId}
              disabled={isStreaming}
              loading={historyLoading}
              onSelect={(conversationId) => void selectConversation(conversationId)}
              onDelete={requestDeleteConversation}
            />
            {historyError && (
              <div className="shrink-0 border border-tomato/25 bg-tomato/10 px-3 py-2 text-xs text-tomato">
                历史会话加载失败：{historyError}
              </div>
            )}
          </div>

          <div className="border-t border-ink/10 p-4">
            <div className="grid gap-2 text-xs text-ink/55">
              <div className="flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-2">
                  <Server className="h-3.5 w-3.5" aria-hidden="true" />
                  API
                </span>
                <span className="truncate font-mono">{API_BASE_URL}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="inline-flex items-center gap-2">
                  <Activity className="h-3.5 w-3.5" aria-hidden="true" />
                  完成
                </span>
                <span>{completedCount}</span>
              </div>
            </div>
          </div>
        </aside>

        <main className="flex min-h-0 min-w-0 flex-col overflow-hidden">
          <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/10 bg-parchment/88 px-4 backdrop-blur lg:px-6">
            <div className="flex min-w-0 items-center gap-3">
              <div className="grid h-9 w-9 shrink-0 place-items-center bg-moss text-white lg:hidden">
                <BarChart3 className="h-4 w-4" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-ink">智能数据分析 Agent</div>
                <div className="truncate text-xs text-ink/45">FastAPI SSE / LangGraph</div>
              </div>
            </div>
            <button
              type="button"
              onClick={beginNewConversation}
              disabled={messages.length === 0 || isStreaming}
              className={cn(
                "grid h-9 w-9 place-items-center rounded-full text-ink/55 transition hover:bg-ink/5 hover:text-ink disabled:cursor-not-allowed disabled:opacity-35",
              )}
              title="清空"
              aria-label="清空"
            >
              <Eraser className="h-4 w-4" aria-hidden="true" />
            </button>
          </header>

          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            {messages.length === 0 ? (
              <EmptyState examples={examples} onUseExample={(example) => setDraft(example)} />
            ) : (
              <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6 lg:px-8">
                {messages.map((message) => (
                  <MessageBubble key={message.id} message={message} />
                ))}
              </div>
            )}
          </div>

          <div className="border-t border-ink/10 bg-[#efe6d8]/45 px-4 py-2 text-center text-xs text-ink/45">
            <span className="inline-flex items-center gap-2">
              <Leaf className="h-3.5 w-3.5 text-moss" aria-hidden="true" />
              {isStreaming ? "运行中" : "就绪"}
            </span>
          </div>
          <Composer
            value={draft}
            disabled={!canSubmit}
            isStreaming={isStreaming}
            onChange={setDraft}
            onSubmit={() => startQuery()}
            onStop={stopQuery}
          />
        </main>
      </div>

      <DeleteConversationDialog
        conversation={deleteCandidate}
        deleting={isDeleting}
        error={deleteError}
        onCancel={cancelDeleteConversation}
        onConfirm={() => void confirmDeleteConversation()}
      />
    </div>
  );
}
