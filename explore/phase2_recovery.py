"""Strict admission for a stopped Phase-2 Curriculum search.

This deliberately supports only a completed first target-learning transaction
and consecutive completed practice transactions, followed by an unpublished
Curriculum turn. All role evidence comes from saved artifacts, never host prose.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from explore.charter import self_evolving_curriculum_charter
# Curriculum 章程（charter）生成器：用于逐字节复现当时给 Curriculum 的提示。
from explore.practice_loop import (
    CURRICULUM_HANDOFF, CURRICULUM_NOTES, PracticeInfrastructureError,
    _curriculum_runtime_contract, _manifest, _read_memory_tree,
)
# 练习主循环的交接路径、笔记路径与记忆树工具。
from explore.target_learning import _memory_tree_sha256
# 目标学习模块的记忆树哈希工具。


def require(ok, message):
    # 严格准入断言：不满足即拒绝恢复（fail-closed）。
    if not ok:
        raise PracticeInfrastructureError("Phase-2 resume refused: " + message)
        # 错误统一带 Phase-2 resume refused 前缀。


def read(path):
    # 读取并解析 JSON 文件的便捷函数。
    return json.loads(Path(path).read_text())


def sha(path):
    # 计算文件字节流的 SHA-256（用于校验归档输入未被改动）。
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclasses.dataclass
class Phase2CurriculumRecovery:
    # Phase-2 Curriculum 搜索停止后的严格恢复凭证：字段全部来自已保存工件，
    # 绝不信任何宿主 prose（角色证据一律以工件为准）。
    plan_path: Path
    root: Path
    cycle: Path
    target: str
    target_learning: dict
    trigger: dict
    project: dict
    history: list
    notes: str
    latest_outcome: str
    remaining_iters: int
    remaining_wall: float
    context_fields: dict
    recovery_observation: str
    # 计划路径、尝试根、进化周期目录、目标、目标学习结果、触发器、最后项目、
    # Curriculum 历史、私密笔记、最近结局、剩余迭代/墙钟预算、上下文字段修订、
    # 恢复观察。
    projects: int = 1
    project_records: list = dataclasses.field(default_factory=list)
    completed_curriculum_turns: int = 1
    pending_practice: object = None
    initial_evolution_memory: object = None
    curriculum_memory_access: str = 'read_only'
    ready_target_cycles: int = 0
    post_target_learning: bool = False
    pending_memory_learning: object = None
    # 可选/派生字段：项目数与记录、完成轮数、待处理练习、初始进化记忆、
    # Curriculum 记忆访问级别、就绪目标周期数、目标后学习标志、待处理记忆学习。

    @classmethod
    def admit(cls, plan_path, root, configs, initial_memory, target, stop_policy):
        # 准入入口：按恢复计划种类分派到专用恢复器，或执行本类的完整校验。
        plan_path, root = Path(plan_path), Path(root)
        plan = read(plan_path)
        # 读取恢复计划。
        if plan.get('recovery_kind') == 'verified_practice_memory_fs':
            from explore.practice_memory_recovery import admit_memory_recovery
            return admit_memory_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
            # 已验证练习的记忆文件系统恢复：分派。
        if plan.get('recovery_kind') == 'post_target_learning_boot_failure':
            from explore.post_target_learning_recovery import admit_post_target_learning
            return admit_post_target_learning(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
            # 目标学习后 VM 启动失败恢复：分派。
        if plan.get('recovery_kind') == 'ready_target_replacement':
            from explore.ready_target_recovery import admit_ready_target_recovery
            return admit_ready_target_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
            # 就绪目标替换恢复：分派。
        if plan.get('recovery_kind') == 'unverified_practice':
            from explore.practice_evidence_recovery import admit_practice_recovery
            return admit_practice_recovery(
                cls, plan_path, root, configs, initial_memory, target, stop_policy)
            # 未验证练习证据恢复：分派。
        count = plan.get('completed_projects', 1)
        require(type(count) is int and count >= 1, 'invalid completed project count')
        # 完成项目数必须为正整数。
        completed_turns = plan.get('completed_curriculum_turns', count)
        require(type(completed_turns) is int and completed_turns == count,
                'unsupported Curriculum/project boundary')
        # 本边界只支持“轮数==项目数”的已完成 Curriculum/项目边界。
        require(root.resolve() == Path(plan['attempt_root']).resolve(), 'root changed')
        # 尝试根目录不得改变。
        require(stop_policy == 'curriculum_review', 'stop policy changed')
        # 停止策略必须是 curriculum_review 且未改变。
        require(plan['status'] == 'admitted_for_isolated_recovery', 'plan is not admitted')
        # 计划状态必须已是“准入隔离恢复”。
        for path, expected in plan['input_sha256'].items():
            require(sha(path) == expected, 'an archived input changed: ' + path)
            # 每个归档输入的 SHA-256 都必须与计划记录一致。
        require(not (root / 'result.json').exists(), 'attempt already completed')
        # 尝试不得已经完成。
        require(sorted(p.name for p in (root / 'target_cycles').iterdir()) == ['cycle_001'],
                'target cycle count changed')
        # 目标周期必须恰好只有 cycle_001。
        require(sorted(p.name for p in (root / 'evolution_cycles').iterdir()) == ['cycle_001'],
                'evolution cycle count changed')
        # 进化周期必须恰好只有 cycle_001。
        cycle = root / 'evolution_cycles/cycle_001'
        require(sorted(p.name for p in (cycle / 'episodes').iterdir()) ==
                [f'ep{i:03d}' for i in range(1, count + 1)],
                'a project is open or a completed project is missing')
        # episode 目录必须恰为 ep001..ep{count}（无开着的、无缺失的项目）。
        require(sorted(p.name for p in (cycle / 'curriculum').iterdir()) ==
                [f'turn_{i:03d}' for i in range(1, completed_turns + 2)],
                'Curriculum turn count changed')
        # Curriculum 轮目录必须恰为 turn_001..turn_{completed_turns+1}（含未完成轮）。
        for parent in (root, cycle):
            require(not any((parent / name).exists() for name in (
                'result.json', 'curriculum_terminal.md', 'curriculum_ready_for_retry.md',
                'audit_rejects.jsonl')), 'terminal or rejected lineage')
            # 不得存在任何终局/重试就绪/被审计拒绝的标记文件。
        for path in root.rglob('boundary_audit.json'):
            record = read(path)
            require(record.get('status') != 'quarantined' and not record.get('hits'),
                    'quarantined transcript')
            # 所有边界审计都不得处于隔离状态或含命中。
        manifest = read(root / 'manifest.json')
        require(manifest['target_direction_sha256'] == hashlib.sha256(target.encode()).hexdigest(),
                'target direction changed')
        # 目标方向（研究目标）的哈希必须一致。
        require(manifest['initial_memory']['manifest'] == _manifest(initial_memory),
                'initial frozen memory changed')
        # 初始冻结记忆必须一致。
        for role, cfg in configs.items():
            saved = manifest['configs'][role]
            require(saved['sha256'] == sha(saved['path']), 'role config bytes changed')
            # 各角色配置文件的字节哈希必须一致。
            effective = json.loads(json.dumps(dataclasses.asdict(cfg)))
            require(saved['effective'] == effective, 'effective role config changed')
            # 各角色生效配置（dataclass→JSON 规整后）必须一致。
        learning = read(root / 'target_cycles/cycle_001/terminal_learning/outcome.json')
        # 读取目标学习的终局结果。
        projects = [read(cycle / f'episodes/ep{i:03d}/outcome.json')
                    for i in range(1, count + 1)]
        # 读取全部项目的结局记录。
        project = projects[-1]
        # 最后一个项目的记录。
        trigger = read(cycle / 'trigger/outcome.json')
        # 读取触发器（trigger）工件：把目标学习结论转交 Curriculum 的凭据。
        state = read(cycle / 'state.json')
        require(state['status'] == 'running' and state['projects'] == state['learning_experiences'] == count,
                'not a complete-project boundary')
        # 状态必须是 running 且项目数==学习经验数==count（完整项目边界）。
        require(learning['target_cycle'] == 1 and learning['target_verifier_verdict'] in ('PASS', 'FAIL'),
                'no grounded target learning')
        # 目标学习必须落在 cycle 1 且有 PASS/FAIL 的落判定（grounded）。
        require(all(p['project_index'] == i and p['terminal_outcome'] in ('PASS', 'FAIL')
                    for i, p in enumerate(projects, 1)),
                'no grounded practice learning')
        # 每个项目都必须索引正确且有落判定的终局。
        require(trigger['target'] == target and trigger['trigger_authority'] == 'phase2_outcome_protocol',
                'trigger identity changed')
        # 触发器身份：目标一致、权威为 phase2_outcome_protocol。
        require(trigger['verifier_report'] == learning['verifier_report']
                and trigger['actor_learning_diagnosis'] == learning['actor_learning_diagnosis']
                and trigger['verifier_outcome'] == learning['target_verifier_verdict'],
                'trigger and target learning disagree')
        # 触发器与目标学习三者（报告/诊断/结论）必须完全一致。
        require(learning['memory_before'] == _manifest(initial_memory), 'initial memory chain mismatch')
        # 记忆链起点：目标学习的 memory_before 必须等于初始记忆。
        active = _read_memory_tree(str(root / 'active_memory'))
        memory = _read_memory_tree(str(cycle / 'memory'))
        # 读取活动记忆树与周期内记忆树。
        require(learning['memory_after'] == trigger['memory_before'] == projects[0]['memory_before'] == _manifest(active),
                'target-to-practice memory chain mismatch')
        # 目标→练习的记忆链：目标学习后 == 触发器前 == 首项目前 == 活动记忆。
        require(all(projects[i]['memory_before'] == projects[i-1]['memory_after']
                    for i in range(1, count)), 'practice memory chain mismatch')
        # 练习记忆链逐环相连。
        require(project['memory_after'] == state['memory_manifest'] == _manifest(memory)
                and state['memory_tree_sha256'] == _memory_tree_sha256(memory),
                'practice memory transaction is incomplete')
        # 练习记忆事务必须完整提交（清单与树哈希双重一致）。
        events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
        require(events and events[-1]['event'] == 'EVOLUTION_PROJECT_CLOSED',
                'a project or learning phase is still open')
        # 最后一个事件必须是“项目关闭”（无开着的项目/学习阶段）。
        closed = events[-1]['payload']
        require(closed['state']['project_open'] is False
                and closed['state']['memory_phase_open'] is False
                and closed['payload']['project_index'] == count
                and closed['payload']['memory_tree_sha256'] == _memory_tree_sha256(memory),
                'last memory-close event disagrees with the committed tree')
        # 关闭事件的项目/记忆阶段标志为关、索引为 count、树哈希与已提交树一致。
        source = Path(plan['source_transcript'])
        turn = cycle / f'curriculum/turn_{completed_turns + 1:03d}'
        transcripts = sorted(turn.glob('segment_*/transcript.json'))
        require(transcripts and source == transcripts[-1], 'checkpoint is not the last completed segment')
        # 检查点必须是最后一轮最后一个完成分段。
        require(sha(source) == plan['source_transcript_sha256'], 'checkpoint changed')
        # 检查点字节未变。
        require(sha(plan['raw_checkpoint']) == sha(source), 'raw archive differs')
        # 原始归档与检查点一致。
        history = read(source)['messages']
        require(history and len(history) % 2 == 0 and all(
            m.get('role') == ('user' if i % 2 == 0 else 'assistant')
            for i, m in enumerate(history)), 'incomplete context pairs')
        # 历史必须是完整的 user/assistant 成对上下文。
        require(len(transcripts) >= 3 and all(not list(p.parent.glob('iter_*/trace.txt'))
                and not list(p.parent.glob('iter_*/look.json')) for p in transcripts[-3:]),
                'no confirmed actionless transport stop')
        # 最近 3 个分段都没有任何 iter 的 trace/look：确认是无动作传输停止。
        # Recover the exact private notes from the last opening charter. Compare
        # the complete regenerated charter, so delimiter text in role evidence
        # cannot substitute for a missing or different checkpoint.
        # （既有英文注释：从最后一条开场章程中恢复精确的私密笔记；比较完整再生的
        # 章程，使角色证据中的分隔符文本无法顶替缺失或不同的检查点。）
        latest = (cycle / f'episodes/ep{count:03d}/outcome.md').read_text()
        # 最近结局的渲染文本。
        summary = [f"Phase-2 target outcome {trigger['verifier_outcome']}: the same Actor Agent "
                   "committed grounded memory before Curriculum selected the next experience",
                   *[f"practice project {i}: " + p['terminal_outcome']
                     for i, p in enumerate(projects, 1)]]
        # 项目历史摘要：目标结局 + 各练习项目的终局。
        notes = None
        # 待恢复的 Curriculum 私密搜索笔记。
        for message in reversed(history):
            content = message.get('content', '')
            if message['role'] != 'user' or not isinstance(content, str):
                continue
                # 只考察 user 消息（章程注入在 user 侧）。
            marker = '\nYOUR PRIVATE SEARCH NOTES:\n---\n'
            if marker not in content:
                continue
                # 无私密笔记标记则跳过。
            candidate = content.rsplit(marker, 1)[1].split('\n---\n\nCURRENT ACTOR-OWNED DURABLE MEMORY', 1)[0]
            # 提取标记之后的候选笔记文本。
            prompt = self_evolving_curriculum_charter(
                target, project_history='\n'.join(summary), latest_outcome=latest,
                curriculum_notes=candidate, handoff_path=CURRICULUM_HANDOFF,
                notes_path=CURRICULUM_NOTES, phase2_outcome=True) + _curriculum_runtime_contract()
            # 用候选笔记重新生成完整章程提示。
            if prompt in content:
                notes = candidate
                break
                # 再生提示逐字节出现在原文中：候选即当时的精确笔记。
        require(notes is not None, 'could not reproduce the exact in-progress Curriculum charter')
        # 无法复现精确章程即拒绝恢复（防伪造/防漂移）。
        curriculum = configs['curriculum']
        used_iters = len(list(turn.glob('segment_*/iter_*/turn.txt')))
        # 统计未完成轮已消耗的迭代数（以 turn.txt 文件计数）。
        require(used_iters == plan['prior_iters'], 'prior iteration count changed')
        # 已耗迭代数必须与计划一致。
        wall = float(plan['prior_wall_secs_conservative'])
        require(wall > 0, 'prior work not charged')
        # 先前工作必须按保守墙钟计费（>0）。
        remaining_iters = curriculum.max_iters - used_iters
        remaining_wall = curriculum.wall_clock_secs - wall
        # 计算剩余迭代与墙钟预算。
        require(remaining_iters > 0 and remaining_wall > 0, 'original role budget exhausted')
        # 原角色预算必须仍有剩余。
        require(set(plan['context_fields']) <= {
            'history_keep_pairs', 'keep_chars', 'ctx_high_water', 'ctx_low_water',
            'fold_batch', 'worklog_max_chars'}, 'amendment changes non-context role settings')
        # 上下文修订只允许触及白名单中的上下文字段（不得改其他角色设置）。
        return cls(plan_path, root, cycle, target, learning, trigger, project,
                   history, notes, latest, remaining_iters, remaining_wall,
                   plan['context_fields'], plan['recovery_observation'],
                   projects=count, project_records=projects,
                   completed_curriculum_turns=completed_turns)
        # 构造恢复凭证实例。

    def validate_evolution(self, root, target, initial_memory, report, diagnosis, authority, outcome):
        # 恢复后的进化调用也必须与触发器证据逐项一致（防证据被换）。
        require(Path(root) == self.cycle and target == self.target, 'evolution identity changed')
        # 周期目录与目标身份不得改变。
        require(authority == 'phase2_outcome_protocol' and outcome == self.trigger['verifier_outcome'],
                'evolution authority changed')
        # 权威与结局必须与触发器一致。
        require(report == self.trigger['verifier_report'] and diagnosis == self.trigger['actor_learning_diagnosis'],
                'evolution evidence changed')
        # 报告与诊断证据不得改变。
        require(_manifest(initial_memory) == self.trigger['memory_before'], 'evolution input memory changed')
        # 进化的输入记忆必须与触发器记录一致。
        require(_manifest(_read_memory_tree(str(self.cycle / 'memory'))) == self.project['memory_after'],
                'committed practice memory changed')
        # 已提交的练习记忆不得被改动。

    def wrap_attempt(self, original):
        # 包装原始调用：仅对未完成轮的 Curriculum 调用应用上下文字段修订。
        if self.pending_memory_learning is not None:
            from explore.practice_memory_recovery import wrap_memory_attempt
            return wrap_memory_attempt(self, original)
            # 有待处理记忆学习时改走专用包装。
        def invoke(instruction, vm, cfg, sink, **kwargs):
            # 透明代理：命中未完成 Curriculum 轮且要求交接文件时，替换生效配置。
            if (Path(sink.root).is_relative_to(
                    self.cycle / f'curriculum/turn_{self.completed_curriculum_turns + 1:03d}')
                    and cfg.practice_done_requires == CURRICULUM_HANDOFF):
                cfg = dataclasses.replace(cfg, **self.context_fields)
                # 仅以不可变方式修订上下文字段（恢复计划校验过白名单）。
            return original(instruction, vm, cfg, sink, **kwargs)
            # 其余调用原样透传。
        return invoke
