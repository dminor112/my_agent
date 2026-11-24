"""第一阶段：不用 Agent 框架，显式实现可检查、可恢复的反馈循环。

阅读主线：调用模型 → 校验提议 → 执行白名单工具 → 回填结果 → 再次调用模型。
成功终点不是模型随口说“完成”，而是 submit_report 通过结构和来源校验。
可恢复状态保存在 AgentState；检查点回调决定如何持久化，事件回调决定如何观测。
有待执行工具 pending 时先处理它们，避免恢复后重复请求同一轮模型提议。
模型响应与数据库事务不是原子操作，崩溃窗口内仍可能重复模型调用和费用。

阶段七补丁：同一轮内的只读工具可以并行，submit_report 仍独占执行。
并行只缩短“等待”时间；工具回填顺序、证据名额和错误隔离都要显式设计。
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from pydantic import ValidationError

from .config import Settings
from .model import BudgetExceeded, ModelError, make_model
from .schemas import ReadArgs, Report, SearchArgs
from .tools import SYSTEM_PROMPT, TOOLS, Corpus, ToolError, validate_report


@dataclass
class AgentState:
    """一条任务可恢复的整体状态，不包含密钥或网络客户端。"""

    # messages 是模型看到的协议历史，包含 system/user/assistant/tool 消息。
    messages: list[dict] = field(default_factory=list)
    # pending 保存已收到但尚未执行完的调用，恢复时不必再次请求模型。
    pending: list[dict] = field(default_factory=list)
    # evidence 只收录真实读取的正文，是最终引用校验的依据。
    evidence: dict[str, dict] = field(default_factory=dict)
    # turns 计模型提议轮数，tool_calls 计工具尝试次数（含校验失败）。
    turns: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    # 只有通过业务校验的报告才能写入 report，不能直接信任自然语言回答。
    report: dict | None = None

    def dump(self) -> dict:
        """生成独立可序列化字典，供持久化回调保存一致的状态快照。"""
        return asdict(self)


# 两个回调把“执行逻辑”与“数据库/观测平台”解耦，CLI 无需依赖 SQLite。
Checkpoint = Callable[[dict], Awaitable[None]]
EventSink = Callable[[str, dict], Awaitable[None]]


async def noop(*args) -> None:
    """默认空回调：独立 CLI 示例不需要持久化或外部事件消费者。"""
    pass


def take_batch(state: AgentState, limit: int) -> list[dict]:
    """从 pending 头部取一批可并行执行的调用，并保持模型的原始顺序。

    submit_report 会写入最终应用状态且应结束循环，因此作为批次边界独占执行；
    否则可能出现“报告已接受、但同批其他工具仍在改证据”的模糊状态。
    limit 只限制并行度，不改变剩余调用的先后；1 即退回逐步串行。
    """
    if not state.pending:
        return []
    first = state.pending[0]["function"].get("name", "")
    if first == "submit_report":
        return [state.pending[0]]
    batch: list[dict] = []
    for call in state.pending:
        if call["function"].get("name", "") == "submit_report":
            break
        batch.append(call)
        if len(batch) >= limit:
            break
    return batch


def prepare_batch(batch: list[dict], state: AgentState) -> list[dict]:
    """串行解析参数、做 Schema 校验并预留读取名额，再交给并行执行。

    解析和名额检查放在并发之前，结果就不依赖线程完成顺序；
    否则两个并发读取可能同时看到“还剩一格”而一起通过。
    """
    jobs: list[dict] = []
    # 已读集合加本批预留，等价于“排队时先占号”。
    reserved = set(state.evidence)
    for call in batch:
        name = call["function"].get("name", "")
        job = {"call": call, "tool": name, "kind": "", "args": None, "output": None}
        try:
            # arguments 是模型生成的 JSON 字符串，必须先解析再做 Schema 校验。
            # 不使用 eval，不根据模型名称反射任意函数，只允许以下三个分支。
            args = json.loads(call["function"].get("arguments", "{}"))
            if name == "search_documents":
                job.update(kind="search", args=SearchArgs.model_validate(args))
            elif name == "read_document":
                parsed = ReadArgs.model_validate(args)
                if len(reserved) >= 4 and parsed.document_id not in reserved:
                    raise ToolError("最多读取 4 篇资料")
                reserved.add(parsed.document_id)
                job.update(kind="read", args=parsed)
            elif name == "submit_report":
                # 队列还有后续调用时不接受提交，避免成功报告后遗留未执行动作。
                if len(state.pending) != 1:
                    raise ToolError("submit_report 必须是当前单独的工具调用")
                job.update(kind="submit", args=Report.model_validate(args))
            else:
                raise ToolError("工具不存在；只能调用已声明工具")
        except (ValidationError, ValueError, TypeError) as exc:
            # 工具错误是可恢复反馈，不是直接宣告任务失败；下一轮让模型修正。
            # Pydantic 错误可能包含原始输入，只返回程序编写的通用提示。
            job["output"] = {
                "error": str(exc)
                if isinstance(exc, ToolError)
                else "工具参数不符合 Schema；请修正参数"
            }
        jobs.append(job)
    return jobs


async def execute_job(job: dict, corpus: Corpus, state: AgentState, settings: Settings) -> dict:
    """执行一个已校验的工具调用，返回可回填给模型的输出字典。

    同步检索/读取放到线程池：它们不是 CPU 密集任务，阻塞事件循环会让
    “并行”失效。纯 Python 计算仍受 GIL 限制，并行不会让 CPU 时间减半。
    超时只丢弃本次结果，已经写入的证据不回滚，模型需要自行处理不一致。
    """
    if job["output"] is not None:
        return job["output"]
    try:
        if job["kind"] == "search":
            matches = await asyncio.wait_for(
                asyncio.to_thread(corpus.search, job["args"]), timeout=settings.tool_timeout
            )
            return {"matches": matches}
        if job["kind"] == "read":
            doc = await asyncio.wait_for(
                asyncio.to_thread(corpus.read, job["args"]), timeout=settings.tool_timeout
            )
            state.evidence[doc["document_id"]] = doc
            return {"document": doc}
        # 提交只做纯内存校验，直接在事件循环执行，不需要线程池。
        report = validate_report(job["args"], state.evidence)
        state.report = report.model_dump()
        return {"accepted": True}
    except TimeoutError:
        # 线程无法被强制终止，超时后它可能仍在后台跑完；这里只是不再等待。
        return {"error": "工具执行超时；请缩小范围或稍后重试"}
    except (ValidationError, ValueError, TypeError) as exc:
        return {"error": str(exc) if isinstance(exc, ToolError) else "工具执行失败；请修正参数"}


async def run_native(
    query: str,
    settings: Settings,
    *,
    state: AgentState | None = None,
    model=None,
    checkpoint: Checkpoint = noop,
    emit: EventSink = noop,
    history: list[dict] | None = None,
) -> tuple[Report, AgentState]:
    """运行或恢复一次 Agent，返回已校验报告和最终执行状态。

    state=None 是新任务；非空 state 必须来自可信的内部检查点。
    history 只在初始化新消息时注入，不与恢复消息重复拼接。
    model 可由测试注入；未传时由本函数创建并在 finally 中关闭。
    checkpoint/emit 都是异步回调，成功写入点与事件发送点显式可见。
    """
    corpus = Corpus(settings.corpus_dir)
    state = state or AgentState()
    if state.report:
        return Report.model_validate(state.report), state
    if not state.messages:
        state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": query},
        ]
    # 所有权约定：调用方注入的模型由调用方清理，不能在这里意外关闭共享客户端。
    owned = model is None
    model = model or make_model(settings)
    try:
        while True:
            # 让事件循环处理取消/其他任务；不靠模型来决定是否允许取消。
            await asyncio.sleep(0)
            if not state.pending:
                # 没有待执行工具时才进入下一轮模型调用，恢复不会跳过旧工具。
                # 每次请求前检查累计预算，避免重复 resume 后预算归零。
                if state.turns >= settings.max_turns:
                    raise BudgetExceeded("达到最大模型调用轮数")
                if state.total_tokens >= settings.max_total_tokens:
                    raise BudgetExceeded("达到累计 token 预算")
                if len(json.dumps(state.messages, ensure_ascii=False)) > settings.max_context_chars:
                    raise BudgetExceeded("上下文超过字符预算；请缩小资料或清理会话")
                await emit("model_started", {"turn": state.turns + 1})
                # wait_for 约束一次逻辑提议；GLM 内部的有限重试也包含在此时间内。
                proposal, tokens = await asyncio.wait_for(
                    model.complete(state.messages, TOOLS), timeout=settings.request_timeout
                )
                state.turns += 1
                state.total_tokens += tokens
                if state.total_tokens > settings.max_total_tokens:
                    await checkpoint(state.dump())
                    raise BudgetExceeded("模型响应后超过 token 预算，停止后续执行")
                calls = proposal.get("tool_calls") or []
                if not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls):
                    await checkpoint(state.dump())
                    raise ModelError("工具调用结构无效")
                if len(calls) > 8:
                    await checkpoint(state.dump())
                    raise ModelError("单轮工具调用数量超过限制")
                if not calls:
                    # 自然语言输出不作为成功报告；要求模型改用结构化提交工具。
                    # 重复不提交仍受 max_turns 限制，不会形成无限对话。
                    text = proposal.get("content") or ""
                    state.messages.extend(
                        [
                            {"role": "assistant", "content": text[:4000]},
                            {"role": "user", "content": "请通过 submit_report 提交结构化报告。"},
                        ]
                    )
                else:
                    # 工具 ID 要唯一且非空，才能把 tool 消息准确关联到 assistant 提议。
                    ids = [c.get("id") for c in calls]
                    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(
                        ids
                    ):
                        raise ModelError("工具调用 ID 无效或重复")
                    if any(not isinstance(c.get("function"), dict) for c in calls):
                        raise ModelError("工具调用结构无效")
                    state.messages.append(
                        {"role": "assistant", "content": None, "tool_calls": calls}
                    )
                    state.pending = list(calls)
                # 先保存收到的提议及待执行调用，再执行工具；这是重要恢复边界。
                await checkpoint(state.dump())
                await emit(
                    "model_finished", {"turn": state.turns, "total_tokens": state.total_tokens}
                )
                if not calls:
                    continue
            # 阶段七：取一批调用并行执行；单个工具失败不会中止同批其他工具。
            batch = take_batch(state, settings.max_parallel_tools)
            jobs = prepare_batch(batch, state)
            started = time.monotonic()
            for job in jobs:
                await emit("tool_started", {"tool": job["tool"], "batch": len(jobs)})
            # gather 按任务创建顺序返回结果，因此回填顺序与模型调用顺序一致。
            outputs = await asyncio.gather(
                *(execute_job(job, corpus, state, settings) for job in jobs)
            )
            for job, output in zip(jobs, outputs, strict=True):
                # 用原 call_id 回填；模型靠它区分哪次调用成功或失败。
                state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": job["call"]["id"],
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )
                state.tool_calls += 1
                await emit("tool_finished", {"tool": job["tool"]})
                if "error" in output:
                    await emit("tool_error", {"tool": job["tool"], "error": output["error"]})
            state.pending = state.pending[len(batch) :]
            # 整批完成后统一保存；批中途崩溃会重跑整批只读工具，但不会重复报告。
            await checkpoint(state.dump())
            await emit(
                "tool_batch_finished",
                {
                    "count": len(batch),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            if state.report:
                await emit("report_ready", {"citations": len(state.report["citations"])})
                return Report.model_validate(state.report), state
    finally:
        if owned:
            await model.close()
