"""配置层：把环境变量和私有 .env 转成经过校验的 Settings 对象。

配置优先级为命令行 mode > 进程环境变量 > .env > 字段默认值。
这里只定义如何读取配置，导入模块不会读取密钥、建立网络连接或启动服务。
模型密钥与服务鉴权令牌使用 SecretStr，避免 repr/日志意外展示明文；
真正发起鉴权请求时才显式调用 get_secret_value()。
"""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr

# 根据源码位置定位仓库根目录，不依赖终端当前目录。
# 因此从 examples 或其他目录启动，仍会读取同一个 my_agent/.env。
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    """一轮 Agent 运行所需的配置快照，数值范围由 Pydantic 提前校验。"""

    # mock 不联网；glm 使用用户选择的服务商模型。
    mode: Literal["mock", "glm"] = "mock"
    # 不要将 get_secret_value() 的结果写入日志、报告或 URL。
    api_key: SecretStr = SecretStr("")
    model: str = "glm-5.3-flash"
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    # 请求超时约束一次模型提议；run_timeout 约束整个任务运行。
    request_timeout: float = Field(default=30, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    # 轮数/token/字符数是三个不同预算，恢复任务时不会清零。
    max_turns: int = Field(default=10, ge=1, le=30)
    max_total_tokens: int = Field(default=30000, ge=100, le=1000000)
    max_context_chars: int = Field(default=60000, ge=1000, le=500000)
    run_timeout: float = Field(default=120, gt=0, le=1800)
    max_concurrent_runs: int = Field(default=2, ge=1, le=8)
    # 服务令牌保护本地任务接口，与 GLM_API_KEY 的用途不同。
    service_token: SecretStr = SecretStr("")
    corpus_dir: Path = ROOT / "data/corpus"
    db_path: Path = ROOT / "data/agent.sqlite3"

    def ensure_model_ready(self) -> None:
        """发请求前检查密钥与端点，配置错误应尽早失败而不是悄悄改用 Mock。

        URL 禁止嵌入用户名/密码、查询参数和片段，减少密钥混入 URL 的风险。
        此处不测试网络或账号权限；通过校验仅代表配置形式正确。
        """
        if self.mode == "glm" and not self.api_key.get_secret_value().strip():
            raise ValueError(f"请在 {ROOT / '.env'} 配置 GLM_API_KEY 并设置 AGENT_MODE=glm")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GLM_BASE_URL 必须是无凭据、无 query 的 HTTPS 地址")


def load_settings(mode: str | None = None) -> Settings:
    """读取配置并解析相对路径，返回独立对象而不污染 os.environ。

    mode 是 CLI 的可选覆盖值；None 表示沿用环境/.env 设置。
    dotenv_values 读取键值但不注入进程；字典右侧的环境变量覆盖左侧文件值。
    Settings 最终负责把字符串数值转换成 int/float 并校验合法范围。
    """
    # 环境变量覆盖 .env；不会修改进程环境，也不会打印秘密。
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    names = {
        "mode": "AGENT_MODE",
        "api_key": "GLM_API_KEY",
        "model": "GLM_MODEL",
        "base_url": "GLM_BASE_URL",
        "request_timeout": "REQUEST_TIMEOUT_SECONDS",
        "max_retries": "MAX_RETRIES",
        "max_turns": "MAX_TURNS",
        "max_total_tokens": "MAX_TOTAL_TOKENS",
        "max_context_chars": "MAX_CONTEXT_CHARS",
        "run_timeout": "RUN_TIMEOUT_SECONDS",
        "max_concurrent_runs": "MAX_CONCURRENT_RUNS",
        "service_token": "AGENT_SERVICE_TOKEN",
        "corpus_dir": "CORPUS_DIR",
        "db_path": "AGENT_DB_PATH",
    }
    kwargs = {key: values[name] for key, name in names.items() if values.get(name)}
    for key in ("corpus_dir", "db_path"):
        if key in kwargs:
            path = Path(kwargs[key])
            kwargs[key] = path if path.is_absolute() else ROOT / path
    if mode:
        kwargs["mode"] = mode
    return Settings(**kwargs)
