"""预算回归测试：失败/恢复保留累计轮数与用量，不因 resume 获得新额度。

使用小预算和 Mock 快速触发边界，另覆盖不合法 tool_calls 的协议错误。
测试名称保留原文，注释解释实际断言范围，避免夸大验证结论。
"""

import asyncio
import json

import pytest

from my_agent.config import Settings
from my_agent.schemas import RunRequest
from my_agent.service import RunManager
from my_agent.store import Store


# 一轮预算触发失败，恢复同任务后仍是相同轮数和 token。
# 证明 resume 不等于重新获得预算，避免无限恢复绕过限制。
async def test_failed_budget_preserved_across_resume(tmp_path):
    settings = Settings(mode="mock", max_turns=1, db_path=tmp_path / "agent.sqlite3")
    store = Store(settings.db_path)
    manager = RunManager(store, settings)
    view = await manager.create(RunRequest(query="Schema"))
    for _ in range(100):
        if store.get(view.run_id).status == "failed":
            break
        await asyncio.sleep(0.01)
    assert store.get(view.run_id).status == "failed"
    checkpoint = json.loads(store.row(view.run_id)["checkpoint"])
    assert checkpoint["turns"] == 1
    await asyncio.sleep(0)
    await manager.resume(view.run_id)
    for _ in range(100):
        if store.get(view.run_id).status == "failed":
            break
        await asyncio.sleep(0.01)
    assert store.get(view.run_id).status == "failed"
    restored = json.loads(store.row(view.run_id)["checkpoint"])
    assert restored["turns"] == 1
    assert restored["total_tokens"] == 100
    await manager.shutdown()


# 此用例实际验证到达 100 token 后停止，指标保留 100。
# 名称虽含 overshoot，当前断言并未模拟单次响应超过预算的场景。
async def test_budget_overshoot_saved_before_failure(tmp_path):
    settings = Settings(mode="mock", max_total_tokens=100, db_path=tmp_path / "agent.sqlite3")
    store = Store(settings.db_path)
    manager = RunManager(store, settings)
    view = await manager.create(RunRequest(query="Schema"))
    for _ in range(100):
        if store.get(view.run_id).status == "failed":
            break
        await asyncio.sleep(0.01)
    assert store.get(view.run_id).status == "failed"
    assert store.get(view.run_id).metrics["total_tokens"] == 100
    await manager.shutdown()


# 参数化测试字符串、非字典元素与 None 元素等非法 tool_calls 形状。
# 应转换成受控 ModelError，而非在后续分发中发生属性异常。
@pytest.mark.parametrize("tools", ["bad", ["bad"], [None]])
async def test_wrong_tool_shape_becomes_model_error(tmp_path, tools):
    from my_agent.model import MockModel, ModelError
    from my_agent.native import run_native

    # 直接返回参数化的非法提议，验证协议校验在工具执行前发生。
    class Malformed(MockModel):
        # 不执行工具，仅制造不合法的提议结构供协议校验测试。
        async def complete(self, messages, schema):
            return {"tool_calls": tools}, 5

    with pytest.raises(ModelError):
        await run_native("Schema", Settings(mode="mock"), model=Malformed())
