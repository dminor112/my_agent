# 项目协作约定

## 仓库与工作范围

- 此项目的唯一协作远端为 `https://github.com/dminor112/my_agent`。
- 后续功能开发、修复、学习说明和调试配置均在本仓库中修改，不另建同名工程。
- 提交前检查远端更新及工作区差异，保留用户已有代码；不强制推送或覆盖远端历史。
- 用户要求完成的后续改动在验证后提交并同步到此远端；网络/认证失败须如实说明未同步。

## 技术与学习目标

- Python 后端学习 Demo：原生 Agent Loop、OpenAI Agents SDK、FastAPI、SQLite、评测、LangGraph。
- 使用 `glm-5.3-flash`，模型端点与密钥由 `.env` 配置，不擅自改变模型。
- 自有代码保留详细中文注释，解释数据契约、反馈循环、恢复、预算及安全边界。
- 常规服务端口 8003；VS Code 调试使用 8004 与独立 debug 数据库。
- 包内文件通过 `python -m my_agent` 或项目 CLI 运行，不直接启动 `src/my_agent/native.py`。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests examples
.venv/bin/python examples/http_smoke.py
```

- 自动验证只使用 Mock 或截获请求，不隐式调用真实 GLM 或产生模型费用。
- 模型提示词、SDK 工具 docstring 和 Pydantic 类文档可能影响工具 Schema；纯注释任务不得改变它们。

## 提交安全

- 不提交 `.env`、密钥、服务令牌、虚拟环境、数据库、运行日志和真实模型报告。
- `.env.example` 必须保持密钥/服务令牌为空。
- 提交前检查 staged 文件，不将测试中的假凭据误当真实密钥，但也不能凭扫描结果保证所有内容安全。
- 本地运行数据保留原地；忽略规则只控制 Git，不删除用户文件。
