"""Admit an explicitly authorized fresh target after a saved READY boundary.

The previous target must have stopped without verdict or learning. This is a
new target execution with retained committed experience, never a fabricated
continuation of its lost Actor/Verifier conversation.
"""
# 在已保存的 READY 边界之后，严格准入一个被明确授权的全新目标（fresh target）。
# 前一个目标必须是在"无判定、无学习"的状态下停止的。这是保留已提交经验之后的
# 一次新目标执行，绝不伪造对丢失 Actor/Verifier 对话的"续篇"。
from __future__ import annotations

import ast
# ast 模块：只解析已保存程序的字面常量，绝不执行它。
import dataclasses
# dataclasses：用于恢复对象构造时的配置等价比较。
import hashlib
# hashlib：目标、笔记与决策文本的 SHA-256 指纹。
import json
# json：解析清单与事件流。
from pathlib import Path
# Path：路径对象。

from explore.practice_loop import _manifest, _read_memory_tree
# 练习主循环工具：记忆清单与记忆树读取。
from explore.target_learning import _memory_tree_sha256
# 目标学习模块：记忆树整体哈希。
from explore.phase2_recovery import read, require, sha
# Phase-2 恢复的严格准入工具：读 JSON、fail-closed 断言、文件哈希。


def recover_appended_notes(program_path, trace_path):
    """Read a literal from saved source; never execute agent-authored code."""
    # 从已保存源码中读取字面常量；绝不执行 Agent 编写的代码。
    tree = ast.parse(Path(program_path).read_text())
    # 仅做 AST 解析（不执行）。
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and any(
                  isinstance(t, ast.Name) and t.id == 'notes_append'
                  for t in node.targets)]
    # 提取全部对 notes_append 的字面赋值（安全求值常量）。
    require(len(values) == 1 and isinstance(values[0], str),
            'ambiguous saved private notes')
    # 严格断言：恰好一次赋值且为字符串（否则视为歧义，拒绝恢复）。
    notes = values[0]
    # 取出笔记字面内容。
    trace = Path(trace_path).read_text()
    # 读取该程序运行轨迹。
    require(trace == 'first line: DECISION: READY_FOR_TARGET\n'
            'handoff bytes: 27\n'
            f'notes appended; size: {len(notes.encode())}\n[exit 0]',
            'saved successful write does not prove complete notes')
    # 严格断言：轨迹逐字节证明"READY 决策已发布、笔记按完整字节数追加且成功退出"。
    return notes
    # 返回恢复出的私有笔记。


def admit_ready_target_recovery(cls, plan_path, root, configs, initial_memory,
                               target, stop_policy):
    # 严格准入"READY 后的新目标恢复"：逐项校验保存的边界与血统未被篡改。
    plan = read(plan_path)
    # 读取恢复计划 JSON。
    source = Path(plan['source_attempt_root'])
    # 原始尝试根目录（被替换方）。
    count = plan['completed_projects']
    # 计划中记录的已完成练习数。
    require(plan['status'] == 'admitted_for_isolated_recovery'
            and plan['fresh_target_authorized'] is True,
            'fresh target execution not authorized')
    # 严格断言：恢复状态合法且"新目标执行"被明确授权。
    require(type(count) is int and count >= 1, 'invalid completed projects')
    # 严格断言：已完成练习数是正整数。
    require(root.resolve() == Path(plan['attempt_root']).resolve()
            and root.resolve() != source.resolve(), 'replacement needs a new root')
    # 严格断言：替换必须在新的尝试根上进行（与原根不同）。
    require(stop_policy == plan['phase2_stop_policy'] == 'curriculum_review',
            'stop policy changed')
    # 严格断言：停止策略与计划一致且为 curriculum_review。
    require(plan['curriculum_memory_access'] == 'none'
            and plan['phase3_auto_launch'] is False
            and plan['automatic_actor_retry'] is False,
            'comparison scope changed')
    # 严格断言：课程记忆访问为 none、不自动启动 Phase-3、不自动重试 Actor（比较范围未变）。
    for name, expected in plan['input_sha256'].items():
        # 逐一校验已保存输入的哈希。
        require(sha(name) == expected, 'saved input changed: ' + name)
        # 任何输入被改动都拒绝恢复。
    for name, expected in plan['clone_sha256'].items():
        # 逐一校验克隆检查点（新根中的副本）的哈希。
        require(sha(root / name) == expected, 'replacement checkpoint changed: ' + name)
        # 检查点被改动则拒绝。
    require(plan['completed_curriculum_turns'] == count + 1,
            'missing READY Curriculum turn')
    # 严格断言：课程轮数恰为 count+1（含最后的 READY 轮）。
    for attempt in (source, root):
        # 对原根与新根同时校验。
        require(not (attempt / 'result.json').exists(), 'attempt already completed')
        # 严格断言：两个尝试都尚未完成。
        require(sorted(p.name for p in (attempt / 'target_cycles').iterdir())
                == ['cycle_001', 'cycle_002'], 'target execution count changed')
        # 严格断言：目标周期恰为两个（第二个是失败的目标执行）。
        require(sorted(p.name for p in (attempt / 'evolution_cycles').iterdir())
                == ['cycle_001'], 'evolution count changed')
        # 严格断言：进化周期恰为一个。
        failed = attempt / 'target_cycles/cycle_002'
        # 失败的目标周期目录。
        require(not (failed / 'terminal_learning').exists()
                and not (failed / 'result.json').exists(),
                'previous target already has a terminal transaction')
        # 严格断言：前一个目标确实"无终局事务"（无终局学习、无结果文件）。
        for audit in attempt.rglob('boundary_audit.json'):
            # 遍历边界审计文件。
            record = read(audit)
            # 读取审计记录。
            require(record.get('status') != 'quarantined' and not record.get('hits'),
                    'quarantined attempt cannot be resumed')
            # 严格断言：未被隔离、无命中。
        require(not list(attempt.rglob('audit_rejects.jsonl')),
                'rejected boundary requires separate review')
        # 严格断言：没有被拒绝的边界（否则需另行审查）。
    manifest = read(root / 'manifest.json')
    # 读取新根的清单。
    original_manifest = read(source / 'manifest.json')
    # 读取原根的清单。
    require(manifest['protocol_run'] == plan['protocol_run']
            and original_manifest['protocol_run'] == plan['source_protocol_run'],
            'lineage identity changed')
    # 严格断言：两个血统的 protocol_run 标识均与计划一致。
    expected_manifest = dict(original_manifest, protocol_run=plan['protocol_run'])
    # 期望清单：原清单仅替换 protocol_run。
    expected_manifest['infrastructure_replacement'] = {
        'source_attempt_root': str(source), 'source_manifest_sha256': sha(source / 'manifest.json'),
        'plan': str(plan_path), 'fresh_target_execution': True}
    # 期望清单必须额外记录"基础设施替换"段（来源根、来源清单哈希、计划、新目标标志）。
    require(manifest == expected_manifest, 'replacement manifest changed')
    # 严格断言：新根清单与期望完全一致（替换过程未被篡改）。
    require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest()
            and manifest['initial_memory']['manifest'] == _manifest(initial_memory),
            'original target or Phase1 memory changed')
    # 严格断言：原目标方向与 Phase-1 初始记忆未变。
    for role, cfg in configs.items():
        # 逐一校验各角色配置。
        saved = manifest['configs'][role]
        # 清单中保存的该角色配置条目。
        require(saved['sha256'] == sha(saved['path'])
                and saved['effective'] == json.loads(json.dumps(dataclasses.asdict(cfg))),
                'effective role configuration changed: ' + role)
        # 严格断言：配置文件哈希与生效配置均未变。
    cycle = root / 'evolution_cycles/cycle_001'
    # 进化周期目录。
    require(sorted(p.name for p in (cycle / 'episodes').iterdir()) ==
            [f'ep{i:03d}' for i in range(1, count + 1)], 'an unfinished practice exists')
    # 严格断言：练习集恰好 count 个（不存在未完成的练习）。
    require(sorted(p.name for p in (cycle / 'curriculum').iterdir()) ==
            [f'turn_{i:03d}' for i in range(1, count + 2)], 'Curriculum context count changed')
    # 严格断言：课程轮恰好 count+1 个（上下文未变）。
    state = read(cycle / 'state.json')
    # 读取周期状态。
    decision = (cycle / 'curriculum_ready_for_retry.md').read_text()
    # 读取已发布的 READY 决策文。
    require(state['status'] == 'ready_for_retry'
            and state['projects'] == state['learning_experiences'] == count
            and state['terminal_text'] == decision == 'DECISION: READY_FOR_TARGET\n',
            'no published semantic READY boundary')
    # 严格断言：状态为 ready_for_retry，且"语义 READY 边界"已发布（决策文逐字节一致）。
    learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
    # 读取首次目标学习的终局记录。
    trigger = read(cycle / 'trigger/outcome.json')
    # 读取触发器记录。
    records = [read(cycle / f'episodes/ep{i:03d}/outcome.json')
               for i in range(1, count + 1)]
    # 读取全部已完成练习的结果记录。
    require(learning['target_verifier_verdict'] in ('PASS', 'FAIL')
            and learning['target_cycle'] == 1
            and learning['memory_before'] == _manifest(initial_memory),
            'first target learning is not grounded')
    # 严格断言：首次目标学习有落地的 PASS/FAIL 判定且起始记忆正确。
    require(trigger['target'] == target
            and trigger['trigger_authority'] == 'phase2_outcome_protocol'
            and trigger['verifier_outcome'] == learning['target_verifier_verdict']
            and trigger['verifier_report'] == learning['verifier_report']
            and trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis'],
            'target trigger changed')
    # 严格断言：触发器的目标、权威、结果、报告与诊断均未被改动。
    before = learning['memory_after']
    # 记忆链游标：从目标学习后的记忆开始。
    require(before == trigger['memory_before'], 'target-to-practice chain broken')
    # 严格断言：目标学习与练习之间的记忆链未断。
    for i, record in enumerate(records, 1):
        # 逐个校验练习事务的完整性与串行性。
        require(record['project_index'] == i
                and record['terminal_outcome'] in ('PASS', 'FAIL')
                and record['memory_before'] == before,
                'practice transaction is not complete or serial')
        # 严格断言：序号连续、有二值终局、且前递记忆链一致。
        before = record['memory_after']
        # 游标推进到该练习之后。
    memory = _read_memory_tree(str(cycle / 'memory'))
    # 读取周期目录中已提交记忆树。
    active = _read_memory_tree(str(root / 'active_memory'))
    # 读取活动记忆树。
    require(before == state['memory_manifest'] == _manifest(memory) == _manifest(active)
            and state['memory_tree_sha256'] == _memory_tree_sha256(memory)
            == plan['committed_memory_tree_sha256'], 'committed memory changed')
    # 严格断言：记忆链终点、状态清单、周期记忆、活动记忆与计划中的树哈希全部一致。
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
    # 解析全部事件流。
    ready = [e['payload'] for e in events if e['event'] == 'EVOLUTION_READY_FOR_TARGET']
    # 收集 READY_FOR_TARGET 事件。
    require(len(ready) == 1 and ready[0]['payload']['projects'] == count
            and ready[0]['payload']['memory_tree_sha256'] == _memory_tree_sha256(memory)
            and ready[0]['payload']['curriculum_decision_sha256'] == hashlib.sha256(decision.encode()).hexdigest()
            and not any(ready[0]['state'].values()), 'READY publication barrier changed')
    # 严格断言：READY 事件恰好一次，且其项目数、记忆树哈希、决策哈希与"全关闭"状态均吻合。
    require(not any(e['event'] in ('TARGET_VERIFIED', 'TARGET_LEARNED')
                    and e['payload'].get('target_cycle') == 2 for e in events),
            'failed target already produced terminal evidence')
    # 严格断言：失败的目标确未产生任何终局证据事件。
    turn = cycle / f'curriculum/turn_{count + 1:03d}'
    # 最后一个课程轮（READY 轮）目录。
    require(sorted(p.name for p in turn.iterdir() if p.is_dir()) == ['segment_000'],
            'READY checkpoint segment changed')
    # 严格断言：该轮只有一个 segment_000 检查点。
    history = read(turn / 'segment_000/transcript.json')['messages']
    # 读取 READY 轮的课程对话历史。
    require(len(history) == plan['curriculum_messages'] and len(history) % 2 == 0
            and all(m.get('role') == ('user' if i % 2 == 0 else 'assistant')
                    for i, m in enumerate(history)), 'saved Curriculum pairs incomplete')
    # 严格断言：历史长度与计划一致且为完整 user/assistant 交替对。
    notes = recover_appended_notes(turn / 'segment_000/iter_02/program.py',
                                  turn / 'segment_000/iter_02/trace.txt')
    # 从已保存程序的字面常量与运行轨迹中恢复追加的私有笔记。
    require(hashlib.sha256(notes.encode()).hexdigest() == plan['curriculum_notes_sha256'],
            'saved private notes changed')
    # 严格断言：恢复笔记的哈希与计划记录一致。
    return cls(Path(plan_path), root, cycle, target, learning, trigger, records[-1],
               history, notes, (cycle / f'episodes/ep{count:03d}/outcome.md').read_text(),
               configs['curriculum'].max_iters, configs['curriculum'].wall_clock_secs,
               {}, '', projects=count, project_records=records,
               completed_curriculum_turns=count + 1, curriculum_memory_access='none',
               ready_target_cycles=2)
    # 构造恢复边界对象：完整课程历史、笔记、成果文、剩余预算，并标记"第 2 轮目标周期"。
