"""Run Phase 2 target-conditioned learning without official evaluation."""
# 【中文注释·模块定位】Phase 2（DRS：Deep Recursive Self-exploration，深度递归自我探索）
# 的命令行入口：在真实开发目标上做"结果接地"的定向学习，全程不接触官方评测。
# 目标尝试 → 持久上下文 Verifier 判定（PASS/FAIL）→ Agent 自有学习（蒸馏+整合）→
# 由 Curriculum 选择定向练习（自进化波次）→ 再试目标；官方评测器一次也不调用，
# 其反馈永不进入学习。停止策略（curriculum_review / verifier_pass）由协议钉死。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值。

import argparse
# 导入 argparse：解析命令行参数。
import dataclasses
# 导入 dataclasses：替换/序列化角色配置 dataclass。
import hashlib
# 导入 hashlib：指令、记忆树、报告等内容的 sha256 指纹。
import json
# 导入 json：清单、预检、事件与结果的序列化。
import logging
# 导入 logging：模块级日志器。
import os
# 导入 os：环境变量与原子文件替换。
import re
# 导入 re：安全目录名白名单正则。
import sys
# 导入 sys：检查当前异常（保留被阻断候选的 VM）。
import time
# 导入 time：计时（环境启动耗时等）。
from pathlib import Path
# 导入 Path：路径对象处理。
from typing import Any
# 导入 Any：动态结构的类型标注。

from benchmarks.osworld.runtime import (
    DEFAULT_BENCHMARK_PROFILE,
    OSWORLD_ROOT,
    RSIAGENT_ROOT,
    _append_event,
    _benchmark_provenance,
    _install_paths,
)
# 【中文注释】从基准运行时导入：默认基准 profile、OSWorld 根、仓库根、
# 事件追加、基准溯源构造与路径安装工具。
from config.runtime_paths import normalize_verifier_config_paths, resolve_path
# 导入配置路径工具：解析与归一化各角色配置路径。
from core.self_evolving_loop import DEFAULT_PHASE2_STOP_POLICY
# 导入 Phase-2 默认停止策略（curriculum_review）。

RECURSIVE_RESULTS_ROOT = RSIAGENT_ROOT / "results" / "recursive_improvement"
# 递归自我改进协议的公共结果根目录。

PHASE2_EXECUTE_ACK = "RUN-PHASE2-FAILURE-CONDITIONED-PRACTICE"
# 付费执行确认口令：--execute 必须逐字等于它才开跑。

ALL_TASKS = tuple((f"task_{index:03d}" for index in range(1, 109)))
# 全部 108 个任务 ID（task_001..task_108），限定 task_id 参数取值。

DEFAULT_PHASE2_TARGET_CONFIG = RSIAGENT_ROOT / "config/roles/target.yaml"
# 默认目标自进化（Actor/Verifier 控制面）配置。

DEFAULT_PRACTICE_ACTOR_CONFIG = RSIAGENT_ROOT / "config/roles/actor.yaml"
# 默认练习 Actor 配置。

DEFAULT_PRACTICE_VERIFIER_CONFIG = RSIAGENT_ROOT / "config/roles/verifier.yaml"
# 默认练习 Verifier 配置。

DEFAULT_CURRICULUM_CONFIG = RSIAGENT_ROOT / "config/roles/curriculum.yaml"
# 默认 Curriculum Agent 配置。

DEFAULT_MEMORY_CONFIG = DEFAULT_PRACTICE_ACTOR_CONFIG
# 默认记忆写入角色配置（与练习 Actor 同模型，故复用 actor.yaml）。

DEFAULT_CORPUS = RSIAGENT_ROOT / "results/explore/corpus_shingles.json"
# 默认指令语料（shingle 指纹库）路径。

_SAFE_NAME = re.compile("[A-Za-z0-9_.-]+\\Z")
# 安全目录名白名单：仅字母/数字/下划线/点/连字符。

log = logging.getLogger("rsiagent.self_evolving")
# 模块级日志器（自进化命名空间）。


def _release_target_vm(environment, emit):
    """Keep a blocked verification candidate available for explicit recovery."""
    # 【中文注释】释放目标 VM：正常情况直接关闭桌面；但若当前异常是
    # "Verifier 无进展"（AgenticVerifierNoProgressError，智能体侧被阻断而非语义结论），
    # 则保留该候选 VM 供基础设施层面的显式恢复审阅，不自动重试、不给语义判定。
    from core.verifier_runtime import AgenticVerifierNoProgressError
    # 延迟导入"无进展"异常类型。

    if isinstance(sys.exc_info()[1], AgenticVerifierNoProgressError):
        # 检查当前正在处理的异常是否为 Verifier 无进展守卫触发。
        emit(
            "TARGET_VM_PRESERVED_FOR_INFRASTRUCTURE_REVIEW",
            {
                "reason": "Verifier actionless guard; no semantic verdict",
                "automatic_retry": False,
            },
        )
        # 发出"保留 VM 供基础设施审阅"事件：原因是无动作守卫，不产生语义判定。
        return
        # 直接返回：不关闭桌面（保留现场）。
    environment.desktop.close()
    # 常规路径：关闭目标桌面环境，释放资源。


@dataclasses.dataclass
class _TargetEnvironment:
    # 【中文注释】一次目标尝试的环境快照容器：桌面与 VM 封装、任务指令、
    # 启动耗时、S0 表面基线（surface_baseline，任务开始状态指纹）以及
    # Verifier 私有路径集合（这些路径不进入指令文本）。
    desktop: Any
    # OSWorld DesktopEnv 实例。
    vm: Any
    # 本项目的 VM 适配封装。
    instruction: str
    # 目标任务指令文本。
    boot_secs: float
    # 环境启动耗时（秒）。
    surface_baseline: dict[str, str]
    # S0 表面基线（pre-Actor 任务开始状态的指纹字典）。
    verifier_private_paths: tuple[str, ...]
    # Verifier 私有路径（指令中未出现的敏感路径，从快照中排除）。


@dataclasses.dataclass
class _TargetVerifier:
    # 【中文注释】目标 Verifier 的周期状态：所属目标周期号、按身份索引的
    # 持久会话与配置、各身份的定向（orientation）报告指纹，以及任务开始身份指纹。
    cycle: int
    # 本 Verifier 绑定的目标周期号。
    sessions: dict[tuple[str, str], Any] = dataclasses.field(default_factory=dict)
    # 持久 Verifier 会话表：键为会话身份二元组。
    configs: dict[tuple[str, str], Any] = dataclasses.field(default_factory=dict)
    # 各会话身份对应的配置。
    orientation_report_sha256s: dict[tuple[str, str], str] = dataclasses.field(
        default_factory=dict
    )
    # 各会话身份的 orientation 报告 sha256（候选盲定向的凭证）。
    task_start_identity_sha256: str = ""
    # 任务开始身份（S0）的 sha256。


@dataclasses.dataclass
class _TargetActor:
    # 【中文注释】目标 Actor 的周期状态：当前记忆树（文件名→字节内容）与周期号。
    memory: dict[str, bytes]
    # Actor 携带的记忆树。
    cycle: int
    # 所属目标周期号（与 Verifier 周期配对校验用）。


@dataclasses.dataclass
class _TargetOutput:
    # 【中文注释】一次 Actor 工作回合的产物：主循环结果、完整历史、活跃历史、
    # 实际生效配置、工件根目录、实际使用的 Verifier 会话身份与定向报告指纹。
    loop_result: Any
    # run_with_resume 的返回结果对象。
    history: list[dict[str, Any]]
    # 完整对话历史。
    active_history: list[dict[str, Any]]
    # 实际活跃分支的历史（可能因升级切换而不同于完整历史）。
    active_cfg: Any
    # 实际生效的配置（可能是升级配置）。
    sink_root: str
    # 本周期工件（transcript 等）的根目录。
    active_verifier_identity: tuple[str, str]
    # 实际使用的 Verifier 会话身份。
    orientation_report_sha256: str
    # 该身份的定向报告 sha256（未启用阶段生命周期时为空串）。


def _sha256_file(path: Path) -> str:
    # 【中文注释】分块计算文件 sha256：按 1MB 流式读取。
    digest = hashlib.sha256()
    # 初始化哈希对象。
    with path.open("rb") as handle:
        # 二进制只读打开。
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            # 每次读 1MB 直到文件尾。
            digest.update(chunk)
            # 滚动喂入哈希。
    return digest.hexdigest()
    # 返回 hex 摘要。


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    # 【中文注释】原子写 JSON：临时文件 + os.replace，避免半写状态。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保父目录存在。
    temporary = path.with_name(path.name + ".tmp")
    # 同目录临时文件。
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # 确定性键序写入 JSON。
    os.replace(temporary, path)
    # 原子改名替换。


def _safe_phase2_result_root(protocol_run: str, task_id: str, seed: int) -> Path:
    # 【中文注释】校验并解析 Phase-2 结果根：血脉名必须是安全 basename、
    # 种子非负，且最终路径必须落在 recursive_improvement/<run>/phase2/<task>/ 之下。
    if not _SAFE_NAME.fullmatch(protocol_run) or protocol_run in {".", ".."}:
        # 血脉名不匹配白名单或为 "."/".." 即拒绝。
        raise ValueError("--protocol-run must be one safe basename")
    if seed < 0:
        # 种子必须非负。
        raise ValueError("--seed must be nonnegative")
    root = (
        RECURSIVE_RESULTS_ROOT
        / protocol_run
        / "phase2"
        / task_id
        / f"attempt_{seed:04d}"
    ).resolve()
    # 拼出"血脉/phase2/任务/attempt_种子"结果根并解析。
    expected = (RECURSIVE_RESULTS_ROOT / protocol_run / "phase2" / task_id).resolve()
    # 计算期望的父目录。
    if root.parent != expected:
        # 父目录不符说明路径逃逸（防目录穿越）。
        raise ValueError("Phase-2 result root escapes recursive_improvement")
    return root
    # 返回通过校验的结果根。


def _task_instruction(task: Any) -> str:
    """Read instance data before class attributes (dynamic credentials depend on it)."""
    # 【中文注释】读取任务指令：优先读实例级数据（动态凭据依赖实例读取顺序），
    # 再回落到类属性；找不到非空指令即报错。
    if hasattr(task, "get") and callable(task.get):
        # 任务配置若是映射式对象，先读其 "instruction" 键（实例数据优先）。
        value = task.get("instruction")
        if isinstance(value, str) and value.strip():
            # 非空字符串才算有效指令。
            return value
    value = getattr(task, "instruction", "")
    # 回落到对象属性 instruction。
    if not isinstance(value, str) or not value.strip():
        # 两种来源都拿不到非空指令即失败。
        raise RuntimeError("loaded task has no nonempty instruction")
    return value
    # 返回指令文本。


def _load_task(task_id: str) -> tuple[Any, str]:
    # 【中文注释】加载目标任务配置：用 OSWorld 官方任务加载器解析 v2 任务 JSON，
    # 并提取指令文本。
    from task_loader import load_task_config, resolve_task_json_path
    # 延迟导入 OSWorld 的任务加载工具（需先安装路径环境）。

    task_path = resolve_task_json_path(
        task_id=task_id, base_dir="evaluation_examples", eval_version="v2"
    )
    # 解析任务 JSON 文件路径（evaluation_examples，v2 版）。
    task = load_task_config(
        task_path, task_id=task_id, base_dir="evaluation_examples", eval_version="v2"
    )
    # 加载任务配置对象。
    return (task, _task_instruction(task))
    # 返回（任务配置, 指令文本）二元组。


def _stable_target_direction(task: Any, instruction: str) -> str:
    """Remove only per-run credentials while retaining task semantics verbatim."""
    # 【中文注释】计算"稳定目标方向"：只剥除每次运行会变的凭据占位，逐字保留
    # 任务语义——该方向是跨周期/跨恢复一致性校验的锚点，任何语义漂移都会被拒绝。
    from explore.commit import normalize_instruction_for_corpus
    # 延迟导入语料归一化工具（去除易变成分）。

    base = getattr(task, "base_instruction", "")
    # 取任务的基准指令（不含凭据实例化的部分）。
    semantic = base if isinstance(base, str) and base.strip() else instruction
    # 有基准指令就用它，否则用完整指令。
    direction = normalize_instruction_for_corpus(semantic).strip()
    # 归一化并去首尾空白，得到稳定方向。
    if not direction:
        # 稳定方向为空即失败。
        raise RuntimeError("stable target direction is empty")
    return direction
    # 返回稳定目标方向。


def _load_configs(args):
    # 【中文注释】加载并逐项校验五个角色的配置：目标配置必须完全匹配 Phase-2 的
    # 隔离画像（模型、独立验证、回滚镜像、隐藏记忆等），练习/课程/记忆角色必须是
    # Agent 自有练习模式，且模型身份满足钉死要求。
    from config.settings import load
    # 延迟导入配置加载器。

    paths = {
        "target_actor": resolve_path(args.target_config, repo_root=RSIAGENT_ROOT),
        "practice_actor": resolve_path(
            args.practice_actor_config, repo_root=RSIAGENT_ROOT
        ),
        "practice_verifier": resolve_path(
            args.practice_verifier_config, repo_root=RSIAGENT_ROOT
        ),
        "curriculum": resolve_path(args.curriculum_config, repo_root=RSIAGENT_ROOT),
        "memory_actor": resolve_path(args.memory_config, repo_root=RSIAGENT_ROOT),
    }
    # 汇总五个角色的配置路径：target_actor=目标自进化配置；practice_actor/practice_verifier
    # =练习侧 Actor/Verifier 配置；curriculum=课程配置；memory_actor=记忆角色配置。
    configs = {role: load(str(path)) for (role, path) in paths.items()}
    # 逐角色加载配置对象。
    for cfg in configs.values():
        # 对每个配置做路径归一化。
        normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
        # 相对路径绑定仓库根。
    target = configs["target_actor"]
    # 取目标自进化配置。
    required_target = {
        "model": "z-ai/glm-5.3",
        "vision_model": "moonshotai/kimi-k3",
        "look_ensemble": 1,
        "independent_verify": True,
        "verifier_model": "moonshotai/kimi-k3",
        "verifier_continuity": True,
        "verifier_evolve_route": True,
        "verifier_local_verdict_only": True,
        "verifier_failure_starts_evolution": True,
        "verifier_hide_actor_memory": True,
        "verifier_stage_lifecycle": False,
        "verifier_persist_scratch": True,
        "agent_decided_stop": True,
        "env_memory_dir": "",
    }
    # 目标配置必须逐项匹配的画像：GLM-5.3 主模型 + Kimi K3 视觉模型、单读者、
    # 独立验证、Kimi K3 Verifier、Verifier 连续上下文、允许走进化路线、仅本地判定、
    # 失败触发进化、隐藏 Actor 记忆、非阶段式生命周期、持久 scratch、Agent 自有停止、
    # 不注入环境记忆目录。
    mismatches = {
        key: {"expected": expected, "observed": getattr(target, key)}
        for (key, expected) in required_target.items()
        if getattr(target, key) != expected
    }
    # 求出与画像不符的字段。
    if mismatches:
        # 任一不符即拒绝（目标配置漂移）。
        raise RuntimeError(f"target self-evolving config mismatch: {mismatches}")
    if not target.agentic_verifier_config:
        # 目标配置必须携带完整的 Agentic Verifier 子配置。
        raise RuntimeError("target config lacks the full Agentic Verifier")
    phase2_required = {
        "verifier_unverified_evidence": True,
        "verifier_execution_mode": "rollback_mirror",
        "actor_evaluator_isolation": True,
    }
    # Phase-2 额外钉死的隔离要求：允许未验证证据模式、回滚镜像执行模式、
    # Actor 评测器隔离（pre-Actor 边界）。
    phase2_mismatches = {
        key: {"expected": expected, "observed": getattr(target, key)}
        for (key, expected) in phase2_required.items()
        if getattr(target, key) != expected
    }
    # 求出与隔离要求不符的字段。
    if phase2_mismatches:
        # 任一不符即拒绝。
        raise RuntimeError(f"Phase-2 target config mismatch: {phase2_mismatches}")
    for role in ("practice_actor", "practice_verifier", "curriculum", "memory_actor"):
        # 校验四个学习侧角色。
        cfg = configs[role]
        # 取当前角色配置。
        if (
            not cfg.agent_decided_stop
            or not cfg.practice_mode
            or cfg.independent_verify
            or (cfg.max_resumes != 0)
        ):
            # 必须是：Agent 自有停止、练习模式、无独立验证、零自动续跑。
            raise RuntimeError(f"{role} config is not Agent-owned practice mode")
    if configs["practice_actor"].model != target.model:
        # 练习 Actor 必须与目标 Actor 同模型（同一智能体在学习）。
        raise RuntimeError("target and practice Actor Agent identities differ")
    if configs["memory_actor"].model != configs["practice_actor"].model:
        # 记忆写入必须延续同一 Actor 模型。
        raise RuntimeError("practice and memory Actor Agent identities differ")
    if configs["practice_verifier"].model != "moonshotai/kimi-k3":
        # 练习 Verifier 必须是 Kimi K3。
        raise RuntimeError("practice Verifier Agent must be Kimi K3")
    if configs["curriculum"].model != "moonshotai/kimi-k3":
        # Curriculum 必须是 Kimi K3。
        raise RuntimeError("Curriculum Agent must be Kimi K3")
    return (configs, paths)
    # 返回（配置对象表, 配置路径表）。


def _practice_agentic_control_config(
    target_cfg: Any, practice_verifier_path: Path
) -> Any:
    """Bind the full practice Verifier to the explicitly selected config.

    ``evolve_until_ready`` needs the target control configuration for rollback
    isolation and other harness policy, while ``verify_agentic`` loads the
    Verifier Agent itself from ``agentic_verifier_config``. Keeping the target
    value here would silently ignore ``--practice-verifier-config``.
    """
    # 【中文注释】构造练习侧的 Agentic 控制配置：保留目标控制配置（回滚隔离等
    # 框架策略依赖它），但把 agentic_verifier_config 替换为显式选择的练习 Verifier
    # 配置——否则 --practice-verifier-config 会被静默忽略。
    return dataclasses.replace(
        target_cfg, agentic_verifier_config=str(practice_verifier_path.resolve())
    )
    # 复制目标配置并把 Verifier 子配置指向练习 Verifier 配置文件的绝对路径。


def _write_phase2_manifest(
    root: Path,
    *,
    task_id: str,
    protocol_run: str,
    instruction: str,
    target_direction: str,
    initial_memory_path: Path,
    initial_memory: dict[str, bytes],
    configs: dict[str, Any],
    paths: dict[str, Path],
    benchmark_provenance: dict[str, Any],
    stop_policy: str = DEFAULT_PHASE2_STOP_POLICY,
    curriculum_memory_access: str = "read_only",
) -> None:
    # 【中文注释】写 Phase-2 运行清单：锁定任务、血脉、指令/方向指纹、初始记忆指纹
    # （writeback=False）、协议元数据、基准溯源（仅宿主可见）与全部角色配置快照。
    from explore.practice_loop import _manifest
    from explore.target_learning import _memory_tree_sha256
    # 延迟导入记忆清单与记忆树指纹工具。

    record = {
        "schema_version": 1,
        "kind": "recursive_self_improvement_phase2_targeted_practice",
        "task_id": task_id,
        "protocol_run": protocol_run,
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        "target_direction_sha256": hashlib.sha256(
            target_direction.encode("utf-8")
        ).hexdigest(),
        "initial_memory": {
            "path": str(initial_memory_path),
            "files": len(initial_memory),
            "bytes": sum((len(data) for data in initial_memory.values())),
            "tree_sha256": _memory_tree_sha256(initial_memory),
            "manifest": _manifest(initial_memory),
            "writeback": False,
        },
        "protocol": phase2_protocol_metadata(stop_policy, curriculum_memory_access),
        "benchmark_provenance_host_only": benchmark_provenance,
        "configs": {
            role: {
                "path": str(paths[role]),
                "sha256": _sha256_file(paths[role]),
                "effective": dataclasses.asdict(configs[role]),
            }
            for role in sorted(configs)
        },
    }
    # 清单记录：schema_version/kind=模式版本与种类；task_id/protocol_run=任务与血脉名；
    # instruction_sha256/target_direction_sha256=指令与稳定方向的指纹；
    # initial_memory=初始记忆的路径/文件数/字节数/树指纹/清单（writeback=False 表示
    # 不可写回）；protocol=协议元数据；benchmark_provenance_host_only=基准溯源（仅宿主）；
    # configs=各角色配置的路径、sha256 与生效值。
    _write_json_atomic(root / "manifest.json", record)
    # 原子写入清单。


def phase2_protocol_metadata(
    stop_policy: str, curriculum_memory_access: str = "read_only"
) -> dict[str, Any]:
    """One description shared by preflight and the persisted run manifest."""
    # 【中文注释】Phase-2 协议元数据：预检与持久化清单共用的一份描述——
    # 停止策略、课程记忆访问模式、通过/失败后的转移语义、课程权限边界等。
    from core.self_evolving_loop import Phase2StopPolicy
    # 延迟导入停止策略枚举。

    policy = Phase2StopPolicy(stop_policy)
    # 把字符串解析为策略枚举（非法值会抛异常）。
    if curriculum_memory_access not in {"read_only", "none"}:
        # 课程记忆访问模式只允许只读或不可见两种。
        raise ValueError("unknown Curriculum memory access mode")
    review = policy is Phase2StopPolicy.CURRICULUM_REVIEW
    # 是否为"课程审查"停止策略。
    return {
        "stop_policy": policy.value,
        "curriculum_memory_access": curriculum_memory_access,
        "curriculum_actor_learning_diagnosis": "available",
        "first_action": "fresh_actor_attempts_development_task",
        "pass_transition": "same_actor_learning_then_curriculum_selects_next_experience"
        if review
        else "same_actor_learning_then_phase2_task_complete",
        "fail_transition": "same_actor_learning_then_curriculum_selects_next_experience",
        "verified_experience_transition": "same_actor_distillation_and_reconciliation_after_pass_or_fail",
        "curriculum_authority": "select_experience_only_never_edit_or_approve_memory",
        "curriculum_after_pass": review,
        "pass_without_high_value_project": "ready_for_target_and_phase2_task_complete"
        if review
        else "phase2_task_complete_without_curriculum_review",
        "project_after_any_outcome": "fresh_target_retry_required_after_practice",
        "unverified_transition": "same_actor_evidence_then_same_persistent_verifier",
        "unresolved_unverified_blocks_phase_transition": True,
        "retry_transition": "fresh_actor_with_updated_memory",
        "target_verifier_orientation": False,
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_enters_learning": False,
    }
    # 协议元数据字段：stop_policy=停止策略取值；curriculum_memory_access=课程记忆访问；
    # curriculum_actor_learning_diagnosis=课程可见 Actor 学习诊断；
    # first_action=第一动作（全新 Actor 尝试开发任务）；
    # pass_transition/fail_transition=通过/失败后的转移（同一 Actor 学习后由课程选下一
    # 经验或完成 Phase-2 任务）；verified_experience_transition=已验证经验经蒸馏与整合
    # 落记忆；curriculum_authority=课程只选经验、绝不编辑或批准记忆；
    # curriculum_after_pass=通过后是否课程审查；pass_without_high_value_project=无高价值
    # 项目的通过语义；project_after_any_outcome=练习后必须全新目标重试；
    # unverified_transition=未验证时同一 Actor 证据交同一持久 Verifier；
    # unresolved_unverified_blocks_phase_transition=未解决的未验证阻断阶段推进；
    # retry_transition=以更新后的记忆启用全新 Actor 重试；
    # target_verifier_orientation=目标 Verifier 不做 pre-Actor 定向（clean 协议）；
    # official_evaluator_calls/official_evaluator_feedback_enters_learning=官方评测零调用、
    # 零反馈进学习。


def _parser() -> argparse.ArgumentParser:
    # 【中文注释】命令行解析器：任务 ID、种子、五个配置路径、语料、初始记忆、
    # 停止策略、课程记忆访问、血脉名，以及预检/执行（互斥必选）。
    from core.self_evolving_loop import Phase2StopPolicy
    # 延迟导入停止策略枚举。

    parser = argparse.ArgumentParser(description=__doc__)
    # 用模块 docstring 作帮助文本。
    parser.add_argument("task_id", choices=ALL_TASKS)
    # 位置参数：目标任务 ID（限定 108 个任务之一）。
    parser.add_argument("--seed", type=int, default=5301)
    # 随机种子（默认 5301）。
    parser.add_argument(
        "--target-config",
        default=None,
        help="target Actor/Verifier control config; defaults to config/roles/target.yaml",
    )
    # 目标 Actor/Verifier 控制配置（默认 target.yaml）。
    parser.add_argument(
        "--practice-actor-config", default=str(DEFAULT_PRACTICE_ACTOR_CONFIG)
    )
    # 练习 Actor 配置路径。
    parser.add_argument(
        "--practice-verifier-config", default=str(DEFAULT_PRACTICE_VERIFIER_CONFIG)
    )
    # 练习 Verifier 配置路径。
    parser.add_argument("--curriculum-config", default=str(DEFAULT_CURRICULUM_CONFIG))
    # Curriculum 配置路径。
    parser.add_argument("--memory-config", default=str(DEFAULT_MEMORY_CONFIG))
    # 记忆角色配置路径。
    parser.add_argument("--benchmark-profile", default=str(DEFAULT_BENCHMARK_PROFILE))
    # 基准溯源 profile 路径。
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    # 指令语料路径。

    parser.add_argument(
        "--initial-memory",
        default="",
        help="required immutable input memory directory for Phase 2",
    )
    # 必填：Phase-2 的不可变输入记忆目录（来自 Phase-1 冻结记忆）。
    parser.add_argument(
        "--phase2-stop-policy",
        choices=[p.value for p in Phase2StopPolicy],
        default=None,
        help="Phase-2 training only: curriculum_review (default) reviews PASS learning for further useful practice and requires a fresh target test after practice; verifier_pass stops after PASS learning",
    )
    # Phase-2 训练停止策略：curriculum_review（默认）在通过后继续审查学习是否还有
    # 有益练习并要求练习后全新目标测试；verifier_pass 在通过学习后即停止。
    parser.add_argument(
        "--phase2-curriculum-memory-access",
        choices=["read_only", "none"],
        default=None,
        help="Phase-2 training only: expose a disposable Actor memory copy to Curriculum (read_only, default), or attach an empty directory for a fresh comparison (none); Actor memory and diagnosis remain",
    )
    # 课程记忆访问：read_only（默认，暴露一次性 Actor 记忆副本）或 none（挂空目录做
    # 对照）；Actor 自身记忆与诊断不受影响。
    parser.add_argument(
        "--protocol-run", default="", help="safe experiment lineage name for Phase 2"
    )
    # 必填：Phase-2 的安全实验血脉名。
    action = parser.add_mutually_exclusive_group(required=True)
    # 预检与执行互斥且必选。
    action.add_argument("--preflight", action="store_true")
    # 预检模式：打印计划，无副作用。
    action.add_argument(
        "--execute",
        default="",
        help=f"paid launch acknowledgement: {PHASE2_EXECUTE_ACK}",
    )
    # 执行模式：值必须等于付费启动确认口令。
    return parser
    # 返回解析器。


def main(argv: list[str] | None = None, *, recovery_plan=None) -> int:
    # 【中文注释】Phase-2 主流程：解析参数→校验恢复计划→装配配置与目标→
    # 预检（或校验执行口令）→写清单→装配全部钩子→运行 DRS 状态机（含恢复路径）→
    # 冻结最终记忆并写结果。recovery_plan 参数供基础设施恢复入口复用。
    args = _parser().parse_args(argv)
    # 解析命令行参数。
    _install_paths()
    # 安装运行时路径环境（sys.path 注入与 chdir）。
    args.phase2_stop_policy = args.phase2_stop_policy or DEFAULT_PHASE2_STOP_POLICY
    # 停止策略缺省为默认值（curriculum_review）。
    args.phase2_curriculum_memory_access = (
        args.phase2_curriculum_memory_access or "read_only"
    )
    # 课程记忆访问缺省为 read_only。
    if recovery_plan is not None:
        # 恢复模式下：恢复计划必须与课程记忆访问一致。
        try:
            declared_access = json.loads(Path(recovery_plan).read_text()).get(
                "curriculum_memory_access", "read_only"
            )
            # 读取恢复计划中声明的课程记忆访问模式。
        except (OSError, TypeError, ValueError) as exc:
            # 计划文件读不了/不是 JSON 即拒绝恢复。
            raise RuntimeError(
                "memory-access ablations require a fresh Phase-2 lineage or a valid condition-preserving recovery plan"
            ) from exc
        if args.phase2_curriculum_memory_access != declared_access:
            # 恢复不允许改变课程记忆访问条件（保持实验条件不变）。
            raise RuntimeError("recovery cannot change Curriculum memory access")
    if args.target_config is None:
        # 目标配置缺省为 target.yaml。
        args.target_config = str(DEFAULT_PHASE2_TARGET_CONFIG)
    if not args.protocol_run:
        # 血脉名必填。
        raise RuntimeError("Phase 2 requires --protocol-run")
    if not args.initial_memory:
        # 初始记忆必填（Phase-2 从 Phase-1 冻结记忆出发）。
        raise RuntimeError("Phase 2 requires --initial-memory")
    root = _safe_phase2_result_root(args.protocol_run, args.task_id, args.seed)
    # 校验并解析结果根。
    (configs, config_paths) = _load_configs(args)
    # 加载并校验全部角色配置。
    corpus = Path(args.corpus).expanduser().resolve(strict=True)
    # 解析语料路径（必须存在）。
    from explore.commit import validate_instruction_corpus

    validate_instruction_corpus(str(corpus))
    # 校验指令语料的完整性与格式。
    (initial_task, initial_instruction) = _load_task(args.task_id)
    # 加载目标任务配置与指令。
    target_direction = _stable_target_direction(initial_task, initial_instruction)
    # 计算稳定目标方向（跨周期一致性锚点）。
    task_class_path = (
        OSWORLD_ROOT / "evaluation_examples/task_class" / f"{args.task_id}.py"
    ).resolve(strict=True)
    # 解析任务类源码路径（评测器实现文件，仅用于溯源指纹）。
    profile_path = resolve_path(args.benchmark_profile, repo_root=RSIAGENT_ROOT)
    # 解析基准溯源 profile 路径。
    benchmark_provenance = _benchmark_provenance(
        args.task_id, task_class_path, profile_path
    )
    # 构造基准溯源记录（任务发布、任务类指纹等，仅宿主可见）。
    target_cfg = configs["target_actor"]
    # 取目标自进化配置。
    practice_agentic_control_cfg = _practice_agentic_control_config(
        target_cfg, config_paths["practice_verifier"]
    )
    # 构造练习侧 Agentic 控制配置（目标控制面 + 显式练习 Verifier 配置）。
    initial_memory_path = None
    # 初始化初始记忆路径变量。
    raw_memory_path = Path(args.initial_memory).expanduser()
    # 展开用户目录缩写。
    initial_memory_path = (
        raw_memory_path
        if raw_memory_path.is_absolute()
        else RSIAGENT_ROOT / raw_memory_path
    )
    # 绝对路径直用；相对路径以仓库根为基。
    initial_memory: dict[str, bytes] = {}
    # 初始化记忆树（文件名→字节）。
    if initial_memory_path is not None:
        # 读取初始记忆。
        if initial_memory_path.is_symlink():
            # 输入记忆目录不允许是符号链接（防指向外部）。
            raise RuntimeError("--initial-memory may not be a symlink")
        initial_memory_path = initial_memory_path.resolve(strict=True)
        # strict 解析（必须存在）。
        if not initial_memory_path.is_dir():
            # 必须是目录。
            raise RuntimeError("--initial-memory must be one directory")
        from explore.practice_loop import _read_memory_tree
        from explore.target_learning import _memory_tree_sha256

        initial_memory = _read_memory_tree(str(initial_memory_path))
        # 读入整个初始记忆树。
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    from env.qemu_rollback import normalize_verifier_execution_mode

    verifier_execution_mode = normalize_verifier_execution_mode(
        target_cfg.verifier_execution_mode
    )
    # 归一化 Verifier 执行模式（rollback_mirror）。
    prepare_checkpointable_docker_provider(verifier_execution_mode)
    # 给后续 Docker 客体安装可检查点化补丁（候选保护用）。
    preflight = {
        "status": "ready",
        "task": args.task_id,
        "result_root": str(root),
        "mode": "outcome_grounded_targeted_practice",
        "phase2_protocol": phase2_protocol_metadata(
            args.phase2_stop_policy, args.phase2_curriculum_memory_access
        ),
        "seed_label": args.seed,
        "target_actor_agent": target_cfg.model,
        "target_verifier_agent": target_cfg.verifier_model,
        "practice_actor_agent": configs["practice_actor"].model,
        "practice_verifier_agent": configs["practice_verifier"].model,
        "practice_verifier_config": str(config_paths["practice_verifier"].resolve()),
        "curriculum_agent": configs["curriculum"].model,
        "actor_eye": target_cfg.vision_model,
        "actor_eye_readers_per_look": target_cfg.look_ensemble,
        "memory": "inherited, mutable only within Phase 2",
        "initial_memory_files": len(initial_memory),
        "initial_memory_bytes": sum((len(data) for data in initial_memory.values())),
        "initial_memory_tree_sha256": _memory_tree_sha256(initial_memory),
        "verifier_execution_mode": verifier_execution_mode,
        "verifier_candidate_protection": "qemu_checkpoint_restore"
        if verifier_execution_mode == "rollback_mirror"
        else "read_only_pid_network_ipc_namespaces",
        "verifier_context": "persistent within one target attempt/resume",
        "target_attempt_verifier_context": "fresh after each evolution wave",
        "target_verifier_stages": ["candidate_verification"],
        "target_verifier_orientation_context": "disabled_clean_protocol",
        "escalation_verifier_orientation": "disabled_clean_protocol",
        "practice_verifier_orientation": False,
        "official_evaluator_inside_loop": False,
        "official_evaluator_calls_after_loop": 0,
        "benchmark_profile": benchmark_provenance["profile"],
        "task_release": benchmark_provenance["task_release"],
    }
    # 组装预检报告：status=就绪；task/result_root=任务与结果根；mode=结果接地的定向
    # 练习；phase2_protocol=协议元数据；seed_label=种子；target/practice/curriculum 各
    # 智能体模型身份；practice_verifier_config=练习 Verifier 配置路径；actor_eye=视觉
    # 模型及其每次观察的读者数；memory=继承记忆且仅在 Phase-2 内可变；
    # initial_memory_* =初始记忆文件数/字节数/树指纹；verifier_execution_mode=执行隔离
    # 模式；verifier_candidate_protection=候选保护方式（回滚镜像下为 QEMU 检查点恢复，
    # 否则为只读 pid/网络/IPC 命名空间）；verifier_context=Verifier 上下文在一次目标
    # 尝试/恢复内持久；target_attempt_verifier_context=每次进化波后目标验证上下文刷新；
    # target_verifier_stages=仅候选验证阶段；各 orientation 项=clean 协议下全部禁用；
    # official_evaluator_inside_loop/calls_after_loop=官方评测不在环内、循环后零调用；
    # benchmark_profile/task_release=基准溯源。
    if args.preflight:
        # 预检模式：打印报告即返回，无任何副作用。
        print(json.dumps(preflight, indent=2, sort_keys=True))
        return 0
    expected_ack = PHASE2_EXECUTE_ACK
    # 期望的执行确认口令。
    if args.execute != expected_ack:
        # 口令不匹配即拒绝执行（付费动作显式确认）。
        raise RuntimeError(f"--execute must equal {expected_ack}")
    recovery = None
    # 恢复计划对象（默认无）。
    if recovery_plan is not None:
        # 有恢复计划：准入校验并装载。
        from explore.phase2_recovery import Phase2CurriculumRecovery

        recovery = Phase2CurriculumRecovery.admit(
            recovery_plan,
            root,
            configs,
            initial_memory,
            target_direction,
            args.phase2_stop_policy,
        )
        # 按计划准入：校验血脉/配置/记忆/方向/停止策略一致后返回恢复对象。
    if recovery is None and root.exists() and any(root.iterdir()):
        # 非恢复模式下结果根必须为空（防覆盖既有血脉）。
        raise RuntimeError(
            "self-evolving result root is nonempty; preserve it and use a new locked retry tag"
        )
    root.mkdir(parents=True, exist_ok=True)
    # 创建结果根目录。
    if recovery is None:
        # 新运行：写运行清单。
        _write_phase2_manifest(
            root,
            task_id=args.task_id,
            protocol_run=args.protocol_run,
            instruction=initial_instruction,
            target_direction=target_direction,
            initial_memory_path=initial_memory_path,
            initial_memory=initial_memory,
            configs=configs,
            paths=config_paths,
            benchmark_provenance=benchmark_provenance,
            stop_policy=args.phase2_stop_policy,
            curriculum_memory_access=args.phase2_curriculum_memory_access,
        )
    events_path = root / "events.jsonl"
    # 事件日志路径。
    from desktop_env.desktop_env import DesktopEnv

    from benchmarks.osworld.actor_boundary import quarantine_actor_visible_evaluators
    from core.actor import build_system
    from core.loop import (
        _snapshot,
        _verifier_session_identity,
        escalation_cfg,
        run_with_resume,
    )
    from core.self_evolving_loop import (
        ActorLearning,
        EvolutionResult,
        EvolutionStatus,
        SelfEvolvingLoopHooks,
        SelfEvolvingStart,
        TargetVerdict,
        TargetVerification,
        run_self_evolving_loop,
    )
    from core.task_baseline import capture_task_start_baseline
    from core.trace import ArtifactSink
    from core.verifier import VerifierSession, verify_agentic
    from env.vm import VM
    from explore.charter import memory_preamble
    from explore.commit import push_memory
    from explore.practice_loop import (
        PracticeHooks,
        _atomic_install_memory,
        _manifest,
        _read_memory_tree,
    )
    from explore.target_learning import _memory_tree_sha256, _promote_learning
    from explore.unified_evolution import UnifiedCurriculumSession, evolve_until_ready
    from llm.client import provider_counts, reset_provider_counts
    # 【中文注释】延迟导入 DRS 全套设施：桌面环境、pre-Actor 评测器隔离、系统提示词
    # 构造、主循环（快照/会话身份/升级配置/可恢复运行）、DRS 状态机类型与主函数、
    # 任务开始基线、工件汇、Verifier 会话与验证入口、VM 封装、记忆前言、记忆推送、
    # 练习钩子与记忆原子安装、目标学习晋升、统一课程会话与自进化入口、LLM 用量统计。

    active_memory_dir = root / "active_memory"
    # 活跃记忆目录（当前周期的 Actor 记忆安装点）。
    memory_journal_dir = root / "memory_journal"
    # 记忆日志目录（学习晋升的审计日志）。
    if recovery is None:
        # 新运行：把初始记忆安装为活跃记忆。
        _atomic_install_memory(str(active_memory_dir), initial_memory)
    curriculum_session = UnifiedCurriculumSession()
    # 创建持久课程会话（跨进化波次保持课程上下文）。
    counters = {"target_learning": 0, "target_orientations": 0}
    # 计数器：目标学习次数与目标定向次数。
    if recovery is not None:
        # 恢复模式：从恢复计划回填课程会话与计数器状态。
        curriculum_session.history = recovery.history
        # 恢复课程历史。
        curriculum_session.notes = recovery.notes
        # 恢复课程笔记。
        curriculum_session.turns = recovery.completed_curriculum_turns
        # 恢复已完成的课程轮次。
        counters["target_learning"] = 1
        # 目标学习已完成一次（恢复点在其后）。
        if recovery.ready_target_cycles:
            # 恢复点在"就绪边界"：回填波次与项目摘要。
            curriculum_session.waves = 1
            # 已完成一个波次。
            curriculum_session.project_summaries = [
                f"Phase-2 target outcome {recovery.trigger['verifier_outcome']}: the same Actor Agent committed grounded memory before Curriculum selected the next experience",
                *[
                    f"practice project {i}: " + record["terminal_outcome"]
                    for (i, record) in enumerate(recovery.project_records, 1)
                ],
            ]
            # 项目摘要：目标结局 + 各练习项目的终局记录。

    def emit(event: str, payload: dict[str, Any]) -> None:
        # 【中文注释】事件汇：统一追加到 events.jsonl。
        _append_event(events_path, event, payload)
        # 写入一条事件。

    def fresh_target_environment(
        direction: str, target_cycle: int
    ) -> _TargetEnvironment:
        # 【中文注释】新建目标环境：校验方向一致性→启动全新 Docker 桌面并安装任务→
        # 隔离评测器源码（pre-Actor 边界）→捕获 S0 表面基线→发事件并打包返回。
        (task, instruction) = _load_task(args.task_id)
        # 重新加载任务配置与指令（保证每次 reset 用新读取的实例数据）。
        observed_direction = _stable_target_direction(task, instruction)
        # 计算本次观察到的稳定方向。
        if direction != target_direction or observed_direction != target_direction:
            # 传入方向或观察方向与锚点不一致即拒绝（目标语义漂移防护）。
            raise RuntimeError("stable target direction drifted across target resets")
        desktop = DesktopEnv(
            provider_name="docker",
            action_space="pyautogui",
            os_type="Ubuntu",
            screen_size=(1920, 1080),
            headless=True,
            cache_dir=os.environ.get("RSIAGENT_OSWORLD_CACHE_DIR", "cache"),
            require_a11y_tree=False,
            volume_size=60,
        )
        # 启动全新 Docker Ubuntu 桌面（无头、1920x1080、60GB 卷、pyautogui 动作）。
        started = time.time()
        # 记录启动开始时间。
        try:
            desktop.reset(task_config=task)
            # 安装目标任务（唯一一次官方任务配置加载，评测器仍不被调用）。
            actual_instruction = str(desktop.instruction or instruction)
            # 取环境实际下发的指令。
            if actual_instruction != instruction:
                # 环境指令与预期不一致即失败（防任务错装）。
                raise RuntimeError("DesktopEnv target instruction drifted")
            vm = VM(desktop)
            # 构造 VM 封装。
            quarantined_evaluators = []
            # 被隔离的评测器文件记录（默认空）。
            if target_cfg.actor_evaluator_isolation:
                # 启用 Actor 评测器隔离时执行 pre-Actor 隔离。
                quarantined_evaluators = quarantine_actor_visible_evaluators(vm)
                # 把客户机内可被 Actor 观察到的评测器源码移入 root-only 隔离目录。
                emit(
                    "ACTOR_VISIBLE_EVALUATORS_QUARANTINED",
                    {
                        "target_cycle": target_cycle,
                        "files": len(quarantined_evaluators),
                        "bytes": sum(
                            (record["bytes"] for record in quarantined_evaluators)
                        ),
                        "content_sha256s": [
                            record["sha256"] for record in quarantined_evaluators
                        ],
                        "before_task_start_baseline": True,
                        "before_verifier_orientation": bool(
                            target_cfg.verifier_stage_lifecycle
                        ),
                        "paths_disclosed_to_agents": False,
                    },
                )
                # 事件：记录隔离的文件数/字节数/内容指纹，声明隔离发生在 S0 基线与
                # Verifier 定向之前、路径未向智能体披露。
            private_paths = tuple(
                (
                    path
                    for path in target_cfg.verifier_private_paths
                    if path not in actual_instruction
                )
            )
            # 过滤出"未出现在指令文本中"的 Verifier 私有路径（出现在指令里的无需隐藏）。
            baseline = _snapshot(vm, excluded_paths=private_paths)
            # 捕获 S0 表面基线（pre-Actor 状态指纹，排除私有路径）。
            emit(
                "TARGET_ENVIRONMENT_READY",
                {
                    "target_cycle": target_cycle,
                    "instruction_sha256": hashlib.sha256(
                        actual_instruction.encode("utf-8")
                    ).hexdigest(),
                    "target_direction_sha256": hashlib.sha256(
                        direction.encode("utf-8")
                    ).hexdigest(),
                    "evaluator_object_visibility": "host_only",
                    "guest_evaluator_source_visibility": "quarantined_pre_s0"
                    if target_cfg.actor_evaluator_isolation
                    else "historical_unmodified",
                    "guest_evaluator_files_quarantined": len(quarantined_evaluators),
                    "evaluator_called": False,
                },
            )
            # 事件：环境就绪——指令/方向指纹、评测器对象仅宿主可见、客户机评测器源码
            # 已在 S0 前隔离（或历史未修改）、隔离文件数、评测器未被调用。
            return _TargetEnvironment(
                desktop=desktop,
                vm=vm,
                instruction=actual_instruction,
                boot_secs=time.time() - started,
                surface_baseline=baseline,
                verifier_private_paths=private_paths,
            )
            # 打包返回目标环境快照容器。
        except Exception:
            # 任何失败都关闭桌面，避免残留 VM。
            desktop.close()
            raise

    def fresh_target_verifier(
        environment: _TargetEnvironment, direction: str, target_cycle: int
    ) -> _TargetVerifier:
        # 【中文注释】新建目标 Verifier 状态：校验方向一致后返回空的周期状态容器
        # （会话与定向报告在 orientation 阶段填充）。
        if direction != target_direction:
            # 方向漂移即拒绝。
            raise RuntimeError("target direction drifted before Verifier creation")
        return _TargetVerifier(cycle=target_cycle)
        # 返回绑定当前周期的新 Verifier 状态。

    def _verifier_identity_token(identity: tuple[str, str]) -> str:
        # 【中文注释】把会话身份二元组编码为 16 位十六进制短令牌（目录命名用）。
        encoded = json.dumps(
            list(identity), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        # 把身份序列化为紧凑 JSON 字节。
        return hashlib.sha256(encoded).hexdigest()[:16]
        # 取 sha256 前 16 位作为令牌。

    def _orientation_configs(environment: _TargetEnvironment) -> tuple[Any, ...]:
        # 【中文注释】确定需要做 pre-Actor 定向的配置集合：主配置 +
        # （若配置了不同模型的升级通道）升级配置——升级 Verifier 也要在同一 S0 上定向。
        primary = dataclasses.replace(
            target_cfg, verifier_private_paths=environment.verifier_private_paths
        )
        # 主配置：注入本环境的 Verifier 私有路径。
        configs_to_orient = [primary]
        # 从主配置开始。
        if (
            primary.max_resumes > 0
            and primary.escalation_model
            and (primary.escalation_model != primary.model)
        ):
            # 存在异于主模型的升级通道时，补充升级配置。
            alternate = escalation_cfg(primary)
            # 构造升级配置。
            if _verifier_session_identity(alternate) != _verifier_session_identity(
                primary
            ):
                # 仅当身份确实不同才追加（避免重复定向）。
                configs_to_orient.append(alternate)
        return tuple(configs_to_orient)
        # 返回待定向配置元组。

    def _orientation_event_callback(target_cycle: int, identity: tuple[str, str]):
        # 【中文注释】构造定向阶段的事件回调：给每个事件打上目标周期与 Verifier
        # 身份标记后转发到事件日志。
        def callback(event: str, payload: dict[str, Any]) -> None:
            # 内层回调：转发事件。
            emit(
                "TARGET_VERIFIER_" + event,
                {
                    "target_cycle": target_cycle,
                    "verifier_identity": list(identity),
                    **payload,
                },
            )
            # 事件名加 TARGET_VERIFIER_ 前缀，负载附加周期与身份。

        return callback
        # 返回该身份专用回调。

    def verifier_orient(
        verifier: _TargetVerifier,
        environment: _TargetEnvironment,
        direction: str,
        target_cycle: int,
    ) -> None:
        # 【中文注释】目标 Verifier 定向（orientation，候选盲阶段）：在每个待定向
        # 配置上，让 Verifier 在"没有任何 Actor 活动"的 S0 上熟悉环境并产出报告，
        # 然后挂起其执行器、登记报告指纹。候选验证将复用同一会话与同一上下文。
        if verifier.cycle != target_cycle or direction != target_direction:
            # 周期或方向漂移即拒绝。
            raise RuntimeError("target Verifier orientation inputs drifted")
        baseline = capture_task_start_baseline(environment.vm)
        # 捕获可信任务开始基线（文件清单/内容指纹/外部服务状态）。
        identity_record = {
            "schema_version": 1,
            "task": args.task_id,
            "target_cycle": target_cycle,
            "instruction_sha256": hashlib.sha256(
                environment.instruction.encode("utf-8")
            ).hexdigest(),
            "target_direction_sha256": hashlib.sha256(
                direction.encode("utf-8")
            ).hexdigest(),
            "task_class_sha256": benchmark_provenance["task_class_sha256"],
            "runtime_checkout": benchmark_provenance["runtime_checkout_describe"],
            "persistent_content_sha256": baseline["content_sha256"],
        }
        # 任务开始身份记录：任务 ID、周期号、指令/方向指纹、任务类源码指纹、
        # 运行时检出描述、持久内容指纹——共同构成 S0 的可审计身份。
        encoded_identity = json.dumps(
            identity_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        # 规范化序列化身份记录。
        verifier.task_start_identity_sha256 = hashlib.sha256(
            encoded_identity
        ).hexdigest()
        # 计算并存下任务开始身份 sha256。
        baseline_dir = root / "task_start_baselines"
        # 基线落盘目录。
        baseline_dir.mkdir(parents=True, exist_ok=True)
        # 确保目录存在。
        baseline_path = baseline_dir / f"cycle_{target_cycle:03d}.json"
        # 按周期命名的基线文件。
        _write_json_atomic(
            baseline_path,
            {
                **baseline,
                "task_start_identity": {
                    **identity_record,
                    "sha256": verifier.task_start_identity_sha256,
                },
            },
        )
        # 原子写入基线清单（含身份记录及其 sha256）。
        emit(
            "TRUSTED_TASK_START_BASELINE_CAPTURED",
            {
                "target_cycle": target_cycle,
                "manifest": str(baseline_path),
                "entry_count": baseline["entry_count"],
                "content_sha256": baseline["content_sha256"],
                "exact_sha256": baseline["exact_sha256"],
                "task_start_identity_sha256": verifier.task_start_identity_sha256,
                "external_service_state": baseline["external_service_state"],
                "actor_has_acted": False,
                "actor_memory_attached": False,
            },
        )
        # 事件：可信任务开始基线已捕获——条目数/内容指纹/精确指纹/身份指纹/
        # 外部服务状态；声明此刻无 Actor 活动、未挂载 Actor 记忆。
        try:
            for position, active_cfg in enumerate(
                _orientation_configs(environment), start=1
            ):
                # 逐个配置执行定向。
                identity = _verifier_session_identity(active_cfg)
                # 计算该配置的会话身份。
                if identity in verifier.sessions:
                    # 身份重复说明定向登记冲突。
                    raise RuntimeError(
                        "duplicate target Verifier identity during orientation"
                    )
                session = VerifierSession(
                    on_event=_orientation_event_callback(target_cycle, identity)
                )
                # 创建带事件回调的 Verifier 会话。
                verifier.sessions[identity] = session
                # 登记会话。
                verifier.configs[identity] = active_cfg
                # 登记配置。
                role = "primary" if position == 1 else "potential_escalation"
                # 第一个为主角色，后续为潜在升级角色。
                context = (
                    "The host archived a complete trusted task-start manifest for audit. It is not mounted into your environment: inspect the exact live, pre-Actor S0 available to you.\nTask-start identity digest: "
                    + verifier.task_start_identity_sha256
                    + "\nPersistent-file entries: "
                    + str(baseline["entry_count"])
                    + "\nPersistent-content digest: "
                    + baseline["content_sha256"]
                    + "\nExternal-service snapshot: "
                    + baseline["external_service_state"]
                    + "\nNo Actor has acted and no Actor memory is attached."
                    + (
                        "\nThis context is being prepared prospectively for the fully asymmetric verifier role that is activated only if the primary Actor later stalls."
                        if role == "potential_escalation"
                        else ""
                    )
                )
                # 定向上下文：告知 Verifier 宿主已归档可信任务开始清单供审计（不挂载
                # 进其环境），并给出身份摘要/持久文件数/内容指纹/外部服务快照；
                # 强调当前无 Actor 活动且无 Actor 记忆；升级角色额外说明这是为
                # "主 Actor 停滞时才激活的全非对称验证角色"预备的上下文。
                counters["target_orientations"] += 1
                # 定向计数加一（turn_no 用负数与候选验证区分）。
                (verdict, findings) = verify_agentic(
                    environment.instruction,
                    environment.vm,
                    active_cfg,
                    sink=ArtifactSink(
                        str(
                            root
                            / "target_verifier_lifecycle"
                            / f"cycle_{target_cycle:03d}"
                            / _verifier_identity_token(identity)
                        )
                    ),
                    turn_no=-counters["target_orientations"],
                    context=context,
                    session=session,
                    wall_budget=active_cfg.wall_clock_secs,
                    stage="orientation",
                )
                # 运行 Agentic 定向：工件按周期/身份令牌归档，turn_no 取负值，阶段为
                # orientation，墙钟预算取配置值。
                if verdict != "orientation_ready":
                    # 未达成"定向就绪"即失败（定向报告必须完成）。
                    raise RuntimeError(
                        "target Verifier Agent did not complete orientation: "
                        + str(findings)
                    )
                report = str(findings)
                # 定向报告文本。
                report_sha256 = hashlib.sha256(
                    report.encode("utf-8", "replace")
                ).hexdigest()
                # 计算报告 sha256（编码错误用 replace 容错）。
                session.detach_executor(environment.vm, preserve=True)
                # 从 VM 分离执行器但保留会话状态（候选验证时复用同一上下文）。
                verifier.orientation_report_sha256s[identity] = report_sha256
                # 登记该身份的定向报告指纹。
                emit(
                    "VERIFIER_ORIENTATION_COMPLETED",
                    {
                        "target_cycle": target_cycle,
                        "role": role,
                        "verifier_identity": list(identity),
                        "verifier_model": active_cfg.verifier_model,
                        "task_start_identity_sha256": verifier.task_start_identity_sha256,
                        "orientation_report_sha256": report_sha256,
                        "same_context_reserved_for_candidate": True,
                    },
                )
                # 事件：定向完成——角色/身份/模型/身份指纹/报告指纹；同一上下文为
                # 候选验证保留。
        except Exception:
            # 定向失败：关闭所有会话执行器后重抛。
            for session in verifier.sessions.values():
                session.close_executor()
            raise

    def fresh_target_actor(memory: dict[str, bytes], target_cycle: int) -> _TargetActor:
        # 【中文注释】新建目标 Actor：复制当前记忆树并绑定周期号。
        return _TargetActor(memory=dict(memory), cycle=target_cycle)
        # 返回携带记忆副本的新 Actor 状态。

    def actor_work(
        actor: _TargetActor,
        verifier: _TargetVerifier,
        environment: _TargetEnvironment,
        direction: str,
    ) -> _TargetOutput:
        # 【中文注释】Actor 工作回合：安装并推送周期记忆→构造周期配置→运行可恢复
        # 主循环（含 Verifier 会话准备钩子）→保存转录→校验身份/定向凭证→打包输出。
        if direction != target_direction:
            # 方向漂移即拒绝。
            raise RuntimeError("target direction drifted before Actor work")
        if verifier.cycle != actor.cycle:
            # Actor/Verifier 周期必须配对。
            raise RuntimeError("target Actor/Verifier cycle identity drifted")
        _atomic_install_memory(str(active_memory_dir), actor.memory)
        # 把 Actor 记忆原子安装到活跃记忆目录。
        if not push_memory(environment.vm, str(active_memory_dir)):
            # 把记忆推送到客户机（挂载给 Actor）；失败即终止。
            raise RuntimeError("could not attach task-local memory to target Actor")
        listing = "\n".join(
            (
                f"  {len(data):>7}  {name}"
                for (name, data) in sorted(actor.memory.items())
            )
        )
        # 构造记忆文件清单文本（按名排序，含文件大小）——供 Agent 自行决定读取哪些。
        cycle_cfg = dataclasses.replace(
            target_cfg,
            env_memory_dir=str(active_memory_dir) if actor.memory else "",
            verifier_private_paths=tuple(environment.verifier_private_paths),
        )
        # 周期配置：有记忆时注入记忆目录，并绑定本环境的 Verifier 私有路径。
        emit(
            "TARGET_MEMORY_ATTACHED",
            {
                "target_cycle": actor.cycle,
                "state": "populated" if actor.memory else "blank",
                "files": len(actor.memory),
                "bytes": sum((len(data) for data in actor.memory.values())),
                "memory_tree_sha256": _memory_tree_sha256(actor.memory),
                "retrieval": "Actor Agent decides from complete filename/size inventory",
            },
        )
        # 事件：目标记忆已挂载——状态（有记忆/空白）、文件数/字节数/树指纹；
        # 检索方式=Actor 依据完整文件名/大小清单自行决定。
        sink_root = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
        # 本周期工件根目录。
        sink = ArtifactSink(str(sink_root))
        # 创建工件汇。
        runtime_state: dict[str, Any] = {}
        # 运行时状态容器（主循环回填活跃身份/配置/历史）。

        def ensure_verifier_orientation(active_cfg, session: VerifierSession) -> None:
            # 【中文注释】阶段生命周期开启时的会话准备钩子：候选验证只允许激活
            # "已在本尝试 S0 上定向过的同一会话身份"。
            identity = _verifier_session_identity(active_cfg)
            # 计算当前配置的会话身份。
            if (
                verifier.sessions.get(identity) is not session
                or identity not in verifier.orientation_report_sha256s
            ):
                # 会话不是登记的那个、或该身份没有定向报告——拒绝激活。
                raise RuntimeError(
                    "candidate verification activated a Verifier identity that did not orient on this attempt's pre-Actor task state"
                )
            session.on_event = _orientation_event_callback(actor.cycle, identity)
            # 重新挂上本周期的事件回调。

        def attach_verifier_event_stream(active_cfg, session: VerifierSession) -> None:
            # 【中文注释】未开启阶段生命周期时的会话准备钩子：只校验会话登记一致并
            # 附加事件流（无定向凭证要求——clean 协议不做 pre-Actor 定向）。
            identity = _verifier_session_identity(active_cfg)
            # 计算会话身份。
            if verifier.sessions.get(identity) is not session:
                # 会话登记漂移即拒绝。
                raise RuntimeError(
                    "candidate verification Verifier session registry drifted"
                )
            session.on_event = _orientation_event_callback(actor.cycle, identity)
            # 附加本周期事件回调。

        try:
            (result, history) = run_with_resume(
                environment.instruction,
                environment.vm,
                cycle_cfg,
                sink,
                opening_extra=memory_preamble(listing),
                verifier_sessions=verifier.sessions,
                runtime_state=runtime_state,
                surface_baseline=environment.surface_baseline,
                verifier_session_prepare=ensure_verifier_orientation
                if target_cfg.verifier_stage_lifecycle
                else attach_verifier_event_stream,
                ask_user=environment.desktop.user_simulator.respond
                if environment.desktop.user_simulator is not None
                else None,
            )
            # 运行可恢复的 Actor→Verifier 主循环：开场附加记忆前言；传入持久 Verifier
            # 会话表与 S0 表面基线；按配置选择会话准备钩子；有用户模拟器则接入应答。
        finally:
            # 无论成败都关闭会话执行器（VM 交互结束）。
            for session in verifier.sessions.values():
                session.close_executor()
        active_identity = runtime_state.get("active_verifier_identity")
        # 取主循环实际使用的 Verifier 会话身份。
        if (
            target_cfg.verifier_stage_lifecycle
            and active_identity not in verifier.orientation_report_sha256s
        ):
            # 阶段生命周期下，活跃身份必须有定向凭证（候选盲 provenance）。
            raise RuntimeError(
                "target result has no candidate-blind orientation provenance"
            )
        if active_identity not in verifier.sessions:
            # 活跃身份必须在会话表中（持久 Verifier）。
            raise RuntimeError("target result has no persistent Verifier session")
        sink.save_transcript(
            build_system(
                cycle_cfg, ask_enabled=environment.desktop.user_simulator is not None
            ),
            history,
        )
        # 保存完整对话转录（含系统提示词，标注是否启用提问）。
        return _TargetOutput(
            loop_result=result,
            history=history,
            active_history=list(runtime_state.get("active_history") or history),
            active_cfg=runtime_state.get("active_cfg") or cycle_cfg,
            sink_root=str(sink_root),
            active_verifier_identity=active_identity,
            orientation_report_sha256=verifier.orientation_report_sha256s[
                active_identity
            ]
            if target_cfg.verifier_stage_lifecycle
            else "",
        )
        # 打包输出：活跃历史/配置缺省回落到完整历史/周期配置；阶段生命周期下附
        # 活跃身份的定向报告指纹，否则为空串。

    def verifier_verify(
        verifier: _TargetVerifier,
        _environment: _TargetEnvironment,
        _direction: str,
        output: _TargetOutput,
    ) -> TargetVerification:
        # 【中文注释】目标判定：把主循环的终态路由转换为 PASS/FAIL/UNVERIFIED——
        # 只有 Verifier 显式提交的 HANDOFF（PASS）或 EVOLVE（FAIL）才构成语义判定；
        # 其他任何终态一律记为 UNVERIFIED，不得当作正确或进化触发。
        if (
            target_cfg.verifier_stage_lifecycle
            and output.active_verifier_identity
            not in verifier.orientation_report_sha256s
        ):
            # 阶段生命周期下判定必须来自已定向的身份（候选盲校验）。
            raise RuntimeError(
                "candidate verdict is detached from its Verifier orientation"
            )
        result = output.loop_result
        # 取主循环结果。
        route = str(getattr(result, "verifier_route", "") or "")
        # 读取 Verifier 传输路由（HANDOFF/EVOLVE 等）。
        report = str(getattr(result, "verifier_report", "") or "")
        # 读取 Verifier 报告文本。
        if result.status == "done" and route == "HANDOFF" and report.strip():
            # 状态 done + 路由 HANDOFF + 报告非空 = Verifier 显式提交通过。
            return TargetVerification(TargetVerdict.PASS, report)
        if result.status == "evolve" and route == "EVOLVE" and report.strip():
            # 状态 evolve + 路由 EVOLVE + 报告非空 = Verifier 显式提交失败。
            return TargetVerification(TargetVerdict.FAIL, report)
        reason = f"Target harness ended without a committed Verifier PASS/FAIL; status={result.status!r}, verifier_transport={route!r}. This is not converted into correctness or an evolution trigger."
        # 其他情况的原因说明：目标框架结束时没有已提交的 PASS/FAIL，不得转换为
        # 正确性或进化触发。
        return TargetVerification(TargetVerdict.UNVERIFIED, report or reason)
        # 返回 UNVERIFIED 判定（有报告附报告，否则附原因）。

    def actor_learn(
        actor: _TargetActor,
        environment: _TargetEnvironment,
        direction: str,
        output: _TargetOutput,
        verdict: TargetVerdict,
        report: str,
        memory: dict[str, bytes],
    ) -> ActorLearning:
        # 【中文注释】目标学习（DRS 的经验晋升）：只有接地的 PASS/FAIL 才能进入
        # 持久记忆——先恢复规范记忆到 VM，再调用 _promote_learning 完成
        # 蒸馏→整合→诊断→审计→落盘，最后写本周期学习结局记录。
        if direction != target_direction or actor.memory != memory:
            # 方向或记忆漂移即拒绝（学习输入必须与尝试时一致）。
            raise RuntimeError("target Actor learning inputs drifted")
        if verdict not in {TargetVerdict.PASS, TargetVerdict.FAIL}:
            # 未验证判定绝不允许进入持久记忆。
            raise RuntimeError("only a grounded PASS/FAIL may enter durable memory")
        if not output.active_history:
            # 没有活跃上下文就无法学习。
            raise RuntimeError("target Actor has no active context to learn from")
        _atomic_install_memory(str(active_memory_dir), memory)
        # 原子安装学习前的规范记忆。
        if not push_memory(environment.vm, str(active_memory_dir)):
            # 重新推送到 VM（学习上下文恢复）。
            raise RuntimeError("could not restore canonical memory before learning")
        counters["target_learning"] += 1
        # 目标学习计数加一。
        episode_dir = Path(output.sink_root) / "terminal_learning"
        # 本周期终局学习目录。
        episode_dir.mkdir(parents=True, exist_ok=True)
        # 确保目录存在。

        def learning_emit(
            event_type: str,
            *,
            status: str,
            project_open: bool,
            memory_phase_open: bool,
            payload: dict[str, Any],
        ):
            # 【中文注释】学习阶段专用事件包装：给每个事件附加状态、项目/记忆阶段
            # 开关标记后转发。
            emit(
                "TARGET_" + event_type,
                {
                    "status": status,
                    "project_open": project_open,
                    "memory_phase_open": memory_phase_open,
                    **payload,
                },
            )
            # 事件名加 TARGET_ 前缀，负载附加三个标记字段。

        (learned, diagnosis, learned_history) = _promote_learning(
            hooks=PracticeHooks(),
            vm=environment.vm,
            cfg=output.active_cfg,
            lineage=root,
            episode_dir=episode_dir,
            experience_index=counters["target_learning"],
            before_memory=memory,
            actor_history=output.active_history,
            terminal_outcome=verdict.value,
            verifier_report=report,
            target=environment.instruction,
            audit_mode="exam",
            emit=learning_emit,
            project_open=False,
            corpus_path=str(corpus),
            memory_dir=active_memory_dir,
            journal_dir=memory_journal_dir,
            experience_kind="phase2-exact-target-" + verdict.value.lower(),
            target_visible_inputs=(),
        )
        # 执行 DRS 经验晋升：以活跃配置在 VM 上运行蒸馏/整合/诊断，输入为活跃历史、
        # 终局判定与 Verifier 报告；audit_mode="exam"（考试式静默审计，任务泄漏绊线，
        # 审计删改即整批拒绝）；experience_kind 标记为 Phase-2 精确目标 + 判定；
        # target_visible_inputs=() 声明无额外目标可见输入豁免。
        _write_json_atomic(
            episode_dir / "outcome.json",
            {
                "schema_version": 1,
                "kind": "full_benchmark_target_learning",
                "target_cycle": actor.cycle,
                "target_verifier_verdict": verdict.value,
                "verifier_report": report,
                "actor_learning_diagnosis": diagnosis,
                "memory_before": _manifest(memory),
                "memory_after": _manifest(learned),
                "active_actor_model": output.active_cfg.model,
                "active_history_messages_before": len(output.active_history),
                "active_history_messages_after": len(learned_history),
            },
        )
        # 写本周期学习结局：判定/报告/学习诊断/前后记忆清单/活跃模型/学习前后消息数。
        return ActorLearning(memory=learned, diagnosis=diagnosis)
        # 返回学习后的记忆与诊断。

    def evolve(
        direction: str,
        verdict: TargetVerdict,
        report: str,
        diagnosis: str,
        memory: dict[str, bytes],
        evolution_index: int,
    ) -> EvolutionResult:
        # 【中文注释】一次自进化波次（DRS 内环）：在全新的空任务 VM 上，由 Curriculum
        # 基于本次目标结局选择定向练习，运行 evolve_until_ready 直到就绪/停滞；
        # 结束后读取演化记忆并安装为活跃记忆。
        from benchmarks.osworld.setup import _boot_vm, _close, _reset_null_vm_sealed
        from explore.target_isolation import TargetIsolationSeal
        # 延迟导入练习环境启停工具与目标隔离封条（egress seal）。

        cycle_root = root / "evolution_cycles" / f"cycle_{evolution_index:03d}"
        # 本进化周期的工件根目录。
        (practice_environment, practice_vm) = _boot_vm()
        # 启动全新空任务练习环境与 VM。
        egress_seal = TargetIsolationSeal()
        # 创建出站隔离封条（练习期网络隔离）。
        try:
            practice_hooks = PracticeHooks(
                reset_vm=lambda active_vm, target: _reset_null_vm_sealed(
                    active_vm, target, egress_seal
                )
            )
            # 练习钩子：项目重置时用"封条版空任务重置"（每个练习项目全新隔离 VM）。
            current_recovery = (
                recovery
                if evolution_index == 1
                and (not getattr(recovery, "post_target_learning", False))
                else None
            )
            # 仅第一次进化且恢复点不在"目标学习之后"时，才携带恢复边界。
            if current_recovery is not None:
                # 有恢复边界时包装尝试钩子。
                practice_hooks = dataclasses.replace(
                    practice_hooks,
                    run_attempt=current_recovery.wrap_attempt(
                        practice_hooks.run_attempt
                    ),
                )
                # 用恢复计划包装 run_attempt（重放已完成的练习项目）。
            result = evolve_until_ready(
                practice_vm,
                str(cycle_root),
                direction,
                report,
                diagnosis,
                memory,
                configs["practice_actor"],
                configs["practice_verifier"],
                configs["curriculum"],
                configs["memory_actor"],
                corpus_path=str(corpus),
                trigger_authority="phase2_outcome_protocol",
                triggering_outcome=verdict.value,
                curriculum_memory_access=args.phase2_curriculum_memory_access,
                agentic_verifier_cfg=practice_agentic_control_cfg,
                target_visible_inputs=(),
                hooks=practice_hooks,
                session=curriculum_session,
                event_sink=lambda event_type, **kwargs: emit(
                    "EVOLUTION_" + event_type, kwargs
                ),
                **{"resume_boundary": current_recovery}
                if current_recovery is not None
                else {},
            )
            # 运行自进化直至就绪/停滞：触发权威=Phase-2 结果协议，触发结局=本次判定；
            # 课程记忆访问与练习 Agentic 控制配置按协议注入；持久课程会话贯穿各波次；
            # 事件统一加 EVOLUTION_ 前缀；有恢复边界时以关键字参数传入。
        finally:
            # 无论成败都关闭练习环境并解除封条。
            _close(practice_environment)
            egress_seal.close()
        learned = _read_memory_tree(result.memory_dir)
        # 读取演化产出的记忆树。
        _atomic_install_memory(str(active_memory_dir), learned)
        # 原子安装为活跃记忆（下一次目标尝试用）。
        if result.status == "ready_for_retry":
            # 就绪：可以再做一次目标尝试。
            status = EvolutionStatus.READY_FOR_RETRY
        elif result.status == "stalled":
            # 停滞：练习已无法产生价值。
            status = EvolutionStatus.STALLED
        else:
            # 其他状态属于基础设施/边界问题，不具语义，必须显式失败。
            raise RuntimeError(
                f"self-evolution ended at non-semantic infrastructure/boundary status {result.status!r}: {result.reason}"
            )
        return EvolutionResult(
            status=status,
            memory=learned,
            projects=result.projects,
            reason=result.reason,
        )
        # 返回演化结果（状态/记忆/项目数/原因）。

    def release_target_environment(environment: _TargetEnvironment) -> None:
        # 【中文注释】释放目标环境：走 _release_target_vm（无进展守卫时保留现场）。
        _release_target_vm(environment, emit)
        # 关闭或保留目标 VM，并发相应事件。

    loop_hooks = SelfEvolvingLoopHooks(
        fresh_target_environment=fresh_target_environment,
        fresh_target_verifier=fresh_target_verifier,
        fresh_target_actor=fresh_target_actor,
        actor_work=actor_work,
        verifier_verify=verifier_verify,
        actor_learn=actor_learn,
        evolve=evolve,
        verifier_orient=verifier_orient
        if target_cfg.verifier_stage_lifecycle
        else None,
        learn_on_pass=True,
        curriculum_after_pass=args.phase2_stop_policy == "curriculum_review",
        release_target_environment=release_target_environment,
        on_event=emit,
    )
    # 装配 DRS 状态机钩子集：环境/Verifier/Actor 工厂、工作/判定/学习/进化回调、
    # 定向回调（仅阶段生命周期启用）、通过后是否课程审查（随停止策略）、
    # 环境释放与事件汇。
    reset_provider_counts()
    # 清零 LLM 提供者用量统计（本进程内统计）。
    if recovery is not None and getattr(recovery, "post_target_learning", False):
        # 恢复路径一：目标学习已提交完成，从课程选择处继续。
        from explore.post_target_learning_recovery import continue_after_target_learning

        emit(
            "COMMITTED_TARGET_LEARNING_RECOVERY_STARTED",
            {
                "target_cycle": 1,
                "recovery_plan": str(recovery.plan_path),
                "completed_target_learning_replayed": False,
                "curriculum_calls_before_recovery": 0,
            },
        )
        # 事件：已提交目标学习的恢复开始——目标周期 1，已完成的学习不重放，
        # 恢复前课程调用为 0。
        loop_result = continue_after_target_learning(
            recovery, target_direction, loop_hooks
        )
        # 从"目标学习之后"继续运行 DRS 状态机。
    elif recovery is not None and recovery.ready_target_cycles:
        # 恢复路径二：恢复点在"就绪边界"（目标已通过，练习已完成）。
        learned = _read_memory_tree(str(active_memory_dir))
        # 读取活跃记忆。
        start = SelfEvolvingStart(
            target_cycles=recovery.ready_target_cycles,
            evolutions=1,
            practice_projects=recovery.projects,
            final_after_stall=False,
        )
        # 构造起点：沿用已就绪的目标周期数、一次进化、已完成的练习项目数。
        emit(
            "READY_BOUNDARY_TARGET_REPLACEMENT",
            {
                "recovery_plan": str(recovery.plan_path),
                "completed_practices": recovery.projects,
                "previous_target_cycle": recovery.ready_target_cycles,
                "fresh_actor": True,
                "completed_work_replayed": False,
            },
        )
        # 事件：就绪边界的全新目标替换——完成练习数、旧目标周期数、启用全新 Actor、
        # 已完成工作不重放。
        loop_result = run_self_evolving_loop(
            target_direction, learned, loop_hooks, start=start
        )
        # 以恢复起点运行 DRS 状态机。
    elif recovery is not None:
        # 恢复路径三：恢复点在第一次进化内。
        learned = (
            recovery.initial_evolution_memory
            if recovery.initial_evolution_memory is not None
            else _read_memory_tree(str(active_memory_dir))
        )
        # 优先用计划中冻结的进化前记忆，否则读活跃记忆。
        trigger = recovery.trigger
        # 取恢复触发的目标结局记录。
        evolved = evolve(
            target_direction,
            TargetVerdict(trigger["verifier_outcome"]),
            trigger["verifier_report"],
            trigger["actor_learning_diagnosis"],
            learned,
            1,
        )
        # 重放第一次进化（用计划中保存的判定/报告/诊断）。
        if evolved.projects < 1:
            # 恢复的练习项目数丢失即失败。
            raise RuntimeError("Recovered completed practice count was lost")
        start = SelfEvolvingStart(
            target_cycles=1,
            evolutions=1,
            practice_projects=evolved.projects,
            final_after_stall=evolved.status is EvolutionStatus.STALLED,
        )
        # 构造起点：一个目标周期、一次进化、恢复出的项目数；停滞则终局处理。
        emit(
            "RECOVERED_EVOLUTION_COMPLETED",
            {
                "target_cycle": 1,
                "evolution": 1,
                "projects": evolved.projects,
                "status": evolved.status.value,
                "recovery_plan": str(recovery.plan_path),
            },
        )
        # 事件：恢复的进化已完成——项目数/状态/恢复计划。
        loop_result = run_self_evolving_loop(
            target_direction, evolved.memory, loop_hooks, start=start
        )
        # 以演化后的记忆与起点继续运行状态机。
    else:
        # 常规路径：全新运行。
        loop_result = run_self_evolving_loop(
            target_direction, initial_memory, loop_hooks
        )
        # 以初始记忆运行 DRS 状态机（目标尝试→判定→学习→进化→再尝试）。
    final_environment = loop_result.environment
    # 取状态机终局时保留的目标环境（可能为 None）。
    try:
        final_memory = dict(loop_result.memory)
        # 复制最终记忆树。
        frozen_memory_dir = root / "memory_frozen"
        # 冻结记忆目录。
        _atomic_install_memory(str(frozen_memory_dir), final_memory)
        # 原子安装最终记忆（Phase-2 的血脉终态，交给 pipeline 校验与 Phase-3 使用）。
        payload = {
            "schema_version": 1,
            "phase": "outcome_grounded_targeted_practice",
            "phase2_stop_policy": args.phase2_stop_policy,
            "phase2_curriculum_memory_access": args.phase2_curriculum_memory_access,
            "task": args.task_id,
            "seed": args.seed,
            "protocol_run": args.protocol_run,
            "model": target_cfg.model,
            "status": loop_result.termination,
            "target_verifier_verdict": loop_result.verifier_verdict.value,
            "target_cycles": loop_result.target_cycles,
            "evolutions": loop_result.evolutions,
            "practice_projects": loop_result.practice_projects,
            "target_learning_updates": counters["target_learning"],
            "initial_memory_tree_sha256": _memory_tree_sha256(initial_memory),
            "final_memory_files": len(final_memory),
            "final_memory_bytes": sum((len(data) for data in final_memory.values())),
            "final_memory_tree_sha256": _memory_tree_sha256(final_memory),
            "final_memory_manifest": _manifest(final_memory),
            "final_memory_path": str(frozen_memory_dir),
            "input_memory_writeback": False,
            "target_verifier_orientation": "disabled_clean_protocol",
            "official_evaluator_exposed_to_agents": False,
            "official_evaluator_calls": 0,
            "official_evaluator_feedback_entered_learning": False,
            "llm_providers": provider_counts(),
        }
        # 组装 Phase-2 结果：模式版本/阶段名/停止策略/课程记忆访问/任务/种子/血脉/
        # 模型；status=状态机终止原因；target_verifier_verdict=目标判定；
        # target_cycles/evolutions/practice_projects=目标周期/进化/练习项目数；
        # target_learning_updates=目标学习次数；initial/final memory 指纹与清单；
        # final_memory_path=冻结记忆路径；input_memory_writeback=False 输入记忆未写回；
        # target_verifier_orientation=clean 协议禁用定向；
        # official_evaluator_* =官方评测从未暴露给智能体、零调用、零反馈进学习；
        # llm_providers=本进程 LLM 用量。
        if recovery is not None:
            # 恢复运行：补充恢复来源与用量口径。
            payload["infrastructure_recovery"] = str(recovery.plan_path)
            # 记录恢复计划路径。
            payload["llm_provider_counts_scope"] = "recovery_process_only"
            # 声明用量统计仅覆盖恢复进程。
        _write_json_atomic(root / "result.json", payload)
        # 原子写入结果。
        print("\n=== PHASE 2 RESULT " + json.dumps(payload, sort_keys=True))
        # 打印结果摘要（供编排器采集）。
        return 2 if loop_result.termination == "target_harness_unverified" else 0
        # "目标框架未验证"返回 2（需要人工/上层关注），其余终止返回 0。
    finally:
        # 终局清理：释放保留的目标环境。
        if final_environment is not None:
            release_target_environment(final_environment)
            # 释放目标 VM（无进展守卫时保留现场供恢复）。
        elif not (
            recovery is not None
            and getattr(recovery, "post_target_learning", False)
            and (loop_result.practice_projects == 0)
        ):
            # 除非是"目标学习后恢复且尚无练习项目"的合法情形，否则缺环境即内部错误。
            raise RuntimeError("Phase2 terminal target environment is missing")


if __name__ == "__main__":
    # 作为脚本运行：以 main 返回值作为进程退出码。
    raise SystemExit(main())
