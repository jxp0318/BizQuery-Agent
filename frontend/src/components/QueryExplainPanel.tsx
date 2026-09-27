/**
 * 结果解释面板（P5.3）
 * 折叠展示本轮使用的 SQL、指标口径、相关表字段与耗时，帮助用户理解结果从哪来。
 */
import { ChevronDown, ChevronRight, ClipboardCopy, Info } from "lucide-react";
import { useState } from "react";

import { cn, toClipboardText } from "../lib/format";
import type { ChatMessage, QueryExplain } from "../types/agent";

type NodeTiming = {
  node: string;
  node_ms: number;
  llm_ms: number;
  tokens_in?: number;
  tokens_out?: number;
};

function formatMs(ms?: number | null) {
  if (ms === null || ms === undefined) return "-";
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms)}ms`;
}

export function QueryExplainPanel({ message }: { message: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const explain = message.explain;
  const metrics = message.metrics;
  const nodes: NodeTiming[] = (metrics?.nodes as NodeTiming[] | undefined) ?? [];
  const hasContent =
    Boolean(message.sql) ||
    Boolean(explain) ||
    nodes.length > 0 ||
    Boolean(message.requestId) ||
    Boolean(message.resolvedQuery);

  if (!hasContent) return null;

  const copyAll = async () => {
    const payload = {
      requestId: message.requestId,
      resolvedQuery: message.resolvedQuery,
      sql: message.sql,
      explain,
      metrics,
    };
    await navigator.clipboard.writeText(toClipboardText(payload));
  };

  return (
    <div className="mt-3 border border-ink/10 bg-parchment/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-ink/70 hover:bg-ink/5"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
        )}
        <Info className="h-3.5 w-3.5" aria-hidden="true" />
        本次查询说明
        {metrics?.total_ms != null && (
          <span className="ml-auto text-ink/45">总耗时 {formatMs(metrics.total_ms)}</span>
        )}
      </button>

      {open && (
        <div className="space-y-3 border-t border-ink/10 px-3 py-3 text-xs text-ink/80">
          {message.resolvedQuery && (
            <Section title="独立问题">
              <p className="leading-5">{message.resolvedQuery}</p>
            </Section>
          )}

          {message.sql && (
            <Section
              title="SQL"
              action={
                <button
                  type="button"
                  className="inline-flex items-center gap-1 text-ink/50 hover:text-ink"
                  onClick={() => navigator.clipboard.writeText(message.sql ?? "")}
                >
                  <ClipboardCopy className="h-3 w-3" aria-hidden="true" />
                  复制
                </button>
              }
            >
              <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-ink/5 px-2 py-2 font-mono text-[11px] leading-5">
                {message.sql}
              </pre>
            </Section>
          )}

          {explain?.metrics?.length ? (
            <Section title="指标口径">
              <ul className="space-y-1">
                {explain.metrics.map((metric) => (
                  <li key={metric.name}>
                    <span className="font-medium">{metric.name}</span>
                    {metric.description ? (
                      <span className="text-ink/60"> — {metric.description}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {explain?.tables?.length ? (
            <Section title="相关表">
              <ul className="space-y-1">
                {explain.tables.map((table) => (
                  <li key={table.name}>
                    <span className="font-medium">{table.name}</span>
                    {table.description ? (
                      <span className="text-ink/60"> — {table.description}</span>
                    ) : null}
                    {table.columns?.length ? (
                      <div className="mt-0.5 text-[11px] text-ink/50">
                        字段：
                        {table.columns
                          .map((c) => c.name)
                          .filter(Boolean)
                          .join("、")}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {(explain?.date_info || explain?.db_info) && (
            <Section title="环境">
              <p>
                {explain.date_info?.date ? `日期 ${explain.date_info.date}` : ""}
                {explain.date_info?.quarter ? ` · ${explain.date_info.quarter}` : ""}
                {explain.db_info?.dialect ? ` · ${explain.db_info.dialect}` : ""}
                {explain.db_info?.version ? ` ${explain.db_info.version}` : ""}
              </p>
            </Section>
          )}

          {nodes.length > 0 && (
            <Section title="耗时">
              <div className="space-y-1">
                <div className="flex gap-3 text-[11px] text-ink/50">
                  <span>总计 {formatMs(metrics?.total_ms)}</span>
                  <span>LLM 合计 {formatMs(metrics?.llm_ms)}</span>
                  <span>
                    tokens {metrics?.tokens_in ?? 0}/{metrics?.tokens_out ?? 0}
                  </span>
                </div>
                <ul className="space-y-0.5">
                  {nodes.map((node) => (
                    <li key={node.node} className="flex items-center gap-2">
                      <span className="w-32 shrink-0 truncate text-ink/60">{node.node}</span>
                      <span
                        className="h-1.5 rounded bg-moss/70"
                        style={{
                          width: `${Math.min(100, (node.node_ms / (metrics?.total_ms || 1)) * 100)}%`,
                          minWidth: 2,
                        }}
                      />
                      <span className="tabular-nums text-ink/50">
                        {formatMs(node.node_ms)}
                        {node.llm_ms > 0 ? ` · LLM ${formatMs(node.llm_ms)}` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </Section>
          )}

          {message.requestId && (
            <Section title="排障">
              <p className="font-mono text-[11px] text-ink/50">request_id: {message.requestId}</p>
            </Section>
          )}

          <button
            type="button"
            onClick={copyAll}
            className="inline-flex items-center gap-1 text-ink/50 hover:text-ink"
          >
            <ClipboardCopy className="h-3 w-3" aria-hidden="true" />
            复制全部诊断信息
          </button>
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <div className={cn("text-[11px] font-semibold uppercase tracking-[0.12em] text-ink/45")}>
          {title}
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}
