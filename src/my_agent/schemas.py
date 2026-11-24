"""数据契约层：限制模型提议、工具参数、报告和 API 请求的结构。

Pydantic 同时负责运行时校验和生成 JSON Schema，但类型校验不等于业务授权。
例如 document_id 格式正确仍可能不存在，quote 长度正确仍可能是伪造原文；
这些需要 tools.py 的证据校验补上。所有 StrictModel 都拒绝未声明的额外字段。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    # 所有业务结构的公共基类：拒绝模型或调用者夹带未声明字段。

    model_config = ConfigDict(extra="forbid")


class SearchArgs(StrictModel):
    # 检索参数：限制问题长度和结果数量，防止上下文无限扩张。

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=3, ge=1, le=4)


class ReadArgs(StrictModel):
    # 读取资料仅接受逻辑 ID，不接受路径；是否存在由 Corpus.read 再校验。

    document_id: str = Field(min_length=1, max_length=100)


class Citation(StrictModel):
    # 引用包含文档身份、来源及精确摘录，不允许只给模型编写的来源名字。

    document_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    # quote 应是正文子串，不能是模型自行改写的“引文”。
    quote: str = Field(min_length=1, max_length=500)


class Report(StrictModel):
    # 提交给应用的报告契约：结构化内容、证据与不确定性同时保留。

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=6000)
    key_points: list[str] = Field(min_length=1, max_length=8)
    # 无证据时允许空列表；有证据时必须引用，由 validate_report 判断。
    citations: list[Citation] = Field(max_length=4)
    limitations: str = Field(min_length=1, max_length=2000)


class RunRequest(StrictModel):
    # 启动任务的 HTTP 请求，不允许调用者通过请求覆盖密钥或模型端点。

    query: str = Field(min_length=1, max_length=1000)
    # 同一 session 可累积成功对话；未指定时服务为该任务创建新会话。
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    # True 表示生成报告后等待人确认，而不是自动标记 completed。
    require_approval: bool = False


class RunView(StrictModel):
    # 对外任务视图：不包含内部检查点、幂等指纹或任何密钥。

    run_id: str
    session_id: str
    # Literal 限定状态集合，防止数据库异常值被当成正常任务状态。
    status: Literal[
        "queued", "running", "awaiting_approval", "completed", "failed", "cancelled", "paused"
    ]
    query: str
    # 执行中可以为 None；待审核时也可能已有 result，应结合 status 判断。
    result: Report | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    metrics: dict
