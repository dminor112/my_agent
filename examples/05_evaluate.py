"""第五阶段入口：固定题集的规则评测，默认 Mock 避免付费调用。

Mock 通过证明管线能执行，不证明真实模型语义质量；参数仍转发给统一 CLI。
"""

import sys

from my_agent.cli import main

if __name__ == "__main__":
    # 默认 Mock 不收费；参数转发仍遵循 CLI，不把通过率当成真实模型质量。
    sys.argv = [sys.argv[0], "eval", "--mode", "mock", *sys.argv[1:]]
    main()
