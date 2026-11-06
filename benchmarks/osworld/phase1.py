#!/usr/bin/env python3
"""Run grader-free distribution-guided capability exploration.

This is Phase 1 of the recursive self-improvement protocol. It boots only
null-task OSWorld desktops, gives a persistent Curriculum Agent an explicitly
supplied task-distribution description, and runs fresh Actor Agent -> Verifier
Agent projects. In parallel-wave mode the Curriculum Agent authors each wave,
all branches read one pre-wave memory snapshot, and Actor-owned memory writes are
serialized afterward. The official benchmark task loader and evaluator are
never imported here.

The external project budget counts complete project/memory lifecycles.  It does
not constrain an Agent turn, prescribe a curriculum, or stand for convergence.
The Curriculum Agent may independently declare SATURATED earlier.  Exact memory
checkpoints are saved without a byte or file-count ceiling.
"""
# 【中文注释·模块定位】Phase 1：BRS（Broad Recursive Self-exploration，广度递归自我探索）
# 的命令行入口。只启动"空任务"（null-task）OSWorld 桌面，把显式给定的任务分布描述交给
# 持久上下文的 Curriculum Agent，再运行全新的 Actor Agent→Verifier Agent 项目。
# 并行波次模式下由 Curriculum 逐波出题：所有分支读取同一份波前记忆快照，Actor 侧的
# 记忆写入随后串行化合并。本文件绝不导入官方任务加载器与官方评测器（无评测器学习）。
# 外部项目预算只统计完整的"项目/记忆生命周期"，不约束智能体回合、不规定课程内容、
# 不代表收敛；Curriculum 可提前自行宣布 SATURATED；记忆检查点保存无字节/数量上限。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值，兼容新类型语法。

import argparse
# 导入 argparse：解析命令行参数。
import dataclasses
# 导入 dataclasses：把配置 dataclass 转成 dict 供清单落盘。
import hashlib
# 导入 hashlib：计算分布文件/语料/配置的 sha256 指纹。
import json
# 导入 json：清单、预检报告与事件序列化。
import os
# 导入 os：路径环境、进程内环境变量与原子替换、fsync。
from pathlib import Path
# 导入 Path：统一的路径对象处理。
import re
# 导入 re：编译"安全目录名"白名单正则。
import signal
# 导入 signal：注册 SIGTERM 处理，保证被终止时以约定退出码退出。
import sys
# 导入 sys：注入 sys.path 并以退出码结束进程。
import time
# 导入 time：事件时间戳（UTC）。
from typing import Any
# 导入 Any：动态结构（事件负载等）的类型标注。

from config.runtime_paths import (
    normalize_verifier_config_paths,
    resolve_path,
    resolve_root,
    resolve_osworld_root,
)
# 【中文注释】导入运行时路径工具：仓库根/OSWorld 根解析、配置路径解析、
# 以及 Verifier 配置路径归一化（把相对路径绑到仓库根上）。


RSIAGENT_ROOT = resolve_root()
# 解析 RSIAgent 仓库根目录（所有结果与配置都相对它定位）。
OSWORLD_ROOT = resolve_osworld_root(repo_root=RSIAGENT_ROOT)
# 解析 OSWorld-V2 检出目录（运行时需 chdir 进去）。
RESULTS_ROOT = RSIAGENT_ROOT / "results" / "recursive_improvement"
# 递归自我改进协议全部运行的公共结果根目录。
DEFAULT_ACTOR_CONFIG = RSIAGENT_ROOT / "config/roles/actor.yaml"
# 默认 Actor Agent 角色配置。
DEFAULT_VERIFIER_CONFIG = RSIAGENT_ROOT / "config/roles/practice_verifier.yaml"
# 默认练习 Verifier Agent 角色配置。
DEFAULT_VERIFIER_CONTROL_CONFIG = (
    RSIAGENT_ROOT / "config/roles/target.yaml")
# 默认 Agentic Verifier 控制面配置（rollback_mirror 等隔离策略的载体）。
DEFAULT_CURRICULUM_CONFIG = RSIAGENT_ROOT / "config/roles/curriculum.yaml"
# 默认 Curriculum Agent 角色配置。
DEFAULT_MEMORY_CONFIG = RSIAGENT_ROOT / "config/roles/actor.yaml"
# 默认记忆写入角色配置（必须与 Actor 同模型，故默认复用 actor.yaml）。
DEFAULT_CORPUS = RSIAGENT_ROOT / "results/explore/corpus_shingles.json"
# 默认指令语料（shingle 指纹库），用于查重与分布引导。
EXECUTE_ACK = "RUN-PHASE1-DISTRIBUTION-EXPLORATION"
# 付费执行确认口令：--execute 必须逐字等于它才真正开跑（防误触发）。
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
# 安全目录名白名单：仅字母/数字/下划线/点/连字符，防路径注入。


signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
# 收到 SIGTERM（如编排器超时杀进程）时以 143 退出，让外层把它识别为基础设施停止而非崩溃。


def _sha256(path: Path) -> str:
    # 【中文注释】分块计算文件 sha256：按 1MB 流式读取，避免大文件占内存。
    digest = hashlib.sha256()
    # 初始化 sha256 哈希对象。
    with path.open("rb") as handle:
        # 二进制只读打开目标文件。
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            # 每次读 1MB，直到文件结束（空字节串）。
            digest.update(chunk)
            # 滚动喂入哈希。
    return digest.hexdigest()
    # 返回十六进制摘要。


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    # 【中文注释】原子写 JSON：先写临时文件再 os.replace 原子改名，避免读到半截结果。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保目标父目录存在。
    temporary = path.with_name(path.name + ".tmp")
    # 同目录临时文件（同文件系统才能原子改名）。
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    # 以确定性的键序写入 JSON（ensure_ascii=False 保留中文可读性）。
    os.replace(temporary, path)
    # 原子替换成正式文件：要么旧内容要么新内容，绝不出现半写状态。


def _append_event(path: Path, event_type: str, *, status: str,
                  state: dict[str, Any], payload: dict[str, Any]) -> None:
    # 【中文注释】追加一条事件到 events.jsonl：带 UTC 时间戳、事件类型、状态、
    # 状态快照与负载；写入后 flush+fsync 保证崩溃后事件不丢（审计链完整性）。
    # 组装单条事件记录：timestamp_utc=UTC 时间戳；event_type=事件类型名；
    # status=当前状态标签；state=状态快照（如周期号、身份等）；payload=事件负载数据。
    record = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": event_type,
        "status": status,
        "state": state,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        # 以追加模式打开事件日志。
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        # 写一行 JSON（JSONL 格式）。
        handle.flush()
        # 冲刷 Python 缓冲。
        os.fsync(handle.fileno())
        # fsync 落盘，保证宕机后事件仍可审计。


def _parse_checkpoints(value: str, budget: int) -> tuple[int, ...]:
    # 【中文注释】解析并校验"记忆检查点"参数：逗号分隔整数，去重排序，
    # 且必须都落在 [0, 项目预算] 区间内。
    try:
        checkpoints = tuple(sorted(set(
            int(item.strip()) for item in value.split(",") if item.strip())))
        # 按逗号切分、忽略空段、转整数、去重并排序成元组。
    except ValueError as exc:
        # 任一段不是整数即参数格式错误。
        raise argparse.ArgumentTypeError(
            "checkpoints must be comma-separated integers") from exc
    if any(index < 0 or index > budget for index in checkpoints):
        # 越界（负数或超过预算）即拒绝：检查点语义是"第 N 个项目完成时保存记忆"。
        raise argparse.ArgumentTypeError(
            "checkpoints must lie between zero and the project budget")
    return checkpoints
    # 返回校验后的检查点元组。


def _safe_phase_root(name: str) -> Path:
    # 【中文注释】把 --run-name 约束为单个安全 basename，并确认解析后的
    # phase1 结果根没有逃出 results/recursive_improvement（防目录穿越）。
    if not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        # 不匹配白名单或恰好是 "."/".." 都拒绝。
        raise ValueError("--run-name must be one safe basename")
    root = (RESULTS_ROOT / name / "phase1").resolve()
    # 拼出本 run 的 phase1 结果根并解析为绝对路径。
    if root.parent.parent != RESULTS_ROOT.resolve():
        # 二级父目录必须正好是 RESULTS_ROOT，否则说明名字里混入了路径成分。
        raise ValueError("Phase-1 result root escapes results/recursive_improvement")
    return root
    # 返回通过校验的 phase1 结果根。


def _resolve_input_path(value: str, *, directory: bool = False) -> Path:
    # 【中文注释】解析输入路径：相对路径以仓库根为基；拒绝符号链接（防指向外部）；
    # 必须 strict 解析成功且类型匹配（目录或文件）。
    raw = Path(value).expanduser()
    # 展开 ~ 等用户目录缩写。
    candidate = raw if raw.is_absolute() else RSIAGENT_ROOT / raw
    # 绝对路径直接用；相对路径相对仓库根。
    if candidate.is_symlink():
        # 输入路径不允许是符号链接：防止把输入偷换成受控外部目标。
        raise RuntimeError(f"input path may not be a symlink: {candidate}")
    path = candidate.resolve(strict=True)
    # strict 解析：路径必须真实存在。
    expected = path.is_dir() if directory else path.is_file()
    # 按 directory 参数决定期望类型是目录还是文件。
    if not expected:
        # 类型不符即拒绝。
        kind = "directory" if directory else "file"
        raise RuntimeError(f"input path is not one {kind}: {path}")
    return path
    # 返回解析并校验后的绝对路径。


def _install_paths() -> None:
    # 【中文注释】安装运行时环境：校验 OSWorld 检出存在、加载其 .env（不覆盖已有变量）、
    # 把两个仓库根注入 sys.path，并 chdir 到 OSWorld 根（其内部代码以相对路径自居）。
    if not OSWORLD_ROOT.is_dir():
        # OSWorld-V2 检出缺失则无法运行。
        raise RuntimeError(f"OSWorld-V2 checkout is missing: {OSWORLD_ROOT}")
    try:
        import dotenv
        # 延迟导入 dotenv（OSWorld 侧依赖）。
        dotenv.load_dotenv(OSWORLD_ROOT / ".env", override=False)
        # 加载 OSWorld 的 .env；override=False 保证已有环境变量（如外部注入的密钥）优先。
    except ImportError:
        # 没有 dotenv 也不阻塞：密钥可完全来自外部环境。
        pass
    for path in (str(OSWORLD_ROOT), str(RSIAGENT_ROOT)):
        # 依次注入两个仓库根。
        if path not in sys.path:
            # 避免重复插入。
            sys.path.insert(0, path)
            # 插到最前，保证本地检出优先于任何已安装副本。
    os.chdir(OSWORLD_ROOT)
    # 切换工作目录到 OSWorld 根：任务加载器按相对路径找 evaluation_examples。


def _load_configs(args) -> tuple[dict[str, Any], dict[str, Path]]:
    # 【中文注释】加载并逐项校验四个角色的配置：任何偏离 Phase-1 协议画像的配置
    # 都直接报错（防配置漂移破坏隔离边界与实验设计）。
    from config.settings import load
    # 延迟导入配置加载器（需先 _install_paths 注入 sys.path）。

    # 汇总各角色配置文件路径（相对路径均以仓库根为基）：actor=Actor Agent 配置；
    # verifier=练习 Verifier Agent 配置；verifier_control=Agentic Verifier 控制面配置；
    # curriculum=Curriculum Agent 配置；memory_actor=记忆写入角色配置。
    paths = {
        "actor": resolve_path(args.actor_config, repo_root=RSIAGENT_ROOT),
        "verifier": resolve_path(
            args.verifier_config, repo_root=RSIAGENT_ROOT),
        "verifier_control": resolve_path(
            args.verifier_control_config, repo_root=RSIAGENT_ROOT),
        "curriculum": resolve_path(
            args.curriculum_config, repo_root=RSIAGENT_ROOT),
        "memory_actor": resolve_path(
            args.memory_config, repo_root=RSIAGENT_ROOT),
    }
    configs = {role: load(str(path)) for role, path in paths.items()}
    # 逐角色加载配置对象。
    for cfg in configs.values():
        # 对每个配置做路径归一化。
        normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
        # 把配置里的相对路径绑定到仓库根，保证子进程语义一致。
    for role in ("actor", "verifier", "curriculum", "memory_actor"):
        # 校验四个学习侧角色都必须是"Agent 自有练习模式"。
        cfg = configs[role]
        # 取当前角色配置。
        if (not cfg.agent_decided_stop or not cfg.practice_mode
                or cfg.independent_verify or cfg.max_resumes != 0):
            # 停止必须 Agent 自有、练习模式开启、不得独立验证、不得自动续跑。
            raise RuntimeError(
                f"Phase-1 {role} must use Agent-owned practice mode")
    if configs["actor"].model != "z-ai/glm-5.3":
        # Phase-1 的 Actor 必须是 GLM-5.3（协议钉死模型身份）。
        raise RuntimeError("Phase-1 Actor Agent must be GLM-5.3")
    if configs["memory_actor"].model != configs["actor"].model:
        # 记忆写入必须延续同一 Actor 模型（记忆归属一致性）。
        raise RuntimeError(
            "memory writing must continue with the same Actor Agent model")
    if configs["verifier"].model != "moonshotai/kimi-k3":
        # Phase-1 的 Verifier 必须是 Kimi K3。
        raise RuntimeError("Phase-1 Verifier Agent must be Kimi K3")
    if configs["curriculum"].model != "moonshotai/kimi-k3":
        # Curriculum 同样必须是 Kimi K3。
        raise RuntimeError("Phase-1 Curriculum Agent must be Kimi K3")
    control = configs["verifier_control"]
    # 取 Agentic Verifier 控制面配置。
    if (control.verifier_model != "moonshotai/kimi-k3"
            or not control.independent_verify
            or control.verifier_stage_lifecycle
            or control.verifier_execution_mode != "rollback_mirror"
            or not control.verifier_hide_actor_memory):
        # 控制面画像：Kimi K3、独立验证、非阶段式生命周期、回滚镜像隔离、
        # 对 Actor 记忆隐藏——任一漂移即拒绝。
        raise RuntimeError(
            "Phase-1 candidate-only Agentic Verifier control profile drifted")
    return configs, paths
    # 返回（配置对象表, 配置文件路径表）。


def _parser() -> argparse.ArgumentParser:
    # 【中文注释】构造命令行参数解析器：预检/执行互斥，执行必须带确认口令。
    parser = argparse.ArgumentParser(description=__doc__)
    # 用模块 docstring 作为帮助文本。
    parser.add_argument("--run-name", required=True)
    # 必填：本次运行的安全命名（决定结果目录）。
    parser.add_argument(
        "--distribution-file", required=True,
        help=("UTF-8 distribution description, or exact query when "
              "--target-query-conditioned is set"))
    # 必填：UTF-8 任务分布描述文件；在 target-query-conditioned 模式下改为精确查询。
    parser.add_argument(
        "--target-query-conditioned", action="store_true",
        help=("derive diverse Phase-1 exploration from the supplied exact query; "
              "the unchanged target itself remains reserved for Phase 2"))
    # 开关：从给定精确查询派生多样的 Phase-1 探索；原目标本身留给 Phase 2。
    parser.add_argument("--project-budget", type=int, default=8)
    # 外部项目预算：完整项目/记忆生命周期的数量上限（默认 8）。
    parser.add_argument("--checkpoints", default="0,2,4,8")
    # 记忆检查点列表（默认 0,2,4,8 个项目完成时各存一份）。
    parser.add_argument(
        "--parallel-waves", action="store_true",
        help=("let the persistent Curriculum Agent author adaptive project "
              "waves instead of one project per outer turn"))
    # 开关：启用并行波次——由持久 Curriculum 逐波自适应出题，而非每轮一个项目。
    parser.add_argument(
        "--max-parallel", type=int, default=4,
        help=("host scheduling capacity for a Curriculum-authored wave; this "
              "does not prescribe or truncate the wave width"))
    # 并行波次的宿主调度容量：只是并发执行上限，不规定也不截断波宽（波宽归 Curriculum 所有）。
    parser.add_argument("--actor-config", default=str(DEFAULT_ACTOR_CONFIG))
    # Actor 配置路径（默认见常量）。
    parser.add_argument("--verifier-config", default=str(DEFAULT_VERIFIER_CONFIG))
    # 练习 Verifier 配置路径。
    parser.add_argument(
        "--verifier-control-config",
        default=str(DEFAULT_VERIFIER_CONTROL_CONFIG))
    # Agentic Verifier 控制面配置路径。
    parser.add_argument(
        "--curriculum-config", default=str(DEFAULT_CURRICULUM_CONFIG))
    # Curriculum 配置路径。
    parser.add_argument("--memory-config", default=str(DEFAULT_MEMORY_CONFIG))
    # 记忆角色配置路径。
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    # 指令语料路径。
    parser.add_argument(
        "--resume-completed-boundary", action="store_true",
        help=("resume an infrastructure-stopped lineage only when every "
              "accepted project is durably closed and the persistent "
              "Curriculum transcript is intact"))
    # 开关：仅当每个已接受项目都持久关闭、持久 Curriculum 转录完整时，
    # 才允许恢复一条因基础设施停止的血脉（lineage）。
    action = parser.add_mutually_exclusive_group(required=True)
    # 预检与执行二选一，必选其一。
    action.add_argument("--preflight", action="store_true")
    # 预检模式：只打印计划与校验结果，不启动任何 VM/模型。
    action.add_argument("--execute", default="", help=EXECUTE_ACK)
    # 执行模式：值必须等于确认口令。
    return parser
    # 返回配置好的解析器。


def main(argv: list[str] | None = None) -> int:
    # 【中文注释】主流程：解析参数→校验输入→装配配置→写清单→运行 BRS 探索→
    # 冻结最终记忆→写结果 JSON。预检模式在装配完成后即返回。
    args = _parser().parse_args(argv)
    # 解析命令行参数。
    _install_paths()
    # 安装运行时路径环境（sys.path 注入与 chdir）。
    if args.project_budget < 0:
        # 项目预算必须非负。
        raise RuntimeError("--project-budget must be nonnegative")
    if args.max_parallel < 1:
        # 并行容量至少为 1。
        raise RuntimeError("--max-parallel must be positive")
    checkpoints = _parse_checkpoints(args.checkpoints, args.project_budget)
    # 解析并校验检查点列表。
    distribution_path = _resolve_input_path(args.distribution_file)
    # 解析并校验分布描述文件路径。
    distribution = distribution_path.read_text(encoding="utf-8").strip()
    # 读入分布描述并去首尾空白。
    if not distribution:
        # 空分布描述没有引导意义，拒绝。
        raise RuntimeError("distribution description is empty")
    corpus = _resolve_input_path(args.corpus)
    # 解析并校验指令语料路径。
    root = _safe_phase_root(args.run_name)
    # 校验并解析 phase1 结果根。
    configs, config_paths = _load_configs(args)
    # 加载并校验全部角色配置。
    from env.qemu_rollback import normalize_verifier_execution_mode
    # 延迟导入：归一化 Verifier 执行模式字符串。
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    # 延迟导入：Docker 提供者的"可检查点化"补丁安装器。
    verifier_execution_mode = normalize_verifier_execution_mode(
        configs["verifier_control"].verifier_execution_mode)
    # 归一化执行模式（如 rollback_mirror）。
    # 按执行模式给后续创建的 Docker 客体打可检查点化补丁（仅启动档案改变）。
    prepare_checkpointable_docker_provider(verifier_execution_mode)

    # 组装预检报告：声明本阶段全部协议边界（供人工/编排器审阅）。字段含义：
    # status=就绪状态；phase=阶段名（分布引导的能力探索）；study_design=研究设计
    # （查询条件化适配 target_conditioned_adaptation，或留出泛化 held_out_generalization）；
    # result_root=结果根目录；project_budget=外部项目预算；memory_checkpoints=检查点列表；
    # parallel_waves/max_parallel=是否并行波次与宿主并行容量（非波次模式恒为 1）；
    # wave_width_agent_owned=波宽由 Agent（Curriculum）所有；
    # branch_memory_snapshot_isolated=各分支读取隔离的波前记忆快照；
    # memory_writes_serialized=分支记忆写入事后串行化合并；
    # curriculum_may_stop_early/curriculum_terminal_token=Curriculum 可提前停止，
    # 终止令牌为 SATURATED；actor_agent/verifier_agent/curriculum_agent=各智能体模型身份；
    # verifier_execution_mode=Verifier 执行隔离模式；verifier_candidate_surface=Verifier
    # 看到真实候选但 Actor 私有产物被隐藏；verifier_pre_actor_orientation=Phase-1 不做
    # Verifier pre-Actor 定向（与 Phase-2 目标验证不同）；
    # official_evaluator_imported/official_evaluator_calls=官方评测器未被导入、调用恒为 0
    # （BRS 无评测器）；distribution_sha256=分布描述文件的内容指纹（锁定输入）。
    preflight = {
        "status": "ready",
        "phase": "distribution_guided_capability_exploration",
        "study_design": (
            "target_conditioned_adaptation" if args.target_query_conditioned
            else "held_out_generalization"),
        "result_root": str(root),
        "project_budget": args.project_budget,
        "memory_checkpoints": list(checkpoints),
        "parallel_waves": args.parallel_waves,
        "max_parallel": args.max_parallel if args.parallel_waves else 1,
        "wave_width_agent_owned": args.parallel_waves,
        "branch_memory_snapshot_isolated": args.parallel_waves,
        "memory_writes_serialized": args.parallel_waves,
        "curriculum_may_stop_early": True,
        "curriculum_terminal_token": "SATURATED",
        "actor_agent": configs["actor"].model,
        "verifier_agent": configs["verifier"].model,
        "verifier_execution_mode": verifier_execution_mode,
        "verifier_candidate_surface":
            "actual_candidate_with_actor_private_artifacts_hidden",
        "verifier_pre_actor_orientation": False,
        "curriculum_agent": configs["curriculum"].model,
        "official_evaluator_imported": False,
        "official_evaluator_calls": 0,
        "distribution_sha256": _sha256(distribution_path),
    }
    if args.preflight:
        # 预检模式：打印报告即返回，不产生任何副作用。
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    if args.execute != EXECUTE_ACK:
        # 执行口令必须逐字匹配（付费动作的显式确认）。
        raise RuntimeError(f"--execute must equal {EXECUTE_ACK}")
    if (root.exists() and any(root.iterdir())
            and not args.resume_completed_boundary):
        # 非恢复模式下结果根必须为空：防止覆盖既有血脉。
        raise RuntimeError("Phase-1 root is nonempty; preserve it and use a new name")
    if args.resume_completed_boundary and not root.is_dir():
        # 恢复模式则要求血脉目录真实存在。
        raise RuntimeError("Phase-1 resume root does not exist")
    root.mkdir(parents=True, exist_ok=True)
    # 创建结果根目录。
    events_path = root / "events.jsonl"
    # 事件日志路径。
    # 组装运行清单：预检内容 + 语料/配置指纹，锁定整个运行的输入。字段含义：
    # schema_version=清单模式版本；kind=清单种类（Phase-1）；**preflight=展开全部预检字段；
    # distribution_path=分布文件路径；corpus_sha256=语料指纹；configs=每个角色配置的
    # path（配置文件路径）、sha256（内容指纹）与 effective（生效配置的完整字段字典），
    # 按 sorted(configs) 遍历排序后的角色名。
    manifest = {
        "schema_version": 1,
        "kind": "recursive_self_improvement_phase1",
        **preflight,
        "distribution_path": str(distribution_path),
        "corpus_sha256": _sha256(corpus),
        "configs": {
            role: {
                "path": str(config_paths[role]),
                "sha256": _sha256(config_paths[role]),
                "effective": dataclasses.asdict(configs[role]),
            }
            for role in sorted(configs)
        },
    }
    # Persistence turns dataclass tuples into JSON arrays.  Compare the same
    # canonical JSON value on resume so tuple/list representation is not
    # mistaken for protocol drift, while every actual field and hash remains
    # locked.
    # 【中文注释】持久化会把 dataclass 中的 tuple 变成 JSON 数组；这里把清单先序列化
    # 再反序列化一次，使恢复比对时 tuple/list 的表示差异不被误判为协议漂移，
    # 而所有实际字段与哈希仍保持锁定。
    manifest = json.loads(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True))
    # 规范化清单：经 JSON 往返，消除 Python 对象与 JSON 表示的差异。
    manifest_path = root / "manifest.json"
    # 清单落盘路径。
    if args.resume_completed_boundary:
        # 恢复模式：读取既有清单并与当前重建的清单严格比对。
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"))
            # 读入既有清单。
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            # 读不到或解析不了即无法安全恢复。
            raise RuntimeError("Phase-1 resume manifest is unreadable") from exc
        if existing_manifest != manifest:
            # 任何字段漂移都拒绝恢复（协议与配置必须逐字节一致）。
            raise RuntimeError(
                "Phase-1 resume protocol/config manifest drifted")
    else:
        # 新运行：原子写入清单。
        _write_json_atomic(manifest_path, manifest)

    from desktop_env.desktop_env import DesktopEnv
    # 延迟导入 OSWorld 的桌面环境类。
    from env.vm import VM
    # 延迟导入本项目对 VM 的适配封装。
    from explore.practice_loop import (
        PracticeHooks,
        _atomic_install_memory,
        _manifest,
        _read_memory_tree,
        evolve_practice,
    )
    # 【中文注释】延迟导入练习环（DRS 骨架模块）的公共设施：
    # 钩子集、记忆原子安装、记忆清单、记忆树读取，以及串行版 Phase-1 主循环 evolve_practice。
    from explore.target_learning import _memory_tree_sha256
    # 延迟导入：计算记忆树整体 sha256。

    def vm_factory():
        # 【中文注释】VM 工厂：每次调用启动一个全新"空任务"Docker 桌面（无任务配置）。
        desktop = DesktopEnv(
            provider_name="docker", action_space="pyautogui", os_type="Ubuntu",
            screen_size=(1920, 1080), headless=True, require_a11y_tree=False,
            volume_size=60)
        # 60GB 卷、1920x1080 无头 Ubuntu、pyautogui 动作空间、不请求无障碍树。
        try:
            desktop.reset(task_config=None)
            # 以 None 任务配置 reset：得到纯桌面（BRS 只探索，不加载官方任务）。
            return desktop, VM(desktop)
            # 返回桌面环境与 VM 封装二元组。
        except Exception:
            # 启动失败时关闭已建环境，避免资源泄漏。
            desktop.close()
            raise

    if args.parallel_waves:
        # 并行波次模式：走 phase1_wave 的多分支并行实现。
        from explore.phase1_wave import evolve_parallel_phase1

        # 延迟导入并行波次主循环。
        # 运行 BRS 并行波次：Curriculum 逐波出题、分支并行 Actor→Verifier、记忆串行合并。
        # 参数：root/target_direction=结果根与目标方向（分布描述）；actor_cfg/curriculum_cfg/
        # memory_cfg/verifier_control_cfg=各角色配置；vm_factory/corpus_path/hooks=
        # VM 工厂、语料路径与默认钩子集；event_sink=事件汇（统一写入 events.jsonl）；
        # project_budget=外部项目预算；checkpoint_projects=记忆检查点；
        # max_parallel=宿主并行容量；target_query_conditioned=是否查询条件化探索；
        # resume_completed_boundary=是否启用完成边界恢复。
        result = evolve_parallel_phase1(
            root=str(root), target_direction=distribution,
            actor_cfg=configs["actor"],
            curriculum_cfg=configs["curriculum"],
            memory_cfg=configs["memory_actor"],
            verifier_control_cfg=configs["verifier_control"],
            vm_factory=vm_factory, corpus_path=str(corpus), hooks=PracticeHooks(),
            event_sink=lambda event_type, **kwargs: _append_event(
                events_path, event_type, **kwargs),
            project_budget=args.project_budget,
            checkpoint_projects=checkpoints,
            max_parallel=args.max_parallel,
            target_query_conditioned=args.target_query_conditioned,
            resume_completed_boundary=args.resume_completed_boundary)
    else:
        # 串行模式：一个外部回合一个项目。
        desktop, vm = vm_factory()
        # 先启动一个空任务桌面。
        try:
            # 运行串行版练习主循环（phase1_exploration=True 打开 BRS 语义）。
            # 参数：vm/str(root)/distribution=VM、结果根与目标方向；
            # 四个角色配置、corpus_path 语料与 PracticeHooks 默认钩子；
            # event_sink=事件汇（写入 events.jsonl）；project_budget=外部项目预算；
            # checkpoint_projects=记忆检查点；phase1_target_conditioned=是否查询条件化；
            # agentic_verifier_cfg=Agentic Verifier 控制面配置；
            # resume_completed_boundary=是否启用完成边界恢复。
            result = evolve_practice(
                vm, str(root), distribution,
                configs["actor"], configs["verifier"], configs["curriculum"],
                configs["memory_actor"], corpus_path=str(corpus), hooks=PracticeHooks(),
                event_sink=lambda event_type, **kwargs: _append_event(
                    events_path, event_type, **kwargs),
                project_budget=args.project_budget,
                checkpoint_projects=checkpoints,
                phase1_exploration=True,
                phase1_target_conditioned=args.target_query_conditioned,
                agentic_verifier_cfg=configs["verifier_control"],
                resume_completed_boundary=args.resume_completed_boundary)
        finally:
            # 无论成败都关闭桌面环境。
            desktop.close()

    memory = _read_memory_tree(result.memory_dir)
    # 读取主循环产出的最终记忆树。
    frozen = root / "memory_frozen"
    # 计算冻结记忆目录路径。
    _atomic_install_memory(str(frozen), memory)
    # 原子安装记忆树到冻结目录：作为 Phase-2 的不可变输入。
    # 组装 Phase-1 结果报告：schema_version=结果模式版本；phase=阶段名；
    # study_design=研究设计（与预检一致）；status=终止状态（saturated / budget_exhausted 等）；
    # projects=完成项目数；stop_reason=停止原因；project_budget/checkpoints=预算与检查点；
    # parallel_waves/max_parallel=是否并行波次与宿主并行容量；
    # memory_files/memory_bytes=冻结记忆文件数与总字节数；memory_manifest=记忆清单
    # （文件名→大小/哈希）；memory_tree_sha256=记忆树整体 sha256（Phase-2 沿此锁定血脉）；
    # memory_frozen_path=冻结记忆目录路径；official_evaluator_calls=官方评测器调用次数（恒为 0）。
    payload = {
        "schema_version": 1,
        "phase": "distribution_guided_capability_exploration",
        "study_design": (
            "target_conditioned_adaptation" if args.target_query_conditioned
            else "held_out_generalization"),
        "status": result.status,
        "projects": result.projects,
        "stop_reason": result.reason,
        "project_budget": args.project_budget,
        "checkpoints": list(checkpoints),
        "parallel_waves": args.parallel_waves,
        "max_parallel": args.max_parallel if args.parallel_waves else 1,
        "memory_files": len(memory),
        "memory_bytes": sum(len(data) for data in memory.values()),
        "memory_manifest": _manifest(memory),
        "memory_tree_sha256": _memory_tree_sha256(memory),
        "memory_frozen_path": str(frozen),
        "official_evaluator_calls": 0,
    }
    _write_json_atomic(root / "result.json", payload)
    # 原子写入结果 JSON。
    print("\n=== PHASE 1 RESULT " + json.dumps(payload, sort_keys=True))
    # 在标准输出打印结果摘要（供编排器采集）。
    return 0 if result.status in {"saturated", "budget_exhausted"} else 2
    # 语义终止（饱和或预算用尽）返回 0，其他返回 2。


if __name__ == "__main__":
    # 作为脚本运行时以 main 的返回值作为进程退出码。
    raise SystemExit(main())
