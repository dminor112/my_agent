"""第五阶段：对固定题集进行可复现的规则评测。

每条用例独立运行 Agent，记录格式、必要引用、无证据行为、耗时和用量。
某一题失败只记为该题失败，继续收集其余题目的结果，便于统一定位问题。
规则通过不等于语义正确，更不等于真实模型线上成功率；
尤其 Mock 是固定摘录策略，主要检验工程管线，而非语言理解能力。
"""

import asyncio
import json
import time

from .config import ROOT, Settings
from .native import run_native
from .sdk import run_sdk


async def evaluate(settings: Settings, engine: str = "native") -> dict:
    """按同一题集比较原生循环或 SDK，返回逐题检查及整体通过数量。

    每题有独立执行状态；评测不共享会话以免上一题证据污染下一题。
    异常只记录类型，避免把模型响应中可能含有的敏感内容写入结果。
    """
    cases = json.loads((ROOT / "data/evals/cases.json").read_text(encoding="utf-8"))
    results = []
    for case in cases:
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.run_timeout):
                if engine == "sdk":
                    report, metrics = await run_sdk(case["query"], settings)
                else:
                    report, state = await run_native(case["query"], settings)
                    metrics = {
                        "turns": state.turns,
                        "total_tokens": state.total_tokens,
                        "tool_calls": state.tool_calls,
                    }
            # run_native/run_sdk 已返回 Pydantic Report，走到此处代表结构/来源已校验。
            ids = {citation.document_id for citation in report.citations}
            # 必要来源可被更大的引用集合覆盖；不要求模型输出字面完全一致。
            # expect_no_evidence=True 时必须 citations 为空；False 时必须有引用。
            checks = {
                "report_schema_valid": True,
                "required_sources_present": set(case["required_sources"]).issubset(ids),
                "no_evidence_handled": bool(report.citations) != case["expect_no_evidence"],
                "has_limitations": bool(report.limitations.strip()),
            }
            results.append(
                {
                    "name": case["name"],
                    "passed": all(checks.values()),
                    "checks": checks,
                    "metrics": metrics,
                    "latency_seconds": round(time.monotonic() - started, 4),
                }
            )
        except Exception as exc:
            # 一题失败不终止整组评测，保留失败类型和耗时便于比较错误分布。
            results.append(
                {
                    "name": case["name"],
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "latency_seconds": round(time.monotonic() - started, 4),
                }
            )
    return {
        "mode": settings.mode,
        "engine": engine,
        "cases": results,
        "passed": sum(case["passed"] for case in results),
        "total": len(results),
        "scope": "规则型格式、来源与无结果评测；不衡量语义正确性，不代表线上成功率",
    }
