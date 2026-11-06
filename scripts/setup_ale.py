#!/usr/bin/env python3
"""Install isolated ALE grader and RSIAgent worker environments."""
# 安装相互隔离的 ALE（Agent 评分基准）评分器环境与 RSIAgent worker 环境。
# 评分器与 worker 各用独立虚拟环境；关键版本点全部钉死，防止基准漂移。

import argparse
# argparse：命令行参数解析。
import json
# json：读取协议锁文件。
from pathlib import Path
# Path：路径对象。
import shutil
# shutil：复制锁文件、探测 uv 可执行文件。
import subprocess
# subprocess：执行 git/uv 等外部命令。


def install(root, ale, worker_env, uv, *, images=False):
    # 安装主流程：克隆 ALE、钉版本、同步依赖、建 worker 环境、可选准备镜像。
    root, ale, worker_env = (
        Path(p).expanduser().resolve() for p in (root, ale, worker_env)
    )
    # 把三个路径统一为展开用户目录后的绝对路径。
    if worker_env in {root / ".venv", ale / ".venv"}:
        # worker 环境绝不能与开发环境或评分器环境共用。
        raise ValueError(
            "Use a dedicated worker environment, separate from the development and grader environments"
        )
        # 直接报错（fail-closed）。
    lock = json.loads((root / "config/ale/protocol.lock.json").read_text())
    # 读取协议锁：钉住 ALE 的提交版本。
    if not ale.exists():
        # ALE 目录不存在时才克隆。
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "https://github.com/rdi-berkeley/agents-last-exam.git",
                str(ale),
            ],
            check=True,
        )
        # 只克隆仓库（不检出工作区），目标为 ale 路径。
        subprocess.run(
            ["git", "-C", str(ale), "checkout", "--detach", lock["ale_commit"]],
            check=True,
        )
        # 分离式检出锁定的提交（保证评分器版本可复现）。
    revision = subprocess.check_output(
        ["git", "-C", str(ale), "rev-parse", "HEAD"], text=True
    ).strip()
    # 查询 ALE 当前 HEAD 提交。
    if revision != lock["ale_commit"]:
        # HEAD 与锁定版本不一致即拒绝。
        raise RuntimeError(
            "ALE checkout differs from the pinned revision; use a separate --ale-root"
        )
        # 提示换一个 --ale-root。
    dependency_lock = root / "benchmarks/ale/ale.uv.lock"
    # 仓库内钉死的依赖锁文件。
    installed_lock = ale / "uv.lock"
    # ALE 目录内的 uv.lock。
    if (
        installed_lock.exists()
        and installed_lock.read_bytes() != dependency_lock.read_bytes()
    ):
        # 已存在的依赖锁与钉死版本不符即拒绝。
        raise RuntimeError("ALE dependency lock differs; use a separate --ale-root")
        # 提示换一个 --ale-root。
    if not installed_lock.exists():
        # 缺少依赖锁时复制仓库钉死版本过去。
        shutil.copy2(dependency_lock, installed_lock)
        # 保留元数据复制。
    subprocess.run(
        [uv, "sync", "--frozen", "--project", str(ale), "--python", "3.12"], check=True
    )
    # 用 uv 按 lock 精确同步 ALE 评分器依赖（Python 3.12）。
    grader_python = ale / ".venv/bin/python"
    # 评分器环境的 Python 解释器路径。
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(grader_python),
            "-r",
            str(root / "benchmarks/ale/requirements-grader.txt"),
        ],
        check=True,
    )
    # 给评分器环境安装额外评分依赖清单。
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(grader_python),
            "torch==2.6.0",
            "--index-url",
            "https://download.pytorch.org/whl/cpu",
        ],
        check=True,
    )
    # 钉死安装 CPU 版 torch 2.6.0（评分器推理用，版本可复现）。
    worker_python = worker_env / "bin/python"
    # worker 环境的 Python 解释器路径。
    if not worker_python.exists():
        # worker 环境不存在时创建。
        subprocess.run([uv, "venv", str(worker_env), "--python", "3.12"], check=True)
        # 新建 Python 3.12 虚拟环境。
    # Use the same maintained dependency list as the public agent runtime.
    # Install only into the dedicated worker environment; never sync root/.venv.
    # （中文）worker 使用与公开 Agent 运行时同一份维护中的依赖清单；
    # 只安装进专用 worker 环境，绝不同步 root/.venv。
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(worker_python),
            "-r",
            str(root / "requirements.txt"),
        ],
        check=True,
    )
    # 给 worker 环境安装 RSIAgent 运行时依赖。
    for python in (grader_python, worker_python):
        # 对两个环境分别做依赖一致性体检。
        subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)
        # 校验无缺失/冲突依赖。
    if images:
        # 可选：准备运行器并下载钉死的 Linux VM 镜像。
        subprocess.run(
            [
                str(grader_python),
                str(root / "run_ale.py"),
                "prepare",
                "--ale-root",
                str(ale),
                "--worker-python",
                str(worker_python),
            ],
            check=True,
        )
        # 调用 run_ale.py prepare 完成 VM 镜像准备。
    print("Grader Python:", grader_python)
    # 输出评分器解释器路径。
    print("Worker Python:", worker_python)
    # 输出 worker 解释器路径。
    print("Configure model access using the repository .env.example.")
    # 提示：按仓库 .env.example 配置模型访问。


def main():
    # 命令行入口。
    root = Path(__file__).resolve().parents[1]
    # 仓库根目录（脚本上一级）。
    parser = argparse.ArgumentParser(description=__doc__)
    # 参数解析器（描述取模块 docstring）。
    parser.add_argument(
        "--ale-root", type=Path, default=root.parent / "agents-last-exam"
    )
    # ALE 检出目录（默认与仓库平级）。
    parser.add_argument("--worker-env", type=Path, default=root / ".venv-ale-worker")
    # worker 虚拟环境目录。
    parser.add_argument("--uv", default=shutil.which("uv"))
    # uv 可执行文件（默认从 PATH 探测）。
    parser.add_argument(
        "--images",
        action="store_true",
        help="Build the runner and download the pinned Linux VM image",
    )
    # 开关：是否构建运行器并下载钉死的 Linux VM 镜像。
    args = parser.parse_args()
    # 解析参数。
    if not args.uv:
        # 找不到 uv 则报参数错误。
        parser.error("Install uv and put it on PATH, or provide --uv")
        # 提示安装或显式指定。
    install(root, args.ale_root, args.worker_env, args.uv, images=args.images)
    # 执行安装流程。


if __name__ == "__main__":
    # 直接运行时进入 main。
    main()
    # 执行安装。
