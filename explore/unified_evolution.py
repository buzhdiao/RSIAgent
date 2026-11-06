"""Curriculum routing and practice adapter for the on-demand unified loop.

This adapter runs practice projects and returns memory for a fresh target retry. The same persistent Curriculum
Agent first routes a target Verifier Agent's FAIL to REVISE or EVOLVE. After EVOLVE,
it runs only null-task practice projects and returns when it declares
``READY_FOR_RETRY``.
"""
# 统一循环的"按需"课程路由与练习适配器。
# 该适配器运行练习项目，并为全新的目标重试返回记忆。同一个持久化的
# Curriculum（课程）Agent 先把目标 Verifier（验证者）Agent 的 FAIL 路由为
# REVISE（重试目标）或 EVOLVE（进化/练习）。EVOLVE 之后，它只运行"空任务"
# 练习项目，并在自己宣布 ``READY_FOR_RETRY`` 时交还目标控制权。

from __future__ import annotations

import hashlib
# hashlib：对目标、报告、决策等文本计算 SHA-256 指纹。
import re
# re：解析交接令牌（route/decision）的正则。
from collections.abc import Callable
# Callable：事件回调的类型注解。
from dataclasses import dataclass, field
# dataclass：会话与决策的不可变数据结构。
from pathlib import Path
# Path：路径对象。
from typing import Any
# Any：通用类型注解。

from explore.charter import (
    self_evolving_curriculum_charter,
    unified_curriculum_pass_charter,
    unified_curriculum_route_charter,
)
# 课程章程（charter）生成器：统一课程、PASS 审查、FAIL 路由三种提示的构造函数。
from explore.practice_loop import (
    _UNIFIED_CURRICULUM_TOKEN,
    CURRICULUM_HANDOFF,
    CURRICULUM_NOTES,
    DEFAULT_CORPUS,
    PROJECT_ROOT,
    PracticeBoundaryError,
    PracticeHooks,
    PracticeInfrastructureError,
    PracticeResult,
    _atomic_json,
    _atomic_text,
    _capture_owned_tree,
    _continuation_after_transport,
    _curriculum_runtime_contract,
    _fresh_vm,
    _manifest,
    _parse_unique_handoff,
    _push_canonical_memory,
    _read_memory_tree,
    _run_handoff_phase,
)
# 练习主循环导出的工具：交接令牌与路径、默认语料、项目根、三类错误、
# 原子写、树捕获、传输后续提示、运行时契约、全新 VM、记忆清单/读取、
# 唯一交接解析、规范记忆推送、交接阶段运行器。
from explore.target_learning import (
    PracticePublicationError,
    _memory_tree_sha256,
    _outcome_text,
    _promote_learning,
    _require_owned_project_shape,
    _run_practice_attempt,
    _target_audit_surface,
)
# 目标学习导出的工具：发布错误、记忆树哈希、成果文本、学习晋升、
# 项目树形状校验、练习尝试执行、目标审计面。


CURRICULUM_ROUTE_HANDOFF = "/home/user/curriculum_route.md"
# 路由专用交接文件在 guest 内的路径。
_CURRICULUM_ROUTE_TOKEN = re.compile(r"^ROUTE: (REVISE|EVOLVE)$")
# FAIL 后路由令牌：REVISE（再试目标）或 EVOLVE（进入练习）。
_CURRICULUM_PASS_TOKEN = re.compile(r"^ROUTE: (HANDOFF|VERIFY_MORE)$")
# PASS 后路由令牌：HANDOFF（收尾交付）或 VERIFY_MORE（继续验证）。
_PHASE2_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_TARGET|STALLED)$")
# Phase-2 课程决策令牌：发布 PROJECT / 交还目标 READY_FOR_TARGET / 停滞 STALLED。


@dataclass
class UnifiedCurriculumSession:
    """One target-scoped Curriculum Agent paused across target retries."""

    # 一个"目标域"的课程 Agent 会话：跨目标重试持续暂停/恢复。
    history: list[dict[str, Any]] = field(default_factory=list)
    # 课程对话历史（跨轮延续）。
    notes: str = ""
    # 课程私有笔记（发布到 guest 后回读累积）。
    project_summaries: list[str] = field(default_factory=list)
    # 历次练习项目的一行摘要（进入后续提示的上下文）。
    route_summaries: list[str] = field(default_factory=list)
    # 历次路由决策的一行摘要。
    turns: int = 0
    # 课程轮计数。
    waves: int = 0
    # 重试波次计数（每轮 evolve_until_ready 为一波）。
    target_failures: int = 0
    # 目标 FAIL 路由次数。
    target_passes: int = 0
    # 目标 PASS 审查次数。


@dataclass(frozen=True)
class CurriculumRoutingDecision:
    """One high-level decision made after a target Verifier Agent FAIL."""

    # 目标 Verifier Agent FAIL 之后的一次高层决策。
    route: str
    # 路由值：REVISE 或 EVOLVE。
    report: str
    # 课程给出的完整路由报告。

    def __post_init__(self) -> None:
        # 构造后校验（fail-closed）。
        normalized = str(self.route).strip().upper()
        # 归一化路由值。
        if normalized not in {"REVISE", "EVOLVE"}:
            # 只接受两个合法路由。
            raise PracticeInfrastructureError(
                f"unknown Curriculum Agent target route: {self.route!r}")
            # 非法路由即基础设施错误。
        if not isinstance(self.report, str) or not self.report.strip():
            # 报告必须是非空字符串。
            raise PracticeInfrastructureError(
                "Curriculum Agent routing report is empty")
            # 空报告即基础设施错误。
        object.__setattr__(self, "route", normalized)
        # frozen 数据类：用 object.__setattr__ 写回归一化值。


@dataclass(frozen=True)
class CurriculumPassDecision:
    """One lifecycle decision made after a target Verifier Agent PASS."""

    # 目标 Verifier Agent PASS 之后的一次生命周期决策。
    route: str
    # 路由值：HANDOFF 或 VERIFY_MORE。
    report: str
    # 课程给出的完整审查报告。

    def __post_init__(self) -> None:
        # 构造后校验（fail-closed）。
        normalized = str(self.route).strip().upper()
        # 归一化路由值。
        if normalized not in {"HANDOFF", "VERIFY_MORE"}:
            # 只接受两个合法路由。
            raise PracticeInfrastructureError(
                f"unknown Curriculum Agent PASS route: {self.route!r}")
            # 非法路由即基础设施错误。
        if not isinstance(self.report, str) or not self.report.strip():
            # 报告必须是非空字符串。
            raise PracticeInfrastructureError(
                "Curriculum Agent PASS routing report is empty")
            # 空报告即基础设施错误。
        object.__setattr__(self, "route", normalized)
        # 写回归一化值。


def route_target_pass(
        vm, root: str, target: str, verifier_orientation: str,
        verifier_report: str, curriculum_cfg, *, hooks: PracticeHooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        ) -> CurriculumPassDecision:
    """Let one persistent Curriculum Agent authorize HANDOFF or VERIFY_MORE.

    The candidate and target environment remain inaccessible.  This audits only
    whether the Verifier's own complete report is sufficient and coherent enough to
    end the outer search; it cannot replace or override the local PASS/FAIL role.
    """

    # 让持久化课程 Agent 在目标 PASS 后授权 HANDOFF 或 VERIFY_MORE。
    # 候选与目标环境对其不可见；它只审查"Verifier 的完整报告是否足以结束外层
    # 搜索"，不能替代或推翻本地的 PASS/FAIL 角色。
    hooks = hooks or PracticeHooks()
    # 未提供 hooks 时用默认空钩子。
    session = session or UnifiedCurriculumSession()
    # 未提供会话时新建。
    if not target.strip():
        # 目标不可为空。
        raise PracticeInfrastructureError("immutable target query is empty")
        # 基础设施错误。
    if not verifier_report.strip():
        # PASS 报告不可为空。
        raise PracticeInfrastructureError("target Verifier Agent PASS report is empty")
        # 基础设施错误。
    if not any(
            line.strip().upper() == "VERDICT: PASS"
            for line in verifier_report.splitlines()):
        # 报告必须包含已提交的 VERDICT: PASS 行。
        raise PracticeInfrastructureError(
            "Curriculum PASS routing requires a committed Verifier Agent PASS")
        # 缺少判定行即基础设施错误。

    audit_target = _target_audit_surface(target)
    # 目标审计面：练习边界检查所允许的目标文本范围。
    for label, text in (
            ("orientation", verifier_orientation),
            ("PASS report", verifier_report)):
        # 逐段审计定向说明与 PASS 报告。
        if text and hooks.audit_text(
                text, mode="exam", authorized_instruction=audit_target):
            # 命中边界信号（exam 模式，允许授权指令）。
            raise PracticeBoundaryError(
                f"target Verifier Agent {label} failed target boundary")
            # 越界即隔离错误。

    route_root = Path(root)
    # 路由工件根目录。
    route_root.mkdir(parents=True, exist_ok=True)
    # 确保目录存在。
    _fresh_vm(hooks, vm, target)
    # 准备全新 VM（课程环境无候选、无目标环境）。
    session.target_passes += 1
    # PASS 审查计数递增。
    session.turns += 1
    # 课程轮计数递增。
    prompt = unified_curriculum_pass_charter(
        target, verifier_orientation, verifier_report,
        route_history="\n".join(session.route_summaries),
        curriculum_notes=session.notes,
        handoff_path=CURRICULUM_ROUTE_HANDOFF,
        notes_path=CURRICULUM_NOTES)
    # 组装 PASS 审查章程：目标、定向、报告、历史路由摘要、课程笔记与路径。
    decision, history, _ = _run_handoff_phase(
        hooks=hooks, vm=vm, cfg=curriculum_cfg, prompt=prompt,
        role="CURRICULUM", handoff_path=CURRICULUM_ROUTE_HANDOFF,
        token_pattern=_CURRICULUM_PASS_TOKEN,
        sink_root=str(route_root / f"turn_{session.turns:03d}"),
        target=audit_target, history=session.history,
        continuation=bool(session.history),
        handoff_parser=_parse_unique_handoff,
        require_completed_publication=True, audit_mode="exam")
    # 运行课程交接阶段：解析 HANDOFF/VERIFY_MORE 令牌，工件落盘，exam 模式审计。
    session.history = history
    # 保存延续后的对话历史。
    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
    # 回读 guest 内课程笔记。
    if notes:
        # 有笔记则审计并保存。
        if hooks.audit_text(
                notes, mode="exam", authorized_instruction=audit_target):
            # 笔记越界。
            raise PracticeBoundaryError("Curriculum notes failed target boundary")
            # 隔离错误。
        session.notes = notes
        # 更新会话笔记。
    session.route_summaries.append(
        f"target PASS review {session.target_passes}: {decision.token}")
    # 追加本次 PASS 审查摘要。
    record = {
        "schema_version": 1,
        "kind": "curriculum_target_pass_route",
        "target_pass_review": session.target_passes,
        "route": decision.token,
        "verifier_orientation_sha256": hashlib.sha256(
            verifier_orientation.encode("utf-8")).hexdigest(),
        "verifier_report_sha256": hashlib.sha256(
            verifier_report.encode("utf-8")).hexdigest(),
        "curriculum_report": decision.text,
    }
    # 组装审计记录：输入哈希与完整课程报告。
    _atomic_json(
        route_root / f"pass_review_{session.target_passes:03d}.json", record)
    # 原子落盘 PASS 审查记录。
    if event_sink is not None:
        # 提供了事件出口则发出事件。
        event_sink(
            "TARGET_PASS_ROUTED", status="completed",
            state={"route": decision.token},
            payload={
                "target_pass_review": session.target_passes,
                "route": decision.token,
                "verifier_report_sha256": record["verifier_report_sha256"],
                "curriculum_report_sha256": hashlib.sha256(
                    decision.text.encode("utf-8")).hexdigest(),
            })
        # 发出 PASS 路由事件（附报告与决策哈希）。
    return CurriculumPassDecision(decision.token, decision.text)
    # 返回决策对象。


def route_target_failure(
        vm, root: str, target: str, verifier_report: str, curriculum_cfg,
        *, hooks: PracticeHooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        ) -> CurriculumRoutingDecision:
    """Let one persistent Curriculum Agent route a concrete target FAIL.

    This phase has no target environment, candidate, Actor-private artifacts, or
    evaluator. It owns only the high-level REVISE/EVOLVE choice and preserves its
    semantic context for later failures and practice search.
    """

    # 让持久化课程 Agent 对具体的目标 FAIL 做路由。
    # 此阶段没有目标环境、候选、Actor 私有工件或评估器；它只拥有高层
    # REVISE/EVOLVE 的选择权，并为后续失败与练习搜索保留语义上下文。
    hooks = hooks or PracticeHooks()
    # 未提供 hooks 时用默认空钩子。
    session = session or UnifiedCurriculumSession()
    # 未提供会话时新建。
    if not target.strip():
        # 目标不可为空。
        raise PracticeInfrastructureError("immutable target query is empty")
        # 基础设施错误。
    if not verifier_report.strip():
        # FAIL 报告不可为空。
        raise PracticeInfrastructureError("target Verifier Agent FAIL report is empty")
        # 基础设施错误。
    if not any(
            line.strip().upper() == "VERDICT: FAIL"
            for line in verifier_report.splitlines()):
        # 报告必须包含已提交的 VERDICT: FAIL 行。
        raise PracticeInfrastructureError(
            "Curriculum routing requires a committed Verifier Agent FAIL")
        # 缺少判定行即基础设施错误。

    audit_target = _target_audit_surface(target)
    # 目标审计面。
    if hooks.audit_text(
            verifier_report, mode="exam",
            authorized_instruction=audit_target):
        # FAIL 报告越界。
        raise PracticeBoundaryError(
            "target Verifier Agent FAIL report failed target boundary")
        # 隔离错误。

    route_root = Path(root)
    # 路由工件根目录。
    route_root.mkdir(parents=True, exist_ok=True)
    # 确保目录存在。
    _fresh_vm(hooks, vm, target)
    # 准备全新 VM。
    session.target_failures += 1
    # FAIL 路由计数递增。
    session.turns += 1
    # 课程轮计数递增。
    prompt = unified_curriculum_route_charter(
        target, verifier_report,
        route_history="\n".join(session.route_summaries),
        curriculum_notes=session.notes,
        handoff_path=CURRICULUM_ROUTE_HANDOFF,
        notes_path=CURRICULUM_NOTES)
    # 组装 FAIL 路由章程：目标、报告、历史路由摘要、课程笔记与路径。
    decision, history, _ = _run_handoff_phase(
        hooks=hooks, vm=vm, cfg=curriculum_cfg, prompt=prompt,
        role="CURRICULUM", handoff_path=CURRICULUM_ROUTE_HANDOFF,
        token_pattern=_CURRICULUM_ROUTE_TOKEN,
        sink_root=str(route_root / f"turn_{session.turns:03d}"),
        target=audit_target, history=session.history,
        continuation=bool(session.history),
        handoff_parser=_parse_unique_handoff,
        require_completed_publication=True, audit_mode="exam")
    # 运行课程交接阶段：解析 REVISE/EVOLVE 令牌，工件落盘，exam 模式审计。
    session.history = history
    # 保存延续后的对话历史。
    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
    # 回读课程笔记。
    if notes:
        # 有笔记则审计并保存。
        if hooks.audit_text(
                notes, mode="exam", authorized_instruction=audit_target):
            # 笔记越界。
            raise PracticeBoundaryError("Curriculum notes failed target boundary")
            # 隔离错误。
        session.notes = notes
        # 更新会话笔记。
    session.route_summaries.append(
        f"target failure {session.target_failures}: {decision.token}")
    # 追加本次失败路由摘要。
    record = {
        "schema_version": 1,
        "kind": "curriculum_target_failure_route",
        "target_failure": session.target_failures,
        "route": decision.token,
        "verifier_report_sha256": hashlib.sha256(
            verifier_report.encode("utf-8")).hexdigest(),
        "curriculum_report": decision.text,
    }
    # 组装审计记录：报告哈希与完整课程报告。
    _atomic_json(route_root / f"route_{session.target_failures:03d}.json", record)
    # 原子落盘路由记录。
    if event_sink is not None:
        # 提供了事件出口则发出事件。
        event_sink(
            "TARGET_FAILURE_ROUTED", status="completed",
            state={"route": decision.token},
            payload={
                "target_failure": session.target_failures,
                "route": decision.token,
                "verifier_report_sha256": record["verifier_report_sha256"],
                "curriculum_report_sha256": hashlib.sha256(
                    decision.text.encode("utf-8")).hexdigest(),
            })
        # 发出 FAIL 路由事件（附报告与决策哈希）。
    return CurriculumRoutingDecision(decision.token, decision.text)
    # 返回决策对象。


def evolve_until_ready(
        vm, root: str, target: str, triggering_report: str,
        triggering_diagnosis: str,
        initial_memory: dict[str, bytes], actor_cfg, verifier_cfg,
        curriculum_cfg, memory_cfg, *, corpus_path: str = DEFAULT_CORPUS,
        routing_report: str = "",
        trigger_authority: str = "curriculum_route",
        triggering_outcome: str = "FAIL",
        curriculum_memory_access: str = "read_only",
        agentic_verifier_cfg=None,
        target_visible_inputs: tuple[str, ...] | None = None,
        hooks: PracticeHooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        session: UnifiedCurriculumSession | None = None,
        resume_boundary=None) -> PracticeResult:
    """Let Curriculum choose experiences until it returns target control.

    ``phase2_outcome_protocol`` accepts either a grounded PASS or FAIL after the
    same Actor has updated memory.  The Curriculum Agent may inspect that memory
    and choose projects, but cannot edit or approve it. Other authorities retain
    the historical FAIL-only contract. A stalled, quarantined, or infrastructure
    result is not readiness and must not be converted into a target retry.
    """

    # 让课程 Agent 选择学习经验，直到它交还目标控制权。
    # ``phase2_outcome_protocol`` 权威接受同一 Actor 更新记忆后的落地 PASS 或 FAIL；
    # 课程 Agent 可检视该记忆并选择项目，但不能编辑或批准它。其他权威保持历史的
    # "仅 FAIL"契约。停滞/隔离/基础设施结果都不是 READY，绝不能转化为目标重试。
    if curriculum_memory_access not in {"read_only", "none"}:
        # 课程记忆访问模式只允许只读或无。
        raise ValueError("unknown Curriculum memory access mode")
        # 非法模式。
    if (curriculum_memory_access == "none"
            and trigger_authority != "phase2_outcome_protocol"):
        # "无记忆"消融只在 Phase-2 协议下允许。
        raise ValueError("Curriculum memory ablation requires Phase 2")
        # 组合非法。
    if (resume_boundary is not None and curriculum_memory_access !=
            getattr(resume_boundary, 'curriculum_memory_access', 'read_only')):
        # 恢复不允许改变课程记忆访问模式。
        raise ValueError("recovery cannot change Curriculum memory access")
        # 组合非法。
    hooks = hooks or PracticeHooks()
    # 未提供 hooks 时用默认空钩子。
    session = session or UnifiedCurriculumSession()
    # 未提供会话时新建。
    if resume_boundary is not None:
        # 有恢复边界时先验证其与当前调用一致。
        resume_boundary.validate_evolution(
            root, target, initial_memory, triggering_report,
            triggering_diagnosis, trigger_authority, triggering_outcome)
        # 校验根、目标、初始记忆、报告、诊断、权威与结果全部吻合。
    session.waves += 1
    # 重试波次计数递增。
    lineage = Path(root)
    # 本波血统根目录。
    memory_dir = lineage / "memory"
    # 规范记忆目录。
    journal_dir = lineage / "memory_journal"
    # 记忆日志目录。
    episodes_dir = lineage / "episodes"
    # 练习集目录。
    curriculum_dir = lineage / "curriculum"
    # 课程轮目录。
    trigger_dir = lineage / "trigger"
    # 触发器工件目录。
    state_path = lineage / "state.json"
    # 周期状态文件。
    for path in (lineage, memory_dir, journal_dir, episodes_dir,
                 curriculum_dir, trigger_dir):
        # 逐一创建目录。
        path.mkdir(parents=True, exist_ok=True)
        # 已存在则跳过。

    project_index = resume_boundary.projects if resume_boundary else 0
    # 已完成项目数（恢复时继承）。
    experience_index = project_index
    # 已完成学习经验数（与项目数同步）。
    curriculum_history = session.history
    # 课程对话历史（跨轮延续）。
    project_summaries = session.project_summaries
    # 项目摘要列表（会话持有）。
    normalized_outcome = str(triggering_outcome).strip().upper()
    # 归一化触发结果。
    if normalized_outcome not in {"PASS", "FAIL"}:
        # 只接受 PASS/FAIL 两种触发结果。
        raise PracticeInfrastructureError(
            f"unknown grounded target outcome: {triggering_outcome!r}")
        # 非法结果即基础设施错误。
    if trigger_authority == "curriculum_route":
        # 课程路由权威：说明本次进化由课程 EVOLVE 决定。
        project_summaries.append(
            f"target retry wave {session.waves}: Curriculum Agent routed EVOLVE "
            "after persistent target Verifier Agent FAIL")
        # 追加波次摘要。
    elif trigger_authority == "verifier_fail_protocol":
        # 验证失败协议权威：基准协议在 FAIL 后直接进入学习。
        project_summaries.append(
            f"target retry wave {session.waves}: the self-evolving benchmark "
            "protocol entered learning after a target Verifier Agent FAIL")
        # 追加波次摘要。
    elif trigger_authority == "phase2_outcome_protocol":
        # Phase-2 结果协议权威：同一 Actor 提交落地记忆后由课程选择下一经验。
        project_summaries.append(
            f"Phase-2 target outcome {normalized_outcome}: the same Actor Agent "
            "committed grounded memory before Curriculum selected the next "
            "experience")
        # 追加波次摘要。
    else:
        # 未知权威。
        raise PracticeInfrastructureError(
            f"unknown evolution trigger authority: {trigger_authority!r}")
        # 基础设施错误。
    if resume_boundary is not None:
        # 恢复时回填已完成项目的摘要。
        records = (resume_boundary.project_records or [resume_boundary.project]
                   if resume_boundary.projects else [])
        # 取项目记录（无记录则用单一项目）。
        project_summaries.extend(
            f'practice project {i}: ' + record['terminal_outcome']
            for i, record in enumerate(records, 1))
        # 逐条追加"练习项目 i：终局"摘要。
    curriculum_notes = session.notes
    # 当前课程笔记。
    audit_target = _target_audit_surface(target, target_visible_inputs)
    # 目标审计面（含目标可见输入）。

    def emit(event_type: str, *, status: str, project_open: bool,
             memory_phase_open: bool, payload: dict[str, Any]) -> Any:
        # 事件发射的内部封装：统一附加状态机两开关。
        if event_sink is None:
            # 无事件出口则忽略。
            return None
            # 返回空。
        return event_sink(
            event_type, status=status,
            state={
                "project_open": project_open,
                "memory_phase_open": memory_phase_open,
                "recovery_open": False,
            },
            payload=payload)
        # 转发事件（recovery_open 恒为 False）。

    def finish(status: str, reason: str = "", terminal_text: str = "") \
            -> PracticeResult:
        # 收尾函数：写终态 state.json 并返回 PracticeResult。
        memory = _read_memory_tree(str(memory_dir))
        # 读取最终记忆树。
        _atomic_json(state_path, {
            "schema_version": 1,
            "kind": "unified_evolution_cycle",
            "status": status,
            "projects": project_index,
            "learning_experiences": experience_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "target_sha256": hashlib.sha256(
                target.encode("utf-8")).hexdigest(),
            "trigger_report_sha256": hashlib.sha256(
                triggering_report.encode("utf-8")).hexdigest(),
            "trigger_authority": trigger_authority,
            "triggering_outcome": normalized_outcome,
            "curriculum_route_sha256": hashlib.sha256(
                routing_report.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(memory),
            "memory_tree_sha256": _memory_tree_sha256(memory),
        })
        # 原子写周期终态：状态、计数、原因、各输入指纹与记忆清单/树哈希。
        return PracticeResult(
            status=status,
            projects=project_index,
            root=str(lineage),
            memory_dir=str(memory_dir),
            reason=reason,
            terminal_text=terminal_text,
            last_project=project_index,
        )
        # 返回结果对象。

    try:
        if not target.strip():
            # 目标不可为空。
            raise PracticeInfrastructureError("immutable target query is empty")
            # 基础设施错误。
        if not triggering_report.strip():
            # 触发报告不可为空。
            raise PracticeInfrastructureError("EVOLVE trigger report is empty")
            # 基础设施错误。
        if not triggering_diagnosis.strip():
            # Actor 学习诊断不可为空。
            raise PracticeInfrastructureError(
                "target Actor learning diagnosis is empty")
            # 基础设施错误。
        if not any(
                line.strip().upper() == f"VERDICT: {normalized_outcome}"
                for line in triggering_report.splitlines()):
            # 报告必须包含与触发结果一致的判定行。
            raise PracticeInfrastructureError(
                "learning trigger lacks the committed Verifier Agent "
                f"{normalized_outcome}")
            # 缺少判定行即基础设施错误。
        if (trigger_authority != "phase2_outcome_protocol"
                and normalized_outcome != "FAIL"):
            # 非 Phase-2 权威只允许 FAIL 触发（历史契约）。
            raise PracticeInfrastructureError(
                "historical evolution authorities require a concrete FAIL")
            # 基础设施错误。
        if trigger_authority == "curriculum_route":
            # 课程路由权威必须附带 ROUTE: EVOLVE 报告。
            if not any(
                    line.strip().upper() == "ROUTE: EVOLVE"
                    for line in routing_report.splitlines()):
                # 缺少 EVOLVE 路由行。
                raise PracticeInfrastructureError(
                    "EVOLVE trigger lacks the Curriculum Agent route")
                # 基础设施错误。
        elif routing_report.strip():
            # 其他权威不得伪造课程路由报告。
            raise PracticeInfrastructureError(
                "outcome protocol trigger must not manufacture a "
                "Curriculum Agent routing report")
            # 基础设施错误。
        if _read_memory_tree(str(memory_dir)) and resume_boundary is None:
            # 非恢复调用要求全新的空血统根（记忆目录必须为空）。
            raise PracticeInfrastructureError(
                "unified evolution cycle requires a new empty lineage root")
            # 基础设施错误。

        hooks.validate_corpus(corpus_path)
        # 校验练习语料存在且合法。
        if hooks.audit_text(
                triggering_report, mode="exam",
                authorized_instruction=audit_target):
            # 触发报告越界。
            raise PracticeBoundaryError("EVOLVE trigger report failed target boundary")
            # 隔离错误。
        if hooks.audit_text(
                triggering_diagnosis, mode="exam",
                authorized_instruction=audit_target):
            # 学习诊断越界。
            raise PracticeBoundaryError(
                "target Actor learning diagnosis failed target boundary")
            # 隔离错误。
        if routing_report and hooks.audit_text(
                routing_report, mode="exam",
                authorized_instruction=audit_target):
            # 路由报告越界。
            raise PracticeBoundaryError(
                "Curriculum Agent routing report failed target boundary")
            # 隔离错误。
        accepted_memory = hooks.audit_memory(
            initial_memory, corpus_path,
            str(lineage / "audit_rejects.jsonl"),
            authorized_instruction=audit_target, require_corpus=True)
        # 对初始记忆做语料审计，得到通过审计的子集（拒绝项写入审计文件）。
        if accepted_memory != initial_memory:
            # 有记忆被拒：整体不接受。
            raise PracticeBoundaryError("input memory failed target boundary")
            # 隔离错误。
        if resume_boundary is None:
            # 非恢复调用：安装初始记忆到规范记忆目录。
            hooks.install_memory(str(memory_dir), accepted_memory)
            # 逐文件写入。

        trigger_record = {
            "schema_version": 1,
            "kind": "target_verifier_learning_outcome",
            "target": target,
            "verifier_report": triggering_report,
            "verifier_outcome": normalized_outcome,
            "trigger_authority": trigger_authority,
            "curriculum_routing_report": routing_report or None,
            "actor_learning_diagnosis": triggering_diagnosis,
            "memory_before": _manifest(accepted_memory),
        }
        # 组装触发器记录：目标、报告、结果、权威、路由报告、诊断与记忆前清单。
        if resume_boundary is None:
            # 非恢复调用才落盘（恢复时触发器工件已存在）。
            _atomic_json(trigger_dir / "outcome.json", trigger_record)
            # 原子写触发器记录。
        route_context = (
            "\n\nCURRICULUM AGENT EVOLVE ROUTING HANDOFF (verbatim):\n"
            + routing_report
            if trigger_authority == "curriculum_route" else
            "\n\nLIFECYCLE TRANSITION:\nThe declared self-evolving benchmark "
            f"protocol entered learning after the target Verifier Agent "
            f"{normalized_outcome}. No Curriculum Agent routing decision was "
            "requested or fabricated.")
        # 路由上下文：课程路由权威附原始路由交接；否则说明"无路由决策、也未伪造"。
        latest_outcome = (
            "ORIGINAL NORTH-STAR TARGET (verbatim):\n" + target
            + "\n\nPERSISTENT TARGET VERIFIER AGENT REPORT (verbatim):\n"
            + triggering_report
            + route_context
            + "\n\nSAME TARGET ACTOR AGENT LEARNING DIAGNOSIS (verbatim):\n"
            + triggering_diagnosis)
        # 拼装"最新成果"文本：北极星目标、验证者报告、路由上下文与 Actor 诊断（逐字引用）。
        if resume_boundary is None:
            # 非恢复调用才落盘。
            _atomic_text(trigger_dir / "outcome.md", latest_outcome)
            # 原子写成果文。
        emit(
            ("EVOLVE_TRIGGER_IMPORTED" if resume_boundary is None
             else ("PRACTICE_MEMORY_BOUNDARY_RESUMED"
                   if getattr(resume_boundary, "pending_memory_learning", None) is not None
                   else "UNVERIFIED_PRACTICE_BOUNDARY_RESUMED"
                   if getattr(resume_boundary, "pending_practice", None) is not None
                   else "COMPLETED_PROJECT_BOUNDARY_RESUMED")), status="completed",
            project_open=getattr(resume_boundary, "pending_practice", None) is not None,
            memory_phase_open=getattr(resume_boundary, "pending_memory_learning", None) is not None,
            payload={
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
                "trigger_report_sha256": hashlib.sha256(
                    triggering_report.encode("utf-8")).hexdigest(),
                "trigger_diagnosis_sha256": hashlib.sha256(
                    triggering_diagnosis.encode("utf-8")).hexdigest(),
                "trigger_authority": trigger_authority,
                "triggering_outcome": normalized_outcome,
                "curriculum_route_sha256": hashlib.sha256(
                    routing_report.encode("utf-8")).hexdigest(),
                "memory_tree_sha256": _memory_tree_sha256(
                    _read_memory_tree(str(memory_dir))
                    if getattr(resume_boundary, "pending_practice", None) is not None
                    else accepted_memory),
            })
        # 发出触发导入/边界恢复事件：事件类型随恢复形态不同；附全部指纹。
        # （此注释行之外均为单条 emit 调用的参数。）

        pending_practice = getattr(resume_boundary, "pending_practice", None)
        # 恢复边界携带的"待恢复练习"状态。
        recovering_turn = resume_boundary is not None and pending_practice is None
        # 是否为"恢复课程轮"（无待恢复练习，恢复的是未完成的课程回合）。
        if recovering_turn:
            # 恢复课程轮时用保存的成果文（而非重建的 latest_outcome）。
            latest_outcome = resume_boundary.latest_outcome
            # 以边界保存的成果文为准。
        while True:
            # 主循环：课程选项目 -> 练习执行 -> 学习晋升 -> 再回到课程。
            practice_resume = pending_practice
            # 取出待恢复练习（若有）。
            if practice_resume is not None:
                # 有待恢复练习：直接进入该练习，不再让课程重新选择。
                pending_practice = None
                # 清空标志（只消费一次）。
                project_index += 1
                # 项目序号递增。
                episode_dir = episodes_dir / f"ep{project_index:03d}"
                # 本练习集目录。
                fixture_dir = episode_dir / "fixtures"
                # 本练习夹具目录。
                project = practice_resume["project"]
                # 恢复的项目要求文本。
                emit("PRACTICE_MEMORY_RESUMED" if practice_resume.get("resume_kind") == "verified_memory"
                     else "UNVERIFIED_PRACTICE_RESUMED", status="in_progress",
                     project_open=True, memory_phase_open=practice_resume.get("resume_kind") == "verified_memory",
                     payload={"project_index": project_index,
                              "recovery_plan": str(resume_boundary.plan_path)})
                # 发出练习恢复事件（区分"已验证记忆"与"未验证证据请求"两种）。
            else:
                # The target environment was already discarded by the unified state
                # machine. Curriculum and practice run only on a fresh null-task VM.
                # （中文）目标环境已被统一状态机丢弃；课程与练习只在全新的空任务 VM 上运行。
                _fresh_vm(hooks, vm, target)
                # 准备全新 VM。
                curriculum_memory_dir = memory_dir
                # 课程默认挂载规范记忆（只读）。
                if curriculum_memory_access == "none":
                    # Use the ordinary full-replacement transport with an empty
                    # source, including after a practice Actor used this VM.
                    # Canonical memory and all Actor learning paths are unchanged.
                    # （中文）消融模式改挂空记忆目录：走普通的全量替换传输（空源），
                    # 规范记忆与所有 Actor 学习路径都不受影响。
                    curriculum_memory_dir = lineage / "curriculum_empty_memory"
                    # 空挂载目录。
                    curriculum_memory_dir.mkdir(exist_ok=True)
                    # 确保存在。
                    if any(curriculum_memory_dir.iterdir()):
                        # 空挂载目录必须保持为空。
                        raise PracticeInfrastructureError(
                            "Curriculum ablation attachment must remain empty")
                        # 非空即基础设施错误。
                _push_canonical_memory(hooks, vm, str(curriculum_memory_dir))
                # 把（可能是空的）课程记忆推送进 VM。
                session.turns += 1
                # 课程轮计数递增。
                curriculum_sink = curriculum_dir / f"turn_{session.turns:03d}"
                # 本轮课程工件目录。
                attached_memory = _read_memory_tree(str(curriculum_memory_dir))
                # 读取实际挂载的记忆。
                _atomic_json(curriculum_sink / "memory_access.json", {
                    "schema_version": 1,
                    "access": curriculum_memory_access,
                    "attached_files": len(attached_memory),
                    "attached_bytes": sum(len(v) for v in attached_memory.values()),
                    "attached_tree_sha256": _memory_tree_sha256(attached_memory),
                    "actor_learning_diagnosis_available": True,
                })
                # 原子记录本轮课程可访问的记忆审计信息。
                curriculum_prompt = self_evolving_curriculum_charter(
                    target,
                    project_history="\n".join(project_summaries),
                    latest_outcome=latest_outcome,
                    curriculum_notes=curriculum_notes,
                    handoff_path=CURRICULUM_HANDOFF,
                    notes_path=CURRICULUM_NOTES,
                    unified_retry=(trigger_authority !=
                                   "phase2_outcome_protocol"),
                    phase2_outcome=(trigger_authority ==
                                    "phase2_outcome_protocol"),
                    curriculum_memory_access=curriculum_memory_access,
                ) + _curriculum_runtime_contract()
                # 组装课程章程：目标、项目史、最新成果、笔记、路径与运行时契约；
                # 令牌模式按权威选择（unified_retry/phase2_outcome 开关）。
                turn_cfg = curriculum_cfg
                # 本轮课程配置。
                if recovering_turn:
                    # 恢复课程轮时收窄预算并附恢复说明。
                    from dataclasses import replace
                    # 局部导入 replace。
                    turn_cfg = replace(
                        curriculum_cfg, max_iters=resume_boundary.remaining_iters,
                        wall_clock_secs=resume_boundary.remaining_wall)
                    # 以边界保存的剩余预算为限。
                    curriculum_prompt += resume_boundary.recovery_observation
                    # 提示中追加恢复观察说明。

                while True:
                    # 课程轮内循环：直到课程发布可执行的项目或终止。
                    decision, curriculum_history, _ = _run_handoff_phase(
                        hooks=hooks, vm=vm, cfg=turn_cfg,
                        prompt=curriculum_prompt, role="CURRICULUM",
                        handoff_path=CURRICULUM_HANDOFF,
                        token_pattern=(
                            _PHASE2_CURRICULUM_TOKEN
                            if trigger_authority == "phase2_outcome_protocol"
                            else _UNIFIED_CURRICULUM_TOKEN),
                        sink_root=str(curriculum_sink), target=audit_target,
                        history=curriculum_history,
                        continuation=bool(curriculum_history),
                        require_completed_publication=True)
                    # 运行课程交接阶段：按权威解析 PROJECT/READY/STALLED 令牌。
                    session.history = curriculum_history
                    # 保存延续后的历史。
                    recovering_turn = False
                    # 恢复说明只在首次提示注入，此后清除标志。
                    notes = hooks.read_guest_text(vm, CURRICULUM_NOTES)
                    # 回读课程笔记。
                    if notes:
                        # 有笔记则审计并保存。
                        if hooks.audit_text(
                                notes, mode="practice",
                                authorized_instruction=audit_target):
                            # 笔记越界（practice 模式，允许授权指令）。
                            raise PracticeBoundaryError(
                                "Curriculum notes failed target boundary")
                            # 隔离错误。
                        curriculum_notes = notes
                        # 更新局部笔记。
                        session.notes = notes
                        # 更新会话笔记。

                    if decision.token == "STALLED":
                        # 课程自报"无有效新假设"：终止为 stalled。
                        _atomic_text(
                            lineage / "curriculum_terminal.md", decision.text)
                        # 原子落盘课程终局文。
                        return finish(
                            "stalled",
                            "Curriculum Agent reported no productive new hypothesis",
                            decision.text)
                        # 返回停滞结果。

                    if decision.token in {"READY_FOR_RETRY", "READY_FOR_TARGET"}:
                        # 课程宣布就绪：把控制权交还目标循环。
                        _atomic_text(
                            lineage / "curriculum_ready_for_retry.md", decision.text)
                        # 原子落盘就绪决策文。
                        emit(
                            decision.token, status="completed",
                            project_open=False, memory_phase_open=False,
                            payload={
                                "projects": project_index,
                                "memory_tree_sha256": _memory_tree_sha256(
                                    _read_memory_tree(str(memory_dir))),
                                "curriculum_decision_sha256": hashlib.sha256(
                                    decision.text.encode("utf-8")).hexdigest(),
                            })
                        # 发出就绪事件（附项目数、记忆树哈希与决策哈希）。
                        return finish(
                            "ready_for_retry",
                            "Curriculum Agent returned control to the target loop",
                            decision.text)
                        # 返回就绪结果。

                    if (not decision.body or PROJECT_ROOT not in decision.body):
                        # 项目体为空或未指向项目根：要求课程重发。
                        curriculum_prompt = _continuation_after_transport(
                            "CURRICULUM", CURRICULUM_HANDOFF,
                            f"a PROJECT must be nonempty and name {PROJECT_ROOT}")
                        # 生成续跑提示。
                        continue
                        # 进入下一轮课程尝试。

                    candidate_index = project_index + 1
                    # 候选项目序号。
                    episode_dir = episodes_dir / f"ep{candidate_index:03d}"
                    # 候选练习集目录。
                    fixture_dir = episode_dir / "fixtures"
                    # 候选夹具目录。
                    try:
                        # 尝试捕获课程在 VM 内发布的夹具树。
                        _require_owned_project_shape(vm)
                        # 校验项目树形状仍归课程所有。
                        _capture_owned_tree(
                            hooks, vm, f"ep{candidate_index:03d}-fixtures",
                            str(fixture_dir), audit_target)
                        # 把项目树捕获为夹具快照。
                    except PracticePublicationError:
                        # 课程未发布或发布不可回放：要求重发。
                        curriculum_prompt = _continuation_after_transport(
                            "CURRICULUM", CURRICULUM_HANDOFF,
                            f"{PROJECT_ROOT} was absent, empty, or not replayable")
                        # 生成续跑提示。
                        continue
                        # 进入下一轮课程尝试。

                    project_index = candidate_index
                    # 接受候选：项目序号推进。
                    project = decision.body
                    # 取项目要求文本。
                    _atomic_text(episode_dir / "project.md", decision.text)
                    # 原子落盘项目文（含决策全文）。
                    emit(
                        "PROJECT_OPENED", status="in_progress",
                        project_open=True, memory_phase_open=False,
                        payload={
                            "project_index": project_index,
                            "project_sha256": hashlib.sha256(
                                project.encode("utf-8")).hexdigest(),
                        })
                    # 发出项目开启事件（附项目哈希）。
                    break
                    # 跳出课程内循环，进入练习执行。

            practice = _run_practice_attempt(
                hooks=hooks, vm=vm, lineage=lineage,
                episode_dir=episode_dir, project_index=project_index,
                project=project, fixture_dir=fixture_dir, target=target,
                actor_cfg=actor_cfg, verifier_cfg=verifier_cfg,
                memory_dir=memory_dir, emit=emit,
                agentic_verifier_cfg=agentic_verifier_cfg,
                target_visible_inputs=target_visible_inputs,
                **({"resume_state": practice_resume} if practice_resume is not None else {}))
            # 执行一次完整练习（Actor -> Verifier）；恢复时携带 resume_state。
            experience_index += 1
            # 学习经验数递增。
            learned, diagnosis, _ = _promote_learning(
                hooks=hooks, vm=vm, cfg=memory_cfg, lineage=lineage,
                episode_dir=episode_dir, experience_index=experience_index,
                before_memory=practice["before_memory"],
                actor_history=practice["actor_history"],
                terminal_outcome=practice["terminal_outcome"],
                verifier_report=practice["verifier_report"], target=target,
                audit_mode="practice", emit=emit, project_open=True,
                corpus_path=corpus_path, memory_dir=memory_dir,
                journal_dir=journal_dir,
                experience_kind="unified-practice-project",
                target_visible_inputs=target_visible_inputs)
            # 学习晋升：把本次经验蒸馏进规范记忆（practice 审计模式，写记忆日志）。
            record = {
                "schema_version": 1,
                "kind": "unified_practice_project",
                "project_index": project_index,
                "request": project,
                "terminal_outcome": practice["terminal_outcome"],
                "actor_handoff": practice["actor_handoff"],
                "verifier_report": practice["verifier_report"],
                "learning_diagnosis": diagnosis,
                "memory_before": _manifest(practice["before_memory"]),
                "memory_after": _manifest(learned),
            }
            # 组装项目结果记录：请求、终局、交接文、报告、诊断与前后记忆清单。
            _atomic_json(episode_dir / "outcome.json", record)
            # 原子落盘项目结果。
            latest_outcome = _outcome_text({
                "label": f"PRACTICE PROJECT {project_index}",
                **record,
            })
            # 生成供下一轮课程使用的最新成果文本。
            _atomic_text(episode_dir / "outcome.md", latest_outcome)
            # 原子落盘成果文。
            project_summaries.append(
                f"practice project {project_index}: "
                + practice["terminal_outcome"])
            # 追加项目摘要（进入后续课程提示）。
            _atomic_json(state_path, {
                "schema_version": 1,
                "kind": "unified_evolution_cycle",
                "status": "running",
                "projects": project_index,
                "learning_experiences": experience_index,
                "target_sha256": hashlib.sha256(
                    target.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(learned),
                "memory_tree_sha256": _memory_tree_sha256(learned),
            })
            # 原子更新周期状态为 running（附最新记忆清单/树哈希）。
            emit(
                "PROJECT_CLOSED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": practice["terminal_outcome"],
                    "memory_tree_sha256": _memory_tree_sha256(learned),
                    "learning_diagnosis_sha256": hashlib.sha256(
                        diagnosis.encode("utf-8")).hexdigest(),
                })
            # 发出项目关闭事件（附终局、记忆树哈希与诊断哈希）。

    except PracticeBoundaryError as exc:
        # 边界违例：以 quarantined 终止。
        return finish("quarantined", str(exc))
        # 返回隔离结果。
    except PracticeInfrastructureError as exc:
        # 基础设施错误：以 infra 终止。
        return finish("infra", str(exc))
        # 返回基础设施结果。
    except Exception as exc:  # noqa: BLE001 - durable inspectable incident
        # 任何其他异常也记为 infra（保留原 noqa 注释：留下可检视的持久事件）。
        return finish("infra", f"{type(exc).__name__}: {exc}")
        # 返回带异常摘要的基础设施结果。


__all__ = [
    "CurriculumRoutingDecision",
    "UnifiedCurriculumSession",
    "evolve_until_ready",
    "route_target_failure",
]
# 模块公开接口：FAIL 路由、会话、就绪进化与路由决策。
