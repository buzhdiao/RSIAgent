#!/usr/bin/env python3
"""Run one OSWorld task with sealed post-Agent evaluation.

The Agent receives the public instruction and a VM handle. Official evaluation
runs only after the action/verification loop returns. Evaluator transport retries
retain the completed VM and never re-enter an Agent.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_path,
    resolve_root,
    resolve_osworld_root,
)
from config.benchmark_runtime import configure_associated_benchmark_lock

# SIGTERM -> 以 143 退出 -> finally 释放环境；SIGKILL 的运行会泄漏容器，停机时优先用 TERM
signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # kill -> finally -> de.close()
#                                                           (SIGKILLed runs still leak;
#                                                            prefer TERM when stopping)

# 解析两个检出根
RSIAGENT_ROOT = resolve_root()
OSWORLD_ROOT = resolve_osworld_root(repo_root=RSIAGENT_ROOT)
# 以下 import 依赖 _install_paths() 调整 sys.path/CWD，故只能在运行期（非模块顶部）执行
from config.settings import load as load_config          # noqa: E402
from core.loop import run_with_resume                    # noqa: E402
from core.actor import build_system                  # noqa: E402
from core.trace import ArtifactSink                  # noqa: E402
from core.grading import GradingBlocked, run_until_success  # noqa: E402
from core.verifier_runtime import AgenticVerifierNoProgressError  # noqa: E402
from env.vm import VM                               # noqa: E402
from llm.client import provider_counts, reset_provider_counts  # noqa: E402

# 日志基础配置与本模块日志器
logging.basicConfig(level=logging.INFO, format="[%(levelname)s %(name)s] %(message)s")
log = logging.getLogger("rsiagent.run")


def smoke(vm) -> bool:
    """Infra check: command roundtrip, program roundtrip, timeout-recovery path."""
    # 汇总通过标志
    ok = True
    out = vm.run_command("echo rsiagent_smoke_ok && whoami")
    print("run_command:", out)
    ok &= "rsiagent_smoke_ok" in out
    tr = vm.run_script("python", "print('py_roundtrip_ok')")
    print("run_script:", tr.stdout.strip()[:200], "| exit", tr.exit_code)
    ok &= tr.exit_code == 0 and "py_roundtrip_ok" in tr.stdout
    tr = vm.run_script("bash", "echo partial_before_sleep; sleep 99", timeout=45)
    print("timeout path:", tr.stdout.strip()[:200], "| exit", tr.exit_code,
          "| timed_out", tr.timed_out)
    ok &= tr.timed_out and "partial_before_sleep" in tr.stdout
    return bool(ok)


def main():
    # 命令行参数解析
    ap = argparse.ArgumentParser()
    ap.add_argument("task_id", help="e.g. task_012")
    ap.add_argument("--seed", type=int, default=0,
                    help="run label only (independent draw; the API has no seed)")
    ap.add_argument("--config", default="config/osworld/baseline.yaml", help="Actor configuration")
    ap.add_argument("--tag", default=None,
                    help="output-dir suffix -> results/<task>/seed<S>_<tag> (for A/B arms)")
    ap.add_argument("--max-iters", type=int, default=None)
    ap.add_argument(
        "--memory-dir", default="",
        help=("immutable host memory snapshot to copy into the Actor environment; "
              "the runner never syncs guest changes back"))
    ap.add_argument("--smoke", action="store_true", help="infra check only, no agent")
    ap.add_argument("--evaluator-correction", default="",
                    choices=["task102-missing-paragraph-style-v1"],
                    help="explicit versioned post-Agent evaluator correction")
    args = ap.parse_args()
    # 先安装 OSWorld/RSIAgent 的路径环境（sys.path + CWD + 凭据）
    from benchmarks.osworld.runtime import _install_paths
    _install_paths()
    if args.evaluator_correction and args.task_id != "task_102":
        # 评审修正只对 task_102 有效
        ap.error("this evaluator correction applies only to task_102")
    # 配置路径：命令行参数优先，其次环境变量 RSIAGENT_CONFIG
    config_reference = args.config or os.environ.get("RSIAGENT_CONFIG")
    config_path = (resolve_path(config_reference, repo_root=RSIAGENT_ROOT)
                   if config_reference else None)
    # 绑定配置关联的基准锁（评审器/任务用户通道运行时）
    benchmark_lock_record = configure_associated_benchmark_lock(
        config_path, repo_root=RSIAGENT_ROOT)
    if benchmark_lock_record:
        log.info(
            "bound direct run to %s evaluator %s through associated lock %s",
            benchmark_lock_record["benchmark_release"],
            benchmark_lock_record["evaluator_model"],
            benchmark_lock_record["path"])
    # 加载 Actor 配置并把 Verifier 的延迟加载路径锚定到仓库根
    cfg = load_config(str(config_path) if config_path else None)
    normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
    # 记忆（memory）记录的默认值：关闭状态
    memory_record = {
        "mode": "off",
        "source": "",
        "files": 0,
        "bytes": 0,
        "tree_sha256": "",
        "host_writeback": False,
    }
    if args.memory_dir:
        from explore.practice_loop import _manifest, _read_memory_tree  # noqa: E402
        from explore.target_learning import _memory_tree_sha256       # noqa: E402

        raw_memory_path = Path(args.memory_dir).expanduser()
        if raw_memory_path.is_symlink():
            # 拒绝符号链接（防目录逃逸）
            raise RuntimeError("--memory-dir may not be a symlink")
        memory_path = raw_memory_path.resolve(strict=True)
        if not memory_path.is_dir():
            raise RuntimeError("--memory-dir must be one real directory")
        configured = str(getattr(cfg, "env_memory_dir", "") or "").strip()
        if configured and Path(configured).expanduser().resolve() != memory_path:
            # 与配置文件声明的记忆目录冲突：拒绝
            raise RuntimeError(
                "--memory-dir conflicts with env_memory_dir in the selected config")
        # 读取冻结的宿主记忆快照（只读拷贝入 VM）
        frozen_memory = _read_memory_tree(str(memory_path))
        manifest = _manifest(frozen_memory)
        cfg.env_memory_dir = str(memory_path)
        # 记录快照的完整审计信息（只进不出，host_writeback 恒为 False）
        memory_record = {
            "mode": "frozen_host_snapshot_copy_in_only",
            "source": str(memory_path),
            "files": len(frozen_memory),
            "bytes": sum(len(data) for data in frozen_memory.values()),
            "tree_sha256": _memory_tree_sha256(frozen_memory),
            "manifest_sha256": hashlib.sha256(json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
            "host_writeback": False,
        }
    from env.qemu_rollback import (                         # noqa: E402
        normalize_verifier_execution_mode,
    )
    from benchmarks.osworld.provider import (                             # noqa: E402
        prepare_checkpointable_docker_provider,
    )
    # 归一化 Verifier 执行模式（effect_isolated / rollback_mirror）
    verifier_execution_mode = normalize_verifier_execution_mode(
        cfg.verifier_execution_mode)
    # 预备可快照回滚的 Docker 提供器（按 Verifier 执行模式）
    prepare_checkpointable_docker_provider(verifier_execution_mode)
    if getattr(cfg, "verifier_evolve_route", False):
        # EVOLVE 路由属于统一循环入口；这里出现会越过封印评分边界
        raise RuntimeError(
            "verifier_evolve_route belongs to the unified-loop entrypoint; "
            "benchmarks/osworld/task.py would cross the sealed grader boundary after EVOLVE")
    if args.max_iters:
        # 命令行覆盖最大轮数
        cfg.max_iters = args.max_iters

    from task_loader import load_task_config, resolve_task_json_path   # noqa: E402
    from desktop_env.desktop_env import DesktopEnv                     # noqa: E402
    # 定位并加载任务配置（OSWorld v2 任务集）
    cf = resolve_task_json_path(task_id=args.task_id, base_dir="evaluation_examples",
                                eval_version="v2")
    task_config = load_task_config(cf, task_id=args.task_id,
                                   base_dir="evaluation_examples", eval_version="v2")
    # 取公开指令文本
    instruction = str(getattr(task_config, "instruction", None)
                      or task_config["instruction"])
    # （中文）GitLab 类任务（026/041）的 per-run 凭据本应作为指令内容交给 Agent，
    # 但 BaseTask 级属性遮蔽会丢掉这一行；这里从同一 task_config 恢复作者放置的
    # 凭据行，保证输入与官方 runner 一致——是任务输入而非评分信号（Constraint #0）。
    # GitLab-backed tasks (026/041) mint per-run credentials in user_config and their
    # own __init__ intends them INSIDE the instruction ("My GitLab account is '...'");
    # a BaseTask-level attribute shadow drops that line, so the loaded instruction says
    # "use my credentials" while naming none. Restore the task-authored credential line
    # from the SAME task_config object de.reset() provisions the account with, so the
    # agent receives exactly the input the official runner delivers. This is task input
    # (author-placed, in-instruction), never grader/gold signal — Constraint #0 intact.
    _uc = getattr(task_config, "user_config", None)
    if isinstance(_uc, dict) and _uc.get("username") and _uc.get("password") \
            and "password is" not in instruction:
        instruction = (f"{instruction} My GitLab account is '{_uc['username']}' "
                       f"and the password is '{_uc['password']}'.")

    # 创建 Docker 桌面环境；60G 磁盘（v2 镜像接近满盘，需扩容 guest 文件系统）
    de = DesktopEnv(provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
                    screen_size=(1920, 1080), headless=True, require_a11y_tree=False,
                    cache_dir=os.environ.get("RSIAGENT_OSWORLD_CACHE_DIR", "cache"),
                    volume_size=60)   # 60G: the near-full v2 image needs the guest fs grown
    # 完成后的 VM 是否保留（评分失败/阻断时不销毁候选）
    preserve_completed_vm = False
    try:
        log.info("resetting env for %s (VM boot + task setup; takes minutes) ...",
                 args.task_id)
        t0 = time.time()
        # 重置环境：VM 启动 + 任务 setup（耗时数分钟）
        de.reset(task_config=task_config)
        boot = time.time() - t0
        log.info("env ready in %.0fs", boot)
        # 包装 VM 句柄供 Agent 使用
        vm = VM(de)

        quarantined_evaluators = []
        if getattr(cfg, "actor_evaluator_isolation", False):
            from benchmarks.osworld.actor_boundary import (                 # noqa: E402
                quarantine_actor_visible_evaluators,
            )
            # Actor 工作开始前隔离 guest 内可见的评审器源文件（有效性边界）
            quarantined_evaluators = quarantine_actor_visible_evaluators(vm)
            log.info(
                "quarantined %d guest-visible evaluator source file(s) before "
                "Actor work", len(quarantined_evaluators))

        if args.smoke:
            # 仅做基础设施冒烟检查，不启动 Agent
            passed = smoke(vm)
            print("SMOKE", "PASS" if passed else "FAIL")
            return 0 if passed else 1

        # 结果目录名：seed<S> 或 seed<S>_<tag>（A/B 分臂）
        _seed_dir = f"seed{args.seed}" + (f"_{args.tag}" if args.tag else "")
        sink = ArtifactSink(os.path.join(RSIAGENT_ROOT, "results", args.task_id, _seed_dir))
        reset_provider_counts()
        print(f"\n=== {args.task_id} seed{args.seed} model={cfg.model} ===\n"
              f"{instruction}\n")
        opening_extra = ""
        if getattr(cfg, "env_memory_dir", ""):
            # P2 eval flag: FROZEN practice memory rides along read-only —
            # synced IN, never synced out (write-lock by construction). Only a
            # mechanical recursive file inventory is shown. Retrieval is entirely
            # the Actor Agent's decision; no body or filename (including INDEX.md)
            # receives privileged treatment.
            # （中文）P2 评估开关：冻结的练习记忆只读随行——只同步进、绝不同步出
            # （写锁由构造保证）；仅展示机械的递归文件清单，检索完全由 Actor 决定，
            # 任何文件内容或文件名（含 INDEX.md）都不享受特权。
            from explore.commit import push_memory       # noqa: E402 (our code,
            from explore.charter import memory_preamble  # no benchmark contact)
            if push_memory(vm, cfg.env_memory_dir):
                # 机械递归清点快照内的全部文件（仅文件名+大小清单）
                _files = sorted(
                    os.path.relpath(os.path.join(root, name), cfg.env_memory_dir)
                    for root, _dirs, names in os.walk(cfg.env_memory_dir)
                    for name in names
                    if os.path.isfile(os.path.join(root, name)))
                _ls = "\n".join(
                    f"  {os.path.getsize(os.path.join(cfg.env_memory_dir, f)):>7}  {f}"
                    for f in _files)
                # 检索完全由 Actor 自行决定，开头只附清单前导
                opening_extra = memory_preamble(_ls)
                log.info("env memory attached (%s, files %d)", cfg.env_memory_dir, len(_files))
            else:
                # 推送失败：按“无记忆”继续运行
                log.warning("env memory push FAILED — running memory-OFF")
        # 任务用户（user simulator）应答通道；无模拟器时为 None
        ask_user = (de.user_simulator.respond
                    if de.user_simulator is not None else None)
        try:
            # 运行主循环（可断点续跑）；官方评审只在循环结束后进行
            res, history = run_with_resume(
                instruction, vm, cfg, sink, opening_extra=opening_extra,
                ask_user=ask_user)
        except AgenticVerifierNoProgressError as blocked:
            # The persistent Verifier may still own a rollback checkpoint and
            # private scratch on this VM. Keep that complete recovery surface;
            # an operator must restore/review the boundary before continuing.
            # （中文）持久 Verifier 可能仍持有本 VM 的回滚检查点与私有暂存；
            # 保留完整恢复面，运维须先恢复/审查边界才能继续。
            preserve_completed_vm = True
            container = getattr(getattr(de, "provider", None), "container", None)
            try:
                # 持久化阻断现场（容器/控制器/执行模式/恢复要求）
                sink.save_recovery({
                    "status": "blocked", "stage": "agentic_verifier",
                    "reason": str(blocked), "vm_preserved": True,
                    "container_id": getattr(container, "id", None),
                    "controller_url": de.controller.http_server,
                    "verifier_execution_mode": verifier_execution_mode,
                    "recovery_requires_verifier_boundary_review": True,
                    "verifier_root": os.path.join(sink.root, "verifier_agent"),
                    "memory": memory_record,
                    "official_evaluator_calls": 0,
                })
            except OSError as state_error:
                log.warning("could not persist blocked verification: %s", state_error)
            log.error("%s; VM retained for Verifier recovery; scoring remains sealed",
                      blocked)
            # 以 75 退出（保留 VM，评分保持封印）
            raise SystemExit(75) from blocked
        # 保存完整 transcript（系统提示 + 全部消息）
        sink.save_transcript(
            build_system(cfg, ask_enabled=ask_user is not None), history)
        preserve_completed_vm = True

        # Save everything needed to diagnose/regrade a completed candidate before
        # entering the sealed evaluator. This is never returned to either Agent.
        # （中文）在进入封印评审器之前保存诊断/复评所需的一切；绝不回传给任何 Agent。
        container = getattr(getattr(de, "provider", None), "container", None)
        # 评分检查点（grading checkpoint）落盘
        sink._json(os.path.join(sink.root, "grading_checkpoint.json"), {
            "task": args.task_id, "seed": args.seed, "tag": args.tag,
            "model": cfg.model, "agent_result": asdict(res),
            "llm_providers": provider_counts(), "memory": memory_record,
            "benchmark_lock": benchmark_lock_record,
            "boot_secs": round(boot, 1),
            "container_id": getattr(container, "id", None),
            "controller_url": de.controller.http_server,
            "evaluator_correction_requested": args.evaluator_correction,
            "actor_visible_evaluator_files_quarantined": len(quarantined_evaluators),
            "official_evaluator_feedback_entered_agent": False,
        })

        # SEALED: grading starts only after both Agents have terminated. Evaluator
        # transport/configuration failures must never vaporize the completed VM and
        # force a stochastic Actor rerun, so the exact same state remains alive until
        # grading succeeds or an operator explicitly interrupts the process.
        # （中文）封印评分：两个 Agent 都终止后才开始；评审传输/配置故障绝不蒸发
        # 已完成的 VM 去逼一次随机重跑，同一状态存活到评分成功或运维显式中断。
        try:
            # 评审重试间隔（外层变量优先，其次评审模型变量，缺省 30 秒）
            grade_delay = max(0.0, float(os.environ.get(
                "OSWORLD_EVAL_OUTER_RETRY_DELAY",
                os.environ.get("OSWORLD_EVAL_MODEL_RETRY_DELAY", "30"))))
        except (TypeError, ValueError):
            # 非法数值：回退默认
            grade_delay = 30.0
        grade_failures = 0

        def _grade_error(attempt, error):
            nonlocal grade_failures
            # 记录失败尝试序号
            grade_failures = attempt
            log.warning(
                "evaluate() attempt %d failed; completed VM preserved: %s",
                attempt, error)
            try:
                # 持久化“待重试”状态
                sink.save_grading_state({
                    "status": "pending",
                    "attempt": attempt,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "retry_delay_seconds": grade_delay,
                })
            except Exception as state_error:  # noqa: BLE001
                # Even a full/temporarily unavailable artifact disk must not turn a
                # recoverable grader outage into candidate destruction.
                # （中文）工件盘满/暂不可用也不能把可恢复的评分故障变成候选销毁。
                log.warning("could not persist grading retry state: %s", state_error)

        def _sealed_evaluate():
            # 封印评审操作本体：调用 OSWorld 官方 evaluate()
            return de.evaluate()

        evaluator_correction_record = None
        if args.evaluator_correction:
            from benchmarks.osworld.evaluator_corrections import apply_evaluator_correction
            # 应用显式版本化的评审器修正（仅限指定任务）
            evaluator_correction_record = apply_evaluator_correction(
                task_config, args.evaluator_correction)

        try:
            # 反复尝试封印评分直至成功（瞬态故障无上限）
            r = run_until_success(
                _sealed_evaluate, on_error=_grade_error, retry_delay=grade_delay)
        except GradingBlocked as blocked:
            # 评分被阻断：保留已完成候选等待运维恢复
            preserve_completed_vm = True
            sink.save_grading_state({
                "status": "blocked", "attempt": blocked.attempt,
                "error_type": type(blocked.error).__name__,
                "error": str(blocked.error),
                "candidate_preserved": True,
                "container_id": getattr(container, "id", None),
            })
            log.error("%s; completed candidate preserved for operator recovery", blocked)
            raise SystemExit(75) from blocked
        # 评分完成：记录重试统计
        sink.save_grading_state({
            "status": "complete", "failed_attempts": grade_failures})
        # 从评审结果中提取分数
        score = r.get("score", r) if isinstance(r, dict) else r
        # 汇总最终结果（评分与运行统计；确认官方评审反馈从未进入 Agent）
        payload = {"task": args.task_id, "seed": args.seed, "tag": args.tag,
                   "model": cfg.model,
                   "score": float(score), "status": res.status, "iters": res.iters,
                   "turns": res.turns, "programs_run": res.programs_run,
                   "asks": res.asks,
                   "checks_passed": res.checks_passed,
                   "inspections": res.inspections, "resumes": res.resumes,
                   "llm_providers": provider_counts(),
                   "wall_secs": round(res.wall_secs, 1),
                   "infra_pause_secs": round(res.infra_pause_secs, 1),
                   "infra_pauses": res.infra_pauses,
                   "boot_secs": round(boot, 1),
                   "official_evaluator_successful_calls": 1,
                   "official_evaluator_failed_attempts": grade_failures,
                   "official_evaluator_attempts": grade_failures + 1,
                   "memory": memory_record,
                   "benchmark_lock": benchmark_lock_record,
                   "evaluator_correction": evaluator_correction_record,
                   "actor_visible_evaluator_files_quarantined":
                       len(quarantined_evaluators),
                   "official_evaluator_feedback_entered_agent": False}
        # 结果落盘
        sink.save_result(payload)
        # 评分完成后 VM 不再保留
        preserve_completed_vm = False
        print("\n=== RESULT " + json.dumps(payload))
    finally:
        if not preserve_completed_vm:
            try:
                # 只在无需保留时释放环境（关闭容器/VM）
                de.close()
            except Exception:   # noqa: BLE001
                pass


if __name__ == "__main__":
    # 入口：以 main() 的返回码作为进程退出码
    raise SystemExit(main())
