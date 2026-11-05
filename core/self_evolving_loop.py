# 【中文注释·模块定位】"无评估器"的目标测试/自我进化生命周期状态机（完整基准使用）。
# 这是 DRS（Phase 2）的控制流核心：全新的 Actor 智能体尝试目标 → 一个"持久上下文"的
# Verifier 智能体调查该尝试中的每一次候选修订 → FAIL 必然触发同一 Actor 学习 +
# 持久 Curriculum 智能体与它的"练习 Actor→Verifier 内环"，然后才允许新的目标尝试；
# PASS 默认（curriculum_review 策略）也要请 Curriculum 复盘"是否值得再做一次对照
# 练习"。外部密封评估器（sealed evaluator）只允许在本状态机返回之后挂接——任何
# 钩子都不接受分数或评估器输出，官方分数被挡在学习环之外。
# 注意：本协议刻意不包含统一循环里的 REVISE/EVOLVE 路由器（那是另一条历史路径）。
"""Evaluator-free target-test/self-evolution lifecycle used by the full benchmark.

This protocol intentionally does not contain the unified loop's REVISE/EVOLVE
router.  A fresh Actor Agent attempts the target and one persistent Verifier
Agent context investigates every candidate revision in that attempt. A caller
may opt into a candidate-blind pre-Actor orientation experiment, but orientation
is not part of the clean default lifecycle. A caller may also opt into learning
from a verified PASS and asking the persistent Curriculum Agent whether one
contrastive experience has higher value than immediately returning target
control. FAIL always lets that same Actor Agent learn, then invokes the persistent
Curriculum Agent and its practice Actor Agent -> Verifier Agent inner loop before
a fresh target attempt.

The caller may attach a sealed external evaluator only after this state machine
returns.  No score or evaluator output is accepted by any hook.
"""

# 延迟注解求值。
from __future__ import annotations

# sys：finally 块里取"当前异常"（sys.exc_info()[1]）用于区分主失败与释放失败。
import sys
# Callable：钩子的调用签名注解（collections.abc 版本支持下标泛型）。
from collections.abc import Callable
# dataclass：冻结数据类承载状态机各边界对象。
from dataclasses import dataclass
# Enum：枚举状态机（str 混入使其可 JSON 序列化、可比较字符串）。
from enum import Enum
# Any：动态记忆与环境对象的类型（由调用方注入具体实现）。
from typing import Any


# 生命周期错误：角色生命周期产生了非法传输级状态时抛出。
class SelfEvolvingLoopError(RuntimeError):
    """The role lifecycle produced an invalid transport-level state."""


class Phase2StopPolicy(str, Enum):
    """Precommitted training policy; neither option changes who judges a task."""

    # 【中文注释】Phase 2 停止策略（预先承诺的训练策略——两者都不改变"谁判定任务"）：
    # verifier_pass：目标 PASS 及其记忆提交后即停止；
    # curriculum_review：目标 PASS 后仍由 Curriculum 复盘是否再练习。
    VERIFIER_PASS = "verifier_pass"
    CURRICULUM_REVIEW = "curriculum_review"


# 默认策略：curriculum_review（PASS 也要 Curriculum 复盘，防"脆弱的成功"）。
DEFAULT_PHASE2_STOP_POLICY = Phase2StopPolicy.CURRICULUM_REVIEW.value


class TargetVerdict(str, Enum):
    # 目标判定三值：PASS / FAIL / UNVERIFIED（基础设施问题不算判定）。
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class EvolutionStatus(str, Enum):
    # 进化结果状态：可重试（READY_FOR_RETRY）或停滞（STALLED）。
    # STALLED 与"成功收敛"在状态上可区分——论文要求停滞退出不得伪装成收敛。
    READY_FOR_RETRY = "READY_FOR_RETRY"
    STALLED = "STALLED"


# 冻结数据类：目标验证结果（判定 + 非空报告）。
@dataclass(frozen=True)
class TargetVerification:
    verdict: TargetVerdict
    report: str

    def __post_init__(self) -> None:
        # __post_init__ 校验/归一化：frozen 数据类里用 object.__setattr__
        # 完成构造期字段修正（合法的唯一途径）。
        if not isinstance(self.verdict, TargetVerdict):
            try:
                # 允许传字符串判定（如 "pass"），归一化为枚举成员。
                object.__setattr__(
                    self, "verdict",
                    TargetVerdict(str(self.verdict).strip().upper()))
            except ValueError as exc:
                # 未知判定直接拒绝（不静默映射成 FAIL/PASS）。
                raise SelfEvolvingLoopError(
                    f"unknown target verdict: {self.verdict!r}") from exc
        # 报告必须是非空字符串——判定必须附带理由（可审计）。
        if not isinstance(self.report, str) or not self.report.strip():
            raise SelfEvolvingLoopError(
                "target Verifier Agent report/reason must be nonempty")


# 冻结数据类：一次学习产物（新记忆 + 非空诊断文本）。
@dataclass(frozen=True)
class ActorLearning:
    memory: Any
    diagnosis: str

    def __post_init__(self) -> None:
        # 诊断必须非空：学习必须留下可读的差距分析（DRS 证据链）。
        if not isinstance(self.diagnosis, str) or not self.diagnosis.strip():
            raise SelfEvolvingLoopError(
                "Actor Agent learning diagnosis must be nonempty")


# 冻结数据类：进化（Curriculum 驱动的练习轮）结果。
@dataclass(frozen=True)
class EvolutionResult:
    status: EvolutionStatus
    memory: Any
    projects: int
    reason: str = ""

    def __post_init__(self) -> None:
        # 状态字符串归一化为枚举；未知值拒绝。
        if not isinstance(self.status, EvolutionStatus):
            try:
                object.__setattr__(
                    self, "status",
                    EvolutionStatus(str(self.status).strip().upper()))
            except ValueError as exc:
                raise SelfEvolvingLoopError(
                    f"unknown evolution status: {self.status!r}") from exc
        # 项目计数必须是非负整数（type is int 显式排除 bool）。
        if type(self.projects) is not int or self.projects < 0:
            raise SelfEvolvingLoopError(
                "evolution project count must be a nonnegative integer")


# 冻结数据类：断点恢复的起点边界（完成练习后的 admitted boundary）。
@dataclass(frozen=True)
class SelfEvolvingStart:
    """Counters at an admitted boundary after completed practice, before a target.

    The caller must validate durable artifacts and resume any interrupted
    Curriculum turn before constructing this boundary. This never accepts an
    infrastructure stop as a semantic Curriculum decision.
    """

    # 【中文注释】恢复边界携带的计数器：目标周期数 / 进化次数 / 练习项目数。
    # 调用方必须先校验持久产物并恢复被打断的 Curriculum 回合才能构造此边界；
    # 基础设施停止绝不能被当作语义上的 Curriculum 决策。
    target_cycles: int
    evolutions: int
    practice_projects: int
    final_after_stall: bool = False

    def __post_init__(self) -> None:
        # 全部计数必须 >=1；进化次数不得超过目标周期数；final_after_stall 必须是 bool。
        if (any(type(v) is not int or v < 1 for v in (
                self.target_cycles, self.evolutions, self.practice_projects))
                or self.evolutions > self.target_cycles
                or type(self.final_after_stall) is not bool):
            raise SelfEvolvingLoopError("invalid completed-practice boundary")


# 冻结数据类：终态"活"目标状态；所有权移交调用方（供 sealed evaluation 使用）。
@dataclass(frozen=True)
class SelfEvolvingLoopResult:
    """Terminal live target state; ownership transfers to the caller."""

    output: Any
    environment: Any
    verifier_verdict: TargetVerdict
    verifier_report: str
    memory: Any
    target_cycles: int
    evolutions: int
    practice_projects: int
    termination: str


def _noop_event(_event: str, _payload: dict[str, Any]) -> None:
    # 默认事件钩子：什么都不做（可选观察点）。
    pass


def _noop_release(_environment: Any) -> None:
    # 默认环境释放钩子：什么都不做（由调用方注入真实释放逻辑）。
    pass


# 冻结数据类：状态机的全部钩子（依赖倒置——本模块不含任何 VM/LLM 具体实现）。
@dataclass(frozen=True)
class SelfEvolvingLoopHooks:
    # 建全新目标环境（按目标方向与周期号）。
    fresh_target_environment: Callable[[str, int], Any]
    # 建全新目标 Verifier（绑定环境；同一上下文要延续到候选验证）。
    fresh_target_verifier: Callable[[Any, str, int], Any]
    # 建全新目标 Actor（携带当前记忆）。
    fresh_target_actor: Callable[[Any, int], Any]
    # Actor 干活：返回输出（候选产物）。
    actor_work: Callable[[Any, Any, Any, str], Any]
    # Verifier 验证：返回 TargetVerification。
    verifier_verify: Callable[[Any, Any, str, Any], TargetVerification]
    # Actor 学习：输入判定与报告，返回 ActorLearning（新记忆 + 诊断）。
    actor_learn: Callable[
        [Any, Any, str, Any, TargetVerdict, str, Any], ActorLearning]
    # 进化：Curriculum 复盘并（按需）驱动练习内环，返回 EvolutionResult。
    evolve: Callable[
        [str, TargetVerdict, str, str, Any, int], EvolutionResult]
    # 可选：候选盲的 Pre-Actor 定向实验（非默认生命周期的一部分）。
    verifier_orient: Callable[[Any, Any, str, int], None] | None = None
    # 可选开关：PASS 也学习；PASS 后也请 Curriculum 复盘。
    learn_on_pass: bool = False
    curriculum_after_pass: bool = False
    # 环境释放与事件钩子，默认 no-op。
    release_target_environment: Callable[[Any], None] = _noop_release
    on_event: Callable[[str, dict[str, Any]], None] = _noop_event


def run_self_evolving_loop(
        target_direction: str, current_memory: Any,
        hooks: SelfEvolvingLoopHooks, *,
        start: SelfEvolvingStart | None = None) -> SelfEvolvingLoopResult:
    """Run target tests and practice without consulting an external evaluator.

    Curriculum ``STALLED`` is a semantic stop condition, not target correctness.
    The latest memory therefore receives one final fresh target test and that live
    state is returned for sealed measurement regardless of its local verdict.
    Infrastructure failures must raise from their hook and are never mapped here.
    """

    # 【中文注释】DRS 主状态机：不咨询任何外部评估器地运行"目标测试 + 练习"。
    # Curriculum 的 STALLED 是语义停止条件而非正确性判定——因此最新记忆还要接受
    # 一次最终的新鲜目标测试，无论本地判定如何，其活状态都交给密封测量。
    # 基础设施失败必须从钩子里抛异常，绝不在本函数内被映射成判定。
    # 目标方向必须非空。
    if not isinstance(target_direction, str) or not target_direction.strip():
        raise ValueError("target_direction must be nonempty")
    # 策略组合校验：PASS 后请 Curriculum 复盘的前提是 PASS 也学习（否则复盘没有
    # 新经验可审），不一致直接拒绝。
    if hooks.curriculum_after_pass and not hooks.learn_on_pass:
        raise SelfEvolvingLoopError(
            "Curriculum review after PASS requires same-Actor PASS learning")

    # 初始化状态：记忆 + 三个计数器（支持从 SelfEvolvingStart 恢复）。
    memory = current_memory
    target_cycles = start.target_cycles if start else 0
    evolutions = start.evolutions if start else 0
    practice_projects = start.practice_projects if start else 0
    final_after_stall = start.final_after_stall if start else False

    while True:
        # ===== 目标周期开始 =====
        target_cycles += 1
        # 建全新目标环境（重置的任务环境，交互历史也是新的）。
        environment = hooks.fresh_target_environment(
            target_direction, target_cycles)
        if environment is None:
            # 环境创建失败是基础设施错误（钩子返回 None 视为违规）。
            raise SelfEvolvingLoopError(
                "fresh target environment was not created")
        # 两个生命周期标志：transferred=活状态已移交调用方；released=环境已释放。
        transferred = False
        released = False
        try:
            # 建目标 Verifier（其上下文要贯穿本次尝试的所有候选修订）。
            verifier = hooks.fresh_target_verifier(
                environment, target_direction, target_cycles)
            if verifier is None:
                raise SelfEvolvingLoopError(
                    "fresh target Verifier Agent was not created")
            if hooks.verifier_orient is not None:
                # 可选实验阶段：候选盲的 Pre-Actor 定向实验。
                hooks.on_event("TARGET_ORIENTATION_STARTED", {
                    "target_cycle": target_cycles,
                    "evolutions": evolutions,
                })
                # Optional experimental stage. No Actor context or action exists
                # at this boundary, and the same Verifier context must continue
                # into candidate verification.
                # 【中文注释】此边界尚不存在任何 Actor 上下文或动作；且同一 Verifier
                # 上下文必须延续到候选验证（保持"一个持久 Verifier"协议）。
                hooks.verifier_orient(
                    verifier, environment, target_direction, target_cycles)
                hooks.on_event("TARGET_ORIENTATION_COMPLETED", {
                    "target_cycle": target_cycles,
                    "evolutions": evolutions,
                })

            # 建目标 Actor：携带当前记忆开始尝试。
            actor = hooks.fresh_target_actor(memory, target_cycles)
            if actor is None:
                raise SelfEvolvingLoopError(
                    "fresh target Actor Agent was not created")
            hooks.on_event("TARGET_STARTED", {
                "target_cycle": target_cycles,
                "evolutions": evolutions,
                "final_after_curriculum_stall": final_after_stall,
            })
            # Actor 干活 → 产出候选输出。
            output = hooks.actor_work(
                actor, verifier, environment, target_direction)
            # Verifier 验证产出。
            verification = hooks.verifier_verify(
                verifier, environment, target_direction, output)
            if not isinstance(verification, TargetVerification):
                # 返回类型违规（传输级状态非法）。
                raise SelfEvolvingLoopError(
                    "verifier_verify must return TargetVerification")
            hooks.on_event("TARGET_VERIFIED", {
                "target_cycle": target_cycles,
                "verdict": verification.verdict.value,
            })

            # ===== 学习决策：FAIL 必学；PASS 仅在 learn_on_pass 开启时学习 =====
            learning = None
            should_learn = (
                verification.verdict is TargetVerdict.FAIL
                or (verification.verdict is TargetVerdict.PASS
                    and hooks.learn_on_pass))
            if should_learn:
                hooks.on_event("TARGET_LEARNING_STARTED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "evolutions": evolutions,
                })
                # 同一 Actor 学习：把尝试经验蒸馏并整合进记忆（即时记忆更新）。
                learning = hooks.actor_learn(
                    actor, environment, target_direction, output,
                    verification.verdict, verification.report, memory)
                if not isinstance(learning, ActorLearning):
                    raise SelfEvolvingLoopError(
                        "actor_learn must return ActorLearning")
                # 学习产出的新记忆立即成为"当前记忆"。
                memory = learning.memory
                hooks.on_event("TARGET_LEARNING_COMPLETED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "evolutions": evolutions,
                })

            # ===== 终止条件判定（三种可终止路径）=====
            # review_pass：PASS + curriculum_review 开启 + 非"停滞后的最终测试"。
            review_pass = (
                verification.verdict is TargetVerdict.PASS
                and hooks.curriculum_after_pass
                and not final_after_stall)
            if (verification.verdict is TargetVerdict.PASS
                    and not review_pass):
                # verifier_pass 策略：PASS 且无需复盘 → 直接终止。
                termination = "verifier_pass"
            elif final_after_stall:
                # 停滞后的最终目标测试：无论判定如何都终止（活状态交密封测量）。
                termination = "curriculum_stalled_final_target_test"
            elif verification.verdict is TargetVerdict.UNVERIFIED:
                # 未验证（基础设施类）：终止但状态如实标注，不得计分。
                termination = "target_harness_unverified"
            else:
                # 其余情形继续循环（FAIL 或待复盘的 PASS）。
                termination = ""

            if termination:
                hooks.on_event("TARGET_LOOP_TERMINATED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "termination": termination,
                    "evolutions": evolutions,
                })
                # 活状态移交：环境不再由本函数释放。
                transferred = True
                return SelfEvolvingLoopResult(
                    output=output,
                    environment=environment,
                    verifier_verdict=verification.verdict,
                    verifier_report=verification.report,
                    memory=memory,
                    target_cycles=target_cycles,
                    evolutions=evolutions,
                    practice_projects=practice_projects,
                    termination=termination,
                )

            # Every grounded outcome has already been learned by the same Actor.
            # FAIL requires targeted practice. An opt-in PASS review lets the
            # Curriculum Agent choose whether a genuinely informative contrast
            # is worth running; it cannot edit or approve memory or correctness.
            # 【中文注释】到达 Curriculum 时"接地结果"必须已被同一 Actor 学过；
            # FAIL 必须触发定向练习；PASS 复盘只允许 Curriculum 决定"是否值得做
            # 一次真正有信息量的对照练习"——它不能编辑或批准记忆，也不能判定正确性。
            if learning is None:
                raise SelfEvolvingLoopError(
                    "grounded outcome reached Curriculum without Actor learning")

            # ===== 进化（Curriculum 驱动的练习轮）=====
            next_evolution = evolutions + 1
            if verification.verdict is TargetVerdict.FAIL:
                hooks.on_event("EVOLUTION_STARTED", {
                    "target_cycle": target_cycles,
                    "evolution": next_evolution,
                })
                # The failed live state is no longer needed. A PASS state remains
                # live while Curriculum decides, so zero-project READY can return
                # without silently changing the generic handoff contract.
                # 【中文注释】FAIL 的活状态不再需要，可立即释放；PASS 的活状态在
                # Curriculum 决策期间保持存活——这样"零项目 READY"可以直接返回
                # 原状态，不改变交接契约。
                released = True
                hooks.release_target_environment(environment)
            else:
                # PASS 复盘开始事件。
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_STARTED", {
                    "target_cycle": target_cycles,
                    "prospective_evolution": next_evolution,
                })

            # 调用 evolve 钩子：Curriculum 审阅学习诊断，选择定向练习或返回就绪。
            evolved = hooks.evolve(
                target_direction, verification.verdict, verification.report,
                learning.diagnosis, memory, next_evolution)
            if not isinstance(evolved, EvolutionResult):
                raise SelfEvolvingLoopError(
                    "evolve must return EvolutionResult")
            # 练习产出的记忆与项目计数并入状态。
            memory = evolved.memory
            practice_projects += evolved.projects

            if (verification.verdict is TargetVerdict.PASS
                    and evolved.projects == 0):
                # PASS + Curriculum 不要求练习：按其就绪/停滞裁决终止。
                termination = (
                    "verifier_pass_curriculum_ready"
                    if evolved.status is EvolutionStatus.READY_FOR_RETRY
                    else "verifier_pass_curriculum_stalled")
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED", {
                    "target_cycle": target_cycles,
                    "projects": 0,
                    "reason": evolved.reason,
                    "termination": termination,
                })
                hooks.on_event("TARGET_LOOP_TERMINATED", {
                    "target_cycle": target_cycles,
                    "verdict": verification.verdict.value,
                    "termination": termination,
                    "evolutions": evolutions,
                })
                # PASS 活状态移交（保持存活，供密封评估）。
                transferred = True
                return SelfEvolvingLoopResult(
                    output=output,
                    environment=environment,
                    verifier_verdict=verification.verdict,
                    verifier_report=verification.report,
                    memory=memory,
                    target_cycles=target_cycles,
                    evolutions=evolutions,
                    practice_projects=practice_projects,
                    termination=termination,
                )

            if verification.verdict is TargetVerdict.PASS:
                # PASS 且 Curriculum 要求了练习：练习要用"不变目标上的全新尝试"
                # 收尾，因此当前 PASS 活状态作废释放。
                hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED", {
                    "target_cycle": target_cycles,
                    "projects": evolved.projects,
                    "reason": evolved.reason,
                    "fresh_target_required": True,
                })
                released = True
                hooks.release_target_environment(environment)
            # 推进进化计数；记录 Curriculum 是否停滞（停滞 → 下轮为最终测试）。
            evolutions = next_evolution
            final_after_stall = evolved.status is EvolutionStatus.STALLED
            # 就绪则发 READY_FOR_RETRY；停滞则发"最终测试必需"事件。
            hooks.on_event(
                "READY_FOR_RETRY" if not final_after_stall
                else "CURRICULUM_STALLED_FINAL_TEST_REQUIRED",
                {
                    "target_cycle": target_cycles,
                    "evolution": evolutions,
                    "projects": evolved.projects,
                    "reason": evolved.reason,
                })
        finally:
            # 统一资源保障：只要活状态既未移交、环境也未正常释放（即异常路径），
            # 就在此释放环境；释放自身失败时的处理——若存在主失败异常，记录事件
            # 且不掩盖主异常；若释放是唯一异常，向上抛出。
            if not transferred and not released:
                primary_failure = sys.exc_info()[1]
                try:
                    hooks.release_target_environment(environment)
                except Exception as release_error:
                    if primary_failure is None:
                        raise
                    hooks.on_event("TARGET_RELEASE_FAILED", {
                        "target_cycle": target_cycles,
                        "error_type": type(release_error).__name__,
                        "error": str(release_error),
                        "primary_error_type": type(primary_failure).__name__,
                    })


# 模块公开 API。
__all__ = [
    "ActorLearning",
    "EvolutionResult",
    "EvolutionStatus",
    "Phase2StopPolicy",
    "SelfEvolvingLoopError",
    "SelfEvolvingLoopHooks",
    "SelfEvolvingLoopResult",
    "TargetVerdict",
    "TargetVerification",
    "run_self_evolving_loop",
]
