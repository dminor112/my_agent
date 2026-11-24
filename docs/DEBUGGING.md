# 本地断点调试

## 一、打开项目并启动

1. 在 VS Code 中打开整个 `my_agent` 文件夹，不要只打开单个文件。
2. 安装 **Python** 和 **Python Debugger** 扩展。
3. 打开 `src/my_agent/native.py`，在工具分发处设置红色断点。
4. 打开左侧“运行和调试”（macOS：`Command + Shift + D`）。
5. 选择 **01 原生 Agent（Mock，推荐先用）**，按 **F5**。
6. 在弹出的输入框中填写问题，默认是“Agent 循环和工作流有什么区别？”。

配置位于 `.vscode/launch.json`，固定使用 `${workspaceFolder}/.venv/bin/python`。
`${workspaceFolder}` 由 VS Code 在启动时展开；不需要手动激活终端虚拟环境。
已有的“Python 调试程序: 当前文件”配置已保留，也指定了项目解释器。

## 二、可选启动项

| 配置 | 用途 | 模型费用 |
|---|---|---|
| 01 原生 Agent（Mock） | 学习原生模型/工具反馈循环 | 无 |
| 01 原生 Agent（GLM） | 在同一流程观察真实模型提议 | 可能产生 |
| 02 SDK Agent（Mock） | 对照 SDK Runner 与业务工具 | 无 |
| 02 SDK Agent（GLM） | SDK 接入真实 GLM | 可能产生 |
| 03 后端服务（Mock，8004） | 调试 API、任务生命周期和数据库 | 无 |
| 03 后端服务（GLM，8004） | 调试真实服务端链路 | 可能产生 |

所有项目配置开启 `justMyCode`，默认不进入 asyncio/HTTP 等第三方代码。
密钥继续由项目 `load_settings()` 读取 `.env`，没有写入或复制到 launch.json。
没有设置 `envFile`，避免 VS Code 将 `.env` 值注入进程环境后覆盖调试专用配置；
项目本身会读取 `.env`，调试配置的显式环境变量优先。

## 三、建议断点和观察变量

### 原生循环

在 `src/my_agent/native.py` 中，按语句定位（注释增加后行号可能变化）：

- `proposal, tokens = await asyncio.wait_for(...)` 后：观察 `proposal` 和本轮用量。
- `args = json.loads(...)` 后：观察工具参数与工具名 `name`。
- `matches = corpus.search(...)`：进入工具检索。
- `report = validate_report(...)`：观察结构/证据校验。
- `state.messages.append(...)`：观察成功/失败结果如何回填。

Watch 建议：`state.pending`、`state.evidence`、`state.turns`、`state.total_tokens`。
不要把密钥字段、客户端鉴权头或私有资料截图分享。

### SDK

在 `src/my_agent/sdk.py` 的 `search_documents`、`read_document`、`submit_report` 内打断点。
这些工具函数才是应用业务代码；Runner 的调度属于 SDK，默认不单步进入。

## 四、调试后端服务

1. 选择 **03 后端服务（Mock，8004，独立数据库）**，按 F5。
2. 浏览器打开 `http://127.0.0.1:8004/docs`。
3. 在 `api.py` 的 `start_run`、`service.py` 的 `execute` 和 `native.py` 工具分支设置断点。
4. 通过页面的 `POST /runs` 发起请求；返回 202 后可用 `GET /runs/{run_id}` 查询。

Mock 调试库：`data/debug.sqlite3`；GLM 调试库：`data/debug-glm.sqlite3`。
它们与正常运行的 `data/agent.sqlite3` 分开，不影响已有 8003 服务的任务恢复状态。
两个服务调试配置都使用 8004，**不要同时启动**。端口被占用时修改该配置 args 中的端口。
调试服务不使用 reload 或多个 worker，避免重启进程和断点不一致。
如果 `.env` 配置了服务令牌，接口仍需要原有 Bearer 鉴权；调试配置不会取消它。

## 五、单步和超时说明

- Continue：继续至下个断点。
- Step Over：执行当前行，不进入被调用函数。
- Step Into：进入自己想观察的函数（例如检索或引用校验）。
- Step Out：退出当前函数。
- Stop：结束本次调试进程；不会停止独立运行的常规服务。

调试配置仅对本次进程把任务超时提高为 1800 秒、请求超时提高为 300 秒，
不修改 `.env` 或生产代码。断点停留时间仍可能被异步超时计入，**不是禁用超时**。
建议先用 Mock，并把断点放在 `await` 返回后。GLM 调试反复重启会产生真实请求/费用。

## 六、常见问题

- 找不到 debugpy：先安装/启用 VS Code 的 Python Debugger 扩展；扩展通常自带调试器。
- 找不到 my_agent：确认打开的根文件夹是 my_agent，且解释器为该仓库 .venv。
- 空心断点：文件尚未加载或配置选错；触发对应函数，查看是否停在自己代码。
- 8004 占用：先停止另一个调试服务，或使用另一个端口；不要杀已有 8003 服务。
- GLM 失败：查看脱敏错误，核对 `.env` 中模型名、端点、Key；不要在调试文件硬编码密钥。
