# 交付验证记录

## 已验证

- Python 3.14 独立虚拟环境；全部依赖通过 `pip check`。
- 40 项自动化测试通过。
- 原生循环与真实 SDK Runner 的离线调用。
- GLM HTTP 请求契约：模型名、请求路径、Authorization、429 有限重试、401 不重试。
- SDK 的 GLM Chat Completions 适配器通过截获请求完成契约验证，不连接真实模型服务。
- 未知工具/非法 JSON 的错误处理、工具白名单、路径越界拒绝与引用匹配。
- 模型轮数、上下文和 token 预算限制；失败恢复不清零累计用量。
- 排队/运行取消、并发限制、超时、检查点保存和显式恢复。
- 会话历史、任务创建幂等、人工确认。
- LangGraph SQLite 检查点跨新连接恢复，确认与拒绝分支。
- 真实 localhost TCP/HTTP 冒烟验证：启动、健康检查、创建、查询、SSE、确认、取消、幂等、历史、鉴权和错误码。
- Native 和 SDK 各 6 项规则评测均通过。
- Ruff 静态检查通过。
- `.env`、`.venv`、任务数据库和运行报告已被排除；只发布源码、教学资料和本验证说明。

## 未验证与限制

- 自动回归测试均不调用真实 GLM。已通过用户配置在本地完成单条真实模型任务，但密钥、原始记录与运行报告不进入 GitHub；该单次成功不代表全部账号/模型能力均已验证。
- Mock 为固定策略，仅验证执行管线，不代表真实模型效果。
- 引用匹配不证明总结的语义正确性。
- 当前服务是单 worker、本地后端教学 Demo，不是生产多租户架构。
- 检索对象是本地 JSON 资料，不是互联网。
- 第三方 LangSmith 依赖在 Python 3.14 下产生 17 条 asyncio API 弃用警告，未导致失败；本项目未修改第三方库。

## 重现

```bash
cd my_agent
source .venv/bin/activate
python examples/verify.py
my-agent eval --mode mock --engine native --output reports/eval-native.json
my-agent eval --mode mock --engine sdk --output reports/eval-sdk.json
```

机器可读结果：`reports/verification.json`、`reports/eval-native.json`、`reports/eval-sdk.json`。
