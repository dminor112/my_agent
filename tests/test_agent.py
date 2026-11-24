"""核心回归测试：配置、工具、原生循环、SDK、服务与持久图。

所有 Settings 显式选择 Mock，真实 GLM 路径仅通过 MockTransport 截获请求。
测试使用 pytest 临时目录隔离数据库，不读取真实任务记录或发起付费模型调用。
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from my_agent.api import create_app
from my_agent.config import ROOT, Settings, load_settings
from my_agent.evaluation import evaluate
from my_agent.graph import run_graph
from my_agent.model import BudgetExceeded, GLMModel, MockModel, ModelError
from my_agent.native import AgentState, run_native
from my_agent.schemas import ReadArgs, RunRequest, SearchArgs
from my_agent.sdk import run_sdk
from my_agent.service import RunManager
from my_agent.store import Conflict, Store
from my_agent.tools import Corpus, ToolError, validate_report


# 每个测试得到独立 Mock 配置和临时数据库。
# 即使用户 .env 已切 glm，测试也不产生真实模型费用。
@pytest.fixture
def settings(tmp_path):
    return Settings(mode="mock", db_path=tmp_path / "agent.sqlite3")


# 注入假密钥并读取配置，验证模型名/资料路径及对象 repr 不显示明文。
# monkeypatch 测试结束自动撤销环境变量改动。
def test_configuration_path_and_key_redaction(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "secret-testing-key")
    config = load_settings("mock")
    assert config.model == "glm-5.3-flash"
    assert "secret-testing-key" not in repr(config)
    assert config.corpus_dir == ROOT / "data/corpus"


# 把 Mock 配置切成 glm 但不提供 Key，期待尽早抛 ValueError。
# 证明配置失败不会悄悄回退为 Mock。
def test_missing_key_does_not_fallback(settings):
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        settings.model_copy(update={"mode": "glm"}).ensure_model_ready()


# 参数化验证 HTTP、URL 内嵌凭据和 query 均被拒绝。
# 只检查配置格式，不连接这些地址。
@pytest.mark.parametrize(
    "url", ["http://example.com", "https://a:b@example.com", "https://a/?key=x"]
)
def test_invalid_base_url(settings, url):
    with pytest.raises(ValueError):
        settings.model_copy(update={"base_url": url}).ensure_model_ready()


# 验证关键词检索与无结果，以及路径字符串、超限数量和额外字段拒绝。
# Schema 正确与文档存在是两层不同校验。
def test_tools_search_read_and_path_rejection(settings):
    corpus = Corpus(settings.corpus_dir)
    assert corpus.search(SearchArgs(query="Schema"))[0]["document_id"] == "tool-design"
    assert corpus.search(SearchArgs(query="量子纠缠超导材料临界温度")) == []
    with pytest.raises(ToolError):
        corpus.read(ReadArgs(document_id="../../.env"))
    with pytest.raises(ValidationError):
        SearchArgs(query="ok", limit=100)
    with pytest.raises(ValidationError):
        SearchArgs(query="ok", secret="x")


# 在临时资料目录建立指向目录外的链接，期待加载失败。
# 只访问测试文件，不读取用户私密文件。
def test_corpus_rejects_external_symlink(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "private.json"
    outside.write_text("{}")
    (corpus / "escape.json").symlink_to(outside)
    with pytest.raises(ToolError):
        Corpus(corpus)


# 先完成成功循环，再复制报告伪造原文或 URL，期待来源校验失败。
# 这些断言证明来源一致性，不证明全部总结的语义正确性。
async def test_native_loop_and_provenance(settings):
    report, state = await run_native("Agent 循环是什么", settings)
    assert report.citations
    assert state.pending == []
    assert state.turns <= settings.max_turns
    assert state.report
    validate_report(report, state.evidence)
    bad = report.model_copy(deep=True)
    bad.citations[0].quote = "invented quote not found in evidence"
    with pytest.raises(ToolError):
        validate_report(bad, state.evidence)
    bad = report.model_copy(deep=True)
    bad.citations[0].url = "https://invented.invalid"
    with pytest.raises(ToolError):
        validate_report(bad, state.evidence)


# 用内置库不包含的问题触发无结果，期待空引用和明确缺少证据。
# 不应该由 Mock 臆造专业答案。
async def test_no_evidence_report(settings):
    report, _ = await run_native("量子纠缠超导材料临界温度", settings)
    assert not report.citations
    assert "没有找到" in report.summary


# 将轮数预算降到 1，检索后应终止而非无限继续读取。
async def test_cumulative_turn_budget(settings):
    with pytest.raises(BudgetExceeded):
        await run_native("Agent", settings.model_copy(update={"max_turns": 1}))


# 制造大输入和小字符预算，验证发模型请求前即可有界失败。
async def test_context_budget(settings):
    with pytest.raises(BudgetExceeded):
        await run_native("x" * 2000, settings.model_copy(update={"max_context_chars": 1000}))


# 固定 Mock 每轮用量为 100，将总预算设为 100，验证不能无限后续调用。
async def test_token_budget(settings):
    with pytest.raises(BudgetExceeded):
        await run_native("Agent", settings.model_copy(update={"max_total_tokens": 100}))


# 第一轮伪造未登记 shell 工具，后续恢复 Mock。
# 期待错误回填且最终完成，未知工具不会被反射执行。
async def test_bad_tool_recovers_without_execution(settings):
    # 模型替身首轮制造未知工具错误，之后委托正常 Mock 固定策略。
    class BadThenMock(MockModel):
        first = True

        # 首次输出未知工具，后续恢复正常提议，验证反馈循环可自行纠错。
        async def complete(self, messages, tools):
            if self.first:
                self.first = False
                return {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "bad",
                            "function": {"name": "shell", "arguments": '{"command":"anything"}'},
                        }
                    ],
                }, 10
            return await super().complete(messages, tools)

    report, state = await run_native("Schema", settings, model=BadThenMock())
    assert report.citations
    assert any(
        "工具不存在" in message.get("content", "")
        for message in state.messages
        if message["role"] == "tool"
    )


# 保存提议后故意崩溃，再恢复 state，检查 pending 优先执行。
# 三轮是搜索、读取、提交，已保存的搜索提议没有重请求。
async def test_checkpoint_resume_from_pending_tools(settings):
    saved = []

    # 仅用于区别预期模拟异常和其他失败，不是生产异常类型。
    class SimulatedCrash(RuntimeError):
        pass

    # 保留完整检查点并触发模拟异常，制造“模型返回、工具未执行”的边界。
    async def crash(data):
        saved.append(data)
        raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        await run_native("Schema", settings, checkpoint=crash)
    assert saved[-1]["pending"]
    report, state = await run_native("Schema", settings, state=AgentState(**saved[-1]))
    assert report.citations[0].document_id == "tool-design"
    assert state.turns == 3  # 搜索、读取、提交三轮；已保存的搜索提议不重复请求。


# Mock 替换模型而非 Runner；三轮与引用断言验证真实 SDK 工具管线。
async def test_sdk_is_real_runner_with_mock_model(settings):
    report, metrics = await run_sdk("Schema", settings)
    assert report.citations[0].document_id == "tool-design"
    assert metrics["turns"] == 3


# SDK 对照无证据分支，报告应无引用而不是伪造来源。
async def test_sdk_no_evidence(settings):
    report, _ = await run_sdk("量子纠缠超导材料临界温度", settings)
    assert report.citations == []


# 包装模型中的预算应约束真实 Runner 的后续请求。
async def test_sdk_budget(settings):
    with pytest.raises(BudgetExceeded):
        await run_sdk("Schema", settings.model_copy(update={"max_total_tokens": 100}))


# MockTransport 拦截 GLM HTTP：第一次 429，第二次给合法消息。
# 验证路径、模型名、Bearer 头、有限重试和推理字段过滤，不访问外网。
async def test_glm_http_contract_and_transient_retry(settings):
    received = []

    # HTTP 假响应处理器：首次限流、随后成功，用来检查有限重试。
    def handler(request):
        body = json.loads(request.content)
        received.append((request, body))
        if len(received) == 1:
            return httpx.Response(429, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call",
                                    "type": "function",
                                    "function": {
                                        "name": "search_documents",
                                        "arguments": '{"query":"Agent"}',
                                    },
                                }
                            ],
                            "reasoning_content": "not persisted",
                        }
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    config = settings.model_copy(update={"mode": "glm", "api_key": SecretStr("test-key")})
    model = GLMModel(config)
    await model.client.aclose()
    model.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.base_url,
        headers={"Authorization": "Bearer test-key"},
    )
    try:
        message, tokens = await model.complete([{"role": "user", "content": "Agent"}], [])
    finally:
        await model.close()
    assert len(received) == 2
    assert str(received[0][0].url).endswith("/api/paas/v4/chat/completions")
    assert received[0][1]["model"] == "glm-5.3-flash"
    assert received[0][0].headers["Authorization"] == "Bearer test-key"
    assert "reasoning_content" not in message
    assert tokens == 42


# 返回带假私密内容的 401，验证只调用一次且错误不包含原始响应 body。
async def test_glm_auth_failure_not_retried_and_sanitized(settings):
    requests = []

    # 模拟鉴权失败并夹带假私密文本，检查错误脱敏与不重试行为。
    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"message": "private-token-from-server"})

    model = GLMModel(settings.model_copy(update={"mode": "glm", "api_key": SecretStr("test")}))
    await model.client.aclose()
    model.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.base_url
    )
    try:
        with pytest.raises(ModelError) as error:
            await model.complete([], [])
    finally:
        await model.close()
    assert len(requests) == 1
    assert "private-token-from-server" not in str(error.value)


# 同键同请求返回同任务；同键不同请求、忙会话和缺失任务分别失败。
def test_store_idempotency_and_conflicts(settings):
    store = Store(settings.db_path)
    request = RunRequest(query="Agent", session_id="learning")
    first, new = store.create(request, "request-key")
    second, new2 = store.create(request, "request-key")
    assert new and not new2 and first.run_id == second.run_id
    with pytest.raises(Conflict):
        store.create(RunRequest(query="other"), "request-key")
    with pytest.raises(Conflict):
        store.create(request)
    with pytest.raises(KeyError):
        store.get("missing")


# 有上限地轮询异步任务，达到预期状态返回；超时明确使测试失败。
# await sleep 让出控制权，避免阻塞真正执行的后台 Task。
async def wait_for(manager, run_id, statuses):
    for _ in range(300):
        if manager.store.get(run_id).status in statuses:
            return manager.store.get(run_id)
        await asyncio.sleep(0.01)
    pytest.fail("task never reached expected state")


# 报告生成后先待审核，不进入成功历史；确认后才 completed 并产生两条对话。
async def test_manager_approval_and_session_history(settings):
    store = Store(settings.db_path)
    manager = RunManager(store, settings)
    view = await manager.create(
        RunRequest(query="Schema", session_id="learn", require_approval=True)
    )
    await wait_for(manager, view.run_id, {"awaiting_approval"})
    assert store.history("learn") == []
    manager.approve(view.run_id)
    assert len(store.history("learn")) == 2
    assert store.get(view.run_id).status == "completed"
    await manager.shutdown()


# 阻塞执行替身使状态可控，分别取消尚未调度和正在运行的任务。
# 取消之后仍回收后台协程，避免测试结束遗留任务。
async def test_cancel_queued_and_running(settings, monkeypatch):
    import my_agent.service as service

    entered = asyncio.Event()

    # 通知测试协程已开始执行，然后无限等待，便于精确控制取消时机。
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "run_native", blocked)
    manager = RunManager(Store(settings.db_path), settings)
    queued = await manager.create(RunRequest(query="Agent"))
    cancelled = await manager.cancel(queued.run_id)
    assert cancelled.status == "cancelled"
    running = await manager.create(RunRequest(query="Agent"))
    await entered.wait()
    assert manager.store.get(running.run_id).status == "running"
    await manager.cancel(running.run_id)
    assert manager.store.get(running.run_id).status == "cancelled"
    await manager.shutdown()


# 新 Store 读取同一临时数据库，遗留 running 标记 paused。
# 显式 resume 后完成且三轮不变，证明恢复不依赖旧对象内存。
async def test_restart_recovers_checkpoint_and_requires_explicit_resume(settings):
    store = Store(settings.db_path)
    view, _ = store.create(RunRequest(query="Schema"))
    store.transition(view.run_id, ("queued",), "running")
    saved = []

    # 提交数据库检查点后制造异常，模拟最后成功保存的位置。
    async def save_then_crash(state):
        store.save_checkpoint(view.run_id, state)
        saved.append(state)
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await run_native("Schema", settings, checkpoint=save_then_crash)
    reopened = Store(settings.db_path)
    reopened.recover()
    assert reopened.get(view.run_id).status == "paused"
    manager = RunManager(reopened, settings)
    await manager.resume(view.run_id)
    completed = await wait_for(manager, view.run_id, {"completed", "failed"})
    assert completed.status == "completed"
    assert completed.metrics["turns"] == 3
    await manager.shutdown()


# 把执行函数替换成长等待，设极短 run_timeout，验证 failed 和超时提示。
async def test_service_timeout(settings, monkeypatch):
    import my_agent.service as service

    # 故意等待超过任务超时，替代真实慢模型，避免测试付费。
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(service, "run_native", slow)
    manager = RunManager(Store(settings.db_path), settings.model_copy(update={"run_timeout": 0.01}))
    view = await manager.create(RunRequest(query="Agent"))
    final = await wait_for(manager, view.run_id, {"failed"})
    assert "超时" in final.error
    await manager.shutdown()


# 两次调用分别重建图和 SQLite 连接；同 thread 确认后报告/用量不变。
# 额外检查重复初始输入和不存在的 thread，避免恢复覆盖历史。
async def test_langgraph_persistent_review_resume(settings):
    first = await run_graph(settings, "graph-approve", "Schema", require_approval=True)
    assert first["interrupted"]
    # 每次调用均重建连接和图，因此恢复依靠 SQLite 而不是旧内存对象。
    second = await run_graph(settings, "graph-approve", decision=True)
    assert not second["interrupted"]
    assert second["state"]["status"] == "completed"
    assert first["state"]["report"] == second["state"]["report"]
    assert first["state"]["metrics"] == second["state"]["metrics"]
    with pytest.raises(ValueError):
        await run_graph(settings, "graph-approve", "duplicate")
    with pytest.raises(ValueError):
        await run_graph(settings, "missing", decision=True)


# 审核值 False 应得到 rejected，而不是以非空值误判为同意。
async def test_langgraph_reject(settings):
    await run_graph(settings, "graph-reject", "Schema", require_approval=True)
    result = await run_graph(settings, "graph-reject", decision=False)
    assert result["state"]["status"] == "rejected"


# 用相同六题分别验证 native/sdk，结果只代表规则管线，不代表模型质量。
@pytest.mark.parametrize("engine", ["native", "sdk"])
async def test_rule_evaluation(settings, engine):
    result = await evaluate(settings, engine)
    assert result["passed"] == result["total"] == 6


# TestClient 启动应用 lifespan，用假服务令牌验证 401/422/404/202。
# 继续验证待审核、SSE、确认、非法恢复和幂等重复任务。
def test_api_contract_auth_errors_and_validation(settings):
    secured = settings.model_copy(update={"service_token": SecretStr("service-test")})
    headers = {"Authorization": "Bearer service-test"}
    with TestClient(create_app(secured)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/runs", json={"query": "Schema"}).status_code == 401
        assert client.post("/runs", json={"query": ""}, headers=headers).status_code == 422
        assert client.get("/runs/missing", headers=headers).status_code == 404
        response = client.post(
            "/runs",
            json={"query": "Schema", "require_approval": True},
            headers={**headers, "Idempotency-Key": "api-key"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(300):
            view = client.get(f"/runs/{run_id}", headers=headers).json()
            if view["status"] == "awaiting_approval":
                break
            import time

            time.sleep(0.01)
        assert view["status"] == "awaiting_approval"
        event_stream = client.get(f"/runs/{run_id}/events", headers=headers)
        assert event_stream.status_code == 200
        assert "event: report_ready" in event_stream.text
        assert "event: awaiting_approval" in event_stream.text
        assert "service-test" not in event_stream.text
        assert (
            client.post(f"/runs/{run_id}/approve", headers=headers).json()["status"] == "completed"
        )
        assert client.post(f"/runs/{run_id}/resume", headers=headers).status_code == 409
        duplicate = client.post(
            "/runs",
            json={"query": "Schema", "require_approval": True},
            headers={**headers, "Idempotency-Key": "api-key"},
        )
        assert duplicate.json()["run_id"] == run_id
