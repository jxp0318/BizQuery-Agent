import { AlertTriangle, LoaderCircle, Trash2, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { ConversationSummary } from "../types/agent";

type Props = {
  /** 待确认删除的会话；为 null 时不渲染模态框。 */
  conversation: ConversationSummary | null;
  /** 删除请求进行中时禁用关闭和重复提交。 */
  deleting: boolean;
  /** 删除 MySQL 历史或 Redis Thread 失败时展示的接口错误。 */
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
};

export function DeleteConversationDialog({
  conversation,
  deleting,
  error,
  onCancel,
  onConfirm,
}: Props) {
  // 保存取消按钮 DOM 引用，打开时把默认焦点放到安全操作上，降低键盘误删风险。
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!conversation) return;

    // 模态框打开后把焦点移到“取消”，降低误删风险；关闭后恢复原焦点，
    // 让键盘用户能继续从之前的位置操作历史列表。
    const previousActiveElement = document.activeElement as HTMLElement | null;
    cancelButtonRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleting) onCancel();
    };
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      previousActiveElement?.focus();
    };
  }, [conversation, deleting, onCancel]);

  if (!conversation) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-ink/45 px-4 backdrop-blur-[2px] modal-backdrop"
      onMouseDown={(event) => {
        // 只允许点击真正的遮罩层关闭，避免点击弹窗内部内容时事件冒泡造成误关闭。
        if (event.target === event.currentTarget && !deleting) onCancel();
      }}
    >
      <section
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-dialog-title"
        aria-describedby="delete-dialog-description"
        className="relative w-full max-w-md overflow-hidden border border-ink/15 bg-parchment shadow-panel modal-panel"
      >
        <div className="absolute inset-x-0 top-0 h-1 bg-tomato" />
        <button
          type="button"
          onClick={onCancel}
          disabled={deleting}
          className="absolute right-4 top-4 grid h-8 w-8 place-items-center text-ink/35 transition hover:bg-ink/5 hover:text-ink focus:outline-none focus:ring-2 focus:ring-moss/30 disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="关闭删除确认"
        >
          <X className="h-4 w-4" aria-hidden="true" />
        </button>

        <div className="px-6 pb-5 pt-7 sm:px-7">
          <div className="flex items-start gap-4">
            <div className="grid h-11 w-11 shrink-0 place-items-center border border-tomato/25 bg-tomato/10 text-tomato">
              <AlertTriangle className="h-5 w-5" aria-hidden="true" />
            </div>
            <div className="min-w-0 pr-8">
              <div className="text-[11px] font-semibold uppercase tracking-[0.2em] text-tomato">
                删除历史会话
              </div>
              <h2 id="delete-dialog-title" className="mt-1.5 text-xl font-semibold text-ink">
                确定要删除吗？
              </h2>
            </div>
          </div>

          <div className="mt-5 border-y border-ink/10 bg-white/40 px-4 py-3">
            <div className="text-[11px] uppercase tracking-[0.14em] text-ink/40">会话</div>
            <div className="mt-1 truncate text-sm font-medium text-ink/80" title={conversation.title}>
              {conversation.title}
            </div>
          </div>

          <p id="delete-dialog-description" className="mt-4 text-sm leading-6 text-ink/60">
            该会话及其中的全部消息将被永久删除，此操作无法撤销。
          </p>

          {error && (
            <div role="alert" className="mt-4 border border-tomato/25 bg-tomato/10 px-3 py-2 text-xs leading-5 text-tomato">
              删除失败：{error}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-3 border-t border-ink/10 bg-[#efe6d8]/60 px-6 py-4 sm:px-7">
          <button
            ref={cancelButtonRef}
            type="button"
            onClick={onCancel}
            disabled={deleting}
            className="h-10 border border-ink/15 bg-white/50 px-5 text-sm font-semibold text-ink/70 transition hover:border-ink/30 hover:bg-white focus:outline-none focus:ring-2 focus:ring-moss/30 disabled:cursor-not-allowed disabled:opacity-45"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={deleting}
            className="inline-flex h-10 min-w-28 items-center justify-center gap-2 bg-tomato px-5 text-sm font-semibold text-white transition hover:bg-[#bd3f2d] focus:outline-none focus:ring-2 focus:ring-tomato/35 focus:ring-offset-2 focus:ring-offset-parchment disabled:cursor-wait disabled:opacity-65"
          >
            {deleting ? (
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Trash2 className="h-4 w-4" aria-hidden="true" />
            )}
            {deleting ? "删除中" : "确认删除"}
          </button>
        </div>
      </section>
    </div>,
    document.body,
  );
}
