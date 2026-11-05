#!/usr/bin/env python3
# 模块级文档字符串：本脚本是 OSWorld 批处理入口，跑完整的固定版本任务群，
# 并且保证每个任务的输出互相独立（独立目录、互不覆盖），支撑并行谱系。
"""Run the full pinned OSWorld cohort, with independent outputs for every task."""

# from __future__ import annotations：启用 PEP 563 延迟注解求值，类型注解写成
# 字符串延迟解析，既兼容较低 Python 版本，也允许前向引用尚未定义的类型。
from __future__ import annotations

# argparse：定义 --arm/--name/--concurrency/--dry-run 等命令行参数。
import argparse
# json：读取 baseline.lock.json 基准锁、生成 plan.json 与 status.json 审计文件。
import json
# os：execute() 需要复制当前进程环境变量并为子进程注入 RSIAGENT_ROOT 等。
import os
# re：校验批处理名称只含安全字符，防止路径注入或非法目录名。
import re
# subprocess：以子进程方式启动内部阶段模块（benchmarks.osworld.task/pipeline）。
import subprocess
# sys：sys.executable 取当前解释器路径，保证子进程用同一 Python 环境。
import sys
# ThreadPoolExecutor：线程池并发调度多个任务谱系（每个任务独立子进程，
# 线程只负责等待与记录，规避 GIL 影响可忽略）。
from concurrent.futures import ThreadPoolExecutor
# datetime/timezone：默认批名使用 UTC 时间戳，保证跨时区可复现、可排序。
from datetime import datetime, timezone
# Path：所有路径操作统一用 pathlib，符合仓库路径管理约定。
from pathlib import Path

# load_public_task_surface：从 OSWorld 源码目录读取任务"公开面"（题面指令等
# 公开信息），用于在缺少 target_queries 文件时归一化任务指令。
from benchmarks.osworld.task_surface import load_public_task_surface
# resolve_osworld_root：按环境变量 RSIAGENT 的约定解析 OSWorld-V2 源码根目录
# （默认为本仓库的兄弟目录 ../OSWorld-V2）。
from config.runtime_paths import resolve_osworld_root
# normalize_instruction_for_corpus：把原始任务指令归一化为语料库（corpus）用
# 的规范文本，保证 RSI 阶段拿到的题面格式一致。
from explore.commit import normalize_instruction_for_corpus

# ROOT：仓库根目录（本文件所在目录），所有结果与配置路径都锚定于此。
ROOT = Path(__file__).resolve().parent
# LOCK：基准锁文件，冻结基准版本（benchmark_release）、任务数、种子标签、
# 网站后缀、资产目录等，确保每次批处理跑的是同一个固定基准。
LOCK = ROOT / "config/osworld/baseline.lock.json"
# TEMPLATE：RSI 协议模板，包含 phase1/2/3 三阶段配置骨架，批处理会为每个任务
# 复制一份并填入任务特定字段。
TEMPLATE = ROOT / "config/osworld/rsi.example.json"


def build_plan(name, arm, *, tasks=None, osworld_root=None):
    # build_plan：把"跑哪些任务、每个任务跑哪些阶段、产物写到哪"展开成一份
    # 纯数据计划（plan），不产生任何副作用——这是 --dry-run 的基础。
    # 先校验批名：1-64 个字母/数字/下划线/连字符，且以字母数字开头。
    # 用 re.fullmatch 精确匹配整个字符串，杜绝 "../x" 之类的路径穿越。
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError(
            "Batch name must contain 1–64 letters, numbers, underscores or hyphens"
        )
    # 读取基准锁：任务总数、种子、基准版本都来自这里，锁定实验配置。
    lock = json.loads(LOCK.read_text())
    # 任务列表：显式传入则用之；否则按锁文件生成 task_001..task_N 全量列表
    #（f-string 的 :03d 保证三位零填充、字典序稳定）。
    tasks = (
        tasks
        if tasks is not None
        else [f"task_{i:03d}" for i in range(1, lock["task_count"] + 1)]
    )
    # OSWorld 源码根目录：未显式给出时按运行时路径规则解析。
    osworld_root = osworld_root or resolve_osworld_root()
    # 批输出目录：results/batches/<name>，构建计划阶段不创建它（延迟到 execute）。
    folder = ROOT / "results/batches" / name
    # 逐任务展开作业（job）。
    jobs = []
    for task in tasks:
        # 每个作业记录：任务名、要执行的命令序列、期望产物路径（用于预检冲突）。
        job = {"task": task, "commands": [], "result_paths": []}
        if arm in ("baseline", "both"):
            # baseline 臂：不做 RSI，直接在冻结（空/初始）记忆下执行任务。
            # 结果目录把种子标签编进名字，同一批名下基线与 RSI 结果不互相覆盖。
            result = ROOT / "results" / task / f"seed{lock['seed_label']}_{name}"
            job["result_paths"].append(str(result))
            # 基线命令：以模块方式运行 benchmarks.osworld.task——冻结记忆下的
            # 任务执行与 sealed evaluation（官方评分在验证之后才运行）。
            # -u 关闭 stdout 缓冲，保证日志实时写入文件。
            job["commands"].append(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "benchmarks.osworld.task",
                    task,
                    "--config",
                    str(ROOT / "config/osworld/baseline.yaml"),
                    "--seed",
                    str(lock["seed_label"]),
                    "--tag",
                    name,
                ]
            )
        if arm in ("rsi", "both"):
            # RSI 臂：三阶段协议（Phase1 BRS 广度探索 → Phase2 DRS 深度定向
            # 练习 → Phase3 冻结记忆测试期复用）。先复制协议模板。
            spec = json.loads(TEMPLATE.read_text())
            # 运行名加上任务后缀：每个任务一个独立 RSI 谱系，记忆互不影响。
            spec["run_name"] = f"{name}_{task}"
            # 目标任务指令：优先使用人工维护的 target_queries/<task>.md；
            # 否则回退到"从 OSWorld 任务面读取并归一化"的自动路径。
            query = ROOT / "config/target_queries" / f"{task}.md"
            instruction = (
                query.read_text()
                if query.is_file()
                else normalize_instruction_for_corpus(
                    load_public_task_surface(
                        osworld_root,
                        task,
                        website_host_suffix=lock["website"]["host_suffix"],
                    ).instruction
                )
            )
            # 把 Phase1 的探索分布文件指到本任务谱系目录下的 direction.md
            #（由 run_job 落盘），实现"每个谱系独立的方向指引"。
            spec["phase1"]["distribution_file"] = str(folder / task / "direction.md")
            # Phase2 的开发目标就是当前任务本身：先尝试真实目标，暴露差距后
            # 再决定是否练习（DRS：深度递归自我探索）。
            spec["phase2"]["development_tasks"] = [task]
            # Phase3 的保留任务也是同一任务：学习完成后用冻结记忆执行并密封评估。
            spec["phase3"]["held_out_tasks"] = [task]
            # 把协议与方向指令挂到作业上，供 run_job 写盘供内部阶段读取。
            job.update(protocol=spec, direction=instruction)
            # 记录 RSI 运行的期望产物目录（results/recursive_improvement/<run_name>）。
            job["result_paths"].append(
                str(ROOT / "results/recursive_improvement" / spec["run_name"])
            )
            # 三个阶段各生成一条命令：分阶段执行的好处是任一阶段失败即可停止
            # 本任务的后续阶段（状态机推进由 pipeline 内部管理）。
            for phase in ("phase1", "phase2", "phase3"):
                job["commands"].append(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "benchmarks.osworld.pipeline",
                        "--protocol",
                        str(folder / task / "protocol.json"),
                        "--phase",
                        phase,
                        "--execute",
                        "RUN-RECURSIVE-IMPROVEMENT-" + phase.upper(),
                    ]
                )
        # 收集完的作业加入计划。
        jobs.append(job)
    # 返回整批计划：批名、臂、基准版本、输出目录与作业列表（纯 JSON 可序列化）。
    return {
        "name": name,
        "arm": arm,
        "task_release": lock["benchmark_release"],
        "output": str(folder),
        "jobs": jobs,
    }


def execute(plan, concurrency, osworld_root):
    # execute：真正执行计划。先做全部前置校验（输出冲突、基准资产完整性），
    # 全部通过后才创建目录、写计划文件并启动任何模型/VM。
    folder = Path(plan["output"])
    # 预检所有会写入的路径（批目录本身 + 每个作业的结果目录）：只要有一个已
    # 存在就整体拒绝。这保证"历史输出永不覆盖"，也防止两个批写同一目录。
    for path in [
        folder,
        *(Path(p) for job in plan["jobs"] for p in job["result_paths"]),
    ]:
        if path.exists():
            raise FileExistsError(
                f"Use a new batch name; output already exists: {path}"
            )
    # 在启动任何模型调用或 VM 之前校验本地基准资产完整性（--verify-only 不下载），
    # 尽早失败（fail fast），避免跑到一半才发现镜像或题库缺失。
    # Validate inputs before any model or VM is launched.
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/prepare_osworld_v2_release.py"),
            "--osworld-root",
            str(osworld_root),
            "--verify-only",
        ],
        cwd=ROOT,
        check=True,
    )
    # 再次读取锁文件（与 build_plan 保持一致），拼装子进程环境：
    lock = json.loads(LOCK.read_text())
    environment = dict(
        os.environ,
        # RSIAGENT_ROOT：子进程内定位本仓库（配置、结果根）。
        RSIAGENT_ROOT=str(ROOT),
        # OSWORLD_ROOT：OSWorld-V2 源码位置，adapter 与评分依赖它。
        OSWORLD_ROOT=str(osworld_root),
        # OSWORLD_FILE_BASE_URL：任务资产（文件下载类任务）的本地服务基址。
        OSWORLD_FILE_BASE_URL=str(osworld_root / lock["task_assets"]["local_dir"]),
        # WEBSITE_HOST_SUFFIX：目标网站的域名后缀，任务环境解析用。
        WEBSITE_HOST_SUFFIX=lock["website"]["host_suffix"],
    )
    # 正式创建批目录（exist_ok=False：并发或重复创建立即报错），并写入 plan.json
    # 审计文件——它记录"这次批处理到底计划跑了什么"，是复现实验的依据。
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")

    def run_job(job):
        # run_job：在批目录下为本任务建工作区，写协议/方向文件，顺序执行命令，
        # 每步日志独立成文件；某步失败立即停止后续步骤（任务级 fail-fast）。
        work = folder / job["task"]
        work.mkdir()
        if "protocol" in job:
            # RSI 作业：先落盘 protocol.json（三阶段协议）与 direction.md
            # （探索方向），内部阶段模块通过 --protocol 参数读回它们。
            (work / "protocol.json").write_text(
                json.dumps(job["protocol"], indent=2) + "\n"
            )
            (work / "direction.md").write_text(job["direction"])
        # completed 记录每一步的命令、退出码与日志路径，构成可审计的步骤链。
        completed = []
        for index, command in enumerate(job["commands"]):
            # 每步注入独立的缓存目录（RSIAGENT_OSWORLD_CACHE_DIR）：不同阶段
            # 的缓存互不污染，也便于按阶段清理。
            env = dict(
                environment, RSIAGENT_OSWORLD_CACHE_DIR=str(work / "cache" / str(index))
            )
            # 每步日志写到 <n>.log（1 起），stdout/stderr 合并写入同一文件。
            log = work / f"{index + 1}.log"
            try:
                with log.open("w") as stream:
                    # check=False：不抛异常，由我们自己把退出码记录进状态；
                    # 这是"批处理中单任务失败不拖垮其他任务"的关键。
                    code = subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        check=False,
                    ).returncode
            except OSError as exc:
                # 进程都起不起来（如解释器缺失）也要留痕：把异常写进日志，
                # 记作失败码 1，状态链保持完整。
                log.write_text(str(exc) + "\n")
                code = 1
            completed.append({"command": command, "returncode": code, "log": str(log)})
            if code:
                # 某步失败：跳出循环，剩余阶段不再执行（例如 Phase1 失败就不跑
                # Phase2/3），这正是"任务失败停止其剩余阶段"的语义。
                break
        # 汇总本任务状态：最后一步退出码非零即 failed，否则 completed。
        result = {
            "task": job["task"],
            "status": "failed" if completed[-1]["returncode"] else "completed",
            "steps": completed,
            "result_paths": job["result_paths"],
        }
        # 每个任务写自己的 status.json（步骤级审计），并在控制台即时汇报。
        (work / "status.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"{job['task']}: {result['status']}", flush=True)
        return result

    # 线程池并发跑所有作业：max_workers=concurrency 控制并发谱系数；
    # 每个作业是独立子进程+独立目录，线程间无共享可变状态。
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(run_job, plan["jobs"]))
    # 全部作业结束后写批级 status.json（任务级审计），然后返回非零退出码表示
    # 存在失败任务——批处理退出码只反映"是否有失败"，不隐藏任何结果。
    (folder / "status.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(result["status"] != "completed" for result in results))


def main(argv=None):
    # 命令行入口：解析参数 → 构建计划 →（dry-run 则仅打印）→ 执行。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        # --arm：baseline / rsi / both，与 run_ale.py 语义一致。
        "--arm", choices=("baseline", "rsi", "both"), default="baseline"
    )
    parser.add_argument(
        # --name：批名，默认用 UTC 时间戳（batch_YYYYmmdd_HHMMSS），可读且唯一。
        "--name", default=datetime.now(timezone.utc).strftime("batch_%Y%m%d_%H%M%S")
    )
    parser.add_argument(
        # --concurrency：并发任务谱系数，默认 1（官方建议先串行验证再并行）。
        # 注意一个 RSI 谱系可能占用多台 VM，实际 VM 数可能高于该值。
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent task lineages; each RSI lineage may own multiple VMs",
    )
    parser.add_argument(
        # --dry-run：只打印计划，不启动 VM、不调用模型、不写任何输出——
        # build_plan 是纯函数，天然支持零副作用预览。
        "--dry-run",
        action="store_true",
        help="Print the full plan without launching or writing outputs",
    )
    args = parser.parse_args(argv)
    # 并发数必须为正，负数或 0 直接报用法错误。
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    # 解析 OSWorld 源码根目录（环境变量或默认兄弟目录）。
    osworld_root = resolve_osworld_root()
    # 构建完整计划（纯数据，无副作用）。
    plan = build_plan(args.name, args.arm, osworld_root=osworld_root)
    if args.dry_run:
        # dry-run：打印缩进 JSON 计划并成功返回，方便人工核对任务群。
        print(json.dumps(plan, indent=2))
        return 0
    # 真实执行：返回批处理退出码（0 全部完成，1 有失败）。
    return execute(plan, args.concurrency, osworld_root)


# 脚本入口：把 main() 的返回值作为进程退出码；被 import 时不执行。
if __name__ == "__main__":
    raise SystemExit(main())
