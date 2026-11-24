"""打包可分享源码：采用根目录/文件白名单并排除私有配置与数据库。

压缩包包括学习代码、测试和说明；不包括 .env、虚拟环境、Git 元数据。
reports 下的普通 JSON 属于白名单范围，分享前应自行检查是否有私密报告。
"""

import zipfile

from my_agent.config import ROOT

if __name__ == "__main__":
    destination = ROOT.parent / "my_agent-demo.zip"
    # 先列允许分享的根文件，.env 只允许示例模板，不允许真实配置。
    include_files = {
        "README.md",
        ".env.example",
        ".gitignore",
        "pyproject.toml",
        "requirements.lock.txt",
    }
    include_roots = {"src", "tests", "examples", "docs", "data", "reports"}
    paths = []
    # 目录白名单配合链接/缓存/数据库排除，避免把运行环境打入源码包。
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if path.is_symlink() or not path.is_file():
            continue
        if relative.parts[0] not in include_roots and str(relative) not in include_files:
            continue
        if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo"} or "sqlite3" in path.name:
            continue
        paths.append(path)
    # 压缩包路径固定；运行此脚本会更新此前由本项目生成的源码包。
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, "my_agent/" + path.relative_to(ROOT).as_posix())
    # 打包后检查归档完整性和危险文件名，不能仅凭写入成功就宣称安全。
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert not any(
            name.endswith("/.env") or ".venv/" in name or ".git/" in name or "sqlite3" in name
            for name in archive.namelist()
        )
    print(f"已打包 {len(paths)} 个文件，排除密钥、虚拟环境、Git 元数据和任务数据库。")
    print(destination)
