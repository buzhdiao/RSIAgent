# 【中文注释·模块定位】练习记忆恢复（practice memory recovery）的边界与重放
# 契约测试（explore/practice_memory_recovery + explore/phase2_recovery.
# Phase2CurriculumRecovery + explore/target_learning + explore/practice_loop
# + explore/unified_evolution + explore/practice_evidence_recovery）。
# 场景：一次以 FAIL 结束的练习（"既定失败学习"）在记忆蒸馏阶段因基础设施
# 故障中断，重启后从盘上证据恢复。被钉死的安全承诺：
# 1) admit 只接受证据链完整且未被篡改的失败练习（判定已验证、记忆已提交、
#    Actor 历史/系统提示逐字节一致、配置哈希一致、预算未耗尽、私有笔记程序
#    未被改写、候选工件在位、无部分写入、输入哈希匹配、事件流闭合）；
# 2) 恢复重放只回放"已保存候选 + 已提交记忆"，绝不重跑 Actor/Verifier；
# 3) 续跑扣除既有预算并保留完整 8 条上下文历史，预算耗尽或历史丢失必须
#    fail-closed；
# 4) 提交只发生一次，随后直接进入下一课程轮次——零 Actor/Verifier 重放，
#    终局 FAIL 原样记录，课程记忆访问权限（none）被如实执行。
# boundary fixture 在磁盘上搭建完整证据状态；全部不起真实 VM/LLM。
from dataclasses import dataclass, asdict
# 导入 dataclass/asdict：定义并序列化测试用配置。
from pathlib import Path
# 导入 Path：构造盘上证据路径。
from types import SimpleNamespace
# 导入 SimpleNamespace：打包 fixture 产物供测试取用。
import hashlib
# 导入 hashlib：目标方向 sha256 与记忆树哈希。
import json
# 导入 json：读写盘上 JSON 证据。
import time
# 导入 time：伪造单调时钟以注入预算耗尽故障。

import pytest
# 导入 pytest：fixture、参数化与异常断言。

from explore import target_learning as V
# 导入目标学习模块：记忆树哈希、VM 创建/重放/记忆推送等被 monkeypatch 的钩子。
from explore.practice_loop import _manifest
# 导入练习循环的记忆清单函数：构造 manifest 形状的记忆记录。
from explore import practice_memory_recovery as R
# 导入被测模块：恢复计划校验、候选重放与回执读写。
from explore.practice_loop import PracticeInfrastructureError
# 导入练习循环的基础设施异常：恢复拒绝时的预期异常类型。
from explore.phase2_recovery import Phase2CurriculumRecovery
# 导入 Phase-2 课程恢复适配器：admit（受理）与 wrap_attempt（续跑包装）。


@dataclass
class Config:
    # 测试用配置 dataclass：默认 2000 次迭代 / 24 小时墙钟预算（与真实
    # Config 字段形状对齐，便于恢复模块读取）。
    max_iters: int = 2000
    wall_clock_secs: float = 86400


def save(path, value):
    # 测试工具：父目录自动创建后写入 JSON 文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先建目录树。
    path.write_text(json.dumps(value))
    # 序列化落盘。


@pytest.fixture
def boundary(tmp_path):
    """搭建一次"FAIL 练习 + 记忆蒸馏中断"的完整盘上证据状态（恢复边界）。"""
    # 重建整个恢复入口所需的证据目录树：
    root = tmp_path / 'attempt'
    # 尝试根目录。
    cycle = root / 'evolution_cycles/cycle_001'
    # 进化周期目录。
    ep = cycle / 'episodes/ep001'
    # 练习 episode 目录。
    target_root = root / 'target_cycles/cycle_001'
    # 目标周期目录（Phase-2 终局学习产物）。
    memory = {'lesson.md': b'committed target memory'}
    # 已提交的记忆：一个 lesson 文件（字节级内容）。
    for folder in [root / 'active_memory', cycle / 'memory']:
        folder.mkdir(parents=True)
        (folder / 'lesson.md').write_bytes(memory['lesson.md'])
        # 活动记忆与周期记忆都持有同一份已提交记忆。
    cfg = Config()
    # 测试配置实例。
    config_path = tmp_path / 'config.json'
    save(config_path, asdict(cfg))
    # 配置落盘（manifest 里的 configs 段要记录其 sha256）。
    report = 'Independent target inspection.\nVERDICT: PASS'
    # 目标 Verifier 报告：独立检查 + PASS 终局令牌。
    diagnosis = 'Original Actor diagnosis.'
    # 原始 Actor 学习诊断。
    learning = dict(target_cycle=1, target_verifier_verdict='PASS', memory_before={},
                    memory_after=_manifest(memory), verifier_report=report,
                    actor_learning_diagnosis=diagnosis)
    save(target_root / 'terminal_learning/outcome.json', learning)
    # Phase-2 终局学习结果：PASS、记忆前后清单、报告与诊断。
    save(cycle / 'trigger/outcome.json', dict(target='target', trigger_authority='phase2_outcome_protocol',
         verifier_outcome='PASS', verifier_report=report, actor_learning_diagnosis=diagnosis,
         memory_before=_manifest(memory)))
    # 触发记录：权威来源为 phase2_outcome_protocol，判定 PASS，报告/诊断/
    # 输入记忆与终局学习一致（恢复时逐项对账）。
    (cycle / 'trigger/outcome.md').write_text('original trigger')
    # 触发的 markdown 原文。
    save(cycle / 'state.json', dict(status='infra', projects=1, learning_experiences=1,
         reason='ACTOR phase ended at emergency/infrastructure status infra',
         memory_manifest=_manifest(memory), memory_tree_sha256=V._memory_tree_sha256(memory)))
    # 周期状态：因基础设施故障中断、已完成 1 个项目/1 条学习经历、记忆树哈希。
    save(root / 'manifest.json', dict(target_direction_sha256=hashlib.sha256(b'target').hexdigest(),
         initial_memory={'manifest': {}}, protocol=dict(curriculum_memory_access='none',
         stop_policy='curriculum_review', official_evaluator_calls=0,
         official_evaluator_feedback_enters_learning=False),
         configs={role: dict(path=str(config_path), sha256=R.sha(config_path), effective=asdict(cfg))
                  for role in ['memory_actor', 'curriculum']}))
    # 尝试 manifest：目标方向哈希、空初始记忆、协议承诺（零官方评分、反馈
    # 不进学习环、课程记忆访问 none）与两个角色的配置记录（路径+哈希+生效值）。
    verdict = 'Independent practice inspection.\nVERDICT: FAIL'
    # 练习 Verifier 报告：独立检查 + FAIL（即"既定失败"场景）。
    save(ep / 'verifier/iter_01/verify2.json', dict(verdict='wrong', findings=verdict))
    # Verifier 第二轮复核 JSON：判 wrong，findings 携带完整 FAIL 报告。
    (ep / 'verifier_report.md').write_text(verdict)
    # 报告 markdown 原文。
    save(root / 'events.jsonl', dict(event='EVOLUTION_MEMORY_PHASE_STARTED', payload=dict(
         payload=dict(experience_index=1, terminal_outcome='FAIL',
                      verifier_report_sha256=hashlib.sha256(verdict.encode()).hexdigest()),
         state=dict(memory_phase_open=True))))
    # 事件流：记忆阶段已开启，携带经验序号、终局 FAIL 与报告哈希（恢复要求
    # 事件流末尾正是这个中断点）。
    pairs = [dict(role='user', content='observation'), dict(role='assistant', content='response')]
    # 一对最小对话消息，反复使用以构造各转写。
    actor = ep / 'actor/attempt_001/segment_000'
    # Actor 执行段目录。
    source = ep / 'memory_distillation/segment_000'
    # 记忆蒸馏源段目录（Actor 的完整 4 倍历史在这里待蒸馏）。
    save(actor / 'transcript.json', dict(system='original actor system', messages=pairs))
    # Actor 转写：系统提示 + 一对消息。
    save(actor / 'result.json', dict(status='done'))
    # Actor 段正常完成。
    save(source / 'transcript.json', dict(system='original actor system', messages=pairs * 4))
    # 蒸馏源转写：同一系统提示 + 8 条消息（pairs×4，即待恢复的完整历史）。
    for idx in range(1, 4):
        p = source / f'iter_{idx:02d}'
        save(p / 'trace_meta.json', dict(infra_fail=True, exit_code=None))
        (p / 'trace.txt').write_text('mktemp: Read-only file system')
        (p / 'turn.txt').write_text('failed staging')
        # 三个蒸馏迭代全部基础设施失败：trace_meta 标记 infra_fail、trace 是
        # 包装脚本的只读文件系统失败原文、turn 是暂存失败说明。
    program = cycle / 'curriculum/turn_001/segment_000/iter_01/program.py'
    save(program.parent / 'trace_meta.json', dict(exit_code=0))
    # 课程轮次 1 的程序：正常退出（exit_code=0）。
    project = 'DECISION: PROJECT\nInspect /home/user/evolution_project.'
    # 课程决策：开下一个练习项目。
    program.write_text(f'handoff={project!r}\nnotes="private notes"\n'
                       "with open('/home/user/curriculum_notes.md','w') as f:\n    f.write(notes)\n")
    # 程序内容：handoff 变量携带项目决策，并把私有课程笔记写入课程私有路径
    # （恢复时要证明笔记程序未被篡改且笔记能被读回）。
    save(program.parent.parent / 'transcript.json', dict(system='curriculum', messages=pairs))
    # 课程段的转写。
    (ep / 'project.md').write_text(project)
    # episode 的项目任务书与决策一致。
    (ep / 'actor_handoff.md').write_text('STATUS: SUBMIT')
    # Actor 交接状态：SUBMIT。
    for folder in [ep / 'candidate/publication_001', ep / 'fixtures']:
        folder.mkdir(parents=True)
        (folder / 'materials.tgz').write_bytes(b'fixture archive')
        # 已保存候选工件与 fixture 归档各一份 tgz（重放时按序推送）。
    log = tmp_path / 'failed.log'
    log.write_text('3 consecutive run-wrapper failures')
    # 失败的运行日志：连续三次包装脚本失败（触发恢复的现场证据）。
    plan = dict(recovery_kind='verified_practice_memory_fs', status='admitted_for_isolated_recovery',
                attempt_root=str(root), completed_projects=0, completed_curriculum_turns=1,
                input_sha256={}, curriculum_memory_access='none', failed_runtime_log=str(log),
                prior_memory_budget=dict(iterations=3, wall_secs_conservative=30),
                partial_guest_memory_written=False, curriculum_publication_program=str(program))
    # 恢复计划：已验证的练习记忆文件系统恢复、已完成 0 项目 1 课程轮、课程
    # 记忆访问 none、先验预算（3 次迭代/30 秒保守墙钟）、无部分写入、指向
    # 课程发布程序。
    plan_path = tmp_path / 'recovery_plan.json'
    save(plan_path, plan)
    # 恢复计划落盘。
    return SimpleNamespace(root=root, cycle=cycle, ep=ep, source=source, memory=memory,
                           cfg=cfg, plan=plan, plan_path=plan_path, program=program)
    # 打包所有路径与产物供各测试取用。


def admit(b):
    """调用 Phase2CurriculumRecovery.admit：对 boundary 证据执行受理校验。"""
    return Phase2CurriculumRecovery.admit(b.plan_path, b.root,
        {'memory_actor': b.cfg, 'curriculum': b.cfg}, {}, 'target', 'curriculum_review')
    # 受理参数：计划文件、尝试根、两角色配置、空初始记忆、目标、停止策略。


def test_admits_saved_fail_and_complete_failed_learning_history(boundary):
    """已保存的 FAIL 练习与完整失败学习历史应被受理，且不伪造 episode 终局。"""
    b = boundary
    # 取 fixture 产物。
    result = admit(b)
    # 执行受理。
    assert result.projects == 0 and result.completed_curriculum_turns == 1
    # 恢复计划中的进度如实带入：0 个已完成项目、1 个已完成课程轮。
    assert result.pending_practice['terminal_outcome'] == 'FAIL'
    # 待恢复练习的终局是 FAIL（既定失败被承认，而不是被重判定）。
    assert len(result.pending_practice['actor_history']) == 8
    # Actor 历史完整保留 8 条消息（蒸馏源转写的全部内容）。
    assert result.notes == 'private notes' and result.initial_evolution_memory == b.memory
    # 课程私有笔记从程序中被机械提取；进化初始记忆与盘上已提交记忆一致。
    assert not (b.ep / 'outcome.json').exists()
    # 关键：受理阶段绝不伪造 episode 终局 outcome.json。


@pytest.mark.parametrize('fault', ['unverified', 'memory', 'history', 'system', 'config',
    'advanced', 'quarantine', 'grading', 'readonly', 'budget', 'iterations', 'notes',
    'candidate', 'partial', 'hash', 'event'])
def test_refuses_unsafe_recovery(boundary, fault):
    """对证据链的 16 种篡改/不安全状态，受理必须逐一 fail-closed 拒绝。"""
    # 参数化故障注入清单：判定未验证、记忆被改、历史被换、系统提示被换、
    # 配置漂移、episode 被推进、被隔离、评分残留、蒸馏失败非基础设施类、
    # 墙钟预算耗尽、迭代预算不足、笔记程序被改、候选工件缺失、部分写入、
    # 输入哈希漂移、事件流已闭合。
    b = boundary
    if fault == 'unverified':
        save(b.ep / 'verifier/iter_01/verify2.json', dict(verdict='unverified', findings='VERDICT: UNVERIFIED'))
        # 故障 1：Verifier 判定变成 UNVERIFIED（未验证的失败不可恢复）。
    elif fault == 'memory':
        (b.cycle / 'memory/lesson.md').write_text('uncommitted edit')
        # 故障 2：已提交记忆被未提交的编辑污染。
    elif fault in ['history', 'system']:
        p = b.source / 'transcript.json';d = R.read(p)
        if fault == 'history':d['messages'][0]['content'] = 'replaced context'
        else:d['system'] = 'replacement system'
        save(p, d)
        # 故障 3/4：蒸馏源转写的首条消息或系统提示被替换。
    elif fault == 'config':b.cfg.max_iters += 1
        # 故障 5：配置与 manifest 记录的哈希漂移。
    elif fault == 'advanced':save(b.ep / 'outcome.json', {})
        # 故障 6：episode 已被推进出终局（不允许重复恢复）。
    elif fault == 'quarantine':save(b.ep / 'boundary_audit.json', dict(status='quarantined'))
        # 故障 7：边界审计把 episode 隔离了。
    elif fault == 'grading':save(b.root / 'grading.json', {})
        # 故障 8：出现评分残留（恢复与评分互斥）。
    elif fault == 'readonly':(b.source / 'iter_03/trace.txt').write_text('ordinary error')
        # 故障 9：末次蒸馏失败不是基础设施类（普通错误），证据不再"既定失败"。
    elif fault == 'budget':b.plan['prior_memory_budget']['wall_secs_conservative'] = b.cfg.wall_clock_secs
        # 故障 10：先验墙钟预算已耗尽（保守值 >= 总预算）。
    elif fault == 'iterations':b.plan['prior_memory_budget']['iterations'] = 2
        # 故障 11：先验迭代预算不足以覆盖剩余需求。
    elif fault == 'notes':b.program.write_text(b.program.read_text().replace('f.write(notes)', 'pass'))
        # 故障 12：课程私有笔记程序被改写（笔记提取点被破坏）。
    elif fault == 'candidate':(b.ep / 'candidate/publication_001/materials.tgz').unlink()
        # 故障 13：已保存候选工件缺失。
    elif fault == 'partial':b.plan['partial_guest_memory_written'] = True
        # 故障 14：guest 侧记忆已被部分写入（无法安全整量重放）。
    elif fault == 'hash':b.plan['input_sha256'] = {str(b.program): 'changed'}
        # 故障 15：计划的输入哈希与实际文件不符。
    elif fault == 'event':save(b.root / 'events.jsonl', dict(event='EVOLUTION_PROJECT_CLOSED'))
        # 故障 16：事件流已推进过 PROJECT_CLOSED（中断点不再是最末事件）。
    save(b.plan_path, b.plan)
    # 把（可能被改动的）计划落盘。
    with pytest.raises((PracticeInfrastructureError, KeyError)):
        admit(b)
        # 受理必须拒绝：基础设施异常或关键证据缺失（KeyError）。


def test_restore_replays_only_saved_candidate_and_committed_memory(boundary, monkeypatch):
    """恢复重放只推送 fixture 与已保存候选及已提交记忆，绝不重放 Actor。"""
    b = boundary;recovery = admit(b);calls = [];events = []
    # 受理后记录钩子调用序列与事件。
    monkeypatch.setattr(V, '_fresh_vm', lambda *a: calls.append('fresh'))
    # 伪 VM 创建钩子。
    monkeypatch.setattr(V, '_replay', lambda h, vm, path: calls.append(path))
    # 伪重放钩子：记录被重放的归档路径。
    monkeypatch.setattr(V, '_push_canonical_memory', lambda h, vm, path: calls.append(path))
    # 伪记忆推送钩子：记录被推送的记忆目录。
    value = R.restore_learning_candidate(hooks=object(), vm=object(), episode_dir=b.ep,
        fixture_dir=b.ep / 'fixtures', memory_dir=b.cycle / 'memory', target='target',
        emit=lambda event, **kw: events.append((event, kw)), saved=recovery.pending_practice)
    # 执行恢复重放：episode 目录、fixture 目录、周期记忆目录全部来自盘上证据。
    assert calls == ['fresh', str(b.ep / 'fixtures'), str(b.ep / 'candidate/publication_001'), str(b.cycle / 'memory')]
    # 调用序列固定：新建 VM → 推 fixture → 推已保存候选 → 推已提交记忆
    # （绝不触碰 Actor 转写或 Verifier 报告）。
    assert value['actor_history'] == R.read(b.source / 'transcript.json')['messages']
    # 恢复出的 Actor 历史逐字节等于蒸馏源转写。
    assert value['terminal_outcome'] == 'FAIL' and value['before_memory'] == b.memory
    # 终局 FAIL 与恢复前记忆原样带回。
    assert events[0][1]['payload']['actor_replayed'] is False
    # 事件明示 Actor 未被重放。
    assert not (b.ep / 'outcome.json').exists()
    # 重放本身也不伪造 episode 终局。


def test_resume_deducts_prior_budget_and_preserves_full_context(boundary):
    """续跑包装必须扣除先验预算、保留完整上下文，并落盘恢复回执。"""
    b = boundary;recovery = admit(b);calls = []
    # 受理后记录对原始尝试函数的调用。
    sink = SimpleNamespace(root=str(b.ep / 'memory_distillation/segment_001'))
    # 输出段：蒸馏源段的下一个 segment。
    def original(instruction, vm, cfg, sink, **kwargs):
        # 伪原始尝试函数（真正的记忆蒸馏调用）。
        calls.append((instruction, cfg, kwargs))
        # 记录指令、配置与关键字参数。
        return SimpleNamespace(status='done', turns=2), kwargs['initial_history']
        # 返回"完成 + 2 轮"与传入的历史。
    result, history = recovery.wrap_attempt(original)('original learning prompt', None, b.cfg, sink,
        initial_history=recovery.pending_practice['actor_history'], continue_context=True)
    # 经续跑包装调用：原始历史与继续上下文开关都传入。
    assert result.status == 'done' and len(history) == 8
    # 结果完成、历史仍是完整的 8 条（上下文无损）。
    assert calls[0][1].max_iters == 1997 and 0 < calls[0][1].wall_clock_secs <= 86370
    # 关键：预算被扣除——2000-3=1997 次迭代；墙钟扣除 30 秒（86400-30，
    # 上下界都断言以防伪造）。
    assert calls[0][2]['continue_context'] is True
    # 继续上下文开关原样下传。
    receipt = R.read(b.ep / 'memory_recovery_receipt.json')
    # 读取恢复回执。
    assert receipt['total_memory_iters'] == 5 and receipt['actor_work_replayed'] is False
    # 回执：记忆迭代总数 = 先验 3 + 本次 2；Actor 工作未被重放。


@pytest.mark.parametrize('fault', ['exhausted', 'dropped_context'])
def test_resume_refuses_exhausted_budget_or_lost_history(boundary, fault):
    """续跑在预算耗尽或历史丢失时必须拒绝，绝不调用 Actor。"""
    # 参数化：预算耗尽 / 历史被丢弃两种不安全续跑。
    b = boundary;recovery = admit(b)
    # 先受理。
    if fault == 'exhausted':recovery.pending_memory_learning['start_monotonic'] = time.monotonic() - 90000
        # 故障 1：把记忆学习的起始时钟拨回 25 小时前——墙钟预算已耗尽。
    history = [] if fault == 'dropped_context' else recovery.pending_practice['actor_history']
    # 故障 2：续跑历史为空（上下文丢失）。
    with pytest.raises(PracticeInfrastructureError):
        recovery.wrap_attempt(lambda *a, **kw: pytest.fail('must not call Actor'))(
            'prompt', None, b.cfg, SimpleNamespace(root=str(b.ep / 'memory_distillation/segment_001')),
            initial_history=history)
        # 续跑包装必须抛基础设施异常；伪 Actor 一旦被调用即测试失败。


def test_commit_once_before_next_curriculum_without_actor_or_verifier_replay(boundary, monkeypatch):
    """提交只发生一次且先于下一课程轮次：零 Actor/Verifier 重放、FAIL 原样入档。"""
    from config.settings import load
    # 导入配置加载（进程内无配置文件时返回默认值）。
    from explore import unified_evolution as U
    # 导入统一进化主循环：恢复边界如何嵌入重启后的进化流程。
    from explore.practice_evidence_recovery import run_practice_with_evidence
    # 导入证据恢复版练习执行（接替被 monkeypatch 的内部入口）。
    from test_unified_evolution import _VM, _hooks
    # 复用批次A测试里的伪 VM 与脚本化钩子工具。
    b = boundary;recovery = admit(b);calls = [];prompts = [];events = []
    # 受理并准备记录容器。
    vm = _VM();hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn control.'], prompts)
    # 伪 VM + 课程 Agent 脚本化回复：一轮课程后交还控制权。
    session = U.UnifiedCurriculumSession(history=recovery.history, notes=recovery.notes, turns=1)
    # 课程会话：从恢复状态接续（历史、笔记、轮次计数）。
    monkeypatch.setattr(V, '_fresh_vm', lambda *a: calls.append('restore'))
    # 记录恢复期 VM 创建。
    monkeypatch.setattr(V, '_replay', lambda *a: None)
    monkeypatch.setattr(V, '_push_canonical_memory', lambda *a: None)
    # 恢复重放阶段不做真实推送。
    monkeypatch.setattr(U, '_run_practice_attempt', run_practice_with_evidence)
    # 把内部练习入口接到证据恢复版实现（与生产路径一致）。
    def learning(**kw):
        # 伪学习晋升钩子：校验恢复上下文后返回"原样提交"。
        assert calls == ['restore'] and not prompts
        # 提交发生在任何课程提示词之前，且 VM 只恢复过一次。
        assert kw['actor_history'] == recovery.pending_practice['actor_history']
        # 学习输入的 Actor 历史就是恢复出的完整历史。
        assert kw['terminal_outcome'] == 'FAIL' and kw['experience_index'] == 1
        # 终局 FAIL、经验序号 1。
        calls.append('learn')
        # 记录学习调用。
        return b.memory, 'same Actor diagnosis', kw['actor_history']
        # 返回：记忆不变、诊断沿用 Actor 原文、历史原样。
    monkeypatch.setattr(U, '_promote_learning', learning)
    # 用伪学习晋升替换真实实现。
    result = U.evolve_until_ready(vm, str(b.cycle), 'target', recovery.trigger['verifier_report'],
        recovery.trigger['actor_learning_diagnosis'], b.memory, load(None), load(None), load(None), load(None),
        hooks=hooks, session=session, trigger_authority='phase2_outcome_protocol', triggering_outcome='PASS',
        resume_boundary=recovery, curriculum_memory_access='none',
        event_sink=lambda event, **kw: events.append((event, kw)))
    # 从恢复边界继续进化直到就绪：触发权威与 PASS 结果对齐触发记录，
    # 课程记忆访问为 none，事件进本地收集器。
    assert result.status == 'ready_for_retry', result.reason
    # 最终状态：就绪可重试（目标已 PASS，无需再练习）。
    assert result.projects == 1 and calls == ['restore', 'learn'] and session.turns == 2
    # 关键：只做了一次恢复 VM 创建 + 一次学习提交（零 Actor/Verifier 重播），
    # 完成第 1 个项目，课程轮次推进到 2。
    assert [event for event, fields in events].count('PROJECT_CLOSED') == 1
    # 恢复的项目恰好闭合一次（不重复入账）。
    assert 'practice project 1: FAIL' in prompts[0]
    # 课程提示词里如实记录"练习项目 1 失败"（失败教训进入课程上下文）。
    assert R.read(b.ep / 'outcome.json')['terminal_outcome'] == 'FAIL'
    # episode 终局此刻才落盘，且是 FAIL（提交时机：下一课程轮之前）。
    access = R.read(b.cycle / 'curriculum/turn_002/memory_access.json')
    assert access['access'] == 'none' and access['attached_files'] == access['attached_bytes'] == 0
    # 下一课程轮的记忆访问记录：none 权限被机械执行——附件文件数与字节数
    # 全为零（课程读不到 Actor 的私有记忆）。
