/**
 * 智能体执行流程图组件
 * 按 LangGraph 节点拓扑展示各步骤状态
 *
 * P3 后链路为：
 *   generate_sql → sql_guard → validate_sql → run_sql
 *   失败进入 correct_sql，修完回到 sql_guard；校验最多 3 轮后 reject_sql
 *
 * 布局约定：分支文字与连线保持 ≥12px 间距，避免箭头压字。
 */
import { Check, Circle, LoaderCircle, X } from "lucide-react";
import { cn } from "../lib/format";
import type { ProgressStatus, StepState } from "../types/agent";

type FlowStatus = ProgressStatus | "pending";

type FlowNode = {
  step: string;
  x: number;
  y: number;
  w?: number;
};

const nodes: FlowNode[] = [
  { step: "理解会话上下文", x: 410, y: 20, w: 176 },
  { step: "抽取关键词", x: 410, y: 112 },
  { step: "召回字段信息", x: 150, y: 204 },
  { step: "召回指标信息", x: 410, y: 204 },
  { step: "召回字段取值", x: 670, y: 204 },
  { step: "合并召回信息", x: 410, y: 306 },
  { step: "过滤指标信息", x: 290, y: 410 },
  { step: "过滤表信息", x: 530, y: 410 },
  { step: "添加额外上下文", x: 410, y: 514, w: 176 },
  { step: "生成SQL", x: 410, y: 618 },
  { step: "安全检查SQL", x: 410, y: 712, w: 168 },
  { step: "校验SQL", x: 410, y: 828 },
  { step: "校正SQL", x: 680, y: 770, w: 148 },
  { step: "放弃修正SQL", x: 680, y: 944, w: 156 },
  { step: "执行SQL", x: 410, y: 944 },
];

const connectors = [
  // 主干：召回与生成
  "M410 60 L410 106",
  "M410 152 L410 176 L150 176 L150 198",
  "M410 152 L410 198",
  "M410 152 L410 176 L670 176 L670 198",
  "M150 244 L150 270 L410 270 L410 300",
  "M410 244 L410 300",
  "M670 244 L670 270 L410 270 L410 300",
  "M410 346 L410 374 L290 374 L290 404",
  "M410 346 L410 374 L530 374 L530 404",
  "M290 450 L290 478 L410 478 L410 508",
  "M530 450 L530 478 L410 478 L410 508",
  "M410 554 L410 612",
  // 生成 → 安全检查
  "M410 658 L410 706",
  // 安全检查 → 校验（无误）：主干竖线，文字在左侧
  "M410 752 L410 822",
  // 校验 → 执行（无误）
  "M410 868 L410 938",
  // 安全检查「有误」→ 校正：先右出，文字在横线上方空隙
  "M494 732 L600 732 L600 790 L656 790",
  // 校验「有误」→ 校正：文字在横线下方空隙
  "M488 848 L600 848 L600 790 L656 790",
  // 校正 → 回到安全检查（修完重检）：走右上外侧，文字在横线上方
  "M754 770 L790 770 L790 688 L488 688 L488 706",
  // 轮次用尽 → 放弃修正
  "M680 830 L680 938",
];

const branchLabels = [
  { text: "有误", x: 520, y: 718 },
  { text: "无误", x: 352, y: 800 },
  { text: "有误", x: 520, y: 876 },
  { text: "无误", x: 352, y: 916 },
  { text: "修完重检", x: 548, y: 676 },
  { text: "轮次用尽", x: 704, y: 888 },
];

function getStatusMap(steps: StepState[]) {
  return steps.reduce<Record<string, StepState>>((map, item) => {
    map[item.step] = item;
    return map;
  }, {});
}

function statusFor(step: string, map: Record<string, StepState>): FlowStatus {
  return map[step]?.status ?? "pending";
}

function NodeIcon({ status }: { status: FlowStatus }) {
  if (status === "running") {
    return <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />;
  }

  if (status === "success") {
    return <Check className="h-3.5 w-3.5" aria-hidden="true" />;
  }

  if (status === "error") {
    return <X className="h-3.5 w-3.5" aria-hidden="true" />;
  }

  return <Circle className="h-3.5 w-3.5" aria-hidden="true" />;
}

function FlowNodeCard({ node, status }: { node: FlowNode; status: FlowStatus }) {
  const width = node.w ?? 156;

  return (
    <div
      className="absolute -translate-x-1/2"
      style={{ left: node.x, top: node.y, width }}
    >
      <div
        className={cn(
          "flex h-10 items-center gap-2 border px-3 text-sm font-semibold shadow-line transition",
          status === "pending" && "border-ink/10 bg-white/55 text-ink/45",
          status === "running" && "border-brass/45 bg-brass/15 text-ink",
          status === "success" && "border-moss/25 bg-moss/10 text-ink",
          status === "error" && "border-tomato/35 bg-tomato/10 text-tomato",
        )}
      >
        <span
          className={cn(
            "grid h-6 w-6 shrink-0 place-items-center rounded-full",
            status === "pending" && "bg-ink/5 text-ink/35",
            status === "running" && "bg-brass/20 text-brass",
            status === "success" && "bg-moss/15 text-moss",
            status === "error" && "bg-tomato/15 text-tomato",
          )}
        >
          <NodeIcon status={status} />
        </span>
        <span className="min-w-0 flex-1 truncate">{node.step}</span>
      </div>
    </div>
  );
}

export function StepRail({ steps = [] }: { steps?: StepState[] }) {
  if (steps.length === 0) return null;

  const statusMap = getStatusMap(steps);

  return (
    <section className="mt-4 border border-ink/10 bg-white/40 px-3 py-4 shadow-line">
      <div className="mb-3 flex items-center justify-between gap-3 px-1">
        <div className="text-sm font-semibold text-ink">执行流程</div>
        <div className="text-xs text-ink/45">LangGraph</div>
      </div>

      <div className="overflow-x-auto">
        <div className="relative mx-auto h-[1000px] w-[820px]">
          <svg
            className="pointer-events-none absolute inset-0 h-full w-full"
            viewBox="0 0 820 1000"
            fill="none"
            aria-hidden="true"
          >
            <defs>
              <marker
                id="flow-arrow"
                markerHeight="8"
                markerWidth="8"
                orient="auto"
                refX="6"
                refY="4"
              >
                <path d="M0 0 L8 4 L0 8 Z" fill="rgba(32,32,29,0.58)" />
              </marker>
            </defs>
            {connectors.map((path) => (
              <path
                key={path}
                d={path}
                stroke="rgba(32,32,29,0.5)"
                strokeWidth="1.5"
                markerEnd="url(#flow-arrow)"
              />
            ))}
            {branchLabels.map((label) => (
              <text
                key={`${label.text}-${label.x}-${label.y}`}
                x={label.x}
                y={label.y}
                fill="rgba(32,32,29,0.72)"
                fontSize="13"
                fontWeight="600"
                paintOrder="stroke"
                stroke="rgba(255,255,255,0.9)"
                strokeWidth="3"
              >
                {label.text}
              </text>
            ))}
          </svg>

          {nodes.map((node) => (
            <FlowNodeCard
              key={node.step}
              node={node}
              status={statusFor(node.step, statusMap)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
