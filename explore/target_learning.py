# 【中文注释·模块定位】目标尝试与练习共用的"候选证据 + Actor 自有学习"模块。
# 这是 DRS（Deep Recursive Self-exploration，深度递归自我探索）的核心构件：
# 一个内部循环 = Actor 单次提交（submission）→ Verifier 单次判定（verdict），
# PASS 与 FAIL 都能"接地"（ground）学习——由执行尝试的同一 Actor 做经验蒸馏与
# 记忆整合（Actor-owned learning），每条已验证经验在后续练习之前先落记忆。
"""Candidate evidence and Actor-owned learning shared by target and practice runs."""

# 延迟注解求值。
from __future__ import annotations

# dataclasses：replace() 派生 Verifier 控制配置。
import dataclasses
# hashlib：交接/报告/清单的 SHA-256 摘要（写入事件负载供审计）。
import hashlib
# Path：谱系与 episode 目录。
from pathlib import Path
# Any/Callable：类型注解。
from typing import Any, Callable

# 角色章程与消息：Actor 自进化章程、记忆蒸馏/整合消息、Verifier 自进化章程。
from explore.charter import (
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
    self_evolving_verifier_charter,
)
# tree_sha256：记忆树根哈希（冻结快照的完整性承诺）。
from explore.memory_hash import tree_sha256
# practice_loop 底层设施（与 phase1_wave 共用同一套原语）。
from explore.practice_loop import (
    _ACTOR_TOKEN,
    _VERIFIER_TOKEN,
    ACTOR_EXECUTION_EVIDENCE,
    ACTOR_HANDOFF,
    PROJECT_ROOT,
    VERIFIER_REPORT,
    PracticeBoundaryError,
    PracticeHooks,
    PracticeInfrastructureError,
    _actor_runtime_contract,
    _atomic_text,
    _build_actor_execution_evidence,
    _capture_owned_tree,
    _continuation_after_transport,
    _fresh_vm,
    _listing,
    _parse_unique_handoff,
    _pull_terminal_memory,
    _push_actor_execution_evidence,
    _push_canonical_memory,
    _read_memory_tree,
    _replay,
    _run_free_phase,
    _run_handoff_phase,
    _run_learning_diagnosis,
    _verify_actor_execution_evidence,
    _verify_candidate,
)

# 【中文注释】导入清单：_ACTOR_TOKEN/_VERIFIER_TOKEN 是两类交接令牌正则；
# ACTOR_EXECUTION_EVIDENCE/ACTOR_HANDOFF/PROJECT_ROOT/VERIFIER_REPORT 是客户机内
# 路径哨兵（执行证据/Actor 交接/项目根/Verifier 报告）；PracticeBoundaryError 边界
# 违规、PracticeInfrastructureError 基础设施错误（不计分可重试）、PracticeHooks 宿主
# 钩子集；_actor_runtime_contract Actor 运行时契约；_atomic_text 原子写文本；
# _build_actor_execution_evidence/_push_actor_execution_evidence/_verify_actor_
# execution_evidence 构建与搬运/校验执行证据；_capture_owned_tree 捕获自有目录树；
# _continuation_after_transport 传输失败续跑提示；_fresh_vm VM 复位；_listing 记忆
# 清单渲染；_parse_unique_handoff 唯一交接解析器；_pull_terminal_memory 拉取终态记忆；
# _push_canonical_memory 推送规范记忆；_read_memory_tree 读记忆树；_replay 重放；
# _run_free_phase 自由回合；_run_handoff_phase 交接回合；_run_learning_diagnosis
# 学习诊断（DRS 的差距分析步骤）；_verify_candidate 校验候选可重放。

# 客户机内路径：练习"原始夹具"的只读证据视图（Verifier 用于对照）。
PRACTICE_FIXTURE_EVIDENCE = "/home/user/.practice_original_project"

# 发布异常：Agent 可自行修复的"自有产物形态缺陷"（如空树/含符号链接），
# 属于可纠正错误而非传输故障——因此单列子类，上层按可重试处理。
class PracticePublicationError(PracticeInfrastructureError):
    """Agent-correctable owned-output shape defect, not transport failure."""


def _target_audit_surface(
    target: str, target_visible_inputs: tuple[str, ...] | None = None
) -> str:
    """Authorize only the ordinary task-visible input names for TARGET.

    The exam fence deliberately
    treats several Task056 asset basenames as suspicious grader constants.
    In TARGET those paths are part of the normal solver-visible desktop, so they
    must be bound into the operator-authorized audit surface.  Structural
    benchmark paths, repositories, task ids, evaluator values, and scores
    remain forbidden by the unchanged fence rules.
    """

    # 目标审计面构造：把"任务可见输入文件名"绑进操作者授权面。
    # 【中文注释】考试围栏（exam fence）刻意把 Task056 的若干资产文件名当作可疑的
    # 评分器常量；但在 TARGET（目标尝试）里这些路径本就是解题者可见桌面的正常部分，
    # 必须显式授权，否则合法记忆会被误拒。结构化基准路径、仓库、任务 ID、评估值与
    # 分数仍被围栏规则禁止（豁免是窄的、显式的）。
    visible_inputs = (
        () if target_visible_inputs is None else tuple(target_visible_inputs)
    )
    if not visible_inputs:
        # 无可见输入：审计面就是目标文本本身。
        return target
    # 有可见输入：追加一节"正常任务可见输入路径"清单，供围栏近邻豁免使用。
    return target + "\n\nNORMAL TASK-VISIBLE INPUT PATHS:\n" + "\n".join(visible_inputs)


def _memory_tree_sha256(files: dict[str, bytes]) -> str:
    # 记忆树哈希：先按名字排序逐文件算 SHA-256，再取树级根哈希。
    # 字典推导式 + sorted 保证确定性；tree_sha256 做映射级校验与规范化。
    hashes = {
        name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())
    }
    return tree_sha256(hashes)




def _stage_original_practice_fixtures(vm) -> None:
    """Publish a read-only comparison view of pre-Actor practice fixtures."""

    # 在客户机上发布"Actor 动手前"练习夹具的只读对照视图。
    # shell 脚本（set -eu 出错即停）：先清证据目录；断言 PROJECT_ROOT 存在且不是
    # 符号链接（test ! -L）；cp -a 保属性拷贝；chmod -R a-w 全树只读；回显成功哨兵。
    command = f"""
set -eu
rm -rf {PRACTICE_FIXTURE_EVIDENCE}
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT}
cp -a {PROJECT_ROOT} {PRACTICE_FIXTURE_EVIDENCE}
chmod -R a-w {PRACTICE_FIXTURE_EVIDENCE}
echo RSI_ORIGINAL_FIXTURE_STAGE_RC=0
"""
    # cap=1024 限制输出捕获量（防大输出撑爆传输）。
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "RSI_ORIGINAL_FIXTURE_STAGE_RC=0" not in out:
        # 舞台化失败 → 基础设施错误（Verifier 无法获得原始对照证据）。
        raise PracticeInfrastructureError(
            "original practice fixtures could not be staged for verification"
        )


def _verify_original_practice_fixtures(
    hooks: PracticeHooks, vm, fixture_dir: Path
) -> None:
    """Fail closed if the Verifier mutated its original-fixture evidence."""

    # 验证后恢复原始夹具并"失败即关闭"（fail closed）：若 Verifier 改动了
    # 只读证据，这里会暴露出来。
    command = f"""
set -eu
test -d {PRACTICE_FIXTURE_EVIDENCE} && \
  test ! -L {PRACTICE_FIXTURE_EVIDENCE}
rm -rf {PROJECT_ROOT}
mv {PRACTICE_FIXTURE_EVIDENCE} {PROJECT_ROOT}
echo RSI_ORIGINAL_FIXTURE_RESTORE_RC=0
"""
    out = vm.run_command(command, timeout=240, cap=1024) or ""
    if "RSI_ORIGINAL_FIXTURE_RESTORE_RC=0" not in out:
        # 恢复失败 → 基础设施错误：证据链断了，绝不能继续。
        raise PracticeInfrastructureError(
            "original practice fixture evidence could not be restored"
        )
    # 再把宿主侧权威夹具重放进 VM，核对一致（候选完整性校验）。
    _verify_candidate(hooks, vm, str(fixture_dir))






def _require_owned_project_shape(vm) -> None:
    """Require a nonempty ordinary project tree before host transport."""

    # 传输前要求"自有项目树"形态合格：非空普通目录树。
    # shell 校验四条：目录存在且非链接；至少有一个普通文件；没有任何符号链接；
    # 没有任何"既非文件也非目录"的特殊对象（设备/套接字等）。bad 计数非零即不合格。
    command = f"""
bad=0
test -d {PROJECT_ROOT} && test ! -L {PROJECT_ROOT} || bad=1
if test "$bad" -eq 0; then
  test -n "$(find {PROJECT_ROOT} -type f -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} -type l -print -quit)" || bad=1
  test -z "$(find {PROJECT_ROOT} ! -type f ! -type d -print -quit)" || bad=1
fi
echo RSI_PROJECT_SHAPE_RC=$bad
"""
    out = vm.run_command(command, timeout=120, cap=1024) or ""
    if "RSI_PROJECT_SHAPE_RC=1" in out:
        # 形态不合格：Agent 可纠正（重新发布即可）→ PracticePublicationError。
        raise PracticePublicationError("owned project root is absent, empty, or unsafe")
    if "RSI_PROJECT_SHAPE_RC=0" not in out:
        # 连检查本身都没完成：基础设施错误。
        raise PracticeInfrastructureError("could not inspect owned project shape")








def _outcome_text(record: dict[str, Any]) -> str:
    # 结果记录渲染：标签、原始请求（逐字）、终态判定、Verifier 报告（逐字）、
    # Actor 学习诊断（逐字），双换行拼接成 markdown 式文本，供 Curriculum 复盘。
    pieces = [
        record["label"],
        "ORIGINAL REQUEST (verbatim):\n" + record["request"],
        "TERMINAL OUTCOME: " + record["terminal_outcome"],
        "VERIFIER REPORT (verbatim):\n" + record["verifier_report"],
        "ACTOR LEARNING DIAGNOSIS (verbatim):\n" + record["learning_diagnosis"],
    ]
    return "\n\n".join(pieces)


def _promote_learning(
    *,
    hooks: PracticeHooks,
    vm,
    cfg,
    lineage: Path,
    episode_dir: Path,
    experience_index: int,
    before_memory: dict[str, bytes],
    actor_history: list[dict[str, Any]],
    terminal_outcome: str,
    verifier_report: str,
    target: str,
    audit_mode: str,
    emit: Callable[..., None],
    project_open: bool,
    corpus_path: str,
    memory_dir: Path,
    journal_dir: Path,
    experience_kind: str,
    target_visible_inputs: tuple[str, ...] | None = None,
) -> tuple[dict[str, bytes], str, list[dict[str, Any]]]:
    # 【中文注释】DRS 的"经验晋升"（promote learning）函数：把一次已验证尝试的
    # 经验落进记忆。调用时机：目标尝试（PASS 或 FAIL）之后、下一次练习/尝试之前
    # ——每条已验证经验先更新记忆（论文 Phase 2 的即时记忆更新纪律）。
    # 关键字参数（*）强制调用方显式命名，防参数错位。
    # 构造审计面（含任务可见输入豁免）。
    audit_target = _target_audit_surface(target, target_visible_inputs)
    # 发射"记忆阶段开始"事件：附经验序号与 Verifier 报告哈希（可追溯证据）。
    emit(
        "MEMORY_PHASE_STARTED",
        status="in_progress",
        project_open=project_open,
        memory_phase_open=True,
        payload={
            "experience_index": experience_index,
            "terminal_outcome": terminal_outcome,
            "verifier_report_sha256": hashlib.sha256(
                verifier_report.encode("utf-8")
            ).hexdigest(),
        },
    )
    # 第一步：记忆蒸馏——基于终态判定与 Verifier 报告，把本次经验提炼成记忆草稿。
    memory_prompt = self_evolving_actor_memory_distillation_msg(
        terminal_outcome, verifier_report
    )
    actor_history, _ = _run_free_phase(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        prompt=memory_prompt,
        role="ACTOR",
        sink_root=str(episode_dir / "memory_distillation"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    # 第二步：记忆整合——与既有记忆对照合并（去重、纠偏、保持一致）。
    actor_history, _ = _run_free_phase(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        prompt=self_evolving_actor_memory_reconciliation_msg(),
        role="ACTOR",
        sink_root=str(episode_dir / "memory_reconciliation"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    # 第三步：学习诊断——Actor 复盘"这次学到/没学到什么"，形成差距分析文本。
    diagnosis, actor_history = _run_learning_diagnosis(
        hooks=hooks,
        vm=vm,
        cfg=cfg,
        terminal_outcome=terminal_outcome,
        verifier_report=verifier_report,
        sink_root=str(episode_dir / "learning_diagnosis"),
        target=audit_target,
        history=actor_history,
        audit_mode=audit_mode,
    )
    # 诊断文本落盘（DRS 证据链的一环）。
    _atomic_text(episode_dir / "learning_diagnosis.md", diagnosis)

    # 第四步：拉取终态记忆并过提交门禁——静默审计（任务泄漏绊线）。
    candidate_memory = _pull_terminal_memory(hooks, vm)
    accepted_memory = hooks.audit_memory(
        candidate_memory,
        corpus_path,
        str(lineage / "audit_rejects.jsonl"),
        authorized_instruction=audit_target,
        require_corpus=True,
    )
    if accepted_memory != candidate_memory:
        # 审计删改了候选记忆 → 边界违规（宁可整批拒绝也不放行被修剪的记忆）。
        raise PracticeBoundaryError("terminal memory failed the target boundary")
    # 第五步：写记忆日志（回滚保险）并安装进规范记忆库——本条经验自此对后续
    # 练习/尝试可见（wave memory barrier 之外的串行单例提交，无竞争）。
    hooks.journal_memory(
        str(journal_dir),
        experience_index,
        accepted_memory,
        {"kind": experience_kind, "terminal_outcome": terminal_outcome},
    )
    hooks.install_memory(str(memory_dir), accepted_memory)
    # 返回：被接受记忆、学习诊断文本、延续的 Actor 历史。
    return accepted_memory, diagnosis, actor_history


def _run_practice_attempt(
    *,
    hooks: PracticeHooks,
    vm,
    lineage: Path,
    episode_dir: Path,
    project_index: int,
    project: str,
    fixture_dir: Path,
    target: str,
    actor_cfg,
    verifier_cfg,
    memory_dir: Path,
    emit: Callable[..., None],
    agentic_verifier_cfg=None,
    target_visible_inputs: tuple[str, ...] | None = None,
    resume_state=None,
) -> dict[str, Any]:
    """Run the existing one-submission/one-verdict null-task inner loop."""

    # 【中文注释】DRS 内环：一次练习尝试 = Actor 单次提交 + Verifier 单次判定。
    # 恢复场景（resume_state 非空）或启用"未验证证据"模式时，走证据恢复变体
    # （practice_evidence_recovery）——它能在证据已捕获后崩溃的场景续跑。
    if resume_state is not None or (
        agentic_verifier_cfg is not None
        and getattr(agentic_verifier_cfg, "verifier_unverified_evidence", False)
    ):
        from explore.practice_evidence_recovery import run_practice_with_evidence

        return run_practice_with_evidence(
            hooks=hooks,
            vm=vm,
            lineage=lineage,
            episode_dir=episode_dir,
            project_index=project_index,
            project=project,
            fixture_dir=fixture_dir,
            target=target,
            actor_cfg=actor_cfg,
            verifier_cfg=verifier_cfg,
            memory_dir=memory_dir,
            emit=emit,
            agentic_verifier_cfg=agentic_verifier_cfg,
            target_visible_inputs=target_visible_inputs,
            resume_state=resume_state,
        )

    # ===== 标准路径 =====
    # 读练习前记忆基线；构造审计面。
    before_memory = _read_memory_tree(str(memory_dir))
    audit_target = _target_audit_surface(target, target_visible_inputs)
    # VM 复位 → 重放夹具 → 推送规范记忆：练习从"干净机器 + 当前记忆"开始。
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    actor_history: list[dict[str, Any]] = []
    # Actor 章程：项目指令 + 当前记忆清单 + 交接路径 + 运行时契约。
    actor_prompt = (
        self_evolving_actor_charter(project, _listing(before_memory), ACTOR_HANDOFF)
        + _actor_runtime_contract()
    )
    # 发射 Actor 阶段开始事件（首次尝试、第 0 验证周期）。
    emit(
        "ACTOR_PHASE_STARTED",
        status="in_progress",
        project_open=True,
        memory_phase_open=False,
        payload={
            "project_index": project_index,
            "actor_attempt": 1,
            "verification_cycle": 0,
        },
    )
    actor_continuation = False
    publication = 0
    while True:
        # Actor 交接回合：要求发布交接文件并产出候选。
        actor_decision, actor_history, _ = _run_handoff_phase(
            hooks=hooks,
            vm=vm,
            cfg=actor_cfg,
            prompt=actor_prompt,
            role="ACTOR",
            handoff_path=ACTOR_HANDOFF,
            token_pattern=_ACTOR_TOKEN,
            sink_root=str(episode_dir / "actor" / "attempt_001"),
            target=audit_target,
            history=actor_history,
            continuation=actor_continuation,
        )
        actor_continuation = True
        publication += 1
        # 每次发布一个候选目录（publication_001、002…），可多次发布重试。
        candidate_dir = episode_dir / "candidate" / f"publication_{publication:03d}"
        try:
            # 先做形态检查（可由 Agent 自行修复），再捕获自有树回宿主。
            _require_owned_project_shape(vm)
            _capture_owned_tree(
                hooks,
                vm,
                f"ep{project_index:03d}-candidate-{publication:03d}",
                str(candidate_dir),
                audit_target,
            )
        except PracticePublicationError:
            # 形态缺陷（缺/空/不安全/不可重放）：续跑提示让 Actor 重新发布，
            # 同一上下文继续（历史不重置），循环直到成功捕获。
            actor_prompt = _continuation_after_transport(
                "ACTOR",
                ACTOR_HANDOFF,
                f"{PROJECT_ROOT} was absent, empty, unsafe, or not replayable",
            )
            continue
        break
    # Actor 交接文本落盘。
    _atomic_text(episode_dir / "actor_handoff.md", actor_decision.text)
    # 构建执行证据包（程序确实跑过、跑出什么）。
    execution_evidence = _build_actor_execution_evidence(
        episode_dir / "actor", 1, episode_dir / "actor_execution_evidence"
    )
    # 发射 Verifier 阶段开始事件：附交接与证据清单的哈希（防篡改锚点）。
    emit(
        "VERIFIER_PHASE_STARTED",
        status="in_progress",
        project_open=True,
        memory_phase_open=False,
        payload={
            "project_index": project_index,
            "verification_cycle": 1,
            "actor_handoff_sha256": hashlib.sha256(
                actor_decision.text.encode("utf-8")
            ).hexdigest(),
            "actor_execution_manifest_sha256": execution_evidence["manifest_sha256"],
        },
    )
    # Verifier 阶段环境：复位 VM → 重放原始夹具 → 舞台化只读原始证据 →
    # 再重放候选。Verifier 看到的是"原始输入 + 候选结果"，看不到 Actor 的私有推理。
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(fixture_dir))
    _stage_original_practice_fixtures(vm)
    _replay(hooks, vm, str(candidate_dir))
    if agentic_verifier_cfg is None:
        # Historical RSI path. It remains byte-for-byte available to registered
        # experiments, while the full benchmark opts into the mechanically
        # effect-isolated Agentic Verifier below.
        # 【中文注释】历史 RSI 路径（逐字节保留给注册实验使用）；完整基准走下方
        # "机械效果隔离"的 Agentic Verifier。此路径把执行证据推给 Verifier 作为输入。
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
        # Verifier 章程：项目 + 报告路径 + 执行证据路径 + 原始夹具路径。
        verifier_prompt = self_evolving_verifier_charter(
            project,
            report_path=VERIFIER_REPORT,
            actor_execution_path=ACTOR_EXECUTION_EVIDENCE,
            original_fixtures_path=PRACTICE_FIXTURE_EVIDENCE,
        )
        # Verifier 交接回合：产出 VERIFIER_REPORT；token 即判定（PASS/FAIL…）。
        verifier_decision, _, _ = _run_handoff_phase(
            hooks=hooks,
            vm=vm,
            cfg=verifier_cfg,
            prompt=verifier_prompt,
            role="VERIFIER",
            handoff_path=VERIFIER_REPORT,
            token_pattern=_VERIFIER_TOKEN,
            sink_root=str(episode_dir / "verifier"),
            target=audit_target,
            history=[],
            continuation=False,
            handoff_parser=_parse_unique_handoff,
            commit_on_publish=True,
        )
        # 历史路径：token 是终态判定，text 是完整报告。
        terminal_outcome = verifier_decision.token
        verifier_report = verifier_decision.text
    else:
        # The Verifier remains a full code-as-policy Agent, but its programs run
        # in the read-only effect-isolated namespace used by the target harness.
        # Actor memory, handoff prose, and execution logs are mechanically hidden;
        # only the authoritative project, candidate, and original fixtures are
        # correctness evidence.
        # 【中文注释】Agentic Verifier 路径：Verifier 仍是完整的 code-as-policy
        # 智能体，但其程序运行在目标框架所用的"只读、效果隔离"命名空间。Actor
        # 记忆、交接散文与执行日志被机械隐藏；只有权威项目、候选与原始夹具才是
        # 正确性证据（Verifier 隔离原则的机械实现）。
        from core.trace import ArtifactSink
        from core.verifier import VerifierSession, verify_agentic

        # 控制配置缺失是基础设施错误（不可静默降级）。
        if not getattr(agentic_verifier_cfg, "agentic_verifier_config", ""):
            raise PracticeInfrastructureError(
                "practice Agentic Verifier control config is missing"
            )
        # 私有路径集：既有集合 + Actor 交接与执行证据（去重保序）。
        private_paths = tuple(
            dict.fromkeys(
                tuple(getattr(agentic_verifier_cfg, "verifier_private_paths", ()) or ())
                + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)
            )
        )
        # dataclasses.replace 派生分支级控制配置（同 phase1 的 _branch_control_config）。
        control_cfg = dataclasses.replace(
            agentic_verifier_cfg,
            verifier_evolve_route=False,
            verifier_local_verdict_only=False,
            verifier_hide_actor_memory=True,
            verifier_stage_lifecycle=False,
            verifier_persist_scratch=False,
            verifier_private_paths=private_paths,
        )
        # 会话对象管理执行器生命周期；context 明确告知证据边界。
        verifier_session = VerifierSession()
        context = (
            f"The candidate project is {PROJECT_ROOT}. The frozen original "
            f"input fixture is {PRACTICE_FIXTURE_EVIDENCE}; use it only as "
            "pre-candidate evidence. Actor-private memory, handoff prose, and "
            "execution logs are hidden by the harness."
        )
        try:
            # agentic 验证：独立调查候选是否满足项目要求。
            verdict, findings = verify_agentic(
                project,
                vm,
                control_cfg,
                sink=ArtifactSink(str(episode_dir / "verifier")),
                turn_no=project_index,
                context=context,
                session=verifier_session,
                wall_budget=control_cfg.wall_clock_secs,
            )
        finally:
            # 无论成败关闭执行器。
            verifier_session.close_executor()
        # 必须有 PASS/FAIL 终态；unresolved 之类视为基础设施错误。
        if verdict not in {"pass", "wrong"}:
            raise PracticeInfrastructureError(
                "practice Agentic Verifier ended without PASS/FAIL: " + str(findings)
            )
        terminal_outcome = "PASS" if verdict == "pass" else "FAIL"
        verifier_report = str(findings)
        # Archive integrity remains auditable, but the evidence is mounted only
        # after the independent Verifier has terminated so it cannot inherit the
        # Actor's narrative or self-checks.
        # 【中文注释】档案完整性仍可审计，但执行证据只在独立 Verifier 终止之后
        # 才挂载——确保它不可能继承 Actor 的叙事或自检（时序隔离）。
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
    # 验证后三重校验：候选可重放、执行证据完整、原始夹具未被 Verifier 改动。
    _verify_candidate(hooks, vm, str(candidate_dir))
    _verify_actor_execution_evidence(vm, execution_evidence)
    _verify_original_practice_fixtures(hooks, vm, fixture_dir)
    # Verifier 报告落盘。
    _atomic_text(episode_dir / "verifier_report.md", verifier_report)
    # 为记忆阶段复位环境：干净 VM + 重放候选 + 推送当前规范记忆
    #（学习由执行该尝试的同一 Actor 在同一上下文中完成）。
    _fresh_vm(hooks, vm, target)
    _replay(hooks, vm, str(candidate_dir))
    _push_canonical_memory(hooks, vm, str(memory_dir))
    # 返回内环结果：判定、报告、交接、Actor 历史、练习前记忆基线。
    return {
        "terminal_outcome": terminal_outcome,
        "verifier_report": verifier_report,
        "actor_handoff": actor_decision.text,
        "actor_history": actor_history,
        "before_memory": before_memory,
    }
