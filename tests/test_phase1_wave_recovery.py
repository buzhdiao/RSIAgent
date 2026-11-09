# 【中文注释·模块定位】Phase-1 并行波次恢复（tools/recover_phase1_wave.py +
# explore/phase1_wave.py）的安全回归测试。核心承诺：
# 1) 分支分类（classify_branch）只认保存的工件：已有 Verifier 判定的候选只复用判定
#    （PASS/FAIL 一视同仁）；有任何 Verifier 证据就绝不允许重抓候选；Verifier 因
#    基础设施失败中断（零机器副作用）才允许 resume_verifier；记忆蒸馏一旦开始
#    就拒绝重放（防记忆被写两次）；
# 2) 恢复目录治理：第一次恢复未收尾不得开第二个；已完成的恢复不可复用；
#    第一次失败档案原样保留；
# 3) 波次执行（execute）必须等整波结束才收尾：一个分支恢复失败 → 冻结 Actor 已写
#    记忆、关闭已恢复 VM、result.json 保持原始 infra 状态且零官方评测调用；
# 4) 边界续跑（evolve_parallel_phase1 + resume_completed_boundary）：记忆/课程历史/
#    波次号/检查点全保留，且对十类持久状态破坏逐一把关——拒绝时零写入、绝不启动 VM。
"""Recovery must preserve grades and stop before memory on incomplete waves."""
# 模块 docstring：恢复必须保留既有评分，且波次未完成时停在记忆阶段之前。

import json
# 导入 json：构造与解析各种持久化工件。
import sys
# 导入 sys：往 sys.modules 注入伪模块（骗过 desktop_env 延迟导入）。
import types
# 导入 types：动态构造伪模块对象。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 context/VM/项目等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from tools import recover_phase1_wave as recovery
# 导入被测主体：Phase-1 波次恢复工具（分类/恢复目录/执行）。


@pytest.mark.parametrize('verdict', ['PASS', 'FAIL'])
def test_both_terminal_verdicts_reuse_the_saved_candidate(tmp_path, verdict):
    # 测试（PASS/FAIL 各一遍）：候选档案已存在且 Verifier 已给出终局判定时，
    # 恢复分支必须分类为 reuse_verdict——复用已保存的判定，不重跑验证。
    candidate = tmp_path / 'candidates/cycle_001/materials.tgz'
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b'archive validated separately')
    # 放置候选档案（内容任取，归档校验在别处单独测）。
    handoffs = tmp_path / 'handoffs'
    handoffs.mkdir()
    (handoffs / 'verifier_001.md').write_text(f'VERDICT: {verdict}\nComplete evidence report.')
    # Verifier 交接报告：以 "VERDICT: {判定}" 开头的完整证据报告。
    assert recovery.classify_branch(tmp_path) == 'reuse_verdict'
    # 分类结果：复用判定（无论 PASS 还是 FAIL）。


def test_capture_retry_is_rejected_after_any_verifier_evidence(tmp_path):
    # 测试：只要存在任何 Verifier 证据（哪怕候选缺失），就绝不允许"重抓候选"。
    (tmp_path / 'verifier').mkdir()
    # 只有 Verifier 目录（无候选档案）：说明抓取曾发生并被验证过。
    with pytest.raises(RuntimeError, match='Missing candidate has Verifier evidence'):
        recovery.classify_branch(tmp_path)
    # 分类必须抛错：候选缺失但已有 Verifier 证据 → 拒绝重抓（防重复计费执行）。


def test_resume_requires_verifier_failure_before_machine_execution(tmp_path):
    # 测试：resume_verifier 只允许"验证者在产生机器副作用之前因基础设施失败"——
    # infra_fail=True 且 secs=0 才放行；一旦跑过真实验证（secs>0）必须拒绝。
    candidate = tmp_path / 'candidates/cycle_001/materials.tgz'
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b'archive validated separately')
    # 候选档案在场。
    trace = tmp_path / 'verifier/iter_01/trace_meta.json'
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps({'infra_fail': True, 'secs': 0}))
    # 验证追踪元数据：基础设施失败且零执行秒数。
    assert recovery.classify_branch(tmp_path) == 'resume_verifier'
    # 分类为 resume_verifier：从零副作用处恢复验证。
    trace.write_text(json.dumps({'infra_fail': False, 'secs': 0.1}))
    # 改为正常完成（0.1 秒真实执行）。
    with pytest.raises(RuntimeError, match='machine effects are unarchived'):
        recovery.classify_branch(tmp_path)
    # 必须拒绝：机器副作用未归档时不能就此收尾（结果不可追溯）。


def test_partially_learned_branch_cannot_be_replayed(tmp_path):
    # 测试：记忆蒸馏目录已出现（记忆阶段已开始）时，分支绝不允许重放——
    # 否则 Actor 记忆会被提交两次。
    (tmp_path / 'memory_distillation').mkdir()
    # 蒸馏目录存在 = 记忆阶段已启动。
    with pytest.raises(RuntimeError, match='Memory phase already began'):
        recovery.classify_branch(tmp_path)
    # 分类必须拒绝：记忆阶段的中间态不可重放。


def test_a_second_recovery_preserves_the_first_failure_archive(tmp_path):
    # 测试：恢复目录的治理规则——第一次恢复未收尾（still open）不得开第二个；
    # 登记失败后可开 infrastructure_recovery_002，且第一次的 failure.json 原样保留；
    # 第一次标记完成后（already completed）再开第三个必须拒绝。
    first = tmp_path / 'infrastructure_recovery_001'
    first.mkdir()
    # 预先存在第一次恢复目录（尚未收尾）。
    with pytest.raises(RuntimeError, match='still open'):
        recovery.next_recovery_dir(tmp_path)
    # 未收尾时再要新目录必须拒绝。
    failure = '{"status": "infra", "reason": "candidate replay failed"}\n'
    (first / 'failure.json').write_text(failure)
    # 给第一次恢复写入失败档案（视为已收尾）。
    assert recovery.next_recovery_dir(tmp_path) == tmp_path / 'infrastructure_recovery_002'
    # 下一个恢复目录按序号递增：002。
    assert (first / 'failure.json').read_text() == failure
    # 第一次的失败档案逐字节不变：不被后续恢复触碰。
    (first / 'completion.json').write_text('{"status": "complete"}')
    # 把第一次标记为完成。
    with pytest.raises(RuntimeError, match='already completed'):
        recovery.next_recovery_dir(tmp_path)
    # 已完成的恢复不可复用/续写：再要目录必须拒绝。


def test_wave_failure_keeps_original_result_and_closes_restored_vms(tmp_path, monkeypatch):
    # 测试：恢复执行中任一分支失败 → 已成功恢复的分支 VM 被关闭、记忆不提交、
    # result.json 保持原始 infra 记录；失败档案与恢复前快照落盘到 recovery_001。
    from explore import phase1_wave as wave
    # 局部导入波次执行模块（记忆提交入口在其命名空间）。
    original = json.dumps({'status': 'infra', 'projects': 6})
    for name in ('state.json', 'result.json', 'manifest.json'):
        (tmp_path / name).write_text(original)
    # 三份核心状态文件都记录原始 infra 现场（6 个已完成项目）。
    module = types.ModuleType('desktop_env.desktop_env')
    module.DesktopEnv = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # 注入伪 desktop_env 模块：恢复工具延迟导入它时不会真的装 OSWorld。
    closed, committed = [], []
    # 记录被关闭的 VM 与被提交的记忆。
    def restore(context, item, factory, hooks):
        # 伪分支恢复：项目 7 成功，项目 8 抛错（模拟验证未完成）。
        if item[0] == 8:
            raise RuntimeError('verification is incomplete')
        return SimpleNamespace(project_index=7, terminal_outcome='PASS',
                               desktop=SimpleNamespace(close=lambda: closed.append(7)))
        # 项目 7 返回已恢复分支，desktop.close 可观察。
    monkeypatch.setattr(recovery, 'restore_branch', restore)
    # 替换分支恢复。
    monkeypatch.setattr(wave, '_commit_branch_memory', lambda **kwargs: committed.append(True))
    # 替换记忆提交：只记录调用（本测试要求零提交）。
    context = SimpleNamespace(root=tmp_path, runner=SimpleNamespace(_append_event=lambda *args, **kwargs: None),
                              configs={'verifier_control': SimpleNamespace(verifier_execution_mode='effect_isolated')},
                              plan={'wave': 2, 'branches': []}, assignments=[(7,), (8,)],
                              protocol={'phase1': {'parallelism': 2}})
    # 伪执行上下文：效果隔离验证模式、两个待恢复任务（7 与 8）、并行度 2。
    with pytest.raises(RuntimeError, match='verification is incomplete'):
        recovery.execute(context)
    # 恢复执行必须把项目 8 的失败如实抛出。
    assert committed == []
    # 任何分支记忆都未提交：整波未完成不收账。
    assert closed == [7]
    # 已恢复的项目 7 VM 被关闭（不留悬空 VM）。
    assert (tmp_path / 'result.json').read_text() == original
    # result.json 逐字节保持原始记录：失败路径不覆盖既有结果。
    assert (tmp_path / 'infrastructure_recovery_001/before_result.json').read_text() == original
    # 恢复前的结果快照存入 recovery_001。
    assert (tmp_path / 'infrastructure_recovery_001/failure.json').is_file()
    # 失败原因档案落盘。


@pytest.mark.parametrize('budget, expected_status', [(8, 'budget_exhausted'), (10, 'infra')])
def test_completion_waits_for_whole_wave_then_freezes_actor_written_memory(
        tmp_path, monkeypatch, budget, expected_status):
    # 测试（预算 8/10 两档参数化）：恢复收尾必须等整波所有分支恢复完毕——
    # Actor 记忆提交按分支序 7→8 进行，且提交前先冻结既有记忆；恢复用掉
    # 项目预算 8 时结果标 budget_exhausted，预算充足时保持 infra。
    from explore import phase1_wave as wave
    from explore.practice_loop import _manifest, _read_memory_tree
    # 导入波次模块与记忆树工具。
    original = {'status': 'infra', 'projects': 6, 'official_evaluator_calls': 0}
    for name in ('state.json', 'result.json', 'manifest.json'):
        (tmp_path / name).write_text(json.dumps(original))
    # 原始状态：infra、6 项目、零官方评测调用。
    memory = tmp_path / 'memory'
    memory.mkdir()
    (memory / 'prior.md').write_text('six completed projects')
    # 既有记忆：前 6 个项目的成果。
    for index in range(1, 7):
        ep = tmp_path / 'episodes' / f'ep{index:03d}'
        ep.mkdir(parents=True)
        (ep / 'outcome.json').write_text('{}')
    # 补齐 6 个已完成项目的 episode 结果目录。
    module = types.ModuleType('desktop_env.desktop_env')
    module.DesktopEnv = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # 注入伪 desktop_env 模块。
    ready, commits = [], []
    assignments = []
    for index, verdict in ((7, 'PASS'), (8, 'FAIL')):
        project = SimpleNamespace(project_id=f'p{index}', instruction='original project')
        episode = tmp_path / 'episodes' / f'ep{index:03d}'
        assignments.append((index, project, episode, 'reuse_verdict'))
    # 两个待恢复分支（7=PASS、8=FAIL），都分类为复用判定。
    def restore(context, item, factory, hooks):
        # 伪分支恢复：依次就绪并携带保存的交接/判定/波次记忆。
        index, project, episode, _ = item
        ready.append(index)
        return SimpleNamespace(project_index=index, project=project, episode_dir=episode,
                               terminal_outcome='PASS' if index == 7 else 'FAIL',
                               actor_handoff='SUBMIT', verifier_report='saved report',
                               wave_memory={'prior.md': b'six completed projects'},
                               desktop=SimpleNamespace(close=lambda: None))
        # 恢复出的分支：终局判定、Actor 交接、Verifier 报告、波次记忆快照。
    def actor_commit(**kwargs):
        # 伪记忆提交：断言整波到齐后才依次提交，并模拟续跑 Actor 的写入。
        assert sorted(ready) == [7, 8]
        # 关键前置：提交只发生在两个分支都恢复完成之后。
        branch = kwargs['branch']
        commits.append((branch.project_index, branch.terminal_outcome))
        before = _manifest(_read_memory_tree(str(memory)))
        # 提交前记忆 manifest。
        # This is the stubbed continuing Actor's output, including FAIL learning.
        (memory / f'actor_{branch.project_index}.md').write_text(branch.terminal_outcome)
        # 续跑 Actor 的产出：按分支写入学习结果（含 FAIL 分支的学习）。
        after = _manifest(_read_memory_tree(str(memory)))
        # 提交后记忆 manifest。
        return {'before': before, 'after': after, 'changes': {}}
        # 返回前后 manifest 供收尾登记。
    monkeypatch.setattr(recovery, 'restore_branch', restore)
    monkeypatch.setattr(wave, '_commit_branch_memory', actor_commit)
    monkeypatch.setattr(wave, '_outcome_text', lambda record: record['terminal_outcome'])
    # 替换恢复/提交/结果文本三个入口。
    context = SimpleNamespace(
        root=tmp_path, runner=SimpleNamespace(_append_event=lambda *args, **kwargs: None,
                                            DEFAULT_CORPUS=tmp_path / 'corpus'),
        configs={'verifier_control': SimpleNamespace(verifier_execution_mode='effect_isolated'),
                 'memory_actor': None},
        state=original, result=original, target='original direction',
        decision=SimpleNamespace(rationale='original rationale'), wave_dir=tmp_path / 'wave',
        plan={'wave': 2, 'branches': [], 'complete_wave_total': 8, 'budget': budget}, assignments=assignments,
        protocol={'phase1': {'parallelism': 2, 'checkpoints': [8]}})
    # 伪上下文：全波总数 8、可变预算（8 或 10）、检查点 8。
    recovery.execute(context)
    # 执行恢复收尾。
    assert commits == [(7, 'PASS'), (8, 'FAIL')]
    # 提交序列严格按分支序：先 7（PASS）后 8（FAIL）。
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['status'] == expected_status and result['projects'] == 8
    # 预算 8 → budget_exhausted；预算 10 → infra；项目数推进到 8。
    assert result['official_evaluator_calls'] == 0
    # 恢复全程零官方评测调用。
    assert _read_memory_tree(str(tmp_path / 'memory_frozen')) == _read_memory_tree(str(memory))
    # 冻结快照与工作记忆逐文件一致：收尾时冻结 Actor 已写记忆。
    assert (tmp_path / 'memory_frozen/actor_8.md').read_text() == 'FAIL'
    # FAIL 分支的学习也被冻结保留（评分与学习一并存档）。


def completed_wave(tmp_path):
    # 现场搭建：完整复刻"wave_001 已完成（1 项目 FAIL + Actor 记忆）+ 状态 infra
    # 待恢复"的磁盘现场，返回当时的课程对话历史。
    import hashlib
    from explore.practice_loop import _manifest
    memory = tmp_path / 'memory'
    memory.mkdir()
    (memory / 'actor.md').write_text('Actor learned after a valid FAIL')
    # Actor 记忆：来自一次合法 FAIL 的学习。
    manifest = _manifest({'actor.md': b'Actor learned after a valid FAIL'})
    # 记忆 manifest（state.json 将登记它）。
    frozen = tmp_path / 'memory_frozen'
    frozen.mkdir()
    (frozen / 'actor.md').write_bytes((memory / 'actor.md').read_bytes())
    # 冻结快照与工作记忆一致（此前收尾的产物）。
    episode = tmp_path / 'episodes/ep001'
    episode.mkdir(parents=True)
    (episode / 'outcome.json').write_text(json.dumps({
        'project_index': 1, 'wave_index': 1, 'project_id': 'one',
        'terminal_outcome': 'FAIL', 'memory_before': {}, 'memory_after': manifest}))
    # episode 结果：项目 1、wave 1、FAIL、记忆前后 manifest。
    (episode / 'outcome.md').write_text('Original FAIL evidence')
    # 结果原文。
    (tmp_path / 'events.jsonl').write_text(json.dumps({
        'event_type': 'PHASE1_WAVE_COMPLETED', 'payload': {'wave': 1, 'total_projects': 1}}) + '\n')
    # 事件流：wave_001 已完成（1 项目）。
    wave_dir = tmp_path / 'waves/wave_001'
    wave_dir.mkdir(parents=True)
    (wave_dir / 'curriculum_decision.json').write_text(json.dumps({
        'decision': 'WAVE', 'rationale': 'practice needed',
        'projects': [{'id': 'one', 'instruction': 'Create /home/user/evolution_project/output'}]}))
    # 课程决策：WAVE、理由、项目指令（校验用）。
    (wave_dir / 'outcomes.md').write_text('Original FAIL and Actor memory changes')
    # 波次结果原文（续跑提示的 previous_wave_outcomes）。
    history = [{'role': 'user', 'content': 'Original direction'},
               {'role': 'assistant', 'content': 'Original completed wave handoff'}]
    # 课程对话历史：原始方向 + 已完成波次的交接。
    transcript = tmp_path / 'curriculum/wave_001/segment_009/transcript.json'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'messages': history}))
    # 课程转写落盘（段 009）。
    state = {'status': 'infra', 'projects': 1, 'last_project': 1, 'waves': 2,
             'phase1_parallel_waves': True, 'project_budget': 8,
             'max_parallel': 4, 'checkpoint_projects': [0, 4, 8],
             'target_query_conditioned': True, 'memory_manifest': manifest,
             'target_sha256': hashlib.sha256(b'original target').hexdigest()}
    (tmp_path / 'state.json').write_text(json.dumps(state))
    # 恢复状态：infra、下一波号 2、预算/并行度/检查点、目标指纹与记忆 manifest。
    return history
    # 返回历史供续跑断言。


def resume_kwargs(root):
    # 续跑参数包：与 state.json 登记的预算/检查点/并行度一致，打开边界续跑。
    return dict(root=str(root), target_direction='original target', actor_cfg=None,
                curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
                corpus_path='unused', project_budget=8, checkpoint_projects=(0, 4, 8),
                max_parallel=4, target_query_conditioned=True, resume_completed_boundary=True)


def test_resume_keeps_memory_curriculum_and_next_wave_number(tmp_path, monkeypatch):
    # 测试：从已完成波次的 infra 边界续跑——记忆、课程历史、波次结果原文、
    # 下一波号（wave_002）全部保留；检查点 0 的原始内容不被覆盖；
    # 课程判定 SATURATED 时正常饱和收尾。
    from explore import phase1_wave as wave
    from explore.practice_loop import PracticeHooks
    history = completed_wave(tmp_path)
    # 搭建已完成波次现场。
    checkpoint = tmp_path / 'checkpoints/project_000/memory/initial'
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text('Preserve original checkpoint zero')
    # 检查点 0 的初始记忆（续跑不得覆盖）。
    calls, events, closed = [], [], []
    def author(**kwargs):
        # 伪波次作者：记录调用参数，返回 SATURATED 决策与延续的历史。
        calls.append(kwargs)
        return wave.parse_wave_handoff(json.dumps({
            'decision': 'SATURATED', 'rationale': 'agent assessment', 'projects': []})), history
    monkeypatch.setattr(wave, '_author_wave', author)
    # 替换波次作者（不起真课程会话）。
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *args: None)
    # 屏蔽规范记忆推送（现场已一致）。
    result = wave.evolve_parallel_phase1(
        **resume_kwargs(tmp_path), hooks=PracticeHooks(validate_corpus=lambda _: None,
                                               audit_text=lambda *args, **kwargs: []),
        vm_factory=lambda: (SimpleNamespace(close=lambda: closed.append(True)), object()),
        event_sink=lambda name, **kw: events.append(name))
    # 以边界续跑参数启动并行 Phase-1 主循环；VM 工厂记录 VM 关闭、事件汇总。
    assert result.status == 'saturated' and result.projects == 1
    # 结果：SATURATED 饱和收尾，项目计数保持 1（边界内的项目不重放）。
    assert calls[0]['history'] == history
    # 新波次作者的对话历史 = 边界保存的历史（逐条相等）。
    assert calls[0]['previous_wave_outcomes'] == 'Original FAIL and Actor memory changes'
    # 上一波结果原文原样进入提示。
    assert calls[0]['sink_root'].name == 'wave_002'
    # 新波次根目录编号为 002：波次计数正确续接。
    assert checkpoint.read_text() == 'Preserve original checkpoint zero'
    # 检查点 0 原文不变。
    assert (tmp_path / 'memory/actor.md').read_text() == 'Actor learned after a valid FAIL'
    # Actor 记忆原样保留（不被续跑重置）。
    assert 'PHASE1_WAVE_BOUNDARY_RESUMED' in events and closed == [True]
    # 事件流记录"波次边界已恢复"，且收尾时 VM 被正常关闭。


@pytest.mark.parametrize('damage', [
    'memory', 'partial_learning', 'curriculum_symlink', 'quarantine', 'budget', 'ledger',
    'wave_counter', 'incomplete_history', 'history_roles', 'invalid_assignment'])
def test_invalid_wave_resume_never_overwrites_preserved_state(tmp_path, damage):
    # 测试（10 类破坏逐一参数化）：持久状态被破坏后，边界续跑必须以
    # PracticeInfrastructureError 拒绝，且拒绝路径零写入——state.json 逐字节不变，
    # 绝不启动任何 VM（防破坏现场被半心半意地"修复"）。
    from explore import phase1_wave as wave
    from explore.practice_loop import PracticeInfrastructureError
    completed_wave(tmp_path)
    # 先搭好合法现场，再按参数注入一种破坏。
    if damage == 'memory':
        (tmp_path / 'memory/actor.md').write_text('drift')
    # 破坏 1：记忆内容漂移（与 state 登记的 manifest 不符）。
    elif damage == 'partial_learning':
        (tmp_path / 'episodes/ep002/memory_distillation').mkdir(parents=True)
    # 破坏 2：出现未收尾的部分学习（ep002 蒸馏目录悬空）。
    elif damage == 'curriculum_symlink':
        (tmp_path / 'curriculum/wave_002').symlink_to(tmp_path, target_is_directory=True)
    # 破坏 3：课程目录出现符号链接（目录遍历逃逸）。
    elif damage in {'quarantine', 'budget', 'wave_counter'}:
        state = json.loads((tmp_path / 'state.json').read_text())
        key, value = {'quarantine': ('status', 'quarantined'),
                      'budget': ('project_budget', 9), 'wave_counter': ('waves', 9)}[damage]
        state[key] = value
        (tmp_path / 'state.json').write_text(json.dumps(state))
    # 破坏 4-6：state.json 被改——隔离状态/预算被篡改/波次计数跳变。
    elif damage in {'incomplete_history', 'history_roles'}:
        path = tmp_path / 'curriculum/wave_001/segment_009/transcript.json'
        transcript = json.loads(path.read_text())
        if damage == 'incomplete_history':
            transcript['messages'].pop()
        else:
            transcript['messages'][0]['role'] = 'assistant'
        path.write_text(json.dumps(transcript))
    # 破坏 7：课程历史缺尾（对话不完整）；破坏 8：历史角色被调换。
    elif damage == 'invalid_assignment':
        path = tmp_path / 'waves/wave_001/curriculum_decision.json'
        decision = json.loads(path.read_text())
        del decision['projects'][0]['instruction']
        path.write_text(json.dumps(decision))
    # 破坏 9：课程决策缺项目 instruction（任务定义不完整）。
    else:
        path = tmp_path / 'episodes/ep001/outcome.json'
        record = json.loads(path.read_text())
        record['project_id'] = 'different'
        path.write_text(json.dumps(record))
    # 破坏 10：episode 结果的 project_id 与课程决策对不上（台账不一致）。
    original = (tmp_path / 'state.json').read_bytes()
    # 注入后给 state.json 拍字节快照。
    with pytest.raises(PracticeInfrastructureError):
        wave.evolve_parallel_phase1(**resume_kwargs(tmp_path),
                                   vm_factory=lambda: pytest.fail('Must not boot VM'))
    # 续跑必须拒绝；VM 工厂一旦被调用立即失败——恢复绝不启动 VM。
    assert (tmp_path / 'state.json').read_bytes() == original
    # state.json 逐字节不变：拒绝路径零写入。
