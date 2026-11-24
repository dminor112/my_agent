"""命令行入口：为六个学习阶段提供统一的参数解析与运行方式。

同步入口 main 解析命令；模型、SDK、图和评测由内部异步 execute 调度。
按命令延迟导入对应模块，避免仅查看配置就加载所有框架或建立客户端。
原生模式的过程事件输出到 stderr，最终 JSON 输出到 stdout，便于分别重定向。
serve 固定本机地址且单 worker；config 仅展示是否配置密钥，不展示密钥内容。
"""

import argparse
import asyncio
import json
import sys
from uuid import uuid4

from .config import ROOT, load_settings


def main():
    """解析子命令并启动同步服务或异步一次性任务，CLI 参数保留原有行为。"""
    # 子命令分开暴露学习阶段；--mode 显式覆盖环境中的运行模式。
    parser = argparse.ArgumentParser(description="资料检索与总结 Agent 学习 Demo")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("native", "sdk", "eval", "graph"):
        item = sub.add_parser(command)
        item.add_argument("--mode", choices=["mock", "glm"])
        if command in ("native", "sdk"):
            item.add_argument("query")
        elif command == "eval":
            item.add_argument("--engine", choices=["native", "sdk"], default="native")
            item.add_argument("--output", default="reports/eval-latest.json")
        else:
            item.add_argument("--query")
            item.add_argument("--thread", default=None)
            item.add_argument("--approval", action="store_true")
            item.add_argument("--resume", choices=["yes", "no", "retry"])
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8003)
    serve.add_argument("--mode", choices=["mock", "glm"])
    sub.add_parser("config")
    args = parser.parse_args()
    settings = load_settings(getattr(args, "mode", None))
    if args.command == "config":
        # 只展示密钥是否配置，不展示 SecretStr 的真实值，适合排查而非导出秘密。
        print(
            json.dumps(
                {
                    "config_path": str(ROOT / ".env"),
                    "mode": settings.mode,
                    "model": settings.model,
                    "base_url": settings.base_url,
                    "api_key_configured": bool(settings.api_key.get_secret_value()),
                    "corpus_dir": str(settings.corpus_dir),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        # 保留本地 8003 默认端口；单 worker 对应当前 SQLite/内存调度约束。
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1)
        return

    async def execute():
        """根据子命令分发，一次任务只运行一种引擎，不隐式调用真实模型。"""
        if args.command == "native":
            from .native import run_native

            async def emit(kind, payload):
                """过程日志走 stderr；stdout 保持最终 JSON，便于脚本消费。"""
                print(json.dumps({"event": kind, **payload}, ensure_ascii=False), file=sys.stderr)

            async with asyncio.timeout(settings.run_timeout):
                report, state = await run_native(args.query, settings, emit=emit)
            return {
                "report": report.model_dump(),
                "metrics": {
                    "turns": state.turns,
                    "total_tokens": state.total_tokens,
                    "tool_calls": state.tool_calls,
                    "mode": settings.mode,
                },
            }
        if args.command == "sdk":
            from .sdk import run_sdk

            report, metrics = await run_sdk(args.query, settings)
            return {"report": report.model_dump(), "metrics": metrics}
        if args.command == "graph":
            from .graph import run_graph

            if args.resume and (not args.thread or args.query):
                raise ValueError("恢复需 --thread 且不能再次传 --query")
            if not args.resume and not args.query:
                raise ValueError("首次执行需 --query")
            # --resume retry 不是审核同意；它传 None，沿已有检查点继续/重试。
            decision = {"yes": True, "no": False, "retry": None}.get(args.resume)
            async with asyncio.timeout(settings.run_timeout):
                return await run_graph(
                    settings, args.thread or uuid4().hex, args.query, args.approval, decision
                )
        from .evaluation import evaluate

        result = await evaluate(settings, args.engine)
        # 解析真实路径再检查根目录，拒绝 ../ 或符号链接把评测产物写到仓库外。
        path = ROOT / args.output
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("评测产物必须保存在仓库内")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    try:
        result = asyncio.run(execute())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "eval" and result["passed"] != result["total"]:
            raise SystemExit(1)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        # 适配器已对已知模型错误脱敏；CLI 不打印任意 traceback 或原始返回数据。
        print(f"执行未完成（{type(exc).__name__}），请检查 my_agent/.env 和参数。", file=sys.stderr)
        raise SystemExit(1) from None
