/**
 * 历史会话与流式问数 API 客户端。
 * 普通会话接口使用 JSON；问数接口消费 SSE，使前端可以在 SQL 执行完成前展示节点进度。
 */
import type {
  AgentEvent,
  ConversationDetail,
  ConversationSummary,
} from "../types/agent";

// 生产环境可配置独立后端地址；开发环境为空时使用 Vite 的同源 /api 代理。
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "";

type QueryOptions = {
  /** 页面点击“停止”时中断 fetch，并把取消传播到后端 SSE 协程。 */
  signal?: AbortSignal;
  /** 每解析出一个完整 SSE 事件就立即交给 App 更新当前 assistant 消息。 */
  onEvent: (event: AgentEvent) => void;
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  // 错误响应优先读取 FastAPI 的 detail，保留后端已经整理过的业务提示；
  // 非 JSON 响应再退回 HTTP 状态码，避免解析失败掩盖真正错误。
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `接口请求失败：HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function listConversations() {
  return requestJson<ConversationSummary[]>("/api/conversations");
}

export function getConversation(conversationId: string) {
  return requestJson<ConversationDetail>(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
  );
}

export function createConversation(title: string) {
  return requestJson<ConversationSummary>("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function deleteConversation(conversationId: string) {
  const response = await fetch(
    `${API_BASE_URL}/api/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `删除会话失败：HTTP ${response.status}`);
  }
}

export async function streamQuery(
  conversationId: string,
  query: string,
  options: QueryOptions,
) {
  /**
   * 在指定持久化会话中发起查询，并按事件到达顺序回调 UI。
   * AbortSignal 会把“停止生成”传播到 fetch/ASGI 流，后端再将该 Turn 记为 cancelled。
   */
  const response = await fetch(
    `${API_BASE_URL}/api/conversations/${encodeURIComponent(conversationId)}/query`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({ query }),
      signal: options.signal,
    },
  );

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `接口请求失败：HTTP ${response.status}`);
  }
  if (!response.body) throw new Error("浏览器未返回可读取的流式响应。");

  // reader 负责取得网络二进制分块，decoder 负责把跨分块 UTF-8 字节安全转成文本。
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  // buffer 保存尚未遇到 SSE 双换行边界的残片，下一网络分块到达后继续拼接。
  let buffer = "";

  // 网络分块边界不等于 SSE 事件边界：一个 JSON 事件可能被拆成多块，也可能
  // 多个事件同时到达。因此保留未形成双换行的尾部，等下一块到达后再解析。
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split(/\n\n/);
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const event = parseSseChunk(chunk);
      if (event) options.onEvent(event);
    }
  }

  buffer += decoder.decode();
  const tail = parseSseChunk(buffer);
  if (tail) options.onEvent(tail);
}

function parseSseChunk(chunk: string): AgentEvent | null {
  /**
   * 解析单个 SSE 事件块。
   * 多个 data 行需要先合并再 JSON.parse；解析失败转换成 error 事件，避免整条流中断后页面无反馈。
   */
  const payload = chunk
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, ""))
    .join("\n")
    .trim();

  if (!payload) return null;
  try {
    return JSON.parse(payload) as AgentEvent;
  } catch {
    return { type: "error", message: `无法解析后端事件：${payload}` };
  }
}
