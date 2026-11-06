#!/usr/bin/env python3
"""Run the complete portable suite from this source checkout."""
# 从当前源码检出目录运行完整的可移植测试套件。
from __future__ import annotations

import os
# os：读取并复制环境变量。
from pathlib import Path
# Path：定位仓库根。
import subprocess
# subprocess：启动 pytest 子进程。
import sys
# sys：用当前解释器启动子进程。


REPO = Path(__file__).resolve().parents[1]
# 仓库根目录（脚本上一级）。


def main() -> int:
    # 运行测试套件并返回退出码。
    environment = dict(os.environ)
    # 复制当前环境变量。
    environment["RSIAGENT_ROOT"] = str(REPO)
    # 注入 RSIAGENT_ROOT，指向本检出（保证测试用的是这份源码）。
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=REPO, env=environment, check=False).returncode
    # 以当前解释器在仓库根运行 pytest（-q 安静模式），透传退出码。


if __name__ == "__main__":
    # 直接运行时以 main 的返回码退出。
    raise SystemExit(main())
    # 透传测试结果退出码。
