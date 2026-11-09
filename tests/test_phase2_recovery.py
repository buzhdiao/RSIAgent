# 【中文注释·模块定位】Phase-2 恢复（explore/phase2_recovery.py）的安全回归测试：
# 一旦 Phase-2 Curriculum 搜索在半途中停止，恢复流程必须满足三条铁律——
# 1) 不重放已完成的工作：已完成的练习项目、已提交的记忆、已完成的目标学习事务
#    一律原样保留，恢复只从"未发布的 Curriculum 回合"继续；
# 2) 传输失败绝不能冒充语义成功：连续无进展（actionless）的传输停止只能报
#    PracticeInfrastructureError，永远不会伪造一个交接令牌；
# 3) 恢复不得扩大权限：上下文修订只允许白名单字段（迭代/墙钟预算、history_keep_pairs），
#    改模型名等"非上下文"字段直接拒绝；准入校验失败时磁盘零写入（fail-closed）。
# fixture 手工搭建一个"第一次目标学习 PASS + 一次练习项目完成 + Curriculum 第二轮
# 未发布"的真实磁盘现场，全部证据来自保存的工件而非宿主 prose。
"""Recovery must not replay completed work or turn transport failure into success."""
# 模块 docstring：恢复不得重放已完成工作，也不得把传输失败变成成功。

import dataclasses
# 导入 dataclasses：不可变派生配置（replace）与 asdict 序列化。
import hashlib
# 导入 hashlib：目标方向的 sha256 指纹。
import json
# 导入 json：构造/解析持久化工件。
from pathlib import Path
# 导入 Path：路径对象处理。
from types import SimpleNamespace
# 导入 SimpleNamespace：打包 fixture 返回的现场对象。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import load
# 导入配置加载器：load(None) 取默认角色配置。
from core.self_evolving_loop import (
    SelfEvolvingStart, TargetVerdict, run_self_evolving_loop,
)
# 导入目标自进化主循环：恢复边界计数器、目标判定与主循环入口。
from explore import practice_loop as E, unified_evolution as U
# 导入练习主循环 E 与统一进化 U：两者的低层工具与主流程都被本文件复用。
from explore.phase2_recovery import Phase2CurriculumRecovery, sha
# 导入被测恢复类与文件 sha256 工具。
from explore.target_learning import _memory_tree_sha256
# 导入目标学习的记忆树哈希（state.json 里登记的一致性凭据）。
from test_self_evolving_loop import _hooks as target_hooks
# 复用目标循环测试的钩子工厂：伪造目标 Verifier 判定序列。
from test_unified_evolution import _VM, _hooks
# 复用统一进化测试的伪 VM 与钩子工厂：不起真实 VM/LLM。


def save(path, obj):
    # 测试工具：原子式落盘 JSON（先建父目录），模拟真实工件写入。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保父目录存在。
    path.write_text(json.dumps(obj))
    # 序列化写入。


def fixture(tmp_path):
    # 核心现场搭建：完整复刻"可被恢复的 Phase-2 中断现场"，返回根/周期/记忆树/计划。
    root = tmp_path / 'attempt'
    # 尝试根目录（一次 Phase-2 尝试的全部产物）。
    cycle = root / 'evolution_cycles/cycle_001'
    # 第一个进化周期目录（已完成）。
    target = 'Update the supplied document.'
    # 目标方向原文。
    initial, active, memory = ({'lesson.md': value} for value in (b'initial', b'target', b'practice'))
    # 三棵记忆树：initial=初始记忆，active=目标学习后提交的活跃记忆，
    # memory=练习项目后提交的实践记忆（恢复时磁盘上应有的最终态）。
    for path, tree in ((root / 'active_memory', active), (cycle / 'memory', memory)):
        E._atomic_install_memory(str(path), tree)
    # 用真实安装函数落两棵树：active_memory 在尝试根，memory 在周期目录。
    cfg = load(None)
    # 当前默认 Curriculum 配置（恢复后必须与登记一致）。
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('model: original-model\n')
    # 写一份配置文件副本，供 manifest 登记其 sha256。
    manifest = {
        'target_direction_sha256': hashlib.sha256(target.encode()).hexdigest(),
        'initial_memory': {'manifest': E._manifest(initial)},
        'configs': {'curriculum': {'path': str(config_path), 'sha256': sha(config_path),
                                  'effective': dataclasses.asdict(cfg)}},
    }
    # 尝试清单：目标方向指纹、初始记忆 manifest、Curriculum 配置（路径/sha/生效值）。
    save(root / 'manifest.json', manifest)
    # 落盘清单。
    learning = {'target_cycle': 1, 'target_verifier_verdict': 'PASS',
                'verifier_report': 'VERDICT: PASS\nrole evidence',
                'actor_learning_diagnosis': 'same Actor diagnosis',
                'memory_before': E._manifest(initial), 'memory_after': E._manifest(active)}
    # 已完成的目标学习事务：周期 1、PASS 判定、报告原文、Actor 诊断、记忆前后 manifest。
    save(root / 'target_cycles/cycle_001/terminal_learning/outcome.json', learning)
    # 目标学习结果工件。
    trigger = {'target': target, 'trigger_authority': 'phase2_outcome_protocol',
               'verifier_report': learning['verifier_report'], 'verifier_outcome': 'PASS',
               'actor_learning_diagnosis': learning['actor_learning_diagnosis'],
               'memory_before': E._manifest(active)}
    # 触发器工件：由 Phase-2 结果协议授权，携带判定证据与记忆前态。
    save(cycle / 'trigger/outcome.json', trigger)
    # 落盘触发器。
    project = {'project_index': 1, 'terminal_outcome': 'PASS',
               'memory_before': E._manifest(active), 'memory_after': E._manifest(memory)}
    # 已完成的练习项目：索引 1、PASS、记忆从 active 推进到 memory。
    save(cycle / 'episodes/ep001/outcome.json', project)
    # 练习项目结果工件。
    latest = 'The completed practice outcome, verbatim.'
    (cycle / 'episodes/ep001/outcome.md').write_text(latest)
    # 最新成果原文（进入 Curriculum 提示的 latest_outcome）。
    save(cycle / 'state.json', {'status': 'running', 'projects': 1, 'learning_experiences': 1,
                               'memory_manifest': E._manifest(memory),
                               'memory_tree_sha256': _memory_tree_sha256(memory)})
    # 周期状态：running（还有未完成轮）、计数器与记忆树一致性凭据。
    event = {'event': 'EVOLUTION_PROJECT_CLOSED', 'payload': {
        'state': {'project_open': False, 'memory_phase_open': False},
        'payload': {'project_index': 1, 'memory_tree_sha256': _memory_tree_sha256(memory)}}}
    (root / 'events.jsonl').write_text(json.dumps(event) + '\n')
    # 事件流末尾必须是"项目关闭"：证明没有悬空的开放项目（恢复准入前提）。
    summary = ('Phase-2 target outcome PASS: the same Actor Agent committed grounded memory '
               'before Curriculum selected the next experience\npractice project 1: PASS')
    # 项目历史摘要（进入 Curriculum 章程提示）。
    prompt = U.self_evolving_curriculum_charter(
        target, project_history=summary, latest_outcome=latest, curriculum_notes='private notes',
        handoff_path=E.CURRICULUM_HANDOFF, notes_path=E.CURRICULUM_NOTES,
        phase2_outcome=True) + E._curriculum_runtime_contract()
    # 用与真实运行完全相同的生成器重建当时的 Curriculum 提示（章程+运行时契约）。
    history = [{'role': 'user', 'content': 'NEXT PHASE:\n' + prompt},
               {'role': 'assistant', 'content': 'actionless narration'}]
    # 第一轮对话历史：提示 + 一条无行动的旁白（未发布任何令牌）。
    (cycle / 'curriculum/turn_001').mkdir(parents=True)
    # 第一轮目录（已完成）。
    for n in range(3):
        source = cycle / f'curriculum/turn_002/segment_{n:03d}/transcript.json'
        save(source, {'messages': history})
        (source.parent / 'iter_01').mkdir()
        (source.parent / 'iter_01/turn.txt').write_text('actionless narration')
    # 第二轮（未完成轮）有三个传输段，每段都有转写与一次迭代记录：
    # 恢复必须继承这 3 次迭代的预算消耗。
    raw = tmp_path / 'raw.json'
    raw.write_bytes(source.read_bytes())
    # 原始检查点：第二轮最后一段转写的字节副本。
    plan = {'attempt_root': str(root), 'status': 'admitted_for_isolated_recovery',
            'input_sha256': {str(root / 'manifest.json'): sha(root / 'manifest.json')},
            'source_transcript': str(source), 'source_transcript_sha256': sha(source),
            'raw_checkpoint': str(raw), 'prior_iters': 3, 'prior_wall_secs_conservative': 12,
            'context_fields': {'history_keep_pairs': 20},
            'recovery_observation': '\nFresh authoring VM; prior private files are absent.'}
    # 恢复计划：输入指纹、源转写与原始检查点、保守记录的先验消耗
    # （3 迭代/12 秒）、白名单上下文修订（history_keep_pairs=20）与恢复观察。
    plan_path = tmp_path / 'plan.json'
    save(plan_path, plan)
    # 落盘恢复计划。
    return SimpleNamespace(root=root, cycle=cycle, initial=initial, active=active, memory=memory,
                           target=target, configs={'curriculum': cfg}, plan=plan_path)
    # 打包现场对象供各测试使用。


def admit(f):
    # 测试工具：对现场执行严格准入（Phase2CurriculumRecovery.admit）。
    return Phase2CurriculumRecovery.admit(
        f.plan, f.root, f.configs, f.initial, f.target, 'curriculum_review')
    # 停止策略固定为 curriculum_review（Phase-2 协议钉死的唯一值）。


def test_admission_restores_context_notes_and_consumed_budget(tmp_path):
    # 测试：合法现场的准入必须完整还原上下文——私有笔记、对话历史、
    # 被消耗的迭代/墙钟预算，且磁盘记忆保持原样。
    f = fixture(tmp_path)
    # 搭建现场。
    b = admit(f)
    # 执行准入。
    assert b.notes == 'private notes'
    # 私有笔记从工件中恢复（不是宿主 prose 重讲）。
    assert b.history[-1]['content'] == 'actionless narration'
    # 历史末尾是未完成轮的最后一条助手消息：从断点续起。
    assert b.remaining_iters == f.configs['curriculum'].max_iters - 3
    # 剩余迭代 = 总预算减去先验消耗的 3 次。
    assert b.remaining_wall == f.configs['curriculum'].wall_clock_secs - 12
    # 剩余墙钟 = 总墙钟减去保守记录的 12 秒。
    assert E._read_memory_tree(str(f.cycle / 'memory')) == f.memory
    # 周期记忆树未被准入改动：恢复只读校验，不重放记忆。


@pytest.mark.parametrize('fault', ['memory', 'open_project', 'quarantine', 'terminal',
                                  'changed_config', 'late_context', 'event_open', 'budget'])
def test_admission_refuses_unsafe_boundaries_without_writes(tmp_path, fault):
    # 测试（8 种注入逐一参数化）：任何不安全边界都必须被拒绝，且拒绝时
    # 整个临时目录零写入（一个字节都不动）——fail-closed 的硬保证。
    f = fixture(tmp_path)
    # 先搭好合法现场，再按参数注入一种故障。
    if fault == 'memory':
        (f.cycle / 'memory/lesson.md').write_bytes(b'partial')
    # 故障 1：周期记忆被部分改写（与 state.json 登记的指纹不符）。
    elif fault == 'open_project':
        (f.cycle / 'episodes/ep002').mkdir()
    # 故障 2：出现未关闭的第二个练习项目目录。
    elif fault == 'quarantine':
        save(f.cycle / 'curriculum/turn_002/boundary_audit.json', {'status': 'quarantined'})
    # 故障 3：未完成轮带有"已隔离"的边界审计记录（曾违规）。
    elif fault == 'terminal':
        save(f.root / 'result.json', {'status': 'complete'})
    # 故障 4：尝试已有终止结果（本已结束，不能恢复）。
    elif fault == 'changed_config':
        f.configs['curriculum'] = dataclasses.replace(f.configs['curriculum'], temperature=0.7)
    # 故障 5：当前配置与 manifest 登记的生效配置不一致（temperature 被改）。
    elif fault == 'late_context':
        save(f.cycle / 'curriculum/turn_002/segment_003/transcript.json', {'messages': []})
    # 故障 6：最后一段转写与原始检查点不一致（上下文被事后篡改）。
    elif fault == 'event_open':
        (f.root / 'events.jsonl').write_text(json.dumps({'event': 'EVOLUTION_PROJECT_OPENED'}) + '\n')
    # 故障 7：事件流末尾是"项目打开"（有悬空项目）。
    else:
        plan = json.loads(f.plan.read_text())
        plan['prior_wall_secs_conservative'] = f.configs['curriculum'].wall_clock_secs + 1
        save(f.plan, plan)
    # 故障 8：先验墙钟消耗超过总预算（恢复后预算为负）。
    before = {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 注入后给全目录文件拍指纹快照。
    with pytest.raises(E.PracticeInfrastructureError):
        admit(f)
    # 准入必须抛 PracticeInfrastructureError：八类不安全边界全部拒绝。
    assert before == {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 拒绝后再拍指纹：与快照逐一相等——拒绝路径零写入。


def test_resume_continues_only_curriculum_and_keeps_completed_practice(tmp_path, monkeypatch):
    # 测试：从准入边界恢复后，只有 Curriculum 继续推进；已完成的练习项目
    # 与已提交的记忆绝不允许被"重放"（重放即测试失败）。
    f = fixture(tmp_path)
    boundary = admit(f)
    # 搭现场并准入。
    vm, prompts = _VM(), []
    hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn to the target.'], prompts)
    # 伪 VM + 钩子：Curriculum 本轮只做一个决策——回到目标。
    session = U.UnifiedCurriculumSession(history=boundary.history, notes=boundary.notes, turns=1)
    # 用边界携带的历史/笔记构造统一课程会话（已有 1 轮）。
    monkeypatch.setattr(U, '_run_practice_attempt', lambda **kw: pytest.fail('completed Actor replayed'))
    # 埋雷：任何练习尝试重放都让测试失败。
    monkeypatch.setattr(U, '_promote_learning', lambda **kw: pytest.fail('memory replayed'))
    # 埋雷：任何记忆提升重放都让测试失败。
    before = sha(f.cycle / 'episodes/ep001/outcome.json')
    # 记录已完成项目结果工件的指纹。
    result = U.evolve_until_ready(
        vm, str(f.cycle), f.target, boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'], f.active,
        load(None), load(None), load(None), load(None),
        hooks=hooks, session=session, trigger_authority='phase2_outcome_protocol',
        triggering_outcome='PASS', resume_boundary=boundary)
    # 以恢复边界续跑统一进化主流程。
    assert result.status == 'ready_for_retry'
    # Curriculum 决策 READY_FOR_TARGET：状态为"可回到目标重试"。
    assert result.projects == 1 and session.turns == 2 and session.waves == 1
    # 项目计数不变（仍为 1），会话推进到第 2 轮、1 波：只多了这一次 Curriculum 决策。
    assert session.history[:len(boundary.history)] == boundary.history
    # 历史前缀与边界历史逐条相等：恢复不篡改既有对话。
    assert result.memory_dir == str(f.cycle / 'memory')
    # 记忆目录仍是周期 memory：未被重置。
    assert E._read_memory_tree(result.memory_dir) == f.memory
    # 记忆树内容原样保留。
    assert sha(f.cycle / 'episodes/ep001/outcome.json') == before
    # 已完成项目工件字节级不变：零重放。
    assert 'private notes' in prompts[0]
    # 恢复后的提示包含私有笔记：上下文（含笔记）被真实续上。


def test_fresh_target_resumes_at_cycle_two_and_stall_gets_one_final_test():
    # 测试：目标 stalls 后带 final_after_stall 恢复，新目标从周期 2 起步，
    # stall 后仍有"最后一次目标测试"机会；一次失败即终止。
    hooks, events, released, learned = target_hooks([TargetVerdict.FAIL])
    # 目标 Verifier 判定序列只有一个 FAIL。
    result = run_self_evolving_loop(
        'target', {'practice_1.md': b'committed'}, hooks,
        start=SelfEvolvingStart(1, 1, 1, final_after_stall=True))
    # 从恢复边界启动主循环：计数器全为 1，且打开 stall 后最终测试。
    assert result.termination == 'curriculum_stalled_final_target_test'
    # 终止原因必须是"stall 后最终目标测试"：最终测试失败即停，不无限续命。
    assert result.target_cycles == 2 and result.evolutions == result.practice_projects == 1
    # 目标周期推进到 2（边界 1 + 新 1），进化次数与项目数不涨（无新练习）。
    assert result.output['cycle'] == 2
    # 输出登记的周期号同为 2。
    assert [actor.cycle for actor, verdict in learned] == [2]
    # Actor 学习只发生在周期 2：边界内的周期 1 不重放学习。
    assert released == []
    # 没有记忆被"发布"到目标外：恢复流程不做额外提交。


@pytest.mark.parametrize('actions,terminal,expected', [
    ([0, 0, 0], False, 'infra'),
    ([0, 0, 0], True, 'done'),
    ([0, 0, 1, 0, 0, 0], True, 'done'),
])
def test_actionless_guard_preserves_terminal_authority_and_productive_resets(
        tmp_path, actions, terminal, expected):
    # 测试（三组参数化）：连续无进展段触发的传输停止守卫——
    # 全程无进展且无终结发布 → 基础设施错误（且不伪造语义决策）；
    # 无进展但最终发布令牌 → 成功；中途一次有进展可重置计数 → 仍成功。
    class TransportVM(_VM):
        def run_command(self, command, **kwargs):
            if 'RSI_REMOVE_TREE_RC' in command:
                return 'RSI_REMOVE_TREE_RC=0'
            return super().run_command(command, **kwargs)
    # 扩展伪 VM：支持递归删树探测命令（守卫在停止时会清理工作区）。
    vm, calls = TransportVM(), []
    def run(prompt, vm, cfg, sink, **kwargs):
        # 伪 run_attempt：按脚本控制每段的"可执行程序数"与是否发布。
        n = len(calls)
        calls.append(kwargs)
        done = terminal and n == len(actions) - 1
        # 最后一段且 terminal=True 时发布交接令牌（语义成功）。
        if done:
            vm.files[E.CURRICULUM_HANDOFF] = 'DECISION: PROJECT\nOwned project.'
        # 写入交接文件：唯一的"语义决策"出口。
        return SimpleNamespace(status='done' if done else 'stalled', programs_run=actions[n],
                               looks=0, iters=1, wall_secs=2), []
        # 每段消耗 1 迭代/2 秒墙钟；programs_run 取自脚本（0=无进展）。
    hooks = dataclasses.replace(_hooks(vm, [], []), run_attempt=run)
    # 用脚本化 run_attempt 替换钩子里的默认实现。
    def invoke():
        return E._run_handoff_phase(
            hooks=hooks, vm=vm, cfg=load(None), prompt='search', role='CURRICULUM',
            handoff_path=E.CURRICULUM_HANDOFF, token_pattern=E._CURRICULUM_TOKEN,
            sink_root=str(tmp_path), target='target', require_completed_publication=True)
    # 直接驱动交接阶段：要求"必须以完成的发布收尾"（Phase-2 Curriculum 的要求）。
    if expected == 'infra':
        with pytest.raises(E.PracticeInfrastructureError, match='no executable progress'):
            invoke()
        # 全程零进展且无发布：必须抛"无可执行进展"的基础设施错误。
        assert json.loads((tmp_path / 'transport_stop.json').read_text())['semantic_decision'] is None
        # 停止审计里 semantic_decision 为 null：传输失败绝不被记成语义决策。
    else:
        assert invoke()[0].token == 'PROJECT'
        # 成功组：解析出的令牌恰为发布的项目决策（PROJECT）。
    assert len(calls) == len(actions)
    # 段数与脚本一致：无进展守卫不多不少按脚本停止。
    assert calls[-1]['iters_budget'] == load(None).max_iters - len(calls) + 1
    # 最后一段的迭代预算 = 总预算减去此前各段消耗（每段 1 次）：预算跨段累计。
    assert calls[-1]['wall_budget'] == load(None).wall_clock_secs - 2 * (len(calls) - 1)
    # 最后一段的墙钟预算 = 总墙钟减去此前每段 2 秒：墙钟同样累计扣减。


def test_compaction_amendment_only_applies_to_interrupted_curriculum_turn(tmp_path):
    # 测试：上下文修订（history_keep_pairs=20）只作用于"被打断的那一轮"的
    # Curriculum 调用；同一轮的后续段、新一轮、练习 Actor 一律不修订。
    boundary = admit(fixture(tmp_path))
    # 搭现场并准入（计划里 context_fields 只有 history_keep_pairs=20）。
    observed = []
    # 收集包装器实际传给原始调用的 cfg。
    def original(instruction, vm, cfg, sink, **kwargs):
        observed.append(cfg)
        return None
    # 原始调用只做记录。
    wrapped = boundary.wrap_attempt(original)
    # 用恢复边界包装原始调用。
    cfg = dataclasses.replace(load(None), practice_done_requires=E.CURRICULUM_HANDOFF)
    # 生效配置：要求 Curriculum 以交接文件收尾（包装器生效的前提条件）。
    for role_path in ('curriculum/turn_002/segment_003', 'curriculum/turn_003/segment_000', 'episodes/ep002/actor'):
        wrapped('instruction', None, cfg, SimpleNamespace(root=str(boundary.cycle / role_path)))
        # 依次模拟三类调用：未完成轮 turn_002 的后续段、全新轮 turn_003、练习 Actor。
    assert observed[0].history_keep_pairs == 20
    # 第一类（被打断轮 turn_002 的段）被修订：history_keep_pairs=20 生效。
    assert observed[1] is cfg and observed[2] is cfg
    # 第二、三类原样透传（同一对象）：新一轮与 Actor 不吃恢复修订。
    assert dataclasses.replace(observed[0], history_keep_pairs=cfg.history_keep_pairs) == cfg
    # 且修订是"仅上下文字段"的：把 history_keep_pairs 换回后与 cfg 完全相等，
    # 证明修订没有顺带改任何其他配置。


def test_recovery_cannot_change_model_via_context_amendment(tmp_path):
    # 测试：恢复计划若试图借"上下文修订"换模型（非上下文字段），必须被拒绝。
    f = fixture(tmp_path)
    plan = json.loads(f.plan.read_text())
    plan['context_fields']['model'] = 'another-model'
    save(f.plan, plan)
    # 篡改恢复计划：往白名单字段里塞 model 字段。
    with pytest.raises(E.PracticeInfrastructureError, match='non-context'):
        admit(f)
    # 准入必须以 "non-context" 错误拒绝：恢复永远不能改变被恢复者的模型。


def test_productive_segments_still_share_one_role_budget(tmp_path, monkeypatch):
    # 测试：同一角色的多个段共享一份预算——每段有真实进展时按段累计扣减，
    # 预算耗尽仍以基础设施错误收场（进展不能换取无限预算）。
    vm, calls = _VM(), []
    monkeypatch.setattr(E, '_remove_incomplete_curriculum_project', lambda vm: None)
    # 屏蔽清理函数：本测试只关心预算累计。
    def run(*args, **kw):
        calls.append(kw)
        return SimpleNamespace(status='budget', programs_run=1, looks=0, iters=1, wall_secs=4), []
    # 每段都是"有进展但预算耗尽"（programs_run=1，消耗 1 迭代/4 秒）。
    hooks = dataclasses.replace(_hooks(vm, [], []), run_attempt=run)
    cfg = dataclasses.replace(load(None), max_iters=2, wall_clock_secs=100)
    # 小预算配置：最多 2 迭代/100 秒。
    with pytest.raises(E.PracticeInfrastructureError, match='cumulative transport budget'):
        E._run_handoff_phase(
            hooks=hooks, vm=vm, cfg=cfg, prompt='search', role='CURRICULUM',
            handoff_path=E.CURRICULUM_HANDOFF, token_pattern=E._CURRICULUM_TOKEN,
            sink_root=str(tmp_path), target='target', require_completed_publication=True)
    # 两段后累计迭代超预算：必须抛"累计传输预算"错误。
    assert len(calls) == 2
    # 恰好尝试了两段（第三段不再发起）。
    assert calls[1]['iters_budget'] == 1 and calls[1]['wall_budget'] == 96
    # 第二段预算 = 2-1=1 迭代、100-4=96 秒：进展段同样计入同一份角色预算。
