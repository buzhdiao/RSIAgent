"""Resume first-practice learning after a disposable guest became read-only.

The saved terminal verdict and complete Actor history are mandatory. No Actor
work or Verifier inspection is repeated, and only normal learning may commit.
"""
# 在"一次性 guest 变为只读"之后恢复首次练习的学习阶段。
# 必须具备：已保存的终局判定与完整的 Actor 历史。不重跑任何 Actor 工作或
# Verifier 审查，且只允许正常学习阶段提交记忆。
from pathlib import Path
# Path：路径对象。
import ast
# ast 模块：只解析已保存 Agent 程序的字面常量，绝不执行它。
import dataclasses
# dataclasses：以 replace 派生收窄预算后的配置。
import hashlib
# hashlib：对报告与转录计算 SHA-256 指纹。
import json
# json：解析工件与清单。
import time
# time：单调时钟，用于恢复预算计时。

from explore import target_learning as V
# 目标学习模块（别名 V）：VM 编排、记忆树读取等工具。
from explore.practice_loop import _manifest, _atomic_json
# 练习主循环工具：记忆清单与原子写 JSON。
from explore.phase2_recovery import read, require, sha
# Phase-2 恢复的严格准入工具：读 JSON、fail-closed 断言、文件哈希。
from explore.practice_evidence_recovery import paired
# 角色历史"完整交替对"校验函数（与证据恢复共用）。


def _published_notes(program, project_text):
    """Read literal private notes; never execute a saved Agent program."""
    # 从已保存程序中读取字面私有笔记；绝不执行该程序。
    tree = ast.parse(program.read_text())
    # 仅做 AST 解析（不执行）。
    literals = {n.targets[0].id: n.value.value for n in tree.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    # 提取全部"单名赋字符串常量"的字面赋值。
    require(literals.get('handoff') == project_text and
            isinstance(literals.get('notes'), str), 'published notes are unavailable')
    # 严格断言：字面 handoff 与已发布项目文逐字节一致，且 notes 是字符串。
    expected = ast.dump(ast.parse(
        "with open('/home/user/curriculum_notes.md','w') as f:\n"
        "    f.write(notes)").body[0])
    # 期望的"with 打开文件并写入 notes"AST 形状。
    require(any(ast.dump(n) == expected for n in tree.body) and
            read(program.parent / 'trace_meta.json')['exit_code'] == 0,
            'private notes were not successfully published')
    # 严格断言：程序确实包含该直接写入，且运行成功退出（exit 0）。
    return literals['notes']
    # 返回已发布笔记的字面内容。


def admit_memory_recovery(cls, plan_path, root, configs, initial_memory, target, stop_policy):
    # 严格准入"首次练习记忆恢复"：逐一校验保存的边界未被篡改。
    from core.verifier import _parse_agentic_verifier_report
    # 延迟导入：报告解析器（允许 unverified 判定）。
    from explore.practice_loop import _parse_handoff, _UNIFIED_CURRICULUM_TOKEN
    # 延迟导入：交接文解析器与统一课程令牌。

    plan_path, root = Path(plan_path), Path(root)
    # 把计划路径与尝试根转为 Path。
    plan = read(plan_path)
    # 读取恢复计划 JSON。
    require(plan['status'] == 'admitted_for_isolated_recovery' and
            plan['recovery_kind'] == 'verified_practice_memory_fs', 'wrong recovery plan')
    # 严格断言：恢复计划确为"已验证练习记忆（文件系统只读失败）"类型。
    require(root.resolve() == Path(plan['attempt_root']).resolve() and
            stop_policy == 'curriculum_review', 'attempt or stop policy changed')
    # 严格断言：尝试根与停止策略未变。
    require(plan['completed_projects'] == 0 and plan['completed_curriculum_turns'] == 1,
            'only first-practice uncommitted learning is supported')
    # 严格断言：只支持"0 个完成练习 + 1 个课程轮"的首练未提交学习。
    for path, digest in plan['input_sha256'].items():
        # 逐一校验归档输入哈希。
        require(sha(path) == digest, 'archived input changed: ' + path)
        # 任何输入被改动都拒绝恢复。
    require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'] and
            sorted(p.name for p in (root / 'evolution_cycles').iterdir()) == ['cycle_001'],
            'target or evolution cycle changed')
    # 严格断言：目标周期与进化周期都只有一个 cycle_001。
    cycle = root / 'evolution_cycles/cycle_001'
    # 进化周期目录。
    episode = cycle / 'episodes/ep001'
    # 唯一的练习集目录 ep001。
    require(sorted(p.name for p in (cycle / 'episodes').iterdir()) == ['ep001'] and
            sorted(p.name for p in (cycle / 'curriculum').iterdir()) == ['turn_001'],
            'a later practice or Curriculum turn exists')
    # 严格断言：不存在更晚的练习或课程轮。
    for parent, names in [(root, ('result.json', 'memory_frozen')),
                          (cycle, ('result.json', 'curriculum_terminal.md', 'curriculum_ready_for_retry.md')),
                          (episode, ('outcome.json', 'memory_reconciliation', 'learning_diagnosis'))]:
        # 三层目录中都不允许出现"已推进学习边界"的工件。
        require(not any((parent / name).exists() for name in names), 'learning boundary advanced')
        # 严格断言：学习边界尚未推进（无终局工件）。
    require(not list(root.rglob('grading.json')) and not list(root.rglob('audit_rejects.jsonl')),
            'graded or rejected lineage cannot resume')
    # 严格断言：无评分工件、无被拒血统（二者均不可续跑）。
    for path in root.rglob('boundary_audit.json'):
        # 遍历边界审计文件。
        audit = read(path)
        # 读取审计记录。
        require(audit.get('status') != 'quarantined' and not audit.get('hits'), 'quarantined lineage')
        # 严格断言：血统未被隔离、无命中。
    manifest = read(root / 'manifest.json')
    # 读取尝试清单。
    protocol = manifest['protocol']
    # 清单中的协议段。
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest() and
            manifest['initial_memory']['manifest'] == _manifest(initial_memory), 'target/input changed')
    # 严格断言：目标方向与初始记忆未被改动。
    require(protocol['curriculum_memory_access'] == plan['curriculum_memory_access'] == 'none' and
            protocol['stop_policy'] == stop_policy and protocol['official_evaluator_calls'] == 0 and
            not protocol['official_evaluator_feedback_enters_learning'], 'protocol changed')
    # 严格断言：课程记忆访问为 none、停止策略一致、官方评估器零调用且其反馈不进学习。
    for role, cfg in configs.items():
        # 逐一校验各角色配置。
        saved = manifest['configs'][role]
        # 清单中保存的该角色配置条目。
        require(saved['sha256'] == sha(saved['path']) and
                saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))), 'role config changed')
        # 严格断言：配置文件哈希与生效配置均未变。
    learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
    # 读取目标学习终局记录。
    trigger = read(cycle / 'trigger/outcome.json')
    # 读取触发器记录。
    state = read(cycle / 'state.json')
    # 读取周期状态。
    memory = V._read_memory_tree(str(cycle / 'memory'))
    # 读取周期目录中已提交的记忆树。
    require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] == 'PASS' and
            learning['memory_before'] == _manifest(initial_memory), 'target learning changed')
    # 严格断言：目标学习是第 1 轮、判定 PASS、起始记忆为初始记忆。
    require(learning['memory_after'] == trigger['memory_before'] == state['memory_manifest'] ==
            _manifest(memory) == _manifest(V._read_memory_tree(str(root / 'active_memory'))),
            'last committed memory changed')
    # 严格断言：目标学习后记忆、触发器前记忆、状态清单、周期记忆与 active_memory 五处一致。
    require(state['status'] == 'infra' and state['projects'] == state['learning_experiences'] == 1 and
            state['reason'] == 'ACTOR phase ended at emergency/infrastructure status infra' and
            state['memory_tree_sha256'] == V._memory_tree_sha256(memory), 'not the failed memory boundary')
    # 严格断言：确为记录在案的"记忆边界基础设施失败"状态。
    require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol' and
            trigger['verifier_outcome'] == learning['target_verifier_verdict'] and
            trigger['verifier_report'] == learning['verifier_report'] and
            trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'], 'trigger changed')
    # 严格断言：触发器的目标、权威、结果、报告与诊断均未被改动。
    verdict = read(episode / 'verifier/iter_01/verify2.json')
    # 读取唯一一轮 Verifier 判定。
    require(verdict['verdict'] in ('pass', 'wrong') and
            _parse_agentic_verifier_report(verdict['findings'], allow_unverified=True) == verdict['verdict'] and
            (episode / 'verifier_report.md').read_text() == verdict['findings'], 'no grounded terminal verdict')
    # 严格断言：判定为二值、报告解析自洽，且落盘报告与工件一致。
    terminal = 'PASS' if verdict['verdict'] == 'pass' else 'FAIL'
    # 把小写判定映射为终局 PASS/FAIL。
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    # 解析全部事件流。
    require(events[-1]['event'] == 'EVOLUTION_MEMORY_PHASE_STARTED' and
            events[-1]['payload']['payload']['experience_index'] == 1 and
            events[-1]['payload']['payload']['terminal_outcome'] == terminal and
            events[-1]['payload']['payload']['verifier_report_sha256'] ==
            hashlib.sha256(verdict['findings'].encode()).hexdigest() and
            events[-1]['payload']['state']['memory_phase_open'] is True, 'memory start event changed')
    # 严格断言：最后事件确是"记忆阶段开始"（经验序号、终局、报告哈希、记忆阶段开启均吻合）。
    actor_source = episode / 'actor/attempt_001/segment_000/transcript.json'
    # Actor 转录来源。
    memory_source = episode / 'memory_distillation/segment_000/transcript.json'
    # 失败的记忆蒸馏转录来源。
    actor, failed = read(actor_source), read(memory_source)
    # 读取两条转录。
    require(read(actor_source.parent / 'result.json')['status'] == 'done' and
            paired(actor['messages']) and paired(failed['messages']) and
            actor['system'] == failed['system'] and
            failed['messages'][:len(actor['messages'])] == actor['messages'], 'same-Actor history lost')
    # 严格断言：Actor 轮已正常完成，且蒸馏对话延续同一 Actor 的完整历史（同系统提示、前缀一致）。
    require(sorted(p.name for p in memory_source.parent.parent.iterdir()) == ['segment_000'],
            'memory recovery already attempted')
    # 严格断言：记忆蒸馏只有一个 segment（此前未尝试过恢复）。
    traces = sorted(memory_source.parent.glob('iter_*/trace_meta.json'))
    # 收集蒸馏各迭代的轨迹元数据。
    require(len(traces) >= 3 and all(read(p)['infra_fail'] and
            'Read-only file system' in p.with_name('trace.txt').read_text()
            for p in traces[-3:]), 'read-only staging failure not established')
    # 严格断言：最近至少 3 次迭代均因"文件系统只读"基础设施失败（只读边界成立）。
    require('3 consecutive run-wrapper failures' in Path(plan['failed_runtime_log']).read_text(),
            'missing failed memory execution guard')
    # 严格断言：失败运行日志中确有"连续 3 次 run-wrapper 失败"的守卫记录。
    budget = plan['prior_memory_budget']
    # 计划中记录的记忆阶段已消耗预算。
    used_iters = len(list(memory_source.parent.glob('iter_*/turn.txt')))
    # 从工件统计实际用掉的迭代数。
    require(used_iters == budget['iterations'] and used_iters > 0 and
            0 < budget['wall_secs_conservative'] < configs['memory_actor'].wall_clock_secs and
            used_iters < configs['memory_actor'].max_iters, 'remaining memory budget invalid')
    # 严格断言：迭代数吻合且严格为正，剩余墙钟/迭代预算有效。
    require(plan['partial_guest_memory_written'] is False, 'partial guest memory needs its own recovery')
    # 严格断言：guest 中没有写入过部分记忆（那种情况需要专门的恢复）。
    curriculum_source = cycle / 'curriculum/turn_001/segment_000/transcript.json'
    # 课程转录来源。
    history = read(curriculum_source)['messages']
    # 读取课程对话历史。
    require(paired(history), 'incomplete Curriculum history')
    # 严格断言：课程历史是完整交替对。
    project_text = (episode / 'project.md').read_text()
    # 读取已发布的练习项目文。
    notes = _published_notes(Path(plan['curriculum_publication_program']), project_text)
    # 从已保存程序的字面常量中恢复私有笔记。
    project = _parse_handoff(project_text, _UNIFIED_CURRICULUM_TOKEN)
    # 解析项目文体得到保存的练习。
    require(project is not None and project.token == 'PROJECT', 'invalid saved practice')
    # 严格断言：保存练习有效且令牌为 PROJECT。
    candidate = episode / 'candidate/publication_001'
    # 候选目录。
    require(sorted(p.name for p in candidate.parent.iterdir()) == ['publication_001'] and
            all((folder / 'materials.tgz').is_file() for folder in [candidate, episode / 'fixtures']),
            'candidate or fixture snapshot is missing')
    # 严格断言：候选只有一代，且候选与夹具的 materials.tgz 快照都在。
    pending = dict(resume_kind='verified_memory', project=project.body,
                   actor_history=failed['messages'], before_memory=memory,
                   terminal_outcome=terminal, verifier_report=verdict['findings'],
                   actor_handoff=(episode / 'actor_handoff.md').read_text(), candidate_dir=str(candidate))
    # 组装待恢复学习状态：类型、项目体、完整 Actor 历史、起始记忆、终局判定、报告与候选目录。
    metadata = dict(episode=str(episode), prior_iters=used_iters,
                    prior_wall_secs=budget['wall_secs_conservative'],
                    source_transcript=str(memory_source), source_sha256=sha(memory_source),
                    source_history=failed['messages'], start_monotonic=time.monotonic())
    # 组装恢复元数据：来源转录及其哈希、已耗预算、恢复起点时钟。
    return cls(plan_path, root, cycle, target, learning, trigger, learning,
               history, notes, (cycle / 'trigger/outcome.md').read_text(),
               configs['curriculum'].max_iters, configs['curriculum'].wall_clock_secs, {}, '',
               projects=0, project_records=[], completed_curriculum_turns=1,
               pending_practice=pending, initial_evolution_memory=memory,
               curriculum_memory_access='none', pending_memory_learning=metadata)
    # 构造恢复边界对象：课程剩余预算、笔记、成果文、待恢复学习及其元数据（记忆访问=none）。


def restore_learning_candidate(*, hooks, vm, episode_dir, fixture_dir, memory_dir,
                               target, emit, saved):
    """Restore archived inputs for learning; do not rerun Actor or Verifier work."""
    # 只为学习阶段恢复归档输入；不重跑 Actor 或 Verifier 的任何工作。
    require(saved['resume_kind'] == 'verified_memory', 'wrong pending memory state')
    # 严格断言：恢复类型确为"已验证记忆"。
    require(V._read_memory_tree(str(memory_dir)) == saved['before_memory'], 'canonical memory changed')
    # 严格断言：规范记忆与保存的起始记忆逐字节一致。
    V._fresh_vm(hooks, vm, target)
    # 准备全新 VM。
    V._replay(hooks, vm, str(fixture_dir))
    # 回放原始输入夹具。
    V._replay(hooks, vm, saved['candidate_dir'])
    # 回放候选项目材料（学习对象）。
    V._push_canonical_memory(hooks, vm, str(memory_dir))
    # 推送规范记忆进 VM。
    emit('PRACTICE_MEMORY_CANDIDATE_RESTORED', status='completed', project_open=True,
         memory_phase_open=True, payload=dict(project_index=1, actor_replayed=False,
             verifier_replayed=False, memory_committed=False))
    # 发出"候选已恢复"事件：明确 Actor/Verifier 未重放、记忆尚未提交。
    return {key: saved[key] for key in ('terminal_outcome', 'verifier_report', 'actor_handoff',
                                      'actor_history', 'before_memory')}
    # 以保存的数据返回结果，供学习阶段直接使用。


def wrap_memory_attempt(recovery, original):
    """Continue full saved context and charge failed distillation work."""
    # 包装记忆蒸馏调用：延续完整已存上下文，并对失败的蒸馏工作计费。
    meta = recovery.pending_memory_learning
    # 取出恢复元数据。
    used_iters = meta['prior_iters']
    # 已消耗的迭代数（含失败尝试）。
    first = True
    # 标记是否为恢复后的首次蒸馏调用。

    def invoke(instruction, vm, cfg, sink, **kwargs):
        # 内层包装：拦截记忆蒸馏目录下的调用。
        nonlocal used_iters, first
        # 修改外层计数与首调标志。
        if not Path(sink.root).is_relative_to(Path(meta['episode']) / 'memory_distillation'):
            return original(instruction, vm, cfg, sink, **kwargs)
        # 不属于记忆蒸馏的调用原样放行。
        if first:
            # 首次恢复调用：校验并注入续跑说明。
            require(kwargs.get('initial_history') == meta['source_history'], 'recovery dropped Actor context')
            # 严格断言：初始历史确为失败前的完整 Actor 上下文。
            require(sha(meta['source_transcript']) == meta['source_sha256'], 'failed transcript changed')
            # 严格断言：失败转录未被改动。
            instruction += ('\n\nINFRASTRUCTURE CONTINUATION: The disposable learning VM became '
                            'read-only before memory distillation completed. This fresh VM has the '
                            'same saved candidate and prior committed memory. Your full work and '
                            'failed learning history are retained. Previous private scratch files '
                            'outside that candidate are absent. Continue this unfinished learning phase.\n')
            # 在指令后追加基础设施续跑说明（不重置任何已完成工作）。
            first = False
            # 后续调用不再注入。
        remaining_wall = cfg.wall_clock_secs - meta['prior_wall_secs'] - (
            time.monotonic() - meta['start_monotonic'])
        # 计算扣除历史消耗与恢复过程耗时后的剩余墙钟。
        require(remaining_wall > 0 and used_iters < cfg.max_iters, 'original memory budget exhausted')
        # 严格断言：原记忆预算仍有剩余。
        limited = dataclasses.replace(cfg, max_iters=cfg.max_iters - used_iters,
                                      wall_clock_secs=remaining_wall)
        # 派生收窄预算后的配置。
        result, history = original(instruction, vm, limited, sink, **kwargs)
        # 调用原蒸馏函数。
        used_iters += max(len(list(Path(sink.root).glob('iter_*/turn.txt'))),
                          int(getattr(result, 'turns', 0) or getattr(result, 'iters', 0)))
        # 计费：取新增 turn 数与结果报告轮数的较大者。
        _atomic_json(Path(meta['episode']) / 'memory_recovery_receipt.json', dict(
            recovery_plan=str(recovery.plan_path), original_history_messages=len(meta['source_history']),
            prior_memory_iters=meta['prior_iters'], total_memory_iters=used_iters,
            prior_wall_secs=meta['prior_wall_secs'], remaining_wall_at_call=remaining_wall,
            resumed_sink=str(sink.root), status=str(getattr(result, 'status', 'infra')),
            actor_work_replayed=False, verifier_replayed=False, official_evaluator_calls=0))
        # 原子落盘恢复回执：完整审计字段（未重放 Actor/Verifier、官方评估零调用）。
        return result, history
        # 返回原样结构的结果。
    return invoke
    # 返回包装后的调用函数。
