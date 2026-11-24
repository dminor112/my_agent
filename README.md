# my_agent：资料检索与总结 Agent 学习仓库

Python 聚合仓库，按「原生循环 → SDK → 服务化 → 可靠性 → 评测 → 图编排」学习。
这是可运行的本地后端 Demo，不是生产部署模板。所有阶段复用同一套本地资料与引用契约。

仓库： https://github.com/dminor112/my_agent

## 1. 配置 GLM

私有配置位于项目根目录 `.env`，从仓库克隆后先复制模板：

```bash
cp .env.example .env
```

然后修改以下字段：

```dotenv
AGENT_MODE=glm
GLM_API_KEY=填写你自己的密钥
GLM_MODEL=glm-5.3-flash
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

- `.env.example` 默认 `mock` 且 Key 为空；真实 `.env` 被 `.gitignore` 排除，不会随仓库发布。
- `GLM_BASE_URL` 可按你的套餐/网关更改；它应是 Chat Completions 基础地址，不含 `/chat/completions`。
- `glm` 模式缺 Key 会报错，不会偷偷回退到 Mock。真实调用可能收费。
- 环境变量优先于 `.env`。修改 `.env` 后重启服务。
- 仓库不包含密钥；离线测试不调用真实 GLM。真实接入需自行配置，费用/权限按你的账号确定。
- 原生接入使用 HTTP；SDK 接入显式使用 `OpenAIChatCompletionsModel`，而不是 Responses API。

参考官方兼容接口与 SDK 第三方模型文档（账号权限仍需你配置后验证）：
https://docs.bigmodel.cn/cn/guide/develop/openai/introduction
https://openai.github.io/openai-agents-python/models/

## 2. 第一次运行

首次从 GitHub 获取项目（Python 3.11+；已在 Python 3.14 上验证）：

```bash
git clone https://github.com/dminor112/my_agent.git
cd my_agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e '.[dev]'
cp .env.example .env
my-agent config
my-agent native --mode mock 'Agent 循环和工作流有什么区别？'
my-agent sdk --mode mock '工具 Schema 如何设计？'
```

配置真实 Key 后：

```bash
my-agent native --mode glm '工具设计的 Schema 与引用来源如何校验？'
my-agent sdk --mode glm '可靠性、检查点和幂等有哪些要点？'
```

重新安装或在其他机器运行（Python 3.11+；本次验证环境为 Python 3.14）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e '.[dev]'
```

## 3. 七个阶段

### 01 原生 Agent Loop

入口：`examples/01_native_loop.py`；核心：`src/my_agent/native.py`、`model.py`、`tools.py`。

```bash
python examples/01_native_loop.py --mode mock
```

模型提议工具 → Pydantic 校验 → 程序执行 → 结果回填 → 继续/停止。
没有 Agent 框架参与循环。日志输出在 stderr，最终 JSON 在 stdout。
阅读时尝试：把 `MAX_TURNS` 改成 1，观察有界失败；给工具增加 Schema 字段，再修改 Mock。

### 02 用 SDK 重写

入口：`examples/02_sdk_agent.py`；核心：`src/my_agent/sdk.py`。

```bash
python examples/02_sdk_agent.py --mode mock
```

真正运行 OpenAI Agents SDK 的 `Runner` 与 `function_tool`。
Mock 模式替换的是 SDK 的模型接口，不是绕过 SDK 循环。
报告以 `submit_report(report)` 工具提交，避免依赖服务商未必支持的严格 JSON 输出模式。
SDK 内部推断函数 Schema，应用仍校验引用证据。默认关闭向其他模型平台上传 tracing。

### 03 FastAPI 服务化

```bash
my-agent serve --mode mock
# 浏览器打开 http://127.0.0.1:8003/docs 可查看交互式 API 文档
# 另一个终端执行端到端客户端
python examples/03_api_client.py
```

服务接口：

- `POST /runs`：启动任务；支持 `Idempotency-Key`。
- `GET /runs/{run_id}`：查询状态/报告/用量。
- `GET /runs/{run_id}/events`：SSE 任务事件流，可通过 `after` 或 `Last-Event-ID` 续接。
- `POST /runs/{run_id}/cancel`：取消。
- `POST /runs/{run_id}/resume`：恢复暂停/失败任务。
- `POST /runs/{run_id}/approve`：确认待审核报告。
- `GET /sessions/{session_id}`：读取该会话最近三次成功任务的历史。

SSE 是**任务事件流**，不是逐 token 输出或隐藏思维链。
服务采用原生循环，使状态生命周期容易阅读；SDK 和图阶段是独立对照示例，不支持通过 HTTP 选择引擎。

### 04 可靠性与人工介入

```bash
python examples/04_checkpoint_resume.py
```

该脚本模拟模型提出工具后进程异常，保存检查点，在新 Store/Manager 中恢复。
检查点同时保存消息、待执行工具、证据、轮数和 token 用量。
关闭/重启服务后未完成任务标记为 `paused`，需显式 `POST /resume`，不会自动重放未知操作。
人工确认演示为报告提交前的审核，不会发送邮件或写入外部系统。

### 05 测试与评测

```bash
pytest -q
ruff check src tests examples
my-agent eval --mode mock --engine native --output reports/eval-native.json
my-agent eval --mode mock --engine sdk --output reports/eval-sdk.json
```

评测集在 `data/evals/cases.json`，覆盖基础问题、引用与无结果。
规则评测验证格式、必要来源、无结果行为和 limitations；不证明语义正确性。
Mock 使用固定策略和摘录，100% 通过只代表工程管线，不代表 GLM 的真实能力。
配置 Key 后可手动用 `--mode glm` 运行同一组评测，需支付模型调用费用。

### 06 LangGraph 编排

```bash
my-agent graph --mode mock --query 'Schema' --thread lesson-review --approval
# 用相同 thread 在另一个进程恢复，不重新运行已完成的总结节点
my-agent graph --mode mock --thread lesson-review --resume yes
# 拒绝使用 --resume no；错误重试使用 --resume retry
```

核心：`src/my_agent/graph.py`。
路径为 plan → retrieve → summarize → review → finish。
plan 和 retrieve 是明确的代码逻辑；summarize 才调用模型。不要误认为每个节点都是 Agent。
`AsyncSqliteSaver` 持久化于 `data/graph.sqlite3`，人工确认使用 `interrupt`/`Command`。
先掌握这些，再自行扩展并行检索或多 Agent；此仓库不预先增加无必要的多 Agent 架构。

### 07 并行只读工具与延迟治理

```bash
python examples/07_parallel_tools.py
```

核心：`src/my_agent/native.py` 中的 `take_batch`、`prepare_batch`、`execute_job`。
同一轮的只读工具可以并行，`submit_report` 仍独占一批；每个工具有独立超时。
示例给读取加人为等待，串行约 0.45s、并行约 0.15s，说明加速来自等待重叠而非计算变快。
三个必须显式设计的点：回填顺序按调用而不按完成、失败只影响自己那条、读取名额在并发前预留。
同步工具经线程池执行；换掉 `max_parallel_tools=1` 即可退回旧行为做对照。

## 4. 放入自己的资料

在 `data/corpus/` 新增 UTF-8 `.json`，结构为：

```json
{
  "document_id": "my-note",
  "title": "我的资料标题",
  "url": "local://corpus/my-note",
  "content": "待检索的正文。"
}
```

内置 4 篇资料是作者编写的教学笔记，不是抓取的互联网正文。检索是英文单词/中文二元词的交集评分，**不是向量检索**。
不提供任意 URL 抓取、联网搜索、PDF 解析、Shell 工具；这样无需第二个 API Key，且避免 SSRF 与任意文件访问。
更换资料后重启服务。接入 GLM 时，实际读取的资料和最近会话内容会发送给你的模型端点；不要放入未经授权的敏感资料。

## 5. 目录与下一步

```text
my_agent/
  .env.example           # 可分享的配置模板；.env 是私有配置
  pyproject.toml         # 包与开发配置
  requirements.lock.txt  # 此次验证的精确依赖版本
  src/my_agent/          # 模型、工具、执行、存储、服务、SDK、图、评测
  examples/              # 01–07 学习入口
  tests/                 # 全离线自动化测试
  data/corpus/           # 演示资料库
  data/evals/            # 评测题集
  docs/LEARNING_GUIDE.md  # 设计原理、阅读顺序与练习
  reports/               # 测试与评测结果
```

建议从 `native.py` 开始，只读一条成功路径，然后跑失败与恢复实验。
详细安全边界与尚未实现的生产能力见 `docs/LEARNING_GUIDE.md`。

## 6. VS Code 断点调试

已提供 `.vscode/launch.json`，保留“当前文件”配置，并新增原生/SDK 的 Mock 与 GLM 启动项。
打开 `my_agent` 文件夹，安装 Python 和 Python Debugger 扩展，选择 **01 原生 Agent（Mock，推荐先用）** 后按 F5。
服务调试使用 8004 端口及独立数据库，不影响常规 8003 服务。
详细操作、断点位置和超时说明见 `docs/DEBUGGING.md`。
