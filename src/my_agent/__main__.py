"""支持 python -m my_agent，与安装后的 my-agent 命令共用同一入口。

仅在作为模块程序执行时调用 main；普通 import 不解析参数、不启动服务。
"""

from .cli import main

if __name__ == "__main__":
    main()
