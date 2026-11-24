"""第二阶段入口：同一套资料与报告契约，改用真正的 SDK Runner。

本脚本只负责转发参数，不另写 Agent 逻辑，方便与原生版本做对照。
"""

import sys

from my_agent.cli import main

if __name__ == "__main__":
    # 仅切换子命令；模型参数、资料库、报告结构和预算复用项目实现。
    sys.argv = [sys.argv[0], "sdk", "工具 Schema 如何设计？", *sys.argv[1:]]
    main()
