# 【中文注释·模块定位】任务域自进化基准（core/self_evolving_loop.py）的状态机
# 契约测试。主循环 run_self_evolving_loop 把"目标定向 → Actor 工作 → 目标
# Verifier 判定 → Actor 学习 → 课程进化 → 重试"组织成状态机，本文件用脚本化
# 钩子（_hooks）逐契约验证状态转移的硬规则：
# 1) PASS 审查（curriculum_after_pass）开启前必须先有同 Actor 的 PASS 学习
#    （learn_on_pass）——缺一即 SelfEvolvingLoopError；
# 2) 早期 PASS 不能为后来失败的目标背书：先 PASS 学习再 FAIL 的目标终局仍是 FAIL；
# 3) UNVERIFIED（评测环境自身不可信）绝不进入 PASS 审查或记忆学习；
# 4) 状态机事件序列（TARGET_ORIENTATION/STARTED/VERIFIED/LEARNING...）与终止原因
#    （verifier_pass / curriculum_stalled_final_target_test /
#    verifier_pass_curriculum_ready / target_harness_unverified）一一对应；
# 5) 钩子结构面里绝无 evaluator/score 字段：循环不可能接官方评测器。
"""State-machine contracts for the full task-local self-evolving benchmark."""

# 模块 docstring：完整任务域自进化基准的状态机契约。

from dataclasses import dataclass, replace
# 导入 dataclass 与 replace：构造伪对象/不可变派生钩子配置。

import pytest
# 导入 pytest：异常断言。

from core.self_evolving_loop import (
    ActorLearning,
    EvolutionResult,
    EvolutionStatus,
    SelfEvolvingLoopHooks,
    SelfEvolvingLoopError,
    TargetVerdict,
    TargetVerification,
    run_self_evolving_loop,
)
# 导入被测状态机的全部构件：学习结果、进化结果/状态、钩子容器、错误类型、
# 目标判定枚举、验证结果与主循环入口。


def test_pass_review_requires_actor_learning_before_any_environment():
    # 测试：开启 PASS 后课程审查（curriculum_after_pass）但未开 PASS 学习
    # （learn_on_pass）时，主循环必须在创建任何环境之前拒绝——审查的输入
    # 只能是同 Actor 的 PASS 学习结果。
    hooks, events, released, _ = _hooks([])
    # 判定序列为空：按理不会走到任何判定。
    with pytest.raises(SelfEvolvingLoopError, match="same-Actor PASS learning"):
        run_self_evolving_loop("target", {}, replace(hooks, curriculum_after_pass=True))
    # 必须抛 SelfEvolvingLoopError：审查依赖的学习开关没开。
    assert events == released == []
    # 拒绝发生在起步前：零事件、零环境释放。


def test_pass_before_new_practice_cannot_certify_a_later_failed_target():
    # 测试：早期目标的 PASS 学习绝不能为后来 FAIL 的目标背书——第一轮 PASS
    # 学习、第二轮 FAIL 触发一次进化后停滞，最终终局是 FAIL（最后一次目标
    # 测试），且每一轮的判定都如实进入学习记录。
    hooks, events, released, learned = _hooks(
        [TargetVerdict.PASS, TargetVerdict.FAIL, TargetVerdict.FAIL],
        [(EvolutionStatus.READY_FOR_RETRY, 1), (EvolutionStatus.STALLED, 1)])
    # 判定脚本：PASS → FAIL → FAIL；进化脚本：先就绪（1 项目）后停滞（1 项目）。
    result = run_self_evolving_loop("target", {}, replace(
        hooks, learn_on_pass=True, curriculum_after_pass=True))
    # 打开 PASS 学习与 PASS 后审查跑主循环。
    assert result.termination == "curriculum_stalled_final_target_test"
    # 终局：课程停滞后的最后一次目标测试（FAIL）。
    assert result.verifier_verdict is TargetVerdict.FAIL
    # 最终判定是 FAIL：早期 PASS 不能通过后续审查。
    assert result.target_cycles == 3
    # 目标周期走了 3 轮。
    assert released == [1, 2]
    # 周期 1、2 的环境被释放（PASS 学习后回炉重试）。
    assert [verdict for _, verdict in learned] == [
        TargetVerdict.PASS, TargetVerdict.FAIL, TargetVerdict.FAIL]
    # 学习记录按轮次保留全部三个判定。
    assert sum(name == "PASS_NEXT_EXPERIENCE_REVIEW_STARTED" for name, _ in events) == 1
    # PASS 后的经验审查恰好启动一次（第一轮 PASS 触发，之后都是 FAIL）。


def test_unverified_never_enters_pass_review_or_memory_learning():
    # 测试：UNVERIFIED（验证环境自身不可信）绝不进入 PASS 审查或记忆学习——
    # 不学、不放行、事件流里不出现任何 LEARNING/REVIEW 事件。
    hooks, events, released, learned = _hooks([TargetVerdict.UNVERIFIED])
    # 判定脚本：唯一一次判定为 UNVERIFIED。
    result = run_self_evolving_loop("target", {}, replace(
        hooks, learn_on_pass=True, curriculum_after_pass=True))
    # 即使两个开关全开，未验证结果也不该触发任何学习。
    assert result.termination == "target_harness_unverified"
    # 终局：目标 harness 未验证（可信度崩塌即停）。
    assert result.memory == {}
    # 记忆为空。
    assert learned == released == []
    # 零学习、零环境释放。
    assert all("LEARNING" not in name and "REVIEW" not in name for name, _ in events)
    # 事件流里不含任何学习/审查事件。


@dataclass
class _Environment:
    # 伪目标环境：只携带周期号。
    cycle: int


@dataclass
class _Actor:
    # 伪 Actor：周期号 + 记忆快照。
    cycle: int
    memory: dict


@dataclass
class _Verifier:
    # 伪 Verifier：周期号 + 定向标记。
    cycle: int
    oriented: bool = False


def _hooks(verdicts, evolutions=()):
    # 钩子工厂：把状态机的每个环节替换成带周期一致性断言的脚本化实现，
    # 并收集事件、释放记录与学习记录供断言。
    verdicts = iter(verdicts)
    # 判定脚本迭代器。
    evolutions = iter(evolutions)
    # 进化脚本迭代器。
    events = []
    # 事件流水。
    released = []
    # 被释放的环境周期号。
    learned_with = []
    # （Actor, 判定）学习记录。

    def environment(_direction, cycle):
        return _Environment(cycle)
        # 新环境：登记当前周期。

    def verifier(active_environment, _direction, cycle):
        assert active_environment.cycle == cycle
        # Verifier 的周期必须与环境一致。
        return _Verifier(cycle)

    def orient(active_verifier, active_environment, _direction, cycle):
        assert active_verifier.cycle == active_environment.cycle == cycle
        # 三方周期一致。
        assert not active_verifier.oriented
        # 每个新周期只定向一次。
        active_verifier.oriented = True
        # 标记已定向。

    def actor(memory, cycle):
        return _Actor(cycle, dict(memory))
        # 新 Actor：拷贝当前记忆快照。

    def work(active_actor, active_verifier, active_environment, direction):
        assert active_actor.cycle == active_environment.cycle
        assert active_verifier.cycle == active_actor.cycle
        assert active_verifier.oriented
        # Actor 工作前：三方周期一致且 Verifier 已定向。
        return {"cycle": active_actor.cycle, "direction": direction}
        # 产出带周期标记。

    def verify(active_verifier, environment, _direction, output):
        assert active_verifier.oriented
        assert environment.cycle == output["cycle"]
        # 验证前：Verifier 已定向、产出周期与环境一致。
        verdict = next(verdicts)
        # 取脚本中的下一个判定。
        return TargetVerification(
            verdict, f"cycle {environment.cycle}\nVERDICT: {verdict.value}")
        # 返回带周期与判定原文的报告。

    def learn(
            active_actor, environment, _direction, output, verdict,
            report, memory):
        assert active_actor.cycle == environment.cycle == output["cycle"]
        assert active_actor.memory == memory
        assert f"VERDICT: {verdict.value}" in report
        # 学习前：周期三方一致、记忆快照即 Actor 快照、报告含判定原文。
        learned_with.append((active_actor, verdict))
        updated = dict(memory)
        updated[f"target_{environment.cycle}.md"] = b"target lesson"
        return ActorLearning(updated, "A grounded transferable diagnosis")
        # 学习产出：新增周期课程 + 接地的可迁移诊断。

    def evolve(_direction, verdict, report, diagnosis, memory, index):
        assert f"VERDICT: {verdict.value}" in report
        assert diagnosis == "A grounded transferable diagnosis"
        # 进化前：报告含判定原文、诊断来自学习。
        expected = next(evolutions)
        if isinstance(expected, tuple):
            expected, projects = expected
        else:
            projects = index
        # 进化脚本可给 (状态, 项目数) 或单状态（项目数默认取索引）。
        updated = dict(memory)
        if projects:
            updated[f"practice_{index}.md"] = b"practice lesson"
        return EvolutionResult(expected, updated, projects=projects)
        # 进化产出：按项目数追加练习课程。

    return SelfEvolvingLoopHooks(
        fresh_target_environment=environment,
        fresh_target_verifier=verifier,
        verifier_orient=orient,
        fresh_target_actor=actor,
        actor_work=work,
        verifier_verify=verify,
        actor_learn=learn,
        evolve=evolve,
        release_target_environment=lambda env: released.append(env.cycle),
        on_event=lambda event, payload: events.append((event, payload)),
    ), events, released, learned_with
    # 组装钩子容器并返回收集器。


def test_pass_transfers_live_state_without_evolution_or_early_release():
    # 测试：默认路径下目标一次 PASS 即终止——不学习、不进化、不提前释放
    # 环境（活动状态原样移交调用方），事件序列固定五步。
    hooks, events, released, learned_with = _hooks([TargetVerdict.PASS])
    # 判定脚本：一次 PASS。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass"
    # 终局：验证通过。
    assert result.verifier_verdict is TargetVerdict.PASS
    assert result.target_cycles == 1
    # 一轮即成。
    assert result.evolutions == result.practice_projects == 0
    # 零进化、零练习项目。
    assert result.memory == {}
    # 默认不开 PASS 学习：记忆为空。
    assert result.environment.cycle == 1
    # 活动环境原样移交（未被释放重建）。
    assert released == []
    # 零释放。
    assert learned_with == []
    # 零学习。
    assert [event for event, _ in events] == [
        "TARGET_ORIENTATION_STARTED", "TARGET_ORIENTATION_COMPLETED",
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LOOP_TERMINATED"]
    # 事件序列恰为：定向开始/完成 → 目标开始 → 验证完成 → 循环终止。


def test_fail_learns_then_practices_then_retries_with_fresh_actor_and_environment():
    # 测试：FAIL → 学习 → 一次进化（一个练习项目）→ 全新 Actor/环境重试 →
    # PASS。旧环境被释放、旧 Actor 只学一轮、记忆含目标课与练习课。
    hooks, events, released, learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.PASS],
        [EvolutionStatus.READY_FOR_RETRY])
    # 判定脚本：先 FAIL 后 PASS；进化脚本：一次就绪（项目数取索引=1）。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass"
    # 重试后通过。
    assert result.target_cycles == 2
    # 两轮目标周期。
    assert result.evolutions == 1
    # 一次进化。
    assert result.practice_projects == 1
    # 一个练习项目。
    assert set(result.memory) == {"target_1.md", "practice_1.md"}
    # 记忆恰含第 1 轮目标课与第 1 号练习课。
    assert result.environment.cycle == 2
    # 活动环境是第 2 轮的全新环境。
    assert released == [1]
    # 第 1 轮环境被释放。
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.FAIL)]
    # 只有第 1 轮 Actor 学了 FAIL（重试的 Actor 还没学）。
    assert "EVOLUTION_STARTED" in [event for event, _ in events]
    assert "READY_FOR_RETRY" in [event for event, _ in events]
    # 事件流记录进化启动与就绪重试。


def test_curriculum_stalled_requests_exactly_one_final_fresh_target_test():
    # 测试：课程停滞时主循环只追加一次全新目标测试——第 2 轮失败后
    # 终局为 FAIL，记忆含两轮目标课与练习课，环境释放与学习记录齐全。
    hooks, _events, released, learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.FAIL],
        [EvolutionStatus.STALLED])
    # 判定脚本：两次 FAIL；进化脚本：一次停滞（仍给 1 个练习项目）。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "curriculum_stalled_final_target_test"
    # 终局：停滞后的最后一次目标测试。
    assert result.verifier_verdict is TargetVerdict.FAIL
    # 最终判定 FAIL。
    assert result.target_cycles == 2
    # 恰好多走一轮（停滞只换来一次机会）。
    assert result.evolutions == 1
    assert result.practice_projects == 1
    # 一次进化、一个项目。
    assert set(result.memory) == {
        "target_1.md", "practice_1.md", "target_2.md"}
    # 记忆含第 1 轮目标课、练习课与第 2 轮目标课（第二次失败也学习）。
    assert result.environment.cycle == 2
    assert released == [1]
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.FAIL), (2, TargetVerdict.FAIL)]
    # 两轮 Actor 各学一次 FAIL。


def test_opt_in_pass_learns_before_transferring_live_state():
    # 测试：显式开启 learn_on_pass 后，PASS 也先学习再移交活动状态——
    # 记忆拿到目标课、环境未被释放、事件流含学习两步。
    hooks, events, released, learned_with = _hooks([TargetVerdict.PASS])
    # 判定脚本：一次 PASS。
    hooks = SelfEvolvingLoopHooks(
        **{**hooks.__dict__, "learn_on_pass": True})
    # 打开 PASS 学习开关。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass"
    assert result.memory == {"target_1.md": b"target lesson"}
    # PASS 学习落进记忆。
    assert result.environment.cycle == 1
    assert released == []
    # 学习发生在同一环境上：无释放。
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS)]
    # 第 1 轮 Actor 学了 PASS。
    assert [event for event, _ in events] == [
        "TARGET_ORIENTATION_STARTED", "TARGET_ORIENTATION_COMPLETED",
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LEARNING_STARTED",
        "TARGET_LEARNING_COMPLETED", "TARGET_LOOP_TERMINATED"]
    # 事件序列在验证后多了学习开始/完成两步。


def test_phase2_pass_learning_then_curriculum_can_return_without_practice():
    # 测试：Phase-2 组合（learn_on_pass + curriculum_after_pass）下，PASS 后
    # 课程审查可以判定"无需新练习"直接放行重试——终局为
    # verifier_pass_curriculum_ready，零进化零项目。
    hooks, events, released, learned_with = _hooks(
        [TargetVerdict.PASS],
        [(EvolutionStatus.READY_FOR_RETRY, 0)])
    # 判定脚本：一次 PASS；进化脚本：就绪且零项目。
    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "learn_on_pass": True,
            "curriculum_after_pass": True,
        })
    # 打开 Phase-2 两个开关。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass_curriculum_ready"
    # 终局：PASS 且课程已就绪。
    assert result.target_cycles == 1
    assert result.evolutions == result.practice_projects == 0
    # 一轮、零进化、零项目。
    assert result.memory == {"target_1.md": b"target lesson"}
    # 只有 PASS 学习的目标课。
    assert released == []
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS)]
    names = [event for event, _ in events]
    assert "PASS_NEXT_EXPERIENCE_REVIEW_STARTED" in names
    assert "PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED" in names
    # 事件流含 PASS 后经验审查的开始/完成。


def test_phase2_pass_can_select_contrast_then_requires_fresh_target():
    # 测试：PASS 后课程也可以选择一次对照练习（第一轮 1 项目）再放行，
    # 第二轮新目标再 PASS 后零项目放行——两轮都学习，旧环境被释放。
    hooks, _events, released, learned_with = _hooks(
        [TargetVerdict.PASS, TargetVerdict.PASS],
        [
            (EvolutionStatus.READY_FOR_RETRY, 1),
            (EvolutionStatus.READY_FOR_RETRY, 0),
        ])
    # 判定脚本：两次 PASS；进化脚本：先 1 项目、后 0 项目。
    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "learn_on_pass": True,
            "curriculum_after_pass": True,
        })
    # Phase-2 开关全开。

    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass_curriculum_ready"
    assert result.target_cycles == 2
    # 两个目标周期。
    assert result.evolutions == result.practice_projects == 1
    # 一次进化、一个对照项目。
    assert set(result.memory) == {
        "target_1.md", "practice_1.md", "target_2.md"}
    # 记忆含两轮目标课与对照练习课。
    assert released == [1]
    # 第 1 轮环境被释放。
    assert [(actor.cycle, verdict) for actor, verdict in learned_with] == [
        (1, TargetVerdict.PASS), (2, TargetVerdict.PASS)]
    # 两轮 Actor 各学一次 PASS。


def test_unverified_is_not_mislabeled_as_pass_or_evolution_evidence():
    # 测试（默认路径）：UNVERIFIED 不会被误标为 PASS 或进化证据——
    # 终局如实为未验证，零进化、零释放、零学习。
    hooks, _events, released, learned_with = _hooks([TargetVerdict.UNVERIFIED])

    result = run_self_evolving_loop("target", {}, hooks)

    assert result.termination == "target_harness_unverified"
    assert result.verifier_verdict is TargetVerdict.UNVERIFIED
    assert result.evolutions == 0
    assert released == []
    assert learned_with == []


def test_infrastructure_exception_propagates_and_releases_target_environment():
    # 测试：基础设施异常（如传输中断）必须原样向外传播，且传播前释放
    # 目标环境——不留悬空的半开环境。
    hooks, _events, released, _learned_with = _hooks([TargetVerdict.PASS])

    def broken_work(*_args):
        raise ConnectionError("transport outage")
        # 伪 Actor 工作：抛基础设施异常。

    hooks = SelfEvolvingLoopHooks(
        **{**hooks.__dict__, "actor_work": broken_work})
    with pytest.raises(ConnectionError, match="transport outage"):
        run_self_evolving_loop("target", {}, hooks)
    # 异常原样传播（不被吞掉、不被包装）。
    assert released == [1]
    # 第 1 轮环境在传播前被释放。


def test_orientation_precedes_actor_and_is_required_each_fresh_target_cycle():
    # 测试：定向（orientation）必须先于 Actor 工作出现，且每个全新目标周期
    # 都要重新定向一次（事件流里定向开始/完成各两次，先于两次目标开始）。
    hooks, events, _released, _learned_with = _hooks(
        [TargetVerdict.FAIL, TargetVerdict.PASS],
        [EvolutionStatus.READY_FOR_RETRY])
    # 两轮判定、一次进化。

    run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    names = [event for event, _ in events]
    assert names.count("TARGET_ORIENTATION_STARTED") == 2
    assert names.count("TARGET_ORIENTATION_COMPLETED") == 2
    # 两轮各定向一次。
    first_orientation = names.index("TARGET_ORIENTATION_COMPLETED")
    first_actor = names.index("TARGET_STARTED")
    assert first_orientation < first_actor
    # 第一次定向完成严格早于第一次 Actor 启动。


def test_clean_protocol_does_not_require_pre_actor_orientation():
    # 测试：干净协议（钩子不提供定向）同样合法——orient 为 None 时主循环
    # 直接跳过定向步骤，事件序列不含定向事件，验证不要求已定向。
    hooks, events, released, _learned_with = _hooks([TargetVerdict.PASS])

    def work_without_orientation(
            active_actor, active_verifier, active_environment, direction):
        assert active_actor.cycle == active_environment.cycle
        assert active_verifier.cycle == active_actor.cycle
        assert not active_verifier.oriented
        # 干净协议：Verifier 不被定向，工作照常。
        return {"cycle": active_actor.cycle, "direction": direction}

    def verify_without_orientation(
            active_verifier, environment, _direction, output):
        assert not active_verifier.oriented
        assert environment.cycle == output["cycle"]
        return TargetVerification(
            TargetVerdict.PASS, "independent evidence\nVERDICT: PASS")
        # 验证同样不要求定向。

    hooks = SelfEvolvingLoopHooks(
        **{
            **hooks.__dict__,
            "verifier_orient": None,
            "actor_work": work_without_orientation,
            "verifier_verify": verify_without_orientation,
        })
    # 关闭定向、换用无定向的工作/验证实现。
    result = run_self_evolving_loop("target", {}, hooks)
    # 跑主循环。

    assert result.termination == "verifier_pass"
    assert released == []
    assert [event for event, _ in events] == [
        "TARGET_STARTED", "TARGET_VERIFIED", "TARGET_LOOP_TERMINATED"]
    # 事件序列不含定向步骤：干净协议三步走完。


def test_protocol_contains_no_external_evaluator_hook():
    # 测试（结构哨兵）：循环钩子的字段面里绝无 evaluator/score——
    # 自进化循环在类型结构上就不可能接入官方评测器。
    assert "evaluator" not in SelfEvolvingLoopHooks.__dataclass_fields__
    assert "score" not in SelfEvolvingLoopHooks.__dataclass_fields__
