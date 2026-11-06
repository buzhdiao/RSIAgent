"""Resume a committed PASS after the first practice VM failed to boot.

No Curriculum context or practice exists at this boundary. The saved target
verdict, same-Actor learning, and original role settings are mandatory inputs.
The target candidate is not restored or regraded; Phase2 exports only memory.
"""
from pathlib import Path
import dataclasses
import hashlib
import json

from explore.practice_loop import _manifest, _read_memory_tree
from explore.phase2_recovery import read, require, sha
# 复用 Phase-2 恢复模块的读取/断言/哈希工具。


def admit_post_target_learning(cls, plan_path, root, configs, initial_memory,
                              target, stop_policy):
    # 准入：只支持“首次练习 VM 启动失败”的已提交 PASS 恢复——此刻既无 Curriculum
    # 上下文也无练习；已保存的目标判定、同 Actor 学习与原角色设置是硬性输入；
    # 目标候选不恢复也不重评，Phase-2 只导出记忆。
    plan_path, root = Path(plan_path), Path(root)
    plan = read(plan_path)
    # 读取恢复计划。
    require(plan['status'] == 'admitted_for_isolated_recovery', 'plan is not admitted')
    # 计划必须已准入隔离恢复。
    require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
    # 尝试根不得改变。
    require(stop_policy == 'curriculum_review', 'stop policy changed')
    # 停止策略不得改变。
    require(plan['completed_projects'] == plan['completed_curriculum_turns'] == 0,
            'Curriculum or practice work already exists')
    # 该边界要求项目数与 Curriculum 轮数均为 0（无练习/课程工作）。
    require(plan['prior_curriculum_iters'] == plan['prior_curriculum_wall_secs'] == 0,
            'unexpected prior Curriculum budget')
    # 不得有先前的 Curriculum 预算消耗。
    for path, digest in plan['input_sha256'].items():
        require(sha(path) == digest, 'archived input changed: ' + path)
        # 归档输入逐一哈希校验。
    for marker in ('result.json', 'memory_frozen', 'evolution_cycles',
                   'curriculum_terminal.md', 'audit_rejects.jsonl'):
        require(not (root / marker).exists(), 'boundary already advanced: ' + marker)
        # 边界不得已前进（不得出现这些终局/冻结标记）。
    require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'],
            'target cycle count changed')
    # 目标周期必须恰好只有 cycle_001。
    for path in root.rglob('boundary_audit.json'):
        audit = read(path)
        require(audit.get('status') != 'quarantined' and not audit.get('hits'),
                'quarantined transcript')
        # 所有边界审计都不得隔离或含命中。
    manifest = read(root / 'manifest.json')
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest(),
            'target direction changed')
    # 目标方向哈希一致。
    require(manifest['initial_memory']['manifest'] == _manifest(initial_memory),
            'initial memory changed')
    # 初始记忆一致。
    protocol = manifest['protocol']
    access = plan['curriculum_memory_access']
    require(access in ('none', 'read_only') and protocol['curriculum_memory_access'] == access
            and protocol['stop_policy'] == stop_policy
            and protocol['official_evaluator_calls'] == 0
            and not protocol['official_evaluator_feedback_enters_learning'],
            'protocol boundary changed')
    # 协议边界：Curriculum 记忆访问为 none/read_only、停止策略一致、官方评测零调用
    # 且官方评测反馈不进入学习。
    require(not list(root.rglob('grading.json')), 'official grading entered Phase2')
    # 任何 grading.json 都不得存在（官方评分未进入 Phase-2）。
    for role, cfg in configs.items():
        saved = manifest['configs'][role]
        require(saved['sha256'] == sha(saved['path']), 'role config bytes changed')
        require(saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))),
                'effective role config changed')
        # 各角色配置字节与生效配置双重校验。
    cycle = root / 'target_cycles/cycle_001'
    learning_root = cycle / 'terminal_learning'
    learning = read(learning_root / 'outcome.json')
    # 读取目标学习结果。
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] == 'PASS',
            'completed target PASS learning required')
    # 必须是 cycle 1 的已完成 PASS 学习。
    require(learning['memory_before'] == _manifest(initial_memory), 'memory chain changed')
    # 记忆链起点一致。
    memory = _read_memory_tree(str(root / 'active_memory'))
    require(learning['memory_after'] == _manifest(memory), 'committed memory changed')
    # 提交后的记忆（活动记忆树）与学习结果一致。
    verifier_path = Path(plan['target_verification'])
    require(verifier_path.is_relative_to(cycle), 'verdict from another target')
    # 判定文件必须属于本目标周期。
    verifier = read(verifier_path)
    require(verifier['verdict'] == 'pass' and verifier['findings'] == learning['verifier_report'],
            'target verdict and learning disagree')
    # 判定与学习的报告必须一致。
    paths = [cycle / 'transcript.json',
             learning_root / 'memory_distillation/segment_000/transcript.json',
             learning_root / 'memory_reconciliation/segment_000/transcript.json',
             learning_root / 'learning_diagnosis/attempt_000/segment_000/transcript.json']
    # 四份同 Actor transcript：主对话、蒸馏、调和、诊断。
    histories = [read(path) for path in paths]
    require(all(a['system'] == b['system']
                and b['messages'][:len(a['messages'])] == a['messages']
                for a, b in zip(histories, histories[1:])), 'same-Actor history prefix lost')
    # 前缀关系校验：后续 transcript 必须逐字节包含前者（同一 Actor 会话的延长）。
    require(len(histories[0]['messages']) == learning['active_history_messages_before']
            and len(histories[-1]['messages']) == learning['active_history_messages_after'],
            'learning history counters changed')
    # 学习前后消息计数与工件记录一致。
    require(all(history['messages'] and len(history['messages']) % 2 == 0
                and all(message.get('role') == ('user' if i % 2 == 0 else 'assistant')
                        for i, message in enumerate(history['messages']))
                for history in histories), 'incomplete Actor context')
    # 全部历史都必须是完整的 user/assistant 成对上下文。
    require((learning_root / 'learning_diagnosis.md').read_text() ==
            learning['actor_learning_diagnosis'], 'diagnosis changed')
    # 诊断渲染文本与工件一致。
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    require(len(events) >= 2 and events[-2]['event'] == 'TARGET_LEARNING_COMPLETED'
            and events[-2]['payload']['target_cycle'] == 1
            and events[-2]['payload']['verdict'] == 'PASS'
            and events[-1]['event'] == 'PASS_NEXT_EXPERIENCE_REVIEW_STARTED'
            and events[-1]['payload']['target_cycle'] == 1
            and events[-1]['payload']['prospective_evolution'] == 1,
            'not the pre-Curriculum boundary')
    # 事件序列必须恰好停在“目标学习完成(PASS)→下一经验评审已开始”的 Curriculum 前边界。
    require('TimeoutError: VM failed to become ready within timeout period' in
            Path(plan['failed_runtime_log']).read_text(), 'VM boot fault is not recorded')
    # 失败运行日志必须记录了 VM 启动超时故障（确证恢复理由）。
    trigger = dict(target=target, trigger_authority='phase2_outcome_protocol',
                   verifier_outcome='PASS', verifier_report=learning['verifier_report'],
                   actor_learning_diagnosis=learning['actor_learning_diagnosis'],
                   memory_before=learning['memory_after'])
    # 依据已验证工件重建触发器（PASS、报告、诊断、记忆链）。
    curriculum = configs['curriculum']
    return cls(plan_path, root, root / 'evolution_cycles/cycle_001', target,
               learning, trigger, {}, [], '', '', curriculum.max_iters,
               curriculum.wall_clock_secs, {}, '', projects=0, project_records=[],
               completed_curriculum_turns=0, initial_evolution_memory=memory,
               curriculum_memory_access=access, post_target_learning=True)
    # 构造恢复凭证：零项目、零轮、全额角色预算、post_target_learning=True。


def continue_after_target_learning(recovery, direction, hooks):
    """Call the original Curriculum once, then honor its normal stop semantics."""
    # （docstring：先调用一次原始 Curriculum，然后遵循其正常停止语义。）
    from core.self_evolving_loop import (
        EvolutionResult, EvolutionStatus, SelfEvolvingLoopResult,
        SelfEvolvingStart, TargetVerdict, run_self_evolving_loop,
    )
    # 延迟导入自进化主循环的类型与运行器。
    require(recovery.post_target_learning and recovery.projects == 0
            and recovery.trigger['verifier_outcome'] == 'PASS', 'wrong recovery boundary')
    # 只在“目标后学习、零项目、PASS”边界上允许继续。
    trigger = recovery.trigger
    evolved = hooks.evolve(direction, TargetVerdict.PASS, trigger['verifier_report'],
                           trigger['actor_learning_diagnosis'],
                           recovery.initial_evolution_memory, 1)
    # 以保存的 PASS 证据调用一次进化（即原始 Curriculum 的一次评审）。
    require(isinstance(evolved, EvolutionResult), 'invalid evolution result')
    # 返回类型必须合法。
    if evolved.projects == 0:
        termination = ('verifier_pass_curriculum_ready'
                       if evolved.status is EvolutionStatus.READY_FOR_RETRY
                       else 'verifier_pass_curriculum_stalled')
        # 零项目：按 Curriculum 状态判定“就绪重试”或“停滞”的终止原因。
        # No completed practice means the memory must still be the committed
        # target learning. No synthetic candidate or extra target test is needed.
        # （既有英文注释：无已完成练习意味着记忆必须仍是已提交的目标学习；
        # 不需要合成候选或额外目标测试。）
        require(evolved.memory == recovery.initial_evolution_memory,
                'zero-practice Curriculum changed Actor memory')
        # 零练习的 Curriculum 不得改动 Actor 记忆。
        hooks.on_event('PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED', dict(
            target_cycle=1, projects=0, reason=evolved.reason, termination=termination))
        # 记录评审完成事件。
        hooks.on_event('TARGET_LOOP_TERMINATED', dict(
            target_cycle=1, verdict='PASS', termination=termination, evolutions=0,
            recovered_after_target_learning=True))
        # 记录目标循环终止事件（带恢复来源标记）。
        return SelfEvolvingLoopResult(None, None, TargetVerdict.PASS,
                                      trigger['verifier_report'], evolved.memory,
                                      1, 0, 0, termination)
        # 直接返回终局结果：PASS、原报告、未改动记忆、0 进化、终止原因。
    start = SelfEvolvingStart(target_cycles=1, evolutions=1,
                             practice_projects=evolved.projects,
                             final_after_stall=evolved.status is EvolutionStatus.STALLED)
    # Curriculum 选出了经验：以恢复出的项目数启动一次受限的自进化循环。
    hooks.on_event('RECOVERED_EVOLUTION_COMPLETED', dict(
        target_cycle=1, evolution=1, projects=evolved.projects,
        status=evolved.status.value, recovery_plan=str(recovery.plan_path)))
    # 记录恢复进化完成事件（含恢复计划路径）。
    return run_self_evolving_loop(direction, evolved.memory, hooks, start=start)
    # 交给标准自进化循环继续执行。
