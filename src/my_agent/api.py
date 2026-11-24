"""HTTP 接口层：用 FastAPI 暴露任务操作与 SSE 事件，不承载模型决策。

create_app 是应用工厂；数据库和任务管理器在 lifespan 启动时创建，停机时释放。
请求通过 Schema 校验后交给 RunManager；HTTP 响应不等待模型完成。
SSE 连接只是观察窗口，断开连接不会自动取消任务；取消必须调用专用接口。
配置服务令牌后保护 /runs 和 /sessions，/health 不返回任何秘密。
这是本机单 worker 示例，不包含生产级多用户权限、限流或 TLS。
"""

import asyncio
import hmac
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .config import Settings, load_settings
from .schemas import RunRequest, RunView
from .service import RunManager
from .store import Conflict, Store


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造可独立测试的应用，允许测试传入 Mock 配置和临时数据库。

    数据库/调度器放在 lifespan 中初始化，必须由 ASGI 服务执行启动流程后再用接口。
    """
    settings = settings or load_settings()
    store, manager = None, None

    @asynccontextmanager
    async def lifespan(app):
        """yield 前是启动，yield 后是停机；不在导入模块时启动任务。"""
        nonlocal store, manager
        store = Store(settings.db_path)
        store.recover()
        manager = RunManager(store, settings)
        app.state.store, app.state.manager = store, manager
        yield
        await manager.shutdown()

    app = FastAPI(title="资料检索与总结 Agent 学习 Demo", version="0.1.0", lifespan=lifespan)

    async def authorize(authorization: str | None = Header(default=None)):
        """可选 Bearer 鉴权依赖；恒定时间比较减少令牌比较时间泄漏。

        未设置令牌时仅适合受信任本机；此机制不是逐用户资源授权。
        """
        token = settings.service_token.get_secret_value()
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "需要有效的 Bearer Token")

    def mapped_error(exc):
        """业务异常转换为稳定 HTTP 状态码，避免直接暴露内部错误对象。"""
        if isinstance(exc, KeyError):
            return HTTPException(404, "任务不存在")
        if isinstance(exc, Conflict):
            return HTTPException(409, str(exc))
        return HTTPException(400, "模型配置不完整；检查 my_agent/.env")

    dependencies = [Depends(authorize)]

    @app.get("/health")
    async def health():
        """只检查应用可访问和当前模式，不发起付费模型请求或测试账号权限。"""
        return {
            "status": "ok",
            "mode": settings.mode,
            "model": settings.model,
            "retrieval": "local_corpus",
        }

    @app.post("/runs", response_model=RunView, status_code=202, dependencies=dependencies)
    async def start_run(
        request: RunRequest, idempotency_key: str | None = Header(default=None, max_length=100)
    ):
        """接受任务并立即返回 202，模型后台运行；幂等键由请求头提供。"""
        try:
            return await manager.create(request, idempotency_key)
        except (KeyError, Conflict, ValueError) as exc:
            raise mapped_error(exc) from exc

    @app.get("/runs/{run_id}", response_model=RunView, dependencies=dependencies)
    async def get_run(run_id: str):
        """轮询任务当前事实；result 是否完成要同时看 status。"""
        try:
            return store.get(run_id)
        except KeyError as exc:
            raise mapped_error(exc) from exc

    @app.post("/runs/{run_id}/cancel", response_model=RunView, dependencies=dependencies)
    async def cancel(run_id: str):
        """显式取消任务；浏览器断开事件流不会自动触发这个动作。"""
        try:
            return await manager.cancel(run_id)
        except (KeyError, Conflict) as exc:
            raise mapped_error(exc) from exc

    @app.post("/runs/{run_id}/resume", response_model=RunView, dependencies=dependencies)
    async def resume(run_id: str):
        """从原任务检查点恢复，不新建任务，也不清零轮数/token 预算。"""
        try:
            return await manager.resume(run_id)
        except (KeyError, Conflict, ValueError) as exc:
            raise mapped_error(exc) from exc

    @app.post("/runs/{run_id}/approve", response_model=RunView, dependencies=dependencies)
    async def approve(run_id: str):
        """人工确认已有待审核报告，不重复调用模型或对外发布。"""
        try:
            return manager.approve(run_id)
        except (KeyError, Conflict) as exc:
            raise mapped_error(exc) from exc

    @app.get("/sessions/{session_id}", dependencies=dependencies)
    async def history(session_id: str):
        """返回有界成功对话历史，服务令牌保护但没有多租户 ACL。"""
        return {"messages": store.history(session_id)}

    @app.get("/runs/{run_id}/events", dependencies=dependencies)
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
    ):
        """建立 SSE 观测流，用 after/Last-Event-ID 恢复游标而非重新启动任务。

        普通查询参数与客户端重连头取最大值，避免重复播放旧事件。
        """
        try:
            store.get(run_id)
        except KeyError as exc:
            raise mapped_error(exc) from exc
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError as exc:
                raise HTTPException(400, "Last-Event-ID 必须是整数") from exc

        async def stream():
            """按序生成 id/event/data 三字段，结束前排空全部事件页。

            冒号开头的 heartbeat 是 SSE 注释，不作为业务事件处理。
            这是任务进度流，不是模型 token 流或隐藏推理。
            """
            cursor = after
            while not await request.is_disconnected():
                rows = store.events(run_id, cursor)
                for event in rows:
                    cursor = event["seq"]
                    body = json.dumps(event, ensure_ascii=False)
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {body}\n\n"
                status = store.get(run_id).status
                if status in {"completed", "failed", "cancelled", "awaiting_approval", "paused"}:
                    # 终态后仍需排空所有分页，避免超过 200 条时只发送第一页。
                    if len(rows) < 200:
                        break
                    continue
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.1)

        # 禁止缓存并提示反向代理不要缓冲，否则事件可能直到任务结束才展示。
        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
