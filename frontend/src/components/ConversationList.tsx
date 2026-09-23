/**
 * 左侧历史会话列表。
 * 列表只展示 MySQL 产品历史；点击会话才读取完整消息，不会触发 Redis 短期记忆初始化。
 */
import { Clock3, LoaderCircle, MessagesSquare, Trash2 } from "lucide-react";
import { cn } from "../lib/format";
import type { ConversationSummary } from "../types/agent";

type Props = {
  /** 后端按 updatedAt 倒序返回的轻量历史列表。 */
  conversations: ConversationSummary[];
  /** 当前正在主区域展示的会话 ID，用于高亮列表项。 */
  activeId: string | null;
  /** 流式查询期间禁止切换和删除，避免 SSE 写入错误会话。 */
  disabled: boolean;
  /** 历史列表正在读取时展示加载图标。 */
  loading: boolean;
  /** 选择会话只传 ID，完整消息由 App 再向 MySQL 历史接口读取。 */
  onSelect: (conversationId: string) => void;
  /** 删除按钮只提交候选会话，真正删除由确认框完成。 */
  onDelete: (conversation: ConversationSummary) => void;
};

function formatUpdatedAt(value: string) {
  /** 历史列表只需要便于扫描的月日和时分，完整时间仍由后端数据保留。 */
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function ConversationList({
  conversations,
  activeId,
  disabled,
  loading,
  onSelect,
  onDelete,
}: Props) {
  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="mb-2 flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">
        <span className="inline-flex items-center gap-2">
          <Clock3 className="h-3.5 w-3.5" aria-hidden="true" />
          历史会话
        </span>
        {loading && <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-label="加载中" />}
      </div>

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {!loading && conversations.length === 0 && (
          <div className="border border-dashed border-ink/15 px-3 py-5 text-center text-xs text-ink/40">
            完成一次问数后，会话会保存在这里
          </div>
        )}
        {conversations.map((conversation) => (
          <div key={conversation.id} className="group relative">
            <button
              type="button"
              disabled={disabled}
              onClick={() => onSelect(conversation.id)}
              className={cn(
                "w-full border py-2.5 pl-3 pr-10 text-left transition disabled:cursor-not-allowed disabled:opacity-55",
                activeId === conversation.id
                  ? "border-moss/45 bg-moss/10"
                  : "border-ink/10 bg-white/42 hover:border-moss/30 hover:bg-white/75",
              )}
            >
              <div className="flex items-start gap-2">
                <MessagesSquare className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-ink/80">{conversation.title}</div>
                  <div className="mt-1 flex justify-between gap-2 text-[11px] text-ink/40">
                    <span>{formatUpdatedAt(conversation.updatedAt)}</span>
                    <span>{conversation.messageCount} 条</span>
                  </div>
                </div>
              </div>
            </button>
            <button
              type="button"
              disabled={disabled}
              onClick={() => onDelete(conversation)}
              className="absolute right-2 top-2 grid h-7 w-7 place-items-center rounded-full text-ink/30 opacity-0 transition hover:bg-tomato/10 hover:text-tomato focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-tomato/30 disabled:cursor-not-allowed group-hover:opacity-100"
              title={`删除会话：${conversation.title}`}
              aria-label={`删除会话：${conversation.title}`}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
