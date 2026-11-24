"""第四阶段恢复实验：模拟提议保存后崩溃，再通过新 Store 读取检查点。

强制 Mock 和临时数据库，不影响真实服务或用户资料。
恢复应继续 pending 工具，而不是重新请求同一轮模型提议。
"""

import asyncio
import json
import tempfile
from pathlib import Path

from my_agent.config import Settings
from my_agent.native import run_native
from my_agent.schemas import RunRequest
from my_agent.service import RunManager
from my_agent.store import Store


# 创建临时任务并模拟异常；重开连接后 paused，再显式恢复到 completed。
# 断言 3 轮表明已保存的搜索提议未再次执行，而非证明任意外部副作用恰好一次。
async def demo(directory: Path):
    settings = Settings(mode="mock", db_path=directory / "agent.sqlite3")
    store = Store(settings.db_path)
    view, _ = store.create(RunRequest(query="Schema"))
    store.transition(view.run_id, ("queued",), "running")

    # 先持久化收到的提议和 pending，再抛出模拟异常。
    # 这里是在同进程模拟故障边界，不是杀死或重启真实服务。
    async def crash_after_checkpoint(state):
        store.save_checkpoint(view.run_id, state)
        raise RuntimeError("演示：检查点保存后模拟进程退出")

    try:
        await run_native("Schema", settings, checkpoint=crash_after_checkpoint)
    except RuntimeError:
        pass
    print("模拟异常前状态：", store.get(view.run_id).status)
    # 新建 Store 对象模拟重新启动；真正恢复依据是持久检查点。
    reopened = Store(settings.db_path)
    reopened.recover()
    print("新服务发现未完成任务：", reopened.get(view.run_id).status)
    manager = RunManager(reopened, settings)
    # recover 只标记 paused，不隐式重新发请求；这里模拟用户显式恢复。
    await manager.resume(view.run_id)
    for _ in range(200):
        final = reopened.get(view.run_id)
        if final.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    assert final.status == "completed", final.error
    assert final.metrics["turns"] == 3
    print(json.dumps(final.model_dump(), ensure_ascii=False, indent=2))
    print("恢复后共 3 轮：搜索提议未重跑，继续读取和提交。")
    await manager.shutdown()


if __name__ == "__main__":
    # 只清理演示自己创建的临时库，不覆盖/删除用户资料或真实任务数据库。
    with tempfile.TemporaryDirectory(prefix="my-agent-resume-") as directory:
        asyncio.run(demo(Path(directory)))
