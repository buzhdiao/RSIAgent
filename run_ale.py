#!/usr/bin/env python3
# 模块级文档字符串：声明本入口的职责——对固定版本（pinned）的 ALE Near-Term 基准
# 执行"规划（plan）、准备（prepare）、冒烟验证（smoke）、运行（run）、报告（report）"全流程。
# ALE 即 Agents' Last Exam，是论文评估所用的第二个基准（第一个是 OSWorld）。
"""Plan, prepare, validate, and run the pinned ALE Near-Term benchmark."""

# 导入 argparse：负责解析命令行参数（如 --arm、--output、--concurrency）。
import argparse
# 导入 asyncio：ALE 的准备与运行流程是异步实现的（大量并发 VM 操作），入口用
# asyncio.run() 驱动协程，与 OSWorld 入口使用线程池的方式形成对照。
import asyncio
# 导入 json：plan 动作要把任务清单（manifest）序列化为 JSON 打印给用户检查。
import json
# 导入 logging：为整个进程配置统一的日志格式，子模块沿用同一 root logger。
import logging
# 导入 pathlib.Path：所有路径参数（缓存目录、输出目录、虚拟环境路径）都用 Path
# 表达，避免手工拼接字符串路径，这是本仓库路径管理的基本约定。
from pathlib import Path


def parser():
    # 先解析出仓库根目录：__file__ 是本脚本位置，resolve() 转为绝对路径并消除
    # 符号链接，parent 即 RSIAgent 检出目录。后续默认路径全部锚定在这里，
    # 保证无论从哪个工作目录启动，行为都一致。
    root = Path(__file__).resolve().parent
    # 创建参数解析器，描述文本直接复用模块 docstring，保证 --help 与文档一致。
    p = argparse.ArgumentParser(description=__doc__)
    # 第一个位置参数是动作（子命令式设计）：plan 只打印计划；run 执行任务队列；
    # prepare 下载 VM 镜像与资产；smoke 做运行前冒烟检查；report 汇总结果。
    p.add_argument("action", choices=("plan", "run", "prepare", "smoke", "report"))
    # --arm 选择实验臂：baseline（仅基线执行，不做 RSI 学习）、rsi（先学习后冻结
    # 记忆评估）、both（两者都跑）。默认 baseline，即不做递归自我改进。
    p.add_argument("--arm", choices=("baseline", "rsi", "both"), default="baseline")
    # --ale-root 指向上游 ALE 仓库（agents-last-exam）的检出位置，默认是本仓库的
    # 兄弟目录；setup_ale.py 会把上游克隆到那里并建立独立虚拟环境。
    p.add_argument("--ale-root", type=Path, default=root.parent / "agents-last-exam")
    # --cache 是 VM 镜像与基准资产的缓存目录，放在用户缓存下；准备、冒烟、正式
    # 运行必须使用同一个 --cache，才能复用已下载的大体积镜像（Linux 约 167 GiB）。
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache/rsiagent/ale")
    # --runner-image 是 checkpoint 运行镜像名：ALE 运行时基于一个可回滚的容器
    # 镜像承载 worker 进程，镜像版本固定为 v1，保证实验可复现。
    p.add_argument("--runner-image", default="rsiagent-ale-checkpoint:v1")
    # --worker-python 指向 ALE worker 专用虚拟环境里的 Python 解释器：ALE 的外层
    # 进程（负责供给与评分）与 worker 进程（跑共享学习运行时）使用不同虚拟环境，
    # 避免两个项目的 Python 包互相遮蔽（shadowing）。
    p.add_argument(
        "--worker-python", type=Path, default=root / ".venv-ale-worker/bin/python"
    )
    # --env-file 指定 .env 文件位置，其中包含 OPENROUTER_API_KEY 等模型凭据，
    # 由内部模块加载，凭据不会写进代码或日志。
    p.add_argument("--env-file", type=Path, default=root / ".env")
    # --output 是本次运行的输出目录；run 动作强制要求显式指定且必须是新目录，
    # 防止覆盖历史实验产物（审计文件是结果可信的基础）。
    p.add_argument("--output", type=Path)
    # --tasks 允许传入一个每行一个任务 ID 的文件来圈定子集；不传则跑全部 67 个
    # Near-Term 任务。
    p.add_argument(
        "--tasks", type=Path, help="Task IDs, one per line; defaults to the full cohort"
    )
    # --concurrency 控制并发任务谱系数：每个谱系是相互独立的任务执行线（独立
    # 记忆快照与交互历史），默认 2，官方建议先用 1 验证流程再逐步调高。
    p.add_argument("--concurrency", type=int, default=2)
    # --vm-limit 限制宿主机上同时存在的 VM 数量上限（1-16），防止资源耗尽；
    # 这与 --concurrency 不同：一个谱系可能占用多个 VM。
    p.add_argument("--vm-limit", type=int, default=16)
    # --pool-root 是 VM 容量池的根目录，用文件锁协调多进程对 VM 名额的争用。
    p.add_argument(
        "--pool-root", type=Path, default=Path("/var/tmp/rsiagent-ale-capacity")
    )
    # --judge-base-url 是评分模型 API 的基础 URL，默认走 OpenRouter 聚合网关；
    # 评分（judge）属于 sealed evaluation 的一部分，与学习环节隔离。
    p.add_argument("--judge-base-url", default="https://openrouter.ai/api/v1")
    # --os 选择客户机操作系统镜像（linux 或 windows），prepare 与 smoke 阶段
    # 都要显式指定，每次准备只下载所选系统的镜像以节省磁盘。
    p.add_argument("--os", choices=("linux", "windows"), default="linux")
    # --runs 是 report 动作的输入：一个或多个历史运行目录，报告阶段把它们汇总
    # 成 tasks.csv 与 summary.json。
    p.add_argument("--runs", nargs="*", type=Path)
    # 返回配置好的解析器，便于测试复用（如 test_ale_checkpoint_timeout.py）。
    return p


def main(argv=None):
    # 构建解析器并解析参数；argv=None 时 argparse 自动取 sys.argv[1:]，
    # 传入列表则便于测试与内部调用。
    p = parser()
    args = p.parse_args(argv)
    # 配置全局日志：INFO 级别、带时间戳/模块名/级别，子模块的 logging.getLogger()
    # 输出都会汇到这里，形成统一的运行日志流。
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    # 参数合法性检查：并发数必须为正；VM 上限必须落在 1-16 之间（对应宿主资源
    # 承载能力）。不合法时用 parser.error 输出用法并退出（退出码 2）。
    if args.concurrency < 1 or not 1 <= args.vm_limit <= 16:
        p.error("Use positive concurrency and a host VM limit between 1 and 16")
    # run 动作必须有 --output：输出目录承载审计文件（日志、状态、结果），
    # 缺省无法落盘，也违背"输出永不覆盖"的原则。
    if args.action == "run" and args.output is None:
        p.error("run requires a new --output directory")
    # prepare 分支：延迟导入 benchmarks.ale.prepare，只在本分支真正需要时加载
    # 重依赖模块，缩短其他动作的启动时间。asyncio.run 驱动异步准备流程
    # （下载镜像、克隆上游、建虚拟环境等）。
    if args.action == "prepare":
        from benchmarks.ale.prepare import prepare

        asyncio.run(prepare(args))
    # smoke 分支：供给一台冒烟 VM 跑最小检查（transport、隔离、回滚等），
    # 不调用模型也不调用官方评分器；正式运行前必须对每种 OS 冒烟成功。
    elif args.action == "smoke":
        from benchmarks.ale.smoke import provision_smoke

        asyncio.run(provision_smoke(args))
    # report 分支：纯宿主机侧的统计汇总（读取各 run 的结果、去重、生成 CSV 与
    # summary.json），是同步代码，直接调用即可。
    elif args.action == "report":
        from benchmarks.ale.report import report

        report(args)
    # 剩下 plan 与 run 两个动作都由 benchmarks.ale.host 承担：它负责任务队列、
    # worker 进程与 VM 供给的编排。
    else:
        from benchmarks.ale.host import plan, run

        # 先构建 manifest：包含任务清单、臂选择、路径规划等；corpus（题库正文）
        # 体积大，单独排除在打印之外。
        manifest = plan(args)
        if args.action == "plan":
            # plan 动作：把 manifest（去掉 corpus）以缩进 JSON 打印出来供人工
            # 检查，不产生任何副作用（不启动 VM、不调用模型、不写文件）。
            print(
                json.dumps(
                    {k: v for k, v in manifest.items() if k != "corpus"}, indent=2
                )
            )
        else:
            # run 动作：先做就绪校验（镜像已下载、冒烟已通过、版本一致），任何
            # 一项不满足都会抛错终止，避免半途浪费模型调用。
            from benchmarks.ale.prepare import validate_readiness

            validate_readiness(args, manifest)
            # 就绪后进入异步主流程：调度 worker 进程、管理 VM 生命周期、收集
            # 结果。整个 ALE 运行是"外层进程供给+评分、worker 进程学习"的双进程结构。
            asyncio.run(run(args, manifest))


# 以脚本方式执行时才进入 main()；被 import 时（例如测试）不产生副作用。
if __name__ == "__main__":
    main()
