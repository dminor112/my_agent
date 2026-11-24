"""第六阶段入口：持久化图执行与人工复核。

首次传 --query，恢复传相同 --thread 和 --resume；不会在脚本内新增规划逻辑。
"""

import sys

from my_agent.cli import main

if __name__ == "__main__":
    # 保留用户的 thread/query/resume 参数，避免重建一次不同的执行历史。
    sys.argv = [sys.argv[0], "graph", *sys.argv[1:]]
    main()
