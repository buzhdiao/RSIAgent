"""Connect ALE environments to the existing evaluator-free RSIAgent RSI loops."""
# 【中文注释·模块定位】把 ALE 环境接到 RSIAgent 既有的"无评估器 RSI 循环"上：
# phase1 = BRS（Broad Recursive Self-exploration，广度探索），phase2 = DRS（Deep
# Recursive Self-exploration，深度探索/定向练习）。两个阶段都严禁调用官方评估器
# （official_evaluator_calls 恒为 0），阶段结束把记忆冻结（memory_frozen）并校验
# 指纹，作为跨阶段传递的不可变边界（phase boundary）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# Path：谱系工作目录操作。
from pathlib import Path
# SimpleNamespace：为钩子回调构造轻量属性容器（environment/actor 等）。
from types import SimpleNamespace

# CleanPool：全新客户机租约池；hooks_for：构造 ALE 专用的 PracticeHooks（含审计）。
from .practice import CleanPool, hooks_for
# protocol：事件落盘、记忆指纹记录/校验、角色配置加载与原子写盘。
from .protocol import event, memory_record, role, verify_memory, write_json


# Phase 1（BRS）：多分支并行波次探索，产出冻结记忆；全程零评估器调用。
def phase1(spec):
    # 延迟导入 Phase 1 波次实现与记忆原子安装/读取工具。
    from explore.phase1_wave import evolve_parallel_phase1
    from explore.practice_loop import _atomic_install_memory, _read_memory_tree

    # 本阶段工作目录。
    root = Path(spec["work_dir"])
    # 练习沙箱池：每次复用租约都会通过复位 RPC 换全新 VM（practice=True 标记一次性客户机）。
    pool = CleanPool(
        spec["practice_sandboxes"], practice=True, reset_endpoint=spec["reset_endpoint"]
    )
    # 保证异常路径也释放 VM 连接。
    try:
        # 构造 ALE 钩子集：语料审计、（可选）公开任务绑定审计、记忆传输、VM 复位。
        hooks = hooks_for(pool, spec["corpus"], spec.get("audit_public_task"))
        # 运行 BRS 并行波次：课程智能体决定每波项目；每分支独立全新 VM 并行 Actor→Verifier；
        # 全部分支拿到终态判定后按序串行恢复、把经验并入唯一规范记忆库（wave memory barrier）。
        # 参数：谱系根、探索总方向=任务指令、五个角色配置、VM 工厂取自练习池、审计语料、
        # 事件落盘、项目预算 8、第 0/4/8 项目处落检查点、最多 4 分支并行、目标查询受条件
        # 约束（防越界）、已验证分支挂起等待串行提交。
        result = evolve_parallel_phase1(
            root=str(root),
            target_direction=spec["instruction"],
            actor_cfg=role("actor"),
            curriculum_cfg=role("curriculum"),
            memory_cfg=role("memory"),
            verifier_control_cfg=role("target"),
            vm_factory=pool.acquire,
            corpus_path=spec["corpus"],
            hooks=hooks,
            event_sink=lambda kind, **payload: event(root, kind, payload),
            project_budget=8,
            checkpoint_projects=(0, 4, 8),
            max_parallel=4,
            target_query_conditioned=True,
            park_verified_branches=True,
        )
        # 把波次产出的记忆树原子安装为冻结记忆 memory_frozen（跨阶段边界）。
        _atomic_install_memory(
            str(root / "memory_frozen"), _read_memory_tree(result.memory_dir)
        )
        # 组装阶段结果记录：phase/status/projects/reason 为波次概况；
        # memory 为冻结记忆指纹；official_evaluator_calls 恒 0（铁律）；
        # transition_ready 仅在饱和或预算耗尽时为真（才允许进入 phase2）。
        record = {
            "phase": "phase1",
            "status": result.status,
            "projects": result.projects,
            "reason": result.reason,
            "memory": memory_record(root / "memory_frozen"),
            "official_evaluator_calls": 0,
            "transition_ready": result.status in ("saturated", "budget_exhausted"),
        }
        # 落盘阶段结果。
        write_json(root / "phase-result.json", record)
        # 返回给宿主。
        return record
    finally:
        # 关闭练习池（释放全部 VM 连接）。
        pool.close()


# Phase 2（DRS）：以真实目标尝试驱动学习——尝试→Verifier 判定→学习/练习进化→再尝试；零评估器调用。
def phase2(spec):
    # 延迟导入 DRS 状态机与配套工具（worker 进程专属环境）。
    from core import actor as actor_module
    from core.loop import run_with_resume
    from core.self_evolving_loop import (
        ActorLearning,
        EvolutionResult,
        EvolutionStatus,
        SelfEvolvingLoopHooks,
        TargetVerdict,
        TargetVerification,
        run_self_evolving_loop,
    )
    from core.trace import ArtifactSink
    from explore.charter import memory_preamble
    from explore.practice_loop import (
        _atomic_install_memory,
        _listing,
        _read_memory_tree,
    )
    from explore.target_learning import _promote_learning
    from explore.unified_evolution import UnifiedCurriculumSession, evolve_until_ready

    # 本阶段工作目录。
    root = Path(spec["work_dir"])
    # 校验 phase1 冻结记忆指纹未被篡改（跨阶段边界不变量）。
    verify_memory(spec["input_memory"])
    # 读取冻结记忆树作为 DRS 的初始记忆。
    initial = _read_memory_tree(spec["input_memory"]["path"])
    # 目标池：真实评估沙箱（practice=False，不做练习清理）。
    target_pool = CleanPool(
        [spec["sandbox"]], practice=False, reset_endpoint=spec["reset_endpoint"]
    )
    # 练习池先置 None：构造失败时 finally 仍可安全关闭。
    practice_pool = None
    try:
        # 建练习池（一次性客户机，复用即复位）。
        practice_pool = CleanPool(
            spec["practice_sandboxes"],
            practice=True,
            reset_endpoint=spec["reset_endpoint"],
        )
        # 练习钩子集（含审计）。
        hooks = hooks_for(practice_pool, spec["corpus"], spec.get("audit_public_task"))
        # 统一课程会话：跨循环保持 Curriculum 的持久上下文。
        curriculum = UnifiedCurriculumSession()
        # 活跃记忆目录（本阶段内的规范记忆库挂载点）。
        active_memory = root / "memory"
        # 记忆学习更新次数计数器。
        learning_updates = 0

        # 目标循环钩子：领一个真实评估沙箱。
        def fresh_environment(direction, cycle):
            # 从目标池取租约与 VM。
            lease, vm = target_pool.acquire()
            # 登记任务可见根目录（输出/输入目录）。
            vm.visible_roots = tuple(spec.get("visible_roots", ()))
            # 以命名空间对象返回租约与 VM。
            return SimpleNamespace(lease=lease, vm=vm, cycle=cycle)

        # 目标循环钩子：Verifier 会话状态由 run_with_resume 自建。
        def fresh_verifier(environment, direction, cycle):
            # 返回空字典：持久 Verifier 上下文交给底层循环按需创建。
            return {}

        # 目标循环钩子：新一轮尝试的 Actor 状态（记忆快照 + 轮次号）。
        def fresh_actor(memory, cycle):
            # 浅拷贝记忆映射，附带轮次编号。
            return SimpleNamespace(memory=dict(memory), cycle=cycle)

        # 目标循环钩子：执行一次真实目标尝试。
        def work(actor, sessions, environment, direction):
            # 把 Actor 的记忆安装为活跃记忆（原子写）。
            _atomic_install_memory(str(active_memory), actor.memory)
            # 上传记忆到目标 VM；失败即中止（目标尝试必须带着记忆上场）。
            if not hooks.push_memory(environment.vm, str(active_memory)):
                raise RuntimeError("Target memory upload failed")
            # 以活跃记忆目录生成目标角色配置（无记忆时传空串）。
            cfg = role("target", str(active_memory) if actor.memory else "")
            # 本轮次轨迹的落盘目录：target_cycles/cycle_NNN。
            sink_root = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
            # 创建产物落盘器。
            sink = ArtifactSink(str(sink_root))
            # 运行时状态容器（活跃配置/历史回传用）。
            runtime = {}
            try:
                # 运行带恢复的 Actor 主循环：注入记忆前言、传入 Verifier 会话与运行时状态。
                result, history = run_with_resume(
                    direction,
                    environment.vm,
                    cfg,
                    sink,
                    opening_extra=memory_preamble(_listing(actor.memory)),
                    verifier_sessions=sessions,
                    runtime_state=runtime,
                )
            finally:
                # 无论成败关闭全部 Verifier 执行器（释放回滚镜像资源）。
                for session in sessions.values():
                    session.close_executor()
            # 保存完整 transcript（构建后的系统提示词 + 历史）。
            sink.save_transcript(actor_module.build_system(cfg), history)
            # 返回本轮次产物：结果、实际生效配置、历史与落盘根。
            return SimpleNamespace(
                result=result,
                cfg=runtime.get("active_cfg") or cfg,
                history=runtime.get("active_history") or history,
                root=sink_root,
            )

        # 目标循环钩子：把运行终态翻译为 Verifier 判定。
        def verify(sessions, environment, direction, output):
            # 取本轮结果对象。
            result = output.result
            # Verifier 报告文本。
            report = str(getattr(result, "verifier_report", "") or "")
            # Verifier 路由（HANDOFF/EVOLVE）。
            route = str(getattr(result, "verifier_route", "") or "")
            # done + HANDOFF + 有报告：任务完成判定 PASS。
            if result.status == "done" and route == "HANDOFF" and report.strip():
                return TargetVerification(TargetVerdict.PASS, report)
            # evolve + EVOLVE + 有报告：判定 FAIL（需学习后再来）。
            if result.status == "evolve" and route == "EVOLVE" and report.strip():
                return TargetVerification(TargetVerdict.FAIL, report)
            # 其余情况：如实记为 UNVERIFIED（报告缺失时给出状态说明）。
            return TargetVerification(
                TargetVerdict.UNVERIFIED,
                report
                or f"No committed verdict: status={result.status}, route={route}",
            )

        # 目标循环钩子：FAIL 后同一 Actor 必学——把经验并入记忆。
        def learn(actor, environment, direction, output, verdict, report, memory):
            # 引用外层计数器。
            nonlocal learning_updates
            # 把传入记忆安装为活跃记忆。
            _atomic_install_memory(str(active_memory), memory)
            # 恢复规范记忆到目标 VM（上传失败即中止）。
            if not hooks.push_memory(environment.vm, str(active_memory)):
                raise RuntimeError("Canonical memory restore failed")
            # 学习更新计数 +1。
            learning_updates += 1
            # 学习回合的产物目录：terminal_learning。
            episode = output.root / "terminal_learning"
            # 创建目录。
            episode.mkdir(parents=True, exist_ok=True)
            # 执行目标学习：把 Actor 历史 + 终局判定 + Verifier 报告蒸馏为记忆更新
            # （审计模式 exam、事件落盘、记忆日志、经验类型标记；练习不接触目标输入）。
            learned, diagnosis, history = _promote_learning(
                hooks=hooks,
                vm=environment.vm,
                cfg=output.cfg,
                lineage=root,
                episode_dir=episode,
                experience_index=learning_updates,
                before_memory=memory,
                actor_history=output.history,
                terminal_outcome=verdict.value,
                verifier_report=report,
                target=direction,
                audit_mode="exam",
                emit=lambda kind, **payload: event(root, kind, payload),
                project_open=False,
                corpus_path=spec["corpus"],
                memory_dir=active_memory,
                journal_dir=root / "memory_journal",
                experience_kind="ale-phase2-target-" + verdict.value.lower(),
                target_visible_inputs=(),
            )
            # 落盘学习回合结果：判定、诊断、记忆指纹、Actor 模型、同上下文标记。
            write_json(
                episode / "outcome.json",
                {
                    "verdict": verdict.value,
                    "diagnosis": diagnosis,
                    "memory": memory_record(active_memory),
                    "actor_model": output.cfg.model,
                    "same_actor_context": True,
                },
            )
            # 返回状态机期望的 ActorLearning（是否学到东西 + 诊断）。
            return ActorLearning(learned, diagnosis)

        # 目标循环钩子：FAIL 后的练习进化（Curriculum 驱动内环）。
        def evolve(direction, verdict, report, diagnosis, memory, index):
            # 从练习池取一次性 VM。
            lease, vm = practice_pool.acquire()
            try:
                # 运行"练习直至就绪"循环：Curriculum 审阅诊断、驱动定向练习；
                # 触发权为 phase2 结果协议，智能体 Verifier 用 target 配置，练习不接触目标输入。
                result = evolve_until_ready(
                    vm,
                    str(root / "evolution_cycles" / f"cycle_{index:03d}"),
                    direction,
                    report,
                    diagnosis,
                    memory,
                    role("actor"),
                    role("verifier"),
                    role("curriculum"),
                    role("memory"),
                    corpus_path=spec["corpus"],
                    trigger_authority="phase2_outcome_protocol",
                    triggering_outcome=verdict.value,
                    agentic_verifier_cfg=role("target"),
                    target_visible_inputs=(),
                    hooks=hooks,
                    session=curriculum,
                    event_sink=lambda kind, **payload: event(root, kind, payload),
                )
            finally:
                # 归还练习租约。
                lease.close()
            # 练习必须以语义完成（可重试/停滞）收场，否则是基础设施异常。
            if result.status not in ("ready_for_retry", "stalled"):
                raise RuntimeError(
                    "Practice stopped without semantic completion: "
                    + result.status
                    + ": "
                    + result.reason
                )
            # 翻译为状态机期望的 EvolutionResult（状态 + 新记忆树 + 项目数 + 原因）。
            return EvolutionResult(
                EvolutionStatus.READY_FOR_RETRY
                if result.status == "ready_for_retry"
                else EvolutionStatus.STALLED,
                _read_memory_tree(result.memory_dir),
                result.projects,
                result.reason,
            )

        # 装配 DRS 状态机钩子集：环境/Verifier/Actor 工厂、尝试/判定/学习/进化回调、
        # PASS 也学习且 PASS 后请 Curriculum 复盘（curriculum_review 策略）、
        # 目标环境释放与事件落盘。
        loop_hooks = SelfEvolvingLoopHooks(
            fresh_target_environment=fresh_environment,
            fresh_target_verifier=fresh_verifier,
            fresh_target_actor=fresh_actor,
            actor_work=work,
            verifier_verify=verify,
            actor_learn=learn,
            evolve=evolve,
            learn_on_pass=True,
            curriculum_after_pass=True,
            release_target_environment=lambda environment: environment.lease.close(),
            on_event=lambda kind, payload: event(root, kind, payload),
        )
        # 运行 DRS 无评估器状态机：直到 PASS/停滞/未验证。
        result = run_self_evolving_loop(spec["instruction"], initial, loop_hooks)
        # 状态机结束后释放其持有的目标环境租约。
        result.environment.lease.close()
        # 把最终记忆原子安装为冻结记忆（跨阶段边界）。
        _atomic_install_memory(str(root / "memory_frozen"), result.memory)
        # 再次校验 phase1 输入记忆未被污染（双向完整性）。
        verify_memory(spec["input_memory"])
        # 组装阶段结果记录：status=终止原因；verdict=最终判定；target_cycles/practice_projects/
        # learning_updates 为过程量；memory=冻结记忆指纹；official_evaluator_calls 恒 0；
        # transition_ready 要求存在 committed 判定（PASS/FAIL），UNVERIFIED 不得进入 phase3。
        record = {
            "phase": "phase2",
            "status": result.termination,
            "verdict": result.verifier_verdict.value,
            "target_cycles": result.target_cycles,
            "practice_projects": result.practice_projects,
            "learning_updates": learning_updates,
            "memory": memory_record(root / "memory_frozen"),
            "official_evaluator_calls": 0,
            "transition_ready": result.verifier_verdict != TargetVerdict.UNVERIFIED,
        }
        # 落盘阶段结果。
        write_json(root / "phase-result.json", record)
        # 返回给宿主。
        return record
    finally:
        # 关闭练习池。
        if practice_pool is not None:
            practice_pool.close()
        # 关闭目标池。
        target_pool.close()
