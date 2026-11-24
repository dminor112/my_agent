"""第七阶段示例：同轮并行只读工具能省多少等待时间。

课程里的检索/读取都在内存里，瞬间返回，量不出并行收益。
这里给读取加人为等待，模拟真实 I/O（网页抓取、数据库查询、文件解析）。
注意：time.sleep 会让出 GIL，所以线程池能重叠这段等待；
纯 CPU 计算不会因此变快，那不是并行度能解决的问题。
"""

import asyncio
import json
import time

from my_agent.config import Settings
from my_agent.model import BudgetExceeded, MockModel
from my_agent.native import AgentState, run_native
from my_agent.tools import Corpus

# 每次读取的模拟等待；3 篇串行约 0.45 秒，并行约 0.15 秒。
DELAY = 0.15
DOCUMENT_IDS = ["agent-loop", "reliability", "tool-design"]


class ThreeReads(MockModel):
    """首轮一次性请求读取三篇资料，制造一个可并行的批次。

    真实模型是否这样“批量发起”取决于提示词和模型习惯；
    这里固定行为，才能把注意力放在调度与等待上。
    """

    def __init__(self):
        self.sent = False

    async def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, int]:
        if not self.sent:
            self.sent = True
            calls = [
                {
                    "id": f"r{index}",
                    "function": {
                        "name": "read_document",
                        "arguments": json.dumps({"document_id": doc_id}),
                    },
                }
                for index, doc_id in enumerate(DOCUMENT_IDS)
            ]
            return {"tool_calls": calls}, 100
        return await super().complete(messages, tools)


async def measure(parallel: int) -> tuple[float, AgentState]:
    """用 max_turns=1 只跑第一批工具，再靠预算终止观察耗时。

    批次结束后没有合法报告，因此这里的 BudgetExceeded 是预期终止，
    不是执行失败；返回 state 只为确认证据确实被写入。
    """

    settings = Settings(mode="mock", max_turns=1, max_parallel_tools=parallel, tool_timeout=5)
    state = AgentState()
    started = time.monotonic()
    try:
        await run_native("工具设计", settings, state=state, model=ThreeReads())
    except BudgetExceeded:
        pass
    return time.monotonic() - started, state


def main() -> None:
    original_read = Corpus.read
    delay = DELAY

    def slow_read(self, args):
        """模拟一次有真实等待的读取，再把控制权交回原实现。"""
        time.sleep(delay)
        return original_read(self, args)

    Corpus.read = slow_read
    try:
        serial_elapsed, _ = asyncio.run(measure(1))
        parallel_elapsed, parallel_state = asyncio.run(measure(3))
    finally:
        # 只在本次进程内替换方法，结束时恢复，避免影响后续调用。
        Corpus.read = original_read

    print(f"三篇资料，每次读取模拟 {delay:.2f}s")
    print(f"串行（max_parallel_tools=1）：{serial_elapsed:.3f}s")
    print(f"并行（max_parallel_tools=3）：{parallel_elapsed:.3f}s")
    speedup = serial_elapsed / parallel_elapsed if parallel_elapsed else 0
    print(f"提速约 {speedup:.1f} 倍，证据数：{len(parallel_state.evidence)}")
    print("同样的等待量下，并行度只改变重叠比例，不减少单次调用本身的开销。")
    print(
        "换成真实 I/O 才有意义；若是 CPU 密集的 Python 处理，线程池仍受 GIL 限制，"
        "应改为进程池或拆分服务。"
    )


if __name__ == "__main__":
    main()
