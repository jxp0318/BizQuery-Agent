"""
问数运行指标采集（P5.1）

一次问数的耗时与 token 需要按节点拆开，才能判断瓶颈在 LLM、检索还是数据库。
这里只做测量累加，不改变业务结果；指标通过 ContextVar 绑定到当前请求协程，
避免写入 LangGraph State / Redis Checkpoint（那是 P2 刻意保持精简的运行状态）。

并发注意：recall_column / recall_metric / recall_value 在图上是并行节点。
「当前节点」必须按任务用 ContextVar 隔离，不能用共享栈，否则 LLM 耗时会记到
错误的节点上（并行时 llm_ms 甚至会大于 node_ms）。

产出去向：
1. loguru 结构化日志（开发排查）
2. 会话消息 metrics 字段（事后回看）
3. SSE `metrics` 事件（evals 采集 latency/token）
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

# 当前请求的指标累加器；无请求上下文（本地脚本）时为 None，埋点自动跳过。
_run_metrics_ctx: ContextVar["RunMetrics | None"] = ContextVar(
    "run_metrics", default=None
)
# 当前任务正在执行的节点指标；并行节点各自独立，避免互相抢归属。
_current_node_ctx: ContextVar["NodeMetric | None"] = ContextVar(
    "current_node_metric", default=None
)


@dataclass
class NodeMetric:
    """单个 LangGraph 节点的耗时与模型调用摘要。"""

    node: str
    node_ms: float = 0.0
    llm_ms: float = 0.0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "node_ms": round(self.node_ms, 1),
            "llm_ms": round(self.llm_ms, 1),
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


@dataclass
class RunMetrics:
    """一轮问数的执行账本：总耗时 + 分节点指标 + 累计 token。"""

    request_id: str = "1"
    started_at: float = field(default_factory=time.perf_counter)
    nodes: list[NodeMetric] = field(default_factory=list)
    # run_id -> (开始时间, 发起调用时所属节点)。结束时按发起时归属，避免
    # 并行节点交叉导致记错节点。
    _llm_inflight: dict[Any, tuple[float, NodeMetric | None]] = field(
        default_factory=dict
    )

    def to_dict(self) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.started_at) * 1000
        tokens_in = sum(n.tokens_in for n in self.nodes)
        tokens_out = sum(n.tokens_out for n in self.nodes)
        return {
            "request_id": self.request_id,
            "total_ms": round(total_ms, 1),
            # llm_ms 是各节点 LLM 之和；并行调用时可能大于 wall，属正常。
            "llm_ms": round(sum(n.llm_ms for n in self.nodes), 1),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @contextmanager
    def node_timer(self, node: str):
        """计时一个图节点；该任务内后续 LLM 调用记入此节点。"""

        metric = NodeMetric(node=node)
        # asyncio 单线程，list.append 无需加锁；顺序即调度完成顺序。
        self.nodes.append(metric)
        token = _current_node_ctx.set(metric)
        started = time.perf_counter()
        try:
            yield metric
        finally:
            metric.node_ms = (time.perf_counter() - started) * 1000
            _current_node_ctx.reset(token)

    def note_llm_start(self, run_id: Any) -> None:
        self._llm_inflight[run_id] = (time.perf_counter(), _current_node_ctx.get())

    def note_llm_end(self, run_id: Any, result: LLMResult | None) -> None:
        started_pair = self._llm_inflight.pop(run_id, None)
        if started_pair is None:
            started_at, target = None, _current_node_ctx.get()
        else:
            started_at, target = started_pair
        elapsed_ms = (
            (time.perf_counter() - started_at) * 1000 if started_at is not None else 0.0
        )
        tokens_in, tokens_out = _extract_token_usage(result)
        if target is None:
            target = NodeMetric(node="_unscoped")
            self.nodes.append(target)
        target.llm_ms += elapsed_ms
        target.llm_calls += 1
        target.tokens_in += tokens_in
        target.tokens_out += tokens_out


def _extract_token_usage(result: LLMResult | None) -> tuple[int, int]:
    """尽量从 LangChain 结果里取 prompt/completion token；拿不到则记 0。"""

    if result is None:
        return 0, 0
    usage: dict[str, Any] = {}
    if result.llm_output:
        usage = result.llm_output.get("token_usage") or usage
    if not usage and result.generations:
        message = getattr(result.generations[0][0], "message", None)
        if message is not None:
            meta = getattr(message, "response_metadata", None) or {}
            usage = meta.get("token_usage") or usage
            if not usage:
                usage = (meta.get("usage") or {}) if isinstance(meta, dict) else {}
    if not isinstance(usage, dict):
        return 0, 0
    tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tokens_out = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    return tokens_in, tokens_out


def current_run_metrics() -> RunMetrics | None:
    """读取当前协程绑定的指标累加器；无则返回 None。"""

    return _run_metrics_ctx.get()


def set_run_metrics(metrics: RunMetrics | None):
    """绑定当前协程的指标累加器（请求入口调用一次）。"""

    _run_metrics_ctx.set(metrics)


@contextmanager
def use_run_metrics(request_id: str = "1"):
    """请求级指标上下文：进入时创建，退出时解绑。"""

    metrics = RunMetrics(request_id=request_id)
    token = _run_metrics_ctx.set(metrics)
    try:
        yield metrics
    finally:
        _run_metrics_ctx.reset(token)


class RunMetricsCallback(BaseCallbackHandler):
    """把每次 LLM 调用的墙钟与 token 记到发起调用的节点。

    通过全局注册在 llm 实例上，节点无需改调用方式；无 RunMetrics 时为空操作。
    """

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        metrics = current_run_metrics()
        if metrics is not None:
            metrics.note_llm_start(run_id)

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        metrics = current_run_metrics()
        if metrics is not None:
            metrics.note_llm_end(run_id, response)

    def on_llm_error(self, error: BaseException, *, run_id, **kwargs):
        metrics = current_run_metrics()
        if metrics is not None:
            metrics.note_llm_end(run_id, None)


def observe_node(name: str, fn):
    """包装 LangGraph 节点函数，自动记录 node_ms，并为内部 LLM 调用归因。

    仅在存在 RunMetrics 时包装行为；否则直接透传，保证图在脚本中可独立运行。
    """

    async def wrapper(state, runtime, *args, **kwargs):
        metrics = current_run_metrics()
        if metrics is None:
            return await fn(state, runtime, *args, **kwargs)
        with metrics.node_timer(name):
            return await fn(state, runtime, *args, **kwargs)

    wrapper.__name__ = getattr(fn, "__name__", name)
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper
