# 学习指南：从一个循环到可靠的 Agent 后端

## 项目目标与范围

学习目标：能解释一次请求怎样从模型提议走到真实工具执行，能调试错误路径，能恢复任务，能说清评测能证明什么。
交付范围：Python 后端、本地资料检索、原生循环/SDK/图对照、服务接口、SQLite、人工确认、全离线测试。
非目标：生产多租户服务、互联网搜索、自动执行代码、付费搜索 API、前端聊天页面、多 Agent 团队。
用户模型设置为 `glm-5.3-flash`，不擅自换名；模型可用性和权限需配置 Key 后联调。

## 建议阅读顺序

1. `schemas.py`：模型输入、工具参数和报告契约。
2. `tools.py`：工具白名单、资料边界、引用校验。
3. `model.py`：HTTP 适配器、暂态错误重试、Mock 的边界。
4. `native.py`：唯一的核心 while 循环。
5. `sdk.py`：相同业务工具如何映射到 SDK；谁管理循环？
6. `store.py`、`service.py`：执行状态和对话历史怎样分开？
7. `api.py`：HTTP 生命周期怎样映射到任务生命周期？
8. `evaluation.py`、`tests/test_agent.py`：如何把“好像可以”变成可复现结论？
9. `graph.py`：何时把固定代码路径与动态模型逻辑混合？

## 阶段一：Agent 是反馈循环，不是一个巨型 Prompt

每一轮都有四个边界：模型输出 → 工具参数校验 → 工具执行 → 证据回填。
工具结果使用同一 `tool_call_id` 与模型提议关联。报告只有通过 submit_report 和引用校验才被应用接受。
自然语言回答不是应用状态；模型说“已完成”不等于执行成功。
原生循环没有 `eval`、子进程或模型提供的任意路径。它只调用显式登记的工具。

练习：
- 给搜索工具增加 `limit` 范围外输入，检查错误怎样返回模型。
- 手动伪造不存在的引用，观察应用拒绝报告，而不是直接展示。
- 增加一个只读工具，同时更新 Mock 与测试。

## 阶段二：框架代替的是哪部分代码？

SDK 管理消息协议、调用循环、函数工具 Schema 与停止条件。
应用仍负责资料权限、业务校验、生命周期和语义质量。
GLM 使用 Chat Completions 模型适配器，submit_report 是函数工具，不要求原生严格 JSON Schema 输出。
`strict_mode=False` 是服务商兼容性选择，不是取消业务校验；Pydantic 和 validate_report 仍执行。
MockSDKModel 实现 SDK Model 接口，因此测试真正经过 Runner。Mock 的固定检索策略不是模型智能。

练习：比较原生/SDK 同一题的模型调用数、工具数和引用结果；把 MAX_TOTAL_TOKENS 降低观察终止。

## 阶段三：三种不同粒度

- 用户轮次：用户发一条请求。
- Run：为完成该请求启动的一次逻辑任务。
- 模型调用：一个 Run 内的多个反馈循环。

服务层创建 run_id，SDK 或原生循环才进行模型调用。不能把 HTTP 连接存活时间当作任务存活时间。
客户端断开 SSE 不代表取消任务，取消需显式接口。SSE 可重新连接并从序号续读。
API 文档只展示接口，不是产品前端。

任务状态：queued → running → completed / awaiting_approval / failed。
人工确认：awaiting_approval → completed。取消：活跃任务 → cancelled。
关闭/异常重启：queued/running → paused；resume：paused/failed → queued。
completed 和 cancelled 不可恢复；failed 的累计轮数/token 预算不被恢复操作清零。

## 阶段四：执行状态、上下文、记忆不能混为一谈

- 执行状态：消息、待执行工具、证据、轮数、累计 token、已验证报告。
- 对话历史：同一会话最近三次成功任务的 user/assistant 消息。
- 长期记忆：本 Demo 未实现；不要把 SQLite 聊天记录称为用户画像记忆。

原生循环在模型提议后、每个工具执行后保存整体检查点。恢复时优先处理 pending，而不是再次请求模型。
SQLite 保存是事务式；外部模型请求和本地事务之间不存在分布式事务。
因此，模型响应已到达但尚未保存就崩溃，恢复可能重复模型调用与费用。token 预算不能覆盖这段未知用量。
工具均为本地只读，故重放不会重复外部写操作。增加邮件、付款或数据库更新时，必须另加业务幂等与授权。
Idempotency-Key 只避免重复创建逻辑任务，**不**保证任意外部副作用 exactly-once。

人工审核演示为确认报告，不发布到任何外部系统。

练习：运行 04_checkpoint_resume.py；把中断点改到工具执行前后，对比持久化内容。

## 阶段五：测试证明工程行为，评测衡量样本效果

本项目包含：
- 工具测试：Schema、范围、路径拒绝、原文引用一致性。
- 原生循环测试：反馈、未知工具错误恢复、轮数/context/token 预算。
- 模型契约测试：MockTransport 验证 GLM 请求路径、模型名、鉴权、429 重试和 401 不重试。
- SDK 测试：Runner 与工具真正运行，模型替换为 Mock。
- 服务测试：创建、状态、人工确认、取消、超时、重启恢复、幂等和鉴权。
- 图测试：SQLite 检查点跨连接恢复，确认/拒绝分支。
- 规则评测：必要来源、格式、无结果行为。

引用校验只证明摘录存在且来源真实，**不证明每个总结观点被原文蕴涵**。真实质量需人工核验或经过校准的评价器。
真实模型不是确定性函数；应重复采样并观察分布，而不是只看一次成功。

练习：添加一条对抗资料，确保不能诱导新工具执行；随后用真实 GLM 检查模型是否正确忽略恶意指令。

## 阶段六：不要把所有节点都叫 Agent

图编排固定执行 plan/retrieve，然后 summarize 中调用模型。节点是功能步骤，不自动等于自主 Agent。
review 用 interrupt 暂停；恢复使用同一 thread_id + Command。SQLite 保存使跨进程恢复成为可能。
review 前 summarize 已完成的检查点不需再次调用模型；summarize 内部尚未完成就崩溃仍可能重跑该节点。
参考 durable execution 文档，节点不是从任意代码行恢复，应把副作用拆成任务并设计幂等。

后续扩展方向：查询拆分 → 并行检索 → 汇总。仅当单 Agent 的工具负担或职责冲突有可测问题时，再引入多 Agent。
阶段七已覆盖“同轮并行执行”，尚未做的是“把一个问题拆成多个检索词”，那是规划能力，不是调度能力。

## 阶段七：并行只缩短等待，不缩短思考

同一轮里模型可能一次提出多个只读调用。串行执行时总耗时接近各次 I/O 之和；
受限并行后，只有重叠的部分被省掉。`MAX_PARALLEL_TOOLS` 默认 3，设为 1 即退回旧行为。

三项必须在代码里显式设计，不能指望模型自己处理：
- **回填顺序**：`tool` 消息按模型调用顺序写回，而不是按完成先后；协议靠 `call_id` 对齐。
- **错误隔离**：单个工具失败或超时，只产生一条 error 输出，同批其他结果照常回填。
- **名额预留**：像“最多读 4 篇”这种全局约束要在并发之前按顺序占号，
  否则两个并发读取会同时看到“还剩一格”而一起通过。

`submit_report` 永远独占一批：它改变应用最终状态，不能和仍在改证据的调用混在一起。
同步函数要放进线程池才谈得上并行；纯 CPU 的 Python 处理仍受 GIL 限制，加速有限。

代价是批内检查点粒度变粗：批中途崩溃，恢复会重跑整批只读工具（幂等无害）。
换成写操作就必须补业务幂等与授权，这条结论和阶段四一致。

练习：
- 分别用 `MAX_PARALLEL_TOOLS=1` 和 `3` 跑 `examples/07_parallel_tools.py`，比较耗时与 `tool_batch_finished` 的 `duration_ms`。
- 给某个工具注入 1 秒延迟，把 `TOOL_TIMEOUT_SECONDS` 调到 0.1，观察它超时后其他结果是否照常回填。
- 把读取上限改成 8 并一次请求六篇，确认重复资料不会重复占号。

## 安全与生产边界

已经实现：
- `.env` 不入 Git；SecretStr 隐藏对象展示中的 Key。
- 模型错误不返回原始响应 body；事件不含 Key 或隐藏推理。
- HTTPS 模型端点；原生 HTTP 不跟随重定向。
- 不接受任意 URL 抓取、任意文件路径或代码执行；资料 symlink 越界拒绝。
- 参数长度/范围/额外字段校验；SQL 参数化；引用匹配。
- 有限重试、任务超时、并发上限、累计轮数/token/context 字符预算。
- 可选 Bearer Token；服务入口固定监听 127.0.0.1。

仍是教学简化：
- 单进程单 worker；没有 Redis/队列、分布式租约、多租户 ACL。不要用多个 Uvicorn worker。
- 并行只发生在同一轮的只读工具之间；没有跨任务调度、写入并行或分布式限流。
- SQLite DB 是本地明文任务内容；自行控制文件权限和敏感资料范围。
- 设置 AGENT_SERVICE_TOKEN 后所有任务接口受保护，但没有逐用户授权。
- 提示词要求把资料当数据，但不能保证真实模型绝不会受提示注入影响；主要依赖工具能力边界。
- SDK 重试策略取决于 OpenAI Client，原生适配器只对列出的暂态错误重试。
- 预算在每次响应后计数，可能因一次响应超出阈值；不是预付费硬额度控制。
- 无自动 context compaction、向量数据库、web search、token 流、后台调度或生产部署。

上线前增加：业务鉴权/权限隔离、TLS、速率限制、持久任务队列、幂等写工具、Secrets 管理、审计与语义评测。

## 参考阅读

- https://www.anthropic.com/engineering/building-effective-agents
- https://openai.github.io/openai-agents-python/running_agents/
- https://openai.github.io/openai-agents-python/models/
- https://docs.langchain.com/oss/python/langgraph/durable-execution
- https://docs.bigmodel.cn/cn/guide/develop/openai/introduction
