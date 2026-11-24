"""模型适配层：统一真实 GLM 与离线 Mock 的消息返回形式。

complete(messages, tools) 返回二元组：模型提议字典、此次 token 用量。
模型只提议工具调用，不在这里执行工具；执行和校验由 native.py 负责。
Mock 采用固定的“检索 → 逐篇读取 → 提交”策略，便于可复现测试，
不是另一个语言模型，也不代表 GLM 的真实回答质量。
GLM 适配器通过 HTTPS Chat Completions 请求，限制重试并脱敏错误。
"""

import asyncio
import json
import random
from uuid import uuid4

import httpx

from .config import Settings


class ModelError(RuntimeError):
    """模型通信或协议失败；服务层只展示脱敏后的提示。"""

    pass


class BudgetExceeded(RuntimeError):
    """预算主动终止信号，与网络故障区分，不能靠恢复任务绕过。"""

    pass


def mock_proposal(messages: list[dict]) -> dict:
    """根据历史工具结果生成下一步固定提议，模拟可复现的模型接口。

    没检索过就提议 search；存在未读结果就提议 read；否则摘录正文提交报告。
    返回的 arguments 是 JSON 字符串，刻意与真实模型的函数调用协议一致。
    无法做语言理解或语义评测，适合测试循环、状态和错误路径。
    """
    # 取最近一条用户消息；测试中无需跨会话语义推理。
    query = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "Agent")
    outputs = []
    for msg in messages:
        if msg["role"] == "tool":
            try:
                outputs.append(json.loads(msg["content"]))
            except (ValueError, TypeError):
                continue
    # 只依据回填的工具输出推进，不绕过工具直接访问资料库。
    searches = [o for o in outputs if isinstance(o, dict) and "matches" in o]
    documents = [o["document"] for o in outputs if isinstance(o, dict) and "document" in o]
    if not searches:
        name, args = "search_documents", {"query": query, "limit": 3}
    else:
        read_ids = {d["document_id"] for d in documents}
        unread = [d for d in searches[-1]["matches"] if d["document_id"] not in read_ids]
        if unread:
            name, args = "read_document", {"document_id": unread[0]["document_id"]}
        else:
            name = "submit_report"
            args = {
                "title": "资料检索与总结（Mock）",
                "summary": "\n".join(d["content"][:180] for d in documents)
                or "没有找到支持该问题的资料，无法据此作答。",
                "key_points": [d["content"].split("\n")[0] for d in documents]
                or ["当前本地资料库未检索到相关证据。"],
                "citations": [
                    {
                        "document_id": d["document_id"],
                        "title": d["title"],
                        "url": d["url"],
                        "quote": d["content"][:100],
                    }
                    for d in documents
                ],
                "limitations": "仅检索本地学习资料。Mock 是固定策略和摘录，不代表真实模型能力。",
            }
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_" + uuid4().hex,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


class MockModel:
    """无网络模型替身：满足原生循环的异步接口，固定每轮测试用量为 100。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, int]:
        """返回固定策略提议并让出事件循环，使取消测试能在调度点生效。"""
        await asyncio.sleep(0)  # 主动让出控制权，不是网络等待或真实模型耗时。
        return mock_proposal(messages), 100

    async def close(self) -> None:
        """保持与真实适配器相同的清理接口；Mock 没有网络连接可释放。"""
        pass


class GLMModel:
    """GLM 的 Chat Completions HTTP 适配器，不依赖 Agent 框架。"""

    def __init__(self, settings: Settings):
        """校验配置并复用一个异步连接池；密钥只放在 Authorization 请求头。"""
        settings.ensure_model_ready()
        self.settings = settings
        # 第一阶段故意直接使用 HTTP，便于看清消息和函数工具的协议边界。
        # 禁止自动重定向，避免凭据随跳转被发送到另一个站点。
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=settings.request_timeout,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
        )

    async def complete(self, messages: list[dict], tools: list[dict]) -> tuple[dict, int]:
        """请求一次逻辑模型提议，对暂态网络/服务错误做有上限的重试。

        max_retries=2 表示首次请求外最多再试 2 次，不是无限轮询。
        401/参数错误等非暂态响应直接失败；错误不附带服务商原始 body。
        返回只保留循环需要的字段，预算由调用方在响应后累计。
        """
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self.client.post(
                    "chat/completions",
                    json={
                        "model": self.settings.model,
                        "messages": messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "temperature": 0.3,
                        "max_tokens": 2000,
                    },
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.settings.max_retries:
                    raise ModelError("模型请求超时或网络失败；检查连接与配置") from exc
            else:
                if response.is_success:
                    try:
                        body = response.json()
                        message = body["choices"][0]["message"]
                        # 只保留行动/回答字段，不将 reasoning_content 或其他服务商字段持久化。
                        output = {
                            k: message[k] for k in ("role", "content", "tool_calls") if k in message
                        }
                        usage = body.get("usage", {}).get("total_tokens", 0)
                        if not isinstance(usage, int) or usage < 0 or usage == 0:
                            # 服务商没给用量时用序列化字符数估算，避免预算完全失效。
                            # 这是近似值，不是精确 tokenizer 或账单金额。
                            usage = len(json.dumps(messages)) + len(json.dumps(output))
                        return output, usage
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        raise ModelError("模型返回格式异常") from exc
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise ModelError(
                        f"模型接口 HTTP {response.status_code}；检查 Key、模型权限和 BASE_URL"
                    )
                if attempt == self.settings.max_retries:
                    raise ModelError(f"模型暂不可用 HTTP {response.status_code}，有限重试已耗尽")
            # 指数退避叠加少量随机抖动，减少多个任务同时重试造成的请求尖峰。
            # 这里只重试模型通信，不负责重放有外部副作用的写工具。
            await asyncio.sleep(min(0.2 * 2**attempt + random.uniform(0, 0.1), 2))
        raise ModelError("模型请求失败")

    async def close(self) -> None:
        """关闭连接池；原生循环只会释放自己创建的适配器。"""
        await self.client.aclose()


def make_model(settings: Settings):
    """按明确配置创建模型；glm 配置失败会抛错，不会隐式切回 Mock。"""
    return MockModel() if settings.mode == "mock" else GLMModel(settings)
