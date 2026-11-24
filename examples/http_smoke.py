"""真实本机 HTTP 冒烟验证：启动临时服务并且仅回收自己创建的子进程。

强制 Mock、独立数据库和临时服务令牌，不读取真实任务数据库、不产生模型费用。
与 TestClient 不同，这里通过真实 TCP/HTTP 验证请求、SSE、审核及鉴权全链路。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from my_agent.config import ROOT


# 申请临时本机端口并启动自己的 Mock 子进程，使用真实 HTTP 检查全链路。
# finally 无论成功失败都回收此进程；不会终止用户的 8003 服务。
def run_smoke() -> dict:
    # 端口 0 让系统挑选空闲端口，不占用用户正在用的 8003 服务。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="my-agent-http-") as temporary:
        # 显式覆盖进程环境，保证即使 .env 已切 glm 也只运行隔离 Mock。
        env = {
            **os.environ,
            "AGENT_MODE": "mock",
            "AGENT_SERVICE_TOKEN": "smoke-local-token",
            "AGENT_DB_PATH": str(Path(temporary) / "agent.sqlite3"),
        }
        # 保存创建的进程句柄，清理时只操作它，不按名称杀其他进程。
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from my_agent.cli import main; main()",
                "serve",
                "--mode",
                "mock",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        checks = {}
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=10,
                headers={"Authorization": "Bearer smoke-local-token"},
            ) as client:
                # 启动需要时间，有限轮询健康检查，子进程提前退出时立即失败。
                for _ in range(100):
                    try:
                        ready = client.get("/health")
                        if ready.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if proc.poll() is not None:
                        raise RuntimeError("local server exited before startup")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("local server startup timed out")
                checks["health"] = ready.json()["mode"] == "mock"
                # 测试库中请求人工审核，观察报告生成和审核完成是两种状态。
                request = {"query": "Schema", "session_id": "smoke", "require_approval": True}
                started = client.post("/runs", json=request, headers={"Idempotency-Key": "one"})
                started.raise_for_status()
                run_id = started.json()["run_id"]
                checks["create_202"] = started.status_code == 202
                for _ in range(100):
                    view = client.get(f"/runs/{run_id}").json()
                    if view["status"] in {"awaiting_approval", "failed"}:
                        break
                    time.sleep(0.01)
                checks["awaiting_approval"] = view["status"] == "awaiting_approval"
                checks["source"] = view["result"]["citations"][0]["document_id"] == "tool-design"
                # 用真实 HTTP 检查 SSE 的文本协议，并验证报告/审核事件存在。
                stream = client.get(f"/runs/{run_id}/events")
                checks["sse"] = (
                    "event: report_ready" in stream.text
                    and "event: awaiting_approval" in stream.text
                )
                approved = client.post(f"/runs/{run_id}/approve")
                checks["approve"] = approved.json()["status"] == "completed"
                # 同一幂等键的重复提交必须返回同任务，不再次运行模型。
                duplicate = client.post("/runs", json=request, headers={"Idempotency-Key": "one"})
                checks["idempotency"] = duplicate.json()["run_id"] == run_id
                checks["history"] = len(client.get("/sessions/smoke").json()["messages"]) == 2
                reject = client.post("/runs", json={"query": "Schema", "require_approval": True})
                reject_id = reject.json()["run_id"]
                client.get(f"/runs/{reject_id}/events")
                checks["cancel"] = (
                    client.post(f"/runs/{reject_id}/cancel").json()["status"] == "cancelled"
                )
                checks["missing_404"] = client.get("/runs/missing").status_code == 404
                checks["invalid_422"] = client.post("/runs", json={"query": ""}).status_code == 422
                checks["protected_401"] = (
                    client.get(
                        "/runs/missing", headers={"Authorization": "Bearer wrong"}
                    ).status_code
                    == 401
                )
        # 无论断言/请求成功与否，都关闭仅属于本脚本的临时服务。
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    result = {
        "mode": "mock",
        "transport": "real localhost TCP/HTTP",
        "checks": checks,
        "passed": all(checks.values()),
        "real_glm_called": False,
    }
    if not result["passed"]:
        raise RuntimeError(json.dumps(result))
    return result


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
