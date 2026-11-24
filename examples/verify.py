"""一键复现验证：运行测试、静态检查、依赖检查和本机 HTTP 冒烟。

应使用仓库虚拟环境执行；模型调用被 Mock 或拦截，不向外部服务发送真实请求。
各检查的退出码及输出写入机器可读报告；不修改正在运行的用户服务。
"""

import json
import subprocess
import sys
from pathlib import Path

from http_smoke import run_smoke

from my_agent.config import ROOT

if __name__ == "__main__":
    report = {"real_glm_called": False, "mode": "mock", "python": sys.version.split()[0]}
    # 用当前虚拟环境解释器与 Ruff，避免误用系统 Python 的依赖版本。
    commands = {
        "pytest": [sys.executable, "-m", "pytest", "-q"],
        "lint": [str(Path(sys.executable).parent / "ruff"), "check", "src", "tests", "examples"],
        "dependency_check": [sys.executable, "-m", "pip", "check"],
    }
    # 保留每个检查结果，不因为第一项失败就丢失其他检查信息。
    for name, command in commands.items():
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        report[name] = {
            "exit_code": result.returncode,
            "output": (result.stdout + result.stderr)[-10000:],
        }
    # 真 TCP 冒烟使用临时 Mock 子进程，不访问用户运行中的 GLM 服务。
    report["http_smoke"] = run_smoke()
    report["passed"] = all(report[key]["exit_code"] == 0 for key in commands)
    # 此脚本会更新统一验证报告；其余真实模型报告不会被覆盖。
    path = ROOT / "reports/verification.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
