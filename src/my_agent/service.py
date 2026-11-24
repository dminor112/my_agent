"""第三/四阶段：管理后端任务生命周期，而不是让模型管理任务状态。

RunManager 把持久化的 run_id 和本进程 asyncio.Task 关联起来。
SQLite 保存可恢复的事实；tasks 字典和 Semaphore 只是当前进程的调度资源。
典型路径：queued → running → completed / awaiting_approval / failed。
取消与停机先更新数据库状态，再取消协程；恢复保留累计预算，不从零重跑。
这是单进程教学实现，没有分布式队列、跨进程租约或多租户授权。
"""

import asyncio
import json
import time

from .config import Settings
from .model import BudgetExceeded, ModelError
from .native import AgentState, run_native
from .schemas import Report, RunRequest
from .store import Conflict, Store


class RunManager:
    """单进程任务调度器：数据库记事实，异步协程执行模型/工具。"""

    def __init__(self, store: Store, settings: Settings):
        """接收依赖，不自行加载配置；这样测试可使用独立临时数据库。"""
        self.store, self.settings = store, settings
        # 保留 Task 强引用，支持取消和停机；任务结束后回调及时清理。
        self.tasks: dict[str, asyncio.Task] = {}
        # semaphore 限制真正执行中的任务数，不限制已创建的持久任务总数。
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_runs)

    def schedule(self, run_id: str) -> None:
        """创建后台协程但不等待完成，避免 HTTP 请求被长任务占住。

        完成回调仅删除它自己的记录，防止旧 Task 误删同 ID 新创建的 Task。
        """
        old = self.tasks.get(run_id)
        if old and not old.done():
            raise Conflict("任务仍在执行")
        task = asyncio.create_task(self.execute(run_id))
        self.tasks[run_id] = task
        task.add_done_callback(
            lambda done: self.tasks.pop(run_id, None) if self.tasks.get(run_id) is done else None
        )

    async def create(self, request: RunRequest, key: str | None = None):
        """创建/复用持久任务，仅新建记录会进入后台调度。

        队列上限先于幂等检查，是教学实现的简化：满队列时幂等重复请求也会拒绝。
        幂等只保证不会重复创建同一逻辑任务，不保证外部模型请求恰好一次。
        """
        if len(self.tasks) >= 32:
            raise Conflict("任务队列已满")
        self.settings.ensure_model_ready()
        view, created = self.store.create(request, key)
        if created:
            self.store.event(view.run_id, "queued", {})
            self.schedule(view.run_id)
        return view

    async def execute(self, run_id: str) -> None:
        """等待执行槽位，加载检查点，运行反馈循环，再写入终态。

        错误转成数据库中的 failed 和脱敏事件；CancelledError 保持取消语义。
        elapsed_seconds 从等待槽位前开始，包含本次排队时间，不是纯模型延迟。
        """
        started = time.monotonic()
        try:
            async with self.semaphore:
                self.store.transition(run_id, ("queued",), "running")
                self.store.event(run_id, "running", {})
                row = self.store.row(run_id)
                # 新任务 state=None；恢复任务把整体字典还原，沿用累计轮数/用量。
                state = AgentState(**json.loads(row["checkpoint"])) if row["checkpoint"] else None

                async def checkpoint(data):
                    """把循环的整体状态绑定到本 run_id，不让循环直接依赖数据库。"""
                    self.store.save_checkpoint(run_id, data)

                async def emit(kind, payload):
                    """追加本轮事件；不记录 Key 或完整私密模型请求。"""
                    self.store.event(run_id, kind, payload)

                # 整体超时覆盖模型和工具运行；不包含前面的等待执行槽位阶段。
                async with asyncio.timeout(self.settings.run_timeout):
                    report, final_state = await run_native(
                        row["query"],
                        self.settings,
                        state=state,
                        checkpoint=checkpoint,
                        emit=emit,
                        history=self.store.history(row["session_id"], run_id),
                    )
                metrics = {
                    "turns": final_state.turns,
                    "total_tokens": final_state.total_tokens,
                    "tool_calls": final_state.tool_calls,
                    "elapsed_seconds": round(time.monotonic() - started, 4),
                    "mode": self.settings.mode,
                }
                # 报告已生成不等于已确认；需要人工复核时停在独立状态。
                if row["approval"]:
                    self.store.transition(
                        run_id, ("running",), "awaiting_approval", result=report, metrics=metrics
                    )
                    self.store.event(run_id, "awaiting_approval", {})
                else:
                    self.store.complete(run_id, ("running",), report, metrics)
                    self.store.event(run_id, "completed", {})
        except asyncio.CancelledError:
            # cancel/shutdown 已先更改数据库状态，不把取消误记为运行失败。
            raise
        except Exception as exc:
            if isinstance(exc, (BudgetExceeded, ModelError)):
                error = str(exc)
            elif isinstance(exc, TimeoutError):
                error = "执行超时；可从上次检查点恢复"
            else:
                # 未知异常只记录类型，避免错误对象中的服务响应或私有数据泄漏。
                error = f"执行失败（{type(exc).__name__}）；请检查配置或运行测试"
            try:
                self.store.transition(run_id, ("queued", "running"), "failed", error=error)
                self.store.event(run_id, "failed", {"error": error})
            except Conflict:
                pass  # 若任务已被取消/暂停，不用失败状态覆盖用户或停机操作。

    async def cancel(self, run_id: str):
        """先将任务标记 cancelled，再取消并等待后台协程退出。

        取消是终态，不能再 resume；已 completed 的任务也不接受取消。
        """
        self.store.transition(
            run_id, ("queued", "running", "awaiting_approval", "paused", "failed"), "cancelled"
        )
        self.store.event(run_id, "cancelled", {})
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.store.get(run_id)

    async def resume(self, run_id: str):
        """只恢复 paused/failed 任务；检查点及累计预算不被重置。

        completed/cancelled 不可恢复；预算已耗尽的 failed 任务可能再次有界失败。
        """
        if len(self.tasks) >= 32:
            raise Conflict("任务队列已满")
        old = self.tasks.get(run_id)
        if old and not old.done():
            raise Conflict("任务仍在执行")
        self.settings.ensure_model_ready()
        self.store.transition(run_id, ("paused", "failed"), "queued")
        self.store.event(run_id, "resumed", {})
        self.schedule(run_id)
        return self.store.get(run_id)

    def approve(self, run_id: str):
        """确认已有的待审核报告，不重新调用模型、不发布到外部系统。"""
        row = self.store.row(run_id)
        if not row["result"]:
            raise Conflict("尚无可确认报告")
        self.store.complete(
            run_id, ("awaiting_approval",), Report.model_validate_json(row["result"])
        )
        self.store.event(run_id, "approved", {})
        return self.store.get(run_id)

    async def shutdown(self):
        """停机时把排队/运行任务标记 paused，并回收当前进程的协程。

        list 快照避免完成回调在遍历时修改 tasks；后续由用户显式恢复。
        """
        tasks = list(self.tasks.items())
        for run_id, task in tasks:
            try:
                self.store.transition(run_id, ("queued", "running"), "paused")
                self.store.event(run_id, "paused", {})
            except Conflict:
                pass
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
