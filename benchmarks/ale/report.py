"""Merge all attempts into a complete-cohort ledger without best-of selection."""
# 【中文注释·模块定位】结果汇总/报告：把一次或多次归档运行目录中每个任务的结果
# 合并成完整队列（complete cohort）台账。纪律：绝不"挑最好成绩"（no best-of
# selection）——每个任务每条臂（baseline/rsi）只允许一份评估结果，发现重复直接报错，
# 缺失结果显式记为 missing，分母固定为 67 个 Near-Term 任务。

# csv：输出逐任务明细 tasks.csv。
import csv
# json：解析各任务的 result.json 与打印汇总。
import json
# math：isfinite 校验分数必须是有限数（拒绝 NaN/Inf 混入台账）。
import math
# Path：遍历运行目录。
from pathlib import Path

# protocol：读取固定协议锁（含 67 任务队列）与原子写盘工具。
from .protocol import protocol, write_json


# 分数合法性校验：必须是 0~1 的有限数值；bool 是 int 子类，需先排除。
def valid_score(value):
    # 布尔值不是合法分数（True/False 会伪装成 1/0），显式拒绝。
    if isinstance(value, bool):
        raise ValueError("ALE score must be a finite number between zero and one")
    # 转换为浮点数（兼容字符串形式的数字）。
    value = float(value)
    # 非有限值（NaN/Inf）或越出 [0,1] 区间一律拒绝，保证台账可信。
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("ALE score must be a finite number between zero and one")
    # 返回规范化的浮点分数。
    return value


# 从多个运行目录收集全部任务的 baseline/rsi 分数，构造完整队列台账。
def collect(roots):
    # 读取固定协议锁：以锁中的 67 任务清单为唯一台账基准。
    lock = protocol()
    # 任务队列表。
    tasks = lock["tasks"]
    # 初始化台账：每个任务一行，两条臂的分数与来源路径初始为空。
    rows = {
        task: {
            "task": task,
            "baseline_score": None,
            "rsi_score": None,
            "baseline_source": "",
            "rsi_source": "",
        }
        for task in tasks
    }

    # 登记单个任务某条臂的评估结果，重复评估直接拒绝。
    def ingest(task, arm, path):
        # 转绝对路径，作为该结果的唯一来源标识。
        path = path.resolve()
        # 读取结果 JSON。
        data = json.loads(path.read_text())
        # 没有分数（如学习阶段未评分）：跳过，不算缺失也不算重复。
        score = data.get("score")
        if score is None:
            return
        # 该臂此前已登记的来源路径。
        source = rows[task][arm + "_source"]
        # 已登记且来自不同文件：同一任务同臂出现两份评估，拒绝合并（防 cherry-picking）。
        if source and source != str(path):
            raise RuntimeError(
                f"Duplicate {arm} evaluations for {task}; explicitly resolve attempts before merging"
            )
        # 记录来源路径。
        rows[task][arm + "_source"] = str(path)
        # 校验并写入分数。
        rows[task][arm + "_score"] = valid_score(score)

    # 遍历每个归档运行目录。
    for root in roots:
        root = Path(root)
        # 遍历队列中的每个任务。
        for task in tasks:
            # baseline 臂读 baseline/phase 的结果；rsi 臂读 phase3（记忆冻结后的密封评估）结果。
            for arm, phase in [("baseline", "baseline"), ("rsi", "phase3")]:
                # 该任务该阶段的标准结果文件路径。
                path = root / task / phase / "result.json"
                # 文件存在才登记。
                if path.is_file():
                    ingest(task, arm, path)
    # 返回完整台账。
    return rows


# 生成汇总：完整队列口径的评估数/缺失数/满分率/平均分，可选落盘 summary.json 与 tasks.csv。
def report(args):
    # 必须显式提供至少一个运行目录，防止把空报告当真。
    if not args.runs:
        raise RuntimeError(
            "report requires --runs with one or more archived run directories"
        )
    # 收集台账。
    rows = collect(args.runs)
    # 汇总结构：队列总数固定 67，两臂分别统计。
    summary = {"cohort_count": 67, "arms": {}}
    # 分别汇总 baseline 与 rsi 两条实验臂。
    for arm in ("baseline", "rsi"):
        # 该臂所有已评估的分数（缺失的为 None 被过滤）。
        scores = [
            r[arm + "_score"] for r in rows.values() if r[arm + "_score"] is not None
        ]
        # 满分（score==1.0，ALE 官方判分）的任务数。
        passes = sum(s == 1.0 for s in scores)
        # 写入该臂统计：evaluated=已评估数；missing=缺失数（分母固定 67，缺失显式暴露）；
        # full_credit=满分任务数；observed_full_credit_over_67=以完整队列 67 为分母的观测满分率；
        # mean_available_score=仅对已评估任务求均值（参考值）；complete_cohort_mean_score=完整队列
        # 口径均值——缺一个任务就无法计算，宁缺毋假（None）。
        summary["arms"][arm] = {
            "evaluated": len(scores),
            "missing": 67 - len(scores),
            "full_credit": passes,
            "observed_full_credit_over_67": passes / 67,
            "mean_available_score": sum(scores) / len(scores) if scores else None,
            "complete_cohort_mean_score": sum(scores) / 67
            if len(scores) == 67
            else None,
        }
    # 指定了输出目录：写汇总与逐任务明细。
    if args.output:
        # 创建输出目录。
        args.output.mkdir(parents=True, exist_ok=True)
        # 写 summary.json（原子写）。
        write_json(args.output / "summary.json", summary)
        # 写 tasks.csv：表头取自台账行结构，逐行写入。
        with (args.output / "tasks.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(next(iter(rows.values()))))
            # 写表头。
            writer.writeheader()
            # 写全部任务行。
            writer.writerows(rows.values())
    # 同时打印汇总到标准输出。
    print(json.dumps(summary, indent=2))
