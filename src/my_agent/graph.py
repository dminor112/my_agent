"""第六阶段：固定检索流程 + 模型总结 + 人工复核的 LangGraph 示例。

plan/retrieve 是普通代码节点，summarize 才运行模型反馈循环，review 可人工暂停。
AsyncSqliteSaver 将节点间状态持久化，thread_id 标识一次图执行历史。
恢复从未完成节点的入口重放，而不是从中断代码行继续；
节点内尚未保存的模型调用仍可能再次执行，因此不能宣称端到端恰好一次。
本阶段复用原生循环，不把每个工作流节点包装成一个“自主 Agent”。
"""

import json
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import Settings
from .native import AgentState, run_native
from .schemas import ReadArgs, SearchArgs
from .tools import SYSTEM_PROMPT, Corpus


class ResearchState(TypedDict, total=False):
    """图节点间共享状态，每个节点只返回自己更新的字段。

    total=False 允许初始输入只有 query 等少量字段，后续由节点逐步补齐。
    这是图状态契约，不是另一套网络模型协议，也不包含客户端连接。
    """

    query: str
    plan: list[str]
    evidence: dict
    report: dict
    metrics: dict
    require_approval: bool
    approved: bool
    status: str


def build_graph(settings: Settings, checkpointer):
    """组装节点及固定执行边，注入持久检查点实现而不绑定具体连接。"""
    corpus = Corpus(settings.corpus_dir)

    def plan(state):
        """给出固定教学计划，不调用模型；适合可预测的执行步骤。"""
        # 这是普通确定性节点，不应把固定列表描述成模型自主规划。
        return {"plan": ["检索本地资料", "读取证据", "模型生成带引用报告", "人工复核"]}

    def retrieve(state):
        """按固定规则检索并读取证据，节点返回后由图保存该步状态。"""
        matches = corpus.search(SearchArgs(query=state["query"], limit=3))
        evidence = {
            m["document_id"]: corpus.read(ReadArgs(document_id=m["document_id"])) for m in matches
        }
        return {"evidence": evidence}

    async def summarize(state):
        """把已检索证据包装成合法消息，再复用原生模型/工具反馈循环。

        前序节点由代码决定检索，模型仍负责选择如何总结以及提交报告。
        伪装的历史调用仅表达已执行的检索事实，不新增网络请求或模型调用次数。
        此节点未完成前发生异常，图恢复可能重跑节点内模型请求。
        """
        # 使用图节点已收集的证据初始化循环，而不是让模型再次从空白开始。
        evidence = state["evidence"]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": state["query"]},
        ]
        search_args = {"query": state["query"], "limit": 3}
        calls = [("search_documents", search_args, {"matches": list(evidence.values())})]
        calls += [
            ("read_document", {"document_id": key}, {"document": doc})
            for key, doc in evidence.items()
        ]
        # 每个已执行结果都配一条 assistant 调用和对应 tool 输出，遵守消息协议。
        for index, (name, args, output) in enumerate(calls):
            call_id = f"graph_evidence_{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(output, ensure_ascii=False),
                    },
                ]
            )
        report, final = await run_native(
            state["query"], settings, state=AgentState(messages=messages, evidence=evidence)
        )
        return {
            "report": report.model_dump(),
            "metrics": {
                "turns": final.turns,
                "total_tokens": final.total_tokens,
                "tool_calls": final.tool_calls,
            },
        }

    def review(state):
        """需要人工确认时暂停，后续 Command 的 resume 值成为 decision。

        恢复会再次从节点入口运行；interrupt 前不应放非幂等写操作。
        仅布尔 True 被接受，避免非空字符串等被误判为授权。
        """
        if state.get("require_approval"):
            decision = interrupt({"message": "请审核报告后确认或拒绝", "report": state["report"]})
            return {"approved": decision is True}
        return {"approved": True}

    def finish(state):
        """把明确的审核结果映射为图终态，拒绝不等于模型执行失败。"""
        return {"status": "completed" if state["approved"] else "rejected"}

    # 节点登记只定义“有什么步骤”，下面的边才定义“按什么顺序执行”。
    # 工作流是固定路径，模型自主反馈循环只位于 summarize 节点内部。
    builder = StateGraph(ResearchState)
    for name, node in [
        ("plan", plan),
        ("retrieve", retrieve),
        ("summarize", summarize),
        ("review", review),
        ("finish", finish),
    ]:
        builder.add_node(name, node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "retrieve")
    builder.add_edge("retrieve", "summarize")
    builder.add_edge("summarize", "review")
    builder.add_edge("review", "finish")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


async def run_graph(
    settings: Settings,
    thread_id: str,
    query: str | None = None,
    require_approval: bool = False,
    decision: bool | None = None,
) -> dict:
    """开始新图执行，或用相同 thread_id 继续已有检查点。

    query 非空表示新任务；恢复时 query=None，避免覆盖原执行输入。
    decision=True/False 是人工决定，None 表示按原状态重试，未审核时仍会暂停。
    每次重建图和数据库连接，展示持久恢复不依赖上次进程的内存对象。
    """
    path = settings.db_path.parent / "graph.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        graph = build_graph(settings, saver)
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await graph.aget_state(config)
        if query is not None:
            if snapshot.values:
                raise ValueError("thread_id 已存在，请换一个 ID；恢复时不要再次传 query")
            payload = {"query": query, "require_approval": require_approval}
        else:
            if not snapshot.values:
                raise ValueError("thread_id 不存在，无法恢复")
            if not snapshot.next:
                return {"thread_id": thread_id, "state": snapshot.values, "interrupted": False}
            payload = Command(resume=decision) if decision is not None else None
        # sync 要求一步检查点写完再继续下一步，以一致性换取少量额外延迟。
        # 它仍不等于节点内模型请求与数据库之间存在分布式事务。
        result = await graph.ainvoke(payload, config, durability="sync")
        interrupted = bool(result.pop("__interrupt__", None))
        return {"thread_id": thread_id, "state": result, "interrupted": interrupted}
