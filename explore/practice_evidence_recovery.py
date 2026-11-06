"""Preserve the practice Actor/Verifier identities across an evidence request."""
# 在练习中跨"证据请求"保全 Actor（行动者）/Verifier（验证者）两种角色身份的恢复模块。
from __future__ import annotations

import ast
# ast 模块：只解析已保存 Agent 程序的字面常量，绝不执行它。
import dataclasses
# dataclasses：以不可变替换（replace）方式派生新配置。
import hashlib
# hashlib：对报告等工件计算 SHA-256 指纹。
import json
# json：解析事件流与工件。
import time
# time：单调时钟计时，用于预算扣减。
import tarfile
# tarfile：只读打开目标学习日志归档，恢复触发器记忆。
from pathlib import Path
# Path：路径对象。

from explore import target_learning as V
# 目标学习模块（别名 V）：提供 VM 编排与审计工具函数。
from explore.practice_loop import _manifest, _atomic_json, _safe_memory_name
# 练习主循环工具：记忆清单、原子写 JSON、安全记忆文件名校验。
from explore.phase2_recovery import read, require, sha
# Phase-2 恢复的严格准入工具：读 JSON、fail-closed 断言、文件哈希。


def paired(messages):
    # 判断消息历史是否为完整的 user/assistant 交替对（角色历史完整性校验）。
    return bool(messages) and len(messages) % 2 == 0 and all(
        m.get('role') == ('user' if i % 2 == 0 else 'assistant')
        for i, m in enumerate(messages))
    # 非空、偶数长度，且偶数位是 user、奇数位是 assistant。


def actor_leads(history, project, cfg):
    """Transport only the Actor's final reproducible Done checks, never its history."""
    # 只搬运 Actor 最终轮中"可复现的 Done 检查项"作为证据线索，绝不搬运其历史对话。
    from core.actor import Done, parse_turn
    # 延迟导入：Done 回合类型与回合解析器。
    from core.checks import validate
    # 检查项校验器。
    from core.loop import _actor_evidence_leads
    # 把已接受的检查项格式化为证据线索的函数。
    turn = parse_turn(history[-1].get('content', '')) if history else None
    # 解析 Actor 历史的最后一轮内容；无历史则为 None。
    if not isinstance(turn, Done):
        # 最后一轮不是 Done（未宣布完成）则没有可搬运的检查。
        return ''
        # 返回空线索。
    accepted, _, _ = validate(turn.checks, cfg.max_checks, instruction=project,
                             witness_gates=getattr(cfg, 'done_witness_gates', False))
    # 用与正式流程相同的规则校验 Done 检查项，得到被接受的子集。
    return _actor_evidence_leads(accepted, [])
    # 把接受的检查项转成供 Verifier 参考的证据线索文本。


def run_practice_with_evidence(*, hooks, vm, lineage, episode_dir, project_index,
                              project, fixture_dir, target, actor_cfg, verifier_cfg,
                              memory_dir, emit, agentic_verifier_cfg,
                              target_visible_inputs=None, resume_state=None):
    # 练习主流程（带证据请求续跑）：Actor 产出候选 -> Verifier 审查；
    # unverified 判定可作为"证据请求"回传给 Actor，形成下一轮循环。
    if resume_state is not None and resume_state.get('resume_kind') == 'verified_memory':
        # 恢复状态声明为"已验证记忆续跑"时改走专用恢复路径。
        from explore.practice_memory_recovery import restore_learning_candidate
        # 延迟导入恢复函数（避免模块循环依赖）。
        return restore_learning_candidate(
            hooks=hooks, vm=vm, episode_dir=episode_dir, fixture_dir=fixture_dir,
            memory_dir=memory_dir, target=target, emit=emit, saved=resume_state)
        # 只恢复归档输入用于学习，不重跑 Actor/Verifier。
    from config.settings import load
    # 延迟导入配置加载器。
    from core.trace import ArtifactSink
    # 工件落盘容器：把回合转录写入指定目录。
    from core.verifier import (VerifierSession, _parse_agentic_verifier_report,
                               unverified_message, verify_agentic)
    # Verifier 会话、报告解析、unverified 续跑提示与主验证函数。

    require(agentic_verifier_cfg is not None and
            agentic_verifier_cfg.agentic_verifier_config, 'missing Agentic Verifier config')
    # 严格断言：必须提供 Agentic Verifier 配置（fail-closed）。
    before_memory = V._read_memory_tree(str(memory_dir))
    # 记录本轮开始前的规范记忆树，用于事后校验记忆未被练习污染。
    audit_target = V._target_audit_surface(target, target_visible_inputs)
    # 计算目标审计面：练习边界检查所允许的目标文本范围。
    saved = resume_state or {}
    # 恢复状态（首次运行为空 dict）。
    history = list(saved.get('actor_history', []))
    # 恢复 Actor 对话历史（续跑时保留完整上下文）。
    session = VerifierSession(saved.get('verifier_history', []))
    # 恢复 Verifier 会话（同一对话跨 VM 延续）。
    session.inspections = session.candidate_generation = int(saved.get('inspections', 0))
    # 恢复已进行的审查/候选生成计数。
    findings = saved.get('findings', '')
    # 恢复上一次 unverified 报告（即待回应的证据请求）。
    candidate = Path(saved['candidate_dir']) if saved else None
    # 恢复上一轮候选目录（无则从零开始）。
    publication = int(saved.get('publication', 0))
    # 已发布的候选代数（第 N 版候选）。
    actor_used = int(saved.get('prior_actor_iters', 0))
    # 已消耗的 Actor 迭代预算。
    actor_wall = float(saved.get('prior_actor_wall_secs', 0))
    # 已消耗的 Actor 墙钟预算（秒）。
    verifier_used = int(saved.get('prior_verifier_iters', 0))
    # 已消耗的 Verifier 迭代预算。
    verifier_wall = float(saved.get('prior_verifier_wall_secs', 0))
    # 已消耗的 Verifier 墙钟预算（秒）。
    verifier_role = load(agentic_verifier_cfg.agentic_verifier_config)
    # 加载 Verifier 角色配置（迭代/墙钟上限等）。
    verifier_wall_cap = min(verifier_role.wall_clock_secs,
                            agentic_verifier_cfg.wall_clock_secs)
    # Verifier 墙钟上限取角色配置与全局配置的较小者。
    private_paths = tuple(dict.fromkeys(
        tuple(getattr(agentic_verifier_cfg, 'verifier_private_paths', ()) or ())
        + (V.ACTOR_HANDOFF, V.ACTOR_EXECUTION_EVIDENCE)))
    # 合并 Verifier 私有路径并去重：加入 Actor 交接文与执行证据（对 Verifier 隐藏）。
    control = dataclasses.replace(
        agentic_verifier_cfg, verifier_evolve_route=False,
        verifier_local_verdict_only=False, verifier_hide_actor_memory=True,
        verifier_stage_lifecycle=False, verifier_persist_scratch=False,
        verifier_private_paths=private_paths)
    # 派生受控 Verifier 配置：不路由进化、隐藏 Actor 记忆、不落私有草稿。
    context = (f'The candidate project is {V.PROJECT_ROOT}. The frozen original '
               f'input fixture is {V.PRACTICE_FIXTURE_EVIDENCE}; use it only as '
               'pre-candidate evidence. Actor-private memory, handoff prose, and '
               'execution logs are hidden by the harness.')
    # 给 Verifier 的情境说明：候选位置、冻结的原始输入夹具（fixture）、
    # Actor 私有记忆/交接文/执行日志均被 harness 隐藏。
    if saved:
        context += (' This inspection continues the same Verifier conversation in '
                    'a fresh verification VM. Previous private scratch files are absent; '
                    're-establish probes against the current candidate and original fixtures.')
        # 续跑时补充说明：同一 Verifier 对话在新 VM 中继续，旧草稿不存在，需重建探针。
    try:
        while True:
            # 主循环：Actor 轮与 Verifier 轮交替，直到二值判定或预算耗尽。
            require(actor_used < actor_cfg.max_iters and actor_wall < actor_cfg.wall_clock_secs,
                    'practice Actor remaining budget exhausted')
            # 严格断言：Actor 仍有剩余迭代与墙钟预算。
            started = time.monotonic()
            # 记录本 Actor 轮开始时间（单调时钟）。
            V._fresh_vm(hooks, vm, target)
            # 准备全新的练习 VM。
            V._replay(hooks, vm, str(fixture_dir))
            # 回放冻结的原始输入夹具。
            if candidate is not None:
                # 已有候选则一并回放（证据请求续跑时基于既有候选继续）。
                V._replay(hooks, vm, str(candidate))
                # 回放候选项目材料。
            V._push_canonical_memory(hooks, vm, str(memory_dir))
            # 把规范记忆推送进 VM（只读工作环境的一部分）。
            prompt = V.self_evolving_actor_charter(
                project, V._listing(before_memory), V.ACTOR_HANDOFF) + V._actor_runtime_contract()
            # 组装 Actor 章程（charter）：项目要求、记忆清单、交接路径，附运行时契约。
            evidence_requested = bool(findings)
            # 是否存在待回应的证据请求（上轮 unverified 报告非空）。
            if evidence_requested:
                # 有证据请求则追加到提示中。
                prompt += '\n\n' + unverified_message(findings, evidence_request=True)
                # 把 unverified 报告包装成"证据请求"消息。
            emit('ACTOR_EVIDENCE_REQUESTED' if evidence_requested else 'ACTOR_PHASE_STARTED',
                 status='in_progress', project_open=True, memory_phase_open=False,
                 payload={'project_index': project_index, 'actor_attempt': 1,
                          'verification_cycle': session.inspections,
                          'verifier_report_sha256': hashlib.sha256(findings.encode()).hexdigest()})
            # 发出事件：证据请求轮或普通 Actor 轮开始（附报告指纹供事后审计）。
            actor_root = episode_dir / 'actor/attempt_001'
            # Actor 工件根目录（唯一尝试目录）。
            before_turns = len(list(actor_root.glob('segment_*/iter_*/turn.txt')))
            # 记录本轮之前的 turn 文件数，用于精确计费新增迭代。
            remaining_wall = actor_cfg.wall_clock_secs - actor_wall - (time.monotonic() - started)
            # 计算扣除环境搭建耗时后的剩余墙钟。
            require(remaining_wall > 0, 'Actor setup exhausted remaining budget')
            # 环境搭建不能耗尽全部剩余预算。
            cfg = dataclasses.replace(actor_cfg, max_iters=actor_cfg.max_iters - actor_used,
                                      wall_clock_secs=remaining_wall)
            # 派生本次 Actor 运行的预算配置（扣除已用量）。
            decision, history, result = V._run_handoff_phase(
                hooks=hooks, vm=vm, cfg=cfg, prompt=prompt, role='ACTOR',
                handoff_path=V.ACTOR_HANDOFF, token_pattern=V._ACTOR_TOKEN,
                sink_root=str(actor_root), target=audit_target,
                history=history, continuation=bool(history), bounded_transport=True)
            # 运行 Actor 交接阶段：产出决策文本、更新历史，工件按限定传输落盘。
            actor_used += max(1, len(list(actor_root.glob('segment_*/iter_*/turn.txt'))) - before_turns,
                              int(getattr(result, 'turns', 0) or getattr(result, 'iters', 0)))
            # 计费：取"新增 turn 文件数"与"结果报告的轮数"的较大者（至少 1）。
            actor_wall += time.monotonic() - started
            # 累计 Actor 墙钟消耗。
            publication += 1
            # 候选代数递增。
            candidate = episode_dir / 'candidate' / f'publication_{publication:03d}'
            # 本轮候选目录名（publication_001、002…）。
            require(not candidate.exists(), 'candidate publication already exists')
            # 严格断言：候选目录必须不存在（防止覆盖历史工件）。
            V._require_owned_project_shape(vm)
            # 校验 VM 中项目树形状仍归 Actor 所有（未被篡改到越界）。
            V._capture_owned_tree(hooks, vm, f'ep{project_index:03d}-candidate-{publication:03d}',
                                  str(candidate), audit_target)
            # 把 VM 内的项目树原样捕获为候选快照（含清单校验）。
            handoff_path = episode_dir / ('actor_handoff.md' if publication == 1 else
                                         f'actor_handoff_{publication:03d}.md')
            # Actor 交接文路径：第一代无序号，之后带代数后缀。
            require(not handoff_path.exists(), 'Actor handoff already exists')
            # 严格断言：交接文不存在（不可覆盖）。
            V._atomic_text(handoff_path, decision.text)
            # 原子写入 Actor 交接文。
            evidence = V._build_actor_execution_evidence(
                episode_dir / 'actor', 1,
                episode_dir / 'actor_execution_evidence' / f'publication_{publication:03d}')
            # 从 Actor 工件构建执行证据包（含清单哈希），供 Verifier 复核。
            require(verifier_used < verifier_role.max_iters and
                    verifier_wall < verifier_wall_cap,
                    'practice Verifier remaining budget exhausted')
            # 严格断言：Verifier 仍有剩余迭代与墙钟预算。
            started = time.monotonic()
            # 记录本 Verifier 轮开始时间。
            V._fresh_vm(hooks, vm, target)
            # 为 Verifier 准备全新 VM。
            V._replay(hooks, vm, str(fixture_dir))
            # 回放原始输入夹具。
            V._stage_original_practice_fixtures(vm)
            # 额外布置"原始练习夹具"副本（作为前候选证据对照）。
            V._replay(hooks, vm, str(candidate))
            # 回放候选项目材料（被审查对象）。
            emit('VERIFIER_PHASE_STARTED', status='in_progress', project_open=True,
                 memory_phase_open=False, payload={'project_index': project_index,
                 'verification_cycle': session.inspections + 1,
                 'actor_execution_manifest_sha256': evidence['manifest_sha256']})
            # 发出 Verifier 阶段开始事件（附执行证据清单哈希）。
            verifier_root = episode_dir / 'verifier'
            # Verifier 工件根目录。
            before_turns = len(list(verifier_root.glob('verifier_agent/inspection_*/segment_*/iter_*/turn.txt')))
            # 记录本轮之前的 Verifier turn 文件数。
            remaining_wall = verifier_wall_cap - verifier_wall - (time.monotonic() - started)
            # 计算扣除环境搭建耗时后的 Verifier 剩余墙钟。
            require(remaining_wall > 0, 'Verifier setup exhausted remaining budget')
            # 环境搭建不能耗尽全部剩余预算。
            verdict, report = verify_agentic(
                project, vm, control, sink=ArtifactSink(str(verifier_root)),
                turn_no=project_index, context=context, session=session,
                wall_budget=remaining_wall, iters_budget=verifier_role.max_iters-verifier_used,
                actor_evidence_leads=actor_leads(history, project, actor_cfg) if evidence_requested else '')
            # 运行 Agentic 验证：返回二值判定与完整报告；
            # 有证据请求时附带 Actor 的可复现检查线索。
            verifier_wall += time.monotonic() - started
            # 累计 Verifier 墙钟消耗。
            verifier_used += max(1, len(list(verifier_root.glob(
                'verifier_agent/inspection_*/segment_*/iter_*/turn.txt'))) - before_turns)
            # 计费：取新增 Verifier turn 数（至少 1）。
            V._verify_candidate(hooks, vm, str(candidate))
            # 复核候选快照与 VM 内容一致（未被 Agent 私自改动）。
            V._verify_original_practice_fixtures(hooks, vm, fixture_dir)
            # 复核原始夹具未被改动。
            require(_manifest(before_memory) == _manifest(
                V._read_memory_tree(str(memory_dir))), 'memory changed during unverified practice')
            # 严格断言：未经证实的练习期间规范记忆绝不能变化。
            _atomic_json(episode_dir / f'evidence_cycle_{publication:03d}.json', {
                'verdict': verdict, 'actor_iters_charged': actor_used,
                'actor_wall_secs_charged': actor_wall, 'verifier_iters_charged': verifier_used,
                'verifier_wall_secs_charged': verifier_wall,
                'actor_memory_unchanged': _manifest(before_memory) ==
                    _manifest(V._read_memory_tree(str(memory_dir)))})
            # 原子记录本轮证据循环：判定、双方计费与"记忆未变"证明。
            if verdict == 'unverified':
                # Verifier 无法给出判定（基础设施缺失等）时：
                require(_parse_agentic_verifier_report(str(report), allow_unverified=True) == 'unverified',
                        'Verifier infrastructure absence is not an evidence request')
                # 严格断言：报告确实是合法的 unverified（而非伪装的证据请求）。
                findings = str(report)
                # 把报告保存为待回应的证据请求。
                V._atomic_text(episode_dir / f'unverified_report_{publication:03d}.md', findings)
                # 原子落盘 unverified 报告。
                continue
                # 回到循环顶部：Actor 下一轮回应证据请求。
            require(verdict in {'pass', 'wrong'}, 'Verifier did not publish a binary task verdict')
            # 严格断言：Verifier 必须给出 pass/wrong 二值判定。
            V._push_actor_execution_evidence(hooks, vm, evidence)
            # 把 Actor 执行证据推送进 VM 供终验。
            V._verify_actor_execution_evidence(vm, evidence)
            # 校验执行证据未被改动。
            V._atomic_text(episode_dir / 'verifier_report.md', str(report))
            # 原子落盘最终 Verifier 报告。
            V._fresh_vm(hooks, vm, target)
            # 起最终复核 VM。
            V._replay(hooks, vm, str(candidate))
            # 回放候选。
            V._push_canonical_memory(hooks, vm, str(memory_dir))
            # 推送规范记忆（保持环境与真实学习时一致）。
            return {'terminal_outcome': 'PASS' if verdict == 'pass' else 'FAIL',
                    'verifier_report': str(report), 'actor_handoff': decision.text,
                    'actor_history': history, 'before_memory': before_memory}
            # 返回终局结果：PASS/FAIL、报告、Actor 交接文、历史与起始记忆。
    finally:
        session.close_executor()
        # 无论成败都关闭 Verifier 的执行器（释放资源）。


def admit_practice_recovery(cls, plan_path, root, configs, initial_memory, target, stop_policy):
    """Admit a preserved first-cycle evidence request after zero or one practices."""
    # 严格准入：在 0 或 1 个练习完成后，恢复被保全的"第一轮证据请求"。
    from core.verifier import _parse_agentic_verifier_report
    # 延迟导入：报告解析器（允许 unverified）。
    from explore.practice_loop import _parse_handoff, _UNIFIED_CURRICULUM_TOKEN
    # 延迟导入：交接文解析器与统一课程令牌。
    plan = read(plan_path)
    # 读取恢复计划 JSON。
    completed = plan.get('completed_projects', 1)
    # 计划中已完成的练习数。
    require(type(completed) is int and completed in {0, 1}, 'unsupported practice boundary')
    # 严格断言：只支持 0 或 1 个已完成练习的边界。
    pending_index = completed + 1
    # 待恢复练习的序号（ep001 或 ep002）。
    require(plan['status'] == 'admitted_for_isolated_recovery' and stop_policy == 'curriculum_review',
            'recovery authorization/policy changed')
    # 严格断言：恢复授权状态与停止策略未变。
    require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
    # 严格断言：尝试根目录未变。
    for path, expected in plan['input_sha256'].items():
        # 逐一校验归档输入的 SHA-256。
        require(sha(path) == expected, 'archived practice input changed: ' + path)
        # 任何输入被改动都拒绝恢复。
    require(not (root / 'result.json').exists(), 'attempt already completed')
    # 严格断言：原尝试尚未完成（无 result.json）。
    require(sorted(p.name for p in (root/'target_cycles').iterdir()) == ['cycle_001'] and
            sorted(p.name for p in (root/'evolution_cycles').iterdir()) == ['cycle_001'], 'cycle count changed')
    # 严格断言：目标周期与进化周期都只有一个 cycle_001。
    cycle = root/'evolution_cycles/cycle_001'
    # 进化周期目录。
    require(sorted(p.name for p in (cycle/'episodes').iterdir()) ==
            [f'ep{i:03d}' for i in range(1, pending_index + 1)] and
            sorted(p.name for p in (cycle/'curriculum').iterdir()) ==
            [f'turn_{i:03d}' for i in range(1, pending_index + 1)],
            'practice/Curriculum boundary changed')
    # 严格断言：练习集与课程轮数恰好到 pending_index（边界未变）。
    episode = cycle/f'episodes/ep{pending_index:03d}'
    # 待恢复的练习集目录。
    require(not any((episode/name).exists() for name in
                    ('outcome.json','verifier_report.md','memory_distillation','memory_reconciliation','learning_diagnosis')),
            'open practice already reached learning or a terminal verdict')
    # 严格断言：未关闭的练习尚未进入学习阶段或终局判定。
    for p in root.rglob('boundary_audit.json'):
        # 遍历所有边界审计文件。
        r = read(p)
        # 读取审计记录。
        require(r.get('status') != 'quarantined' and not r.get('hits'), 'quarantined lineage')
        # 严格断言：血统未被隔离、无命中。
    require(not list(root.rglob('audit_rejects.jsonl')), 'rejected lineage needs separate review')
    # 严格断言：没有被拒绝的血统（否则需另行审查）。
    manifest = read(root/'manifest.json')
    # 读取尝试清单。
    require(manifest.get('protocol', {}).get('curriculum_memory_access', 'read_only') ==
            plan.get('curriculum_memory_access', 'read_only'), 'Curriculum memory access changed')
    # 严格断言：课程记忆访问模式与计划一致。
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest() and
            manifest['initial_memory']['manifest'] == _manifest(initial_memory), 'target/initial memory changed')
    # 严格断言：目标方向与初始记忆未被改动。
    for role,cfg in configs.items():
        # 逐一校验各角色配置。
        entry = manifest['configs'][role]
        # 清单中保存的该角色配置条目。
        require(sha(entry['path']) == entry['sha256'] and entry['effective'] ==
                json.loads(json.dumps(dataclasses.asdict(cfg))), 'role configuration changed')
        # 严格断言：配置文件哈希与生效配置均未变。
    learning = read(root/'target_cycles/cycle_001/terminal_learning/outcome.json')
    # 读取目标学习的终局记录。
    records = [read(cycle/f'episodes/ep{i:03d}/outcome.json') for i in range(1, completed + 1)]
    # 读取全部已完成练习的结果记录。
    project = records[-1] if records else learning
    # 取最后一个练习记录；无练习则用目标学习记录。
    trigger = read(cycle/'trigger/outcome.json')
    # 读取触发器记录。
    state = read(cycle/'state.json')
    # 读取周期状态。
    require(state['status'] == 'infra' and state['projects'] == pending_index
            and state['learning_experiences'] == completed
            and state['reason'].startswith('practice Agentic Verifier ended without PASS/FAIL:'),
            'not the recorded UNVERIFIED contract failure')
    # 严格断言：确为记录在案的"练习 Verifier 未给出 PASS/FAIL"基础设施失败。
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] in ('PASS','FAIL')
            and all(p['project_index'] == i and p['terminal_outcome'] in ('PASS','FAIL')
                    for i, p in enumerate(records, 1)),
            'completed learning has no grounded verdict')
    # 严格断言：已完成的学习都有落地的 PASS/FAIL 判定。
    require(learning['memory_before'] == _manifest(initial_memory) and
            learning['memory_after'] == trigger['memory_before'] and
            (not records or trigger['memory_before'] == records[0]['memory_before']),
            'target/practice memory chain changed')
    # 严格断言：目标学习->触发器->各练习的记忆链完整一致。
    # The failed evolve wrapper installed the last committed practice memory
    # into active_memory before raising. Recover its original trigger input from
    # the immutable target-learning journal, without changing either live tree.
    # （中文）失败的进化包装器在抛错前已把"最后一次提交的练习记忆"装入 active_memory；
    # 这里从不可变的目标学习日志恢复其原始触发器输入，且不改动两棵活记忆树。
    require(project['memory_after'] == _manifest(V._read_memory_tree(str(root/'active_memory'))),
            'failed evolution did not preserve the last complete practice memory')
    # 严格断言：active_memory 确实保存着最后一次完整的练习记忆。
    with tarfile.open(root/'memory_journal/ep001.tgz') as archive:
        # 只读打开目标学习日志归档。
        members = archive.getmembers()
        # 列出全部成员。
        require(all(m.isfile() and _safe_memory_name(m.name) for m in members) and
                len({m.name for m in members}) == len(members), 'unsafe target-learning journal')
        # 严格断言：成员全是普通文件、路径安全且无重名（防路径穿越）。
        trigger_memory = {m.name: archive.extractfile(m).read() for m in members}
        # 读出归档中的原始触发器记忆（文件名->字节）。
    require(_manifest(trigger_memory) == trigger['memory_before'], 'target-learning journal changed')
    # 严格断言：日志记忆与触发器记录的 memory_before 一致。
    require(project['memory_after'] == state['memory_manifest'] ==
            _manifest(V._read_memory_tree(str(cycle/'memory'))) and state['memory_tree_sha256'] ==
            V._memory_tree_sha256(V._read_memory_tree(str(cycle/'memory'))), 'committed memory changed')
    # 严格断言：已提交记忆（清单与树哈希）未被改动。
    require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol' and
            trigger['verifier_report'] == learning['verifier_report'] and
            trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'], 'trigger changed')
    # 严格断言：触发器的目标、权威、报告与诊断均未被改动。
    events=[json.loads(line) for line in (root/'events.jsonl').read_text().splitlines()]
    # 解析全部事件流。
    require(events[-1]['event'] == 'EVOLUTION_VERIFIER_PHASE_STARTED' and
            events[-1]['payload']['payload']['project_index'] == pending_index and
            events[-1]['payload']['state']['memory_phase_open'] is False, 'a later transition exists')
    # 严格断言：最后事件确是待恢复练习的 Verifier 阶段开始（之后无其他状态迁移）。
    verification=read(episode/f'verifier/iter_{pending_index:02d}/verify2.json')
    # 读取该练习的验证结果工件。
    require(verification['verdict'] == 'unverified' and _parse_agentic_verifier_report(
        verification['findings'],allow_unverified=True) == 'unverified', 'no valid evidence request')
    # 严格断言：确有合法的 unverified 证据请求。
    source=Path(plan['source_transcript'])
    # 计划中记录的课程转录来源。
    require(source == sorted((cycle/f'curriculum/turn_{pending_index:03d}').glob('segment_*/transcript.json'))[-1],
            'not latest Curriculum checkpoint')
    # 严格断言：来源确为最新课程检查点。
    history=read(source)['messages']
    # 读取课程对话历史。
    actor_source=sorted((episode/'actor/attempt_001').glob('segment_*/transcript.json'))[-1]
    # 定位 Actor 最新转录。
    actor_history=read(actor_source)['messages']
    # 读取 Actor 对话历史。
    require(paired(history) and paired(actor_history) and paired(verification['transcript']), 'incomplete role history')
    # 严格断言：三条角色历史都是完整交替对。
    # Parse only literal constants from the last successful Curriculum publication;
    # never execute the saved Agent Program or infer its private notes from prose.
    # （中文）只从最后一次成功的课程发布中解析字面常量；
    # 绝不执行已保存的 Agent 程序，也绝不从 prose 推断其私有笔记。
    program=Path(plan['curriculum_publication_program'])
    # 课程发布程序路径。
    nodes=ast.parse(program.read_text()).body
    # 仅做 AST 解析（不执行）。
    literals={n.targets[0].id:ast.literal_eval(n.value) for n in nodes
              if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)
              and n.targets[0].id in ('notes','handoff') and isinstance(n.value,ast.Constant)}
    # 提取 notes/handoff 两个字面常量赋值。
    require(isinstance(literals.get('notes'),str) and literals.get('handoff') ==
            (episode/'project.md').read_text(), 'exact published Curriculum notes/handoff unavailable')
    # 严格断言：字面 notes 是字符串，且 handoff 与已发布项目文逐字节一致。
    trace = (program.parent/'trace.txt').read_text()
    # 读取该程序运行轨迹。
    # Accept the first-practice publication's exact UTF-8 byte-count witness
    # only alongside a direct top-level write of the literal private notes.
    # （中文）仅当存在"对字面私有笔记的直接顶层写入"时，才接受首次练习发布的
    # 精确 UTF-8 字节计数见证。
    notes_write = ast.dump(ast.parse(
        "open('/home/user/curriculum_notes.md', 'w').write(notes)").body[0])
    # 期望的"直接写笔记"AST 形状（用于结构等价比较）。
    direct_write = any(ast.dump(n) == notes_write for n in nodes)
    # 程序中是否存在这样一次直接写入。
    byte_witness = f"curriculum_notes.md: {len(literals['notes'].encode('utf-8'))} bytes"
    # 期望的字节计数见证行。
    require(read(program.parent/'trace_meta.json')['exit_code'] == 0 and
            ('notes exists: True' in trace or (direct_write and byte_witness in trace)),
            'notes were not published')
    # 严格断言：程序成功退出，且"笔记存在"或（直接写入且字节计数吻合）。
    saved_project = _parse_handoff(literals['handoff'], _UNIFIED_CURRICULUM_TOKEN)
    # 从字面 handoff 解析出保存的项目体。
    require(saved_project is not None and saved_project.token == 'PROJECT', 'invalid saved project')
    # 严格断言：保存项目有效且令牌为 PROJECT。
    candidate=episode/'candidate/publication_001'
    # 候选目录。
    require(sorted(p.name for p in (episode/'candidate').iterdir()) == ['publication_001'],
            'candidate generation changed')
    # 严格断言：候选只有一代（publication_001）。
    budget=plan['prior_role_budget']
    # 计划中记录的双方已消耗预算。
    require(budget['prior_actor_iters'] == len(list((episode/'actor').rglob('turn.txt'))) and
            budget['prior_verifier_iters'] == len(list((episode/'verifier').rglob('turn.txt'))),
            'saved role iteration count changed')
    # 严格断言：保存的迭代数与工件中 turn 文件数一致。
    for role,kind in [('practice_actor','actor'),('practice_verifier','verifier')]:
        # 逐一校验双方剩余预算仍然为正。
        cap = configs[role].wall_clock_secs
        # 该角色的墙钟上限。
        if kind == 'verifier':
            # Verifier 上限取其自身与目标 Actor 配置的较小者。
            cap = min(cap, configs['target_actor'].wall_clock_secs)
        require(0 < budget[f'prior_{kind}_wall_secs'] < cap and
                0 < budget[f'prior_{kind}_iters'] < configs[role].max_iters, 'remaining role budget exhausted')
        # 严格断言：已用量严格介于 0 与上限之间（确有余量）。
    pending=dict(budget,actor_history=actor_history,verifier_history=verification['transcript'],
                 findings=verification['findings'],candidate_dir=str(candidate),publication=1,inspections=1,
                 project=saved_project.body)
    # 组装待恢复练习的状态包：预算、双方历史、证据请求、候选目录与项目体。
    latest = cycle/f'episodes/ep{completed:03d}/outcome.md' if completed else cycle/'trigger/outcome.md'
    # 最新成果文：有练习取其 outcome，否则取触发器 outcome。
    return cls(Path(plan_path),root,cycle,target,learning,trigger,project,history,literals['notes'],
               latest.read_text(),configs['curriculum'].max_iters,
               configs['curriculum'].wall_clock_secs,{},'',projects=completed,project_records=records,
               completed_curriculum_turns=pending_index,pending_practice=pending,
               initial_evolution_memory=trigger_memory,
               curriculum_memory_access=plan.get('curriculum_memory_access', 'read_only'))
    # 构造恢复边界对象：课程剩余预算、已保存笔记/成果、待恢复练习与初始进化记忆。
