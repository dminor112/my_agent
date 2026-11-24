"""阶段七回归测试：并行只读工具的顺序、隔离、名额与延迟。

模型调用一律使用 Mock 或自定义替身，不联网、不产生模型费用。
计时用例只比较同一台机器上的相对耗时，阈值留有余量，不是性能基准。
"""

import json
import time

import pytest

from my_agent.config import Settings
from my_agent.model import BudgetExceeded, MockModel
from my_agent.native import AgentState, prepare_batch, run_native
from my_agent.tools import Corpus


def call(call_id: str, name: str, **arguments) -> dict:
    """按 Chat Completions 的形状构造一条工具调用，arguments 仍是 JSON 字符串。"""
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class BatchModel(MockModel):
    """首轮一次性发出多条工具调用，之后再委托固定 Mock 策略。"""

    def __init__(self, calls: list[dict]):
        self.calls = calls
        self.sent = False

    async def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, int]:
        if not self.sent:
            self.sent = True
            return {"tool_calls": self.calls}, 100
        return await super().complete(messages, tools)


async def run_first_batch(settings: Settings, calls: list[dict]) -> AgentState:
    """只跑到第一批工具结束，用预算终止代替等待完整报告。

    捕捉 BudgetExceeded 是刻意设计：本测试关心批内行为，不需要构造合法报告。
    """
    state = AgentState()
    with pytest.raises(BudgetExceeded):
        await run_native("工具设计", settings, state=state, model=BatchModel(calls))
    return state


async def test_batch_preserves_call_order(settings):
    """三个检索并行执行，tool 消息仍必须按 s1、s2、s3 的原始顺序回填。"""
    settings = settings.model_copy(update={"max_turns": 1, "max_parallel_tools": 3})
    calls = [
        call("s1", "search_documents", query="Agent", limit=2),
        call("s2", "search_documents", query="可靠性", limit=2),
        call("s3", "search_documents", query="工具", limit=2),
    ]
    state = await run_first_batch(settings, calls)
    tool_messages = [m for m in state.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["s1", "s2", "s3"]
    assert all("matches" in json.loads(m["content"]) for m in tool_messages)
    assert state.tool_calls == 3


async def test_serial_and_parallel_measure_different_wait(settings, monkeypatch):
    """给读取加人为等待，串行应接近三倍耗时，并行接近一倍。

    这只是教学用的人为 I/O；真实工具的收益取决于等待时间占比，
    纯 CPU 计算在线程池里仍受 GIL 限制，不会有这种线性下降。
    """
    original_read = Corpus.read
    delay = 0.15

    def slow_read(self, args):
        time.sleep(delay)
        return original_read(self, args)

    monkeypatch.setattr(Corpus, "read", slow_read)
    calls = [
        call(f"r{index}", "read_document", document_id=doc_id)
        for index, doc_id in enumerate(["agent-loop", "reliability", "tool-design"])
    ]

    serial_settings = settings.model_copy(
        update={"max_turns": 1, "max_parallel_tools": 1, "tool_timeout": 5}
    )
    started = time.monotonic()
    await run_first_batch(serial_settings, calls)
    serial_elapsed = time.monotonic() - started

    parallel_settings = settings.model_copy(
        update={"max_turns": 1, "max_parallel_tools": 3, "tool_timeout": 5}
    )
    started = time.monotonic()
    state = await run_first_batch(parallel_settings, calls)
    parallel_elapsed = time.monotonic() - started

    assert len(state.evidence) == 3
    assert serial_elapsed > delay * 2.4
    assert parallel_elapsed < delay * 2


async def test_one_tool_error_does_not_block_batch(settings):
    """未知工具报错时，同批的合法读取仍应完成并进入证据。"""
    settings = settings.model_copy(update={"max_turns": 1, "max_parallel_tools": 3})
    state = await run_first_batch(
        settings,
        [
            call("bad", "delete_everything"),
            call("good", "read_document", document_id="tool-design"),
        ],
    )
    outputs = [json.loads(m["content"]) for m in state.messages if m["role"] == "tool"]
    assert "工具不存在" in outputs[0]["error"]
    assert outputs[1]["document"]["document_id"] == "tool-design"
    assert state.evidence == {"tool-design": outputs[1]["document"]}


async def test_tool_timeout_is_isolated(settings, monkeypatch):
    """单个工具超时只影响它自己，不会取消同批其他调用。"""
    original_read = Corpus.read

    def slow_read(self, args):
        time.sleep(0.6)
        return original_read(self, args)

    monkeypatch.setattr(Corpus, "read", slow_read)
    settings = settings.model_copy(
        update={"max_turns": 1, "max_parallel_tools": 3, "tool_timeout": 0.10}
    )
    started = time.monotonic()
    state = await run_first_batch(
        settings,
        [
            call("a", "read_document", document_id="agent-loop"),
            call("b", "read_document", document_id="reliability"),
        ],
    )
    assert time.monotonic() - started < 0.5
    outputs = [json.loads(m["content"]) for m in state.messages if m["role"] == "tool"]
    assert all("超时" in output["error"] for output in outputs)
    # 超时不会回滚证据，这里两项都没写成功，符合“先读后存”的顺序。
    assert state.evidence == {}


def test_prepare_reserves_read_slots_in_order():
    """名额检查在并发之前完成：第五篇不同资料应被拒绝，重复资料不占新名额。"""
    state = AgentState(
        evidence={
            name: {"document_id": name}
            for name in ["agent-loop", "evaluation", "reliability", "tool-design"]
        }
    )
    jobs = prepare_batch([call("x", "read_document", document_id="another")], state)
    assert jobs[0]["output"]["error"] == "最多读取 4 篇资料"

    reserved_state = AgentState(evidence={"agent-loop": {"document_id": "agent-loop"}})
    jobs = prepare_batch(
        [
            call("a", "read_document", document_id="agent-loop"),
            call("b", "read_document", document_id="agent-loop"),
        ],
        reserved_state,
    )
    assert all(job["output"] is None for job in jobs)


def test_submit_report_stays_exclusive():
    """队列里提交后面还有调用时，不允许提交，避免报告已接受但仍有动作待执行。"""
    state = AgentState(
        messages=[],
        pending=[
            call(
                "submit",
                "submit_report",
                title="t",
                summary="s",
                key_points=["k"],
                citations=[],
                limitations="l",
            ),
            call("read", "read_document", document_id="agent-loop"),
        ],
    )
    jobs = prepare_batch([state.pending[0]], state)
    assert "单独" in jobs[0]["output"]["error"]


def test_settings_parallel_bounds():
    """并行度限制在 1–4：0 会让循环死锁，过大则失去并发边界的意义。"""
    with pytest.raises(ValueError):
        Settings(mode="mock", max_parallel_tools=0)
    assert Settings(mode="mock", max_parallel_tools=4).max_parallel_tools == 4


@pytest.fixture
def settings(tmp_path):
    """每个用例独立的 Mock 配置与临时数据库，避免污染真实任务记录。"""
    return Settings(mode="mock", db_path=tmp_path / "agent.sqlite3")


async def test_parallel_mode_still_produces_valid_report(settings):
    """并行改造后完整流程仍要通过引用校验，而不是只跑得快。"""
    settings = settings.model_copy(update={"max_parallel_tools": 3})
    report, state = await run_native("Agent Loop", settings)
    assert report.citations
    assert state.turns == 3
