"""第一阶段入口：运行原生反馈循环。

默认附带一条学习问题，用户可传 --mode mock/glm。
过程事件在 stderr，报告 JSON 在 stdout，便于观察行动与最终结果的区别。
"""

import sys

from my_agent.cli import main

if __name__ == "__main__":
    # 附带默认问题，后面的 --mode 等参数保持原样转交给统一 CLI。
    sys.argv = [sys.argv[0], "native", "Agent 循环和工作流有什么区别？", *sys.argv[1:]]
    main()
