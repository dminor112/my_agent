"""第三阶段客户端：连接已启动的本地服务，观察 SSE 任务事件与报告。

客户端不启动服务器、不直接调用模型；服务的运行模式决定是否产生费用。
require_approval=True 会停在人工审核状态，脚本不会代替用户自动确认。
"""

import argparse
import asyncio
import json

import httpx

from my_agent.config import load_settings


# 连接本地服务，读取可选服务令牌仅用于 HTTP 鉴权头，不打印令牌。
# 依次创建任务、消费事件、查询报告；人工审核留给用户操作。
async def demo(base_url: str):
    token = load_settings().service_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=150) as client:
        response = await client.post(
            "/runs",
            json={
                "query": "可靠性、持久化和幂等有哪些要点？",
                "require_approval": True,
            },
        )
        response.raise_for_status()
        # 202 只表示已接受任务，不表示总结已完成。
        run_id = response.json()["run_id"]
        # SSE 只读取任务事件，连接结束不等于自动批准报告。
        async with client.stream("GET", f"/runs/{run_id}/events") as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    print(line)
        # 流结束后再查询最终状态；失败或待审核也会关闭当前观察流。
        result = await client.get(f"/runs/{run_id}")
        result.raise_for_status()
        print(json.dumps(result.json(), ensure_ascii=False, indent=2))
        if result.json()["status"] == "awaiting_approval":
            print("上述报告等待审核；本客户端不会自动代表你确认。")
            print(f"确认：POST {base_url}/runs/{run_id}/approve；拒绝：POST /cancel")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    args = parser.parse_args()
    asyncio.run(demo(args.base_url))
