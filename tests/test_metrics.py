"""P5.1 指标采集单元测试：节点计时、LLM token 归因与上下文隔离。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.core.metrics import (
    RunMetricsCallback,
    observe_node,
    set_run_metrics,
    use_run_metrics,
)


def test_node_timer_and_llm_attribution():
    with use_run_metrics("req-1") as metrics:
        cb = RunMetricsCallback()
        cb.on_llm_start({}, ["hi"], run_id="r1")
        cb.on_llm_end(
            LLMResult(
                generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
                llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            ),
            run_id="r1",
        )
        with metrics.node_timer("generate_sql"):
            cb.on_llm_start({}, ["hi"], run_id="r2")
            cb.on_llm_end(
                LLMResult(
                    generations=[[ChatGeneration(message=AIMessage(content="sql"))]],
                    llm_output={
                        "token_usage": {"prompt_tokens": 5, "completion_tokens": 3}
                    },
                ),
                run_id="r2",
            )

    payload = metrics.to_dict()
    assert payload["request_id"] == "req-1"
    assert payload["tokens_in"] == 15
    assert payload["tokens_out"] == 5
    nodes = {n["node"]: n for n in payload["nodes"]}
    assert "generate_sql" in nodes
    assert nodes["generate_sql"]["tokens_in"] == 5
    assert nodes["generate_sql"]["tokens_out"] == 3


def test_observe_node_records_node_ms():
    async def fake_node(state, runtime):
        await asyncio.sleep(0.01)
        return {"ok": True}

    wrapped = observe_node("fake", fake_node)

    async def run():
        with use_run_metrics("req-2") as metrics:
            result = await wrapped({}, None)
            return result, metrics.to_dict()

    result, payload = asyncio.run(run())
    assert result == {"ok": True}
    assert payload["nodes"][0]["node"] == "fake"
    assert payload["nodes"][0]["node_ms"] >= 10


def test_no_metrics_context_is_noop():
    set_run_metrics(None)
    cb = RunMetricsCallback()
    cb.on_llm_start({}, ["x"], run_id="z")
    cb.on_llm_end(LLMResult(generations=[[]]), run_id="z")

    async def fake(state, runtime):
        return 1

    wrapped = observe_node("fake", fake)
    assert asyncio.run(wrapped({}, None)) == 1


if __name__ == "__main__":
    test_node_timer_and_llm_attribution()
    test_observe_node_records_node_ms()
    test_no_metrics_context_is_noop()
    print("all ok")
