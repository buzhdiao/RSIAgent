# 【中文注释·模块定位】练习证据恢复（practice evidence recovery）契约测试：
# 证据请求绝不能被转换成判定、重置或提前学习（explore/practice_evidence_
# recovery.run_practice_with_evidence + explore/phase2_recovery 的受理 + 
# explore/unified_evolution 的续跑 + explore/practice_loop + core/verifier）。
# 场景：练习的 Agent 化 Verifier 未给出 PASS/FAIL，而是发出"需要可独立复现
# 证据"的 UNVERIFIED 证据请求——恢复路径必须在同一 Actor 历史/同一 Verifier
# 会话上续跑，扣减各自先验预算，直到拿到真实 FAIL/PASS 判定。被钉死的承诺：
# 1) 受理保留全部三个角色（课程笔记/Actor 历史/Verifier 独立历史）与触发
#    记忆，且盘上零写入（前后 sha 快照逐文件相等）；
# 2) 七种被篡改的练习检查点（记忆/日志归档/隔离/部分学习/判定/预算/笔记
#    程序）一律拒绝且零写入；
# 3) 挂起练习在下一课程轮之前完成并学习一次——零重放，早段 outcome 不变；
# 4) UNVERIFIED 续跑：同一 Actor + 同一 Verifier 会话，候选/交接原样保留，
#    不写 outcome/记忆蒸馏，UNVERIFIED 报告按发布序号归档；
# 5) 证据预算耗尽永远不能变成成功；Verifier 传输缺失（基础设施缺席）绝不
#    触发 Actor 重试；
# 6) 首个练习的受理保留目标记忆且课程记忆关闭（access=none），条件漂移与
#    部分学习被拒绝；笔记字节见证（trace 里的字节数）必须与字面笔记一致，
#    绝不执行程序本身；
# 7) 首个练习续跑在"记忆关闭"的课程轮之前恰好学习一次。
# 复用批次A的 fixture/_VM/_hooks 工具；全部不起真实 VM/LLM。
"""Evidence requests cannot be converted into verdicts, resets, or early learning."""
# 模块 docstring：证据请求绝不能被转换成判定、重置或提前学习。

import dataclasses
# 导入 dataclasses：replace 派生配置变体。
import io
# 导入 io：构造内存字节流写 tar 成员。
import json
# 导入 json：读写盘上 JSON 证据。
import tarfile
# 导入 tarfile：构造/破坏记忆日志归档（ep001.tgz）。
import shutil
# 导入 shutil：目录重命名/删除（重排 episode 编号）。
from pathlib import Path
# 导入 Path：盘上路径构造。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 Trace/结果等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import load
# 导入配置加载（None 参数=进程内默认配置）。
from explore import practice_loop as E, unified_evolution as U
# 导入练习循环与统一进化：原子记忆安装、异常类型与 evolve_until_ready。
from explore import practice_evidence_recovery as R
# 导入被测模块：UNVERIFIED 证据请求的续跑实现。
from explore.phase2_recovery import sha
# 导入 sha：文件哈希（用于"零写入"快照对账）。
from test_phase2_recovery import fixture, save, admit
# 复用批次A工具：Phase-2 恢复底稿 fixture、JSON 落盘、受理入口。
from test_unified_evolution import _VM, _hooks
# 复用批次A工具：伪 VM 与课程钩子脚本化回复。


def stopped_practice(tmp_path):
    """在 Phase-2 底稿上搭建"练习停在 UNVERIFIED 证据请求"的完整证据状态。"""
    # 核心状态：ep002 的 Verifier 判 UNVERIFIED（证据请求），课程已发布
    # turn_002 的项目与笔记，等待恢复续跑。
    f = fixture(tmp_path)
    # 取批次A的 Phase-2 恢复底稿（含 cycle/trigger/ep001/manifest/plan）。
    cfg = f.configs['curriculum']
    # 用课程配置充当其余三个角色（练习 Actor/Verifier/目标 Actor）。
    f.configs.update(practice_actor=cfg, practice_verifier=cfg, target_actor=cfg)
    manifest = json.loads((f.root/'manifest.json').read_text())
    for name in f.configs:
        manifest['configs'][name] = dict(manifest['configs']['curriculum'])
        # manifest 的 configs 段补齐四个角色（全部同配置）。
    save(f.root/'manifest.json', manifest)
    # 回写 manifest。
    E._atomic_install_memory(str(f.root/'active_memory'), f.memory)
    # 安装活动记忆（f.memory = 练习后的记忆树）。
    journal = f.root/'memory_journal/ep001.tgz'
    journal.parent.mkdir()
    with tarfile.open(journal, 'w:gz') as out:
        for name, data in f.active.items():
            item = tarfile.TarInfo(name); item.size = len(data)
            out.addfile(item, io.BytesIO(data))
            # 用 f.active 的字节内容打包真实 gz 归档（ep001 的记忆日志）。
    state = json.loads((f.cycle/'state.json').read_text())
    state.update(status='infra', projects=2,
                 reason='practice Agentic Verifier ended without PASS/FAIL: request')
    # 周期状态：因"Verifier 未给出判定（证据请求）"而基础设施停止，已完成 2 个项目。
    save(f.cycle/'state.json', state)
    # 回写状态。
    event={'event':'EVOLUTION_VERIFIER_PHASE_STARTED','payload':{
        'payload':{'project_index':2},'state':{'project_open':True,'memory_phase_open':False}}}
    with (f.root/'events.jsonl').open('a') as out:out.write(json.dumps(event)+'\n')
    # 事件流追加：Verifier 阶段开启，项目 2 打开、记忆阶段未开（中断点）。
    ep = f.cycle/'episodes/ep002'
    ep.mkdir()
    # 创建 ep002（挂起的练习 episode）。
    project='DECISION: PROJECT\nUpdate /home/user/evolution_project/document.txt from supplied fixtures.'
    (ep/'project.md').write_text(project)
    # ep002 的项目任务书（与课程发布程序的 handoff 一致）。
    publication=f.cycle/'curriculum/turn_002/segment_002/iter_01/program.py'
    publication.write_text('notes='+repr('exact latest notes')+'\nhandoff='+repr(project)+'\n')
    # 课程发布程序：notes 字面量 + handoff 决策（恢复时机械提取，不执行）。
    save(publication.parent/'trace_meta.json',{'exit_code':0})
    (publication.parent/'trace.txt').write_text('notes exists: True\n[exit 0]')
    # 程序执行痕迹：正常退出且笔记文件存在（笔记见证）。
    history=[{'role':'user','content':'same actor context'},
             {'role':'assistant','content':'{"done":true,"checks":[]}'}]
    save(ep/'actor/attempt_001/segment_000/transcript.json',{'messages':history})
    (ep/'actor/attempt_001/segment_000/turn.txt').write_text('previous reply')
    # Actor 段转写与最后一轮回复原文（续跑起点）。
    verification={'verdict':'unverified','findings':'Need independently reproducible evidence.\nVERDICT: UNVERIFIED',
                  'transcript':[{'role':'user','content':'independent context'},
                                {'role':'assistant','content':'independent probe'}]}
    save(ep/'verifier/iter_02/verify2.json',verification)
    (ep/'verifier/turn.txt').write_text('previous verifier reply')
    # Verifier 第二轮复核：判 unverified、findings 携带 UNVERIFIED 令牌、
    # 附独立会话转写（与 Actor 上下文隔离）。
    (ep/'candidate/publication_001').mkdir(parents=True)
    (ep/'fixtures').mkdir()
    # 候选发布目录与 fixture 目录（续跑期间必须原样保留）。
    plan=json.loads(f.plan.read_text())
    plan.update(recovery_kind='unverified_practice',
                curriculum_publication_program=str(publication),
                prior_role_budget={'prior_actor_iters':1,'prior_actor_wall_secs':10,
                                   'prior_verifier_iters':1,'prior_verifier_wall_secs':5},
                input_sha256={str(f.root/'manifest.json'):sha(f.root/'manifest.json')})
    # 恢复计划：类型 unverified_practice、指向发布程序、双角色先验预算
    # （Actor 1 迭代 10 秒 / Verifier 1 迭代 5 秒）、manifest 输入哈希。
    save(f.plan,plan)
    # 计划落盘。
    return f
    # 返回底稿命名空间。


def test_stopped_practice_admission_retains_all_three_roles_and_trigger_memory(tmp_path):
    """受理保留三角色状态与触发记忆，且全程零盘上写入。"""
    f=stopped_practice(tmp_path)
    # 搭建证据状态。
    before={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 受理前的全文件 sha 快照。
    b=admit(f)
    # 执行受理。
    assert b.projects==1 and b.completed_curriculum_turns==2
    # 挂起练习是第 1 个未完成项目，课程轮已到 2。
    assert b.notes=='exact latest notes'
    # 课程笔记从发布程序字面量中机械提取。
    assert b.pending_practice['actor_history'][0]['content']=='same actor context'
    # Actor 历史原样带入（首条内容对账）。
    assert b.pending_practice['verifier_history'][0]['content']=='independent context'
    # Verifier 独立历史也原样带入（第三角色）。
    assert b.initial_evolution_memory==f.active
    # 触发记忆 = 底稿的活动记忆树。
    assert E._read_memory_tree(str(f.root/'active_memory'))==f.memory
    # 盘上活动记忆未被受理过程改写。
    assert before=={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 关键：受理是纯读取——盘上每一个文件都逐字节未变。


@pytest.mark.parametrize('fault',['memory','journal','quarantine','partial_learning','verdict','budget','notes'])
def test_invalid_practice_checkpoint_is_rejected_without_writes(tmp_path,fault):
    """七种被篡改的练习检查点一律拒绝，且拒绝过程零盘上写入。"""
    # 参数化故障：记忆被改、日志归档损坏、被隔离、已有部分学习、判定变成
    # pass、Actor 预算耗尽、笔记程序被换。
    f=stopped_practice(tmp_path);ep=f.cycle/'episodes/ep002'
    # 先搭好合法底稿。
    if fault=='memory':(f.cycle/'memory/lesson.md').write_bytes(b'partial')
        # 故障 1：周期记忆被部分写入污染。
    elif fault=='journal':(f.root/'memory_journal/ep001.tgz').write_bytes(b'bad archive')
        # 故障 2：记忆日志归档损坏。
    elif fault=='quarantine':save(ep/'boundary_audit.json',{'status':'quarantined'})
        # 故障 3：episode 已被边界审计隔离。
    elif fault=='partial_learning':(ep/'memory_distillation').mkdir()
        # 故障 4：已出现部分学习产物（不允许半途学习）。
    elif fault=='verdict':save(ep/'verifier/iter_02/verify2.json',{'verdict':'pass'})
        # 故障 5：判定不是 unverified（证据请求前提被破坏）。
    elif fault=='budget':
        plan=json.loads(f.plan.read_text());plan['prior_role_budget']['prior_actor_iters']=cfg_limit=f.configs['practice_actor'].max_iters;save(f.plan,plan)
        # 故障 6：Actor 先验迭代数已等于配置上限（预算耗尽）。
    else:
        plan=json.loads(f.plan.read_text());Path(plan['curriculum_publication_program']).write_text('notes=untrusted_call()')
        # 故障 7：课程发布程序被换成不可信调用（笔记提取点被破坏）。
    before={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 注入后的快照。
    with pytest.raises((E.PracticeInfrastructureError,tarfile.ReadError)):
        admit(f)
        # 受理必须拒绝（基础设施异常或归档损坏异常）。
    assert before=={str(p):sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 关键：拒绝过程零写入（含坏的归档也不被改写）。


def test_pending_practice_finishes_and_learns_before_next_curriculum_without_replay(tmp_path,monkeypatch):
    """挂起练习在下一课程轮之前完成并学习一次，且零重放、早段不变。"""
    f=stopped_practice(tmp_path);boundary=admit(f);vm=_VM();prompts=[];calls=[]
    # 受理后准备伪 VM/钩子与记录容器。
    hooks=_hooks(vm,['DECISION: READY_FOR_TARGET\nReturn control.'],prompts)
    # 课程脚本化回复：一轮后交还控制权。
    session=U.UnifiedCurriculumSession(history=boundary.history,notes=boundary.notes,turns=2)
    # 课程会话：接续恢复状态（已完成 2 轮）。
    def practice(**kw):
        # 伪练习执行：校验恢复参数后返回 FAIL 终局。
        assert not prompts, 'Curriculum ran before resolving open practice'
        # 关键：课程绝不能先于挂起练习运行。
        assert kw['project_index']==2 and kw['resume_state'] is boundary.pending_practice
        # 续跑的是项目 2，恢复状态就是受理产物（身份断言）。
        calls.append('practice2')
        return {'terminal_outcome':'FAIL','verifier_report':'VERDICT: FAIL\nEvidence establishes a violation.',
                'actor_handoff':'STATUS: SUBMIT','actor_history':kw['resume_state']['actor_history'],
                'before_memory':f.memory}
        # 返回真实 FAIL 报告与原 Actor 历史。
    def learning(**kw):
        # 伪学习晋升：FAIL 如实进入学习环。
        assert calls==['practice2'] and kw['terminal_outcome']=='FAIL' and kw['experience_index']==2
        # 学习发生在练习之后；终局 FAIL、经验序号 2。
        assert kw['actor_history']==boundary.pending_practice['actor_history']
        # 学习输入是恢复出的 Actor 历史。
        calls.append('learn2');return f.memory,'same Actor diagnosis',kw['actor_history']
        # 记忆不变、诊断沿用、历史原样。
    monkeypatch.setattr(U,'_run_practice_attempt',practice)
    monkeypatch.setattr(U,'_promote_learning',learning)
    # 替换统一进化的两个内部入口。
    old=sha(f.cycle/'episodes/ep001/outcome.json')
    # 早段（ep001）outcome 的哈希基准。
    result=U.evolve_until_ready(vm,str(f.cycle),f.target,boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'],boundary.initial_evolution_memory,
        load(None),load(None),load(None),load(None),hooks=hooks,session=session,
        trigger_authority='phase2_outcome_protocol',triggering_outcome='PASS',resume_boundary=boundary)
    # 从恢复边界继续进化直到就绪。
    assert result.status=='ready_for_retry',result.reason
    # 最终就绪可重试。
    assert result.projects==2 and session.turns==3 and calls==['practice2','learn2']
    # 项目推进到 2、课程轮推进到 3；调用序列恰为"练习→学习"各一次。
    assert 'practice project 2: FAIL' in prompts[0] and 'exact latest notes' in prompts[0]
    # 课程提示词记录"练习项目 2 失败"，且携带恢复的课程笔记。
    assert sha(f.cycle/'episodes/ep001/outcome.json')==old
    # 早段 outcome 未被触碰（零重放、不重写历史）。


def evidence_setup(tmp_path,monkeypatch,reports):
    """搭建 run_practice_with_evidence 的完整参数：UNVERIFIED 证据请求续跑。"""
    # 核心断言基线：Actor 续跑同一私有历史且提示词带 UNVERIFIED 报告与
    # 有界传输；Verifier 会话隔离 Actor 上下文、预算扣除先验消耗。
    import core.verifier as verifier
    import config.settings as settings
    ep=tmp_path/'episodes/ep002';original=ep/'candidate/publication_001';original.mkdir(parents=True)
    (original/'artifact').write_bytes(b'original candidate');(ep/'actor_handoff.md').write_text('original handoff')
    # 候选工件与 Actor 交接（续跑期间必须原样保留）。
    cfg=dataclasses.replace(load(None),max_iters=100,wall_clock_secs=300)
    # Actor 配置：100 迭代 / 300 秒。
    control=dataclasses.replace(cfg,agentic_verifier_config='verifier-role',
        verifier_unverified_evidence=True,wall_clock_secs=120)
    # Agent 化 Verifier 控制配置：允许 UNVERIFIED 证据请求、120 秒墙钟。
    monkeypatch.setattr(settings,'load',lambda p:dataclasses.replace(cfg,wall_clock_secs=200))
    # 伪配置加载：任何角色读到 200 秒变体（模拟其他角色预算）。
    memory={'lesson.md':b'committed'};calls=[];sessions=[]
    # 已提交记忆、调用序列、Verifier 会话收集。
    monkeypatch.setattr(R.V,'_read_memory_tree',lambda p:dict(memory))
    # 伪记忆树读取：恒返回已提交记忆。
    for name in ('_fresh_vm','_replay','_push_canonical_memory','_require_owned_project_shape',
                 '_stage_original_practice_fixtures','_verify_candidate','_verify_original_practice_fixtures',
                 '_push_actor_execution_evidence','_verify_actor_execution_evidence'):
        monkeypatch.setattr(R.V,name,lambda *a,**kw:None)
        # 其余九个目标学习内部原语全部放行（本测试聚焦续跑/预算/会话）。
    monkeypatch.setattr(R.V,'_capture_owned_tree',lambda hooks,vm,label,path,target:Path(path).mkdir(parents=True))
    # 捕获自有树 = 创建目标目录。
    monkeypatch.setattr(R.V,'_build_actor_execution_evidence',lambda *a:{'manifest_sha256':'frozen'})
    # Actor 执行证据 = 冻结清单哈希占位。
    def actor(**kw):
        # 伪 Actor 交接段：校验续跑上下文后追加一轮问答。
        assert kw['history'][0]['content']=='original Actor private context'
        # 续跑历史以原始 Actor 私有上下文开头（不换历史）。
        assert 'VERDICT: UNVERIFIED' in kw['prompt'] and kw['continuation'] and kw['bounded_transport']
        # 提示词携带 UNVERIFIED 报告；续跑与有界传输开关开启。
        calls.append('actor');history=kw['history']+[{'role':'user','content':'requested evidence'},
                                                {'role':'assistant','content':'{"done":true,"checks":[]}'}]
        # 追加"请求证据"问答。
        return SimpleNamespace(text='STATUS: SUBMIT\nCandidate preserved.'),history,SimpleNamespace(turns=1,iters=1)
        # 返回交接文本、新历史、循环遥测。
    def verify(*args,**kw):
        # 伪 Agent 化 Verifier：校验会话隔离与预算后按脚本返回判定。
        assert kw['session'][0]['content']=='original Verifier independent context'
        # 会话以原始 Verifier 独立上下文开头。
        assert all('Actor private context' not in str(m) for m in kw['session'])
        # 关键：Verifier 会话里绝不能出现 Actor 私有上下文（隔离）。
        assert args[2].verifier_hide_actor_memory and 0 < kw['wall_budget'] <= 110
        # Verifier 配置隐藏 Actor 记忆；墙钟预算 = 120-10（先验）且为正。
        assert kw['iters_budget'] <= 96
        # 迭代预算 = 100-4（先验 Verifier 迭代）。
        calls.append('verify');sessions.append(kw['session']);kw['session'].inspections+=1
        # 记录调用、会话身份，并递增检查次数。
        v=next(reports);return v,('VERDICT: '+{'unverified':'UNVERIFIED','wrong':'FAIL','pass':'PASS'}[v])
        # 按脚本依次返回判定与对应终局令牌。
    monkeypatch.setattr(R.V,'_run_handoff_phase',actor);monkeypatch.setattr(verifier,'verify_agentic',verify)
    # 替换 Actor 交接段与 Agent 化 Verifier 入口。
    saved={'actor_history':[{'role':'user','content':'original Actor private context'},{'role':'assistant','content':'old'}],
           'verifier_history':[{'role':'user','content':'original Verifier independent context'},{'role':'assistant','content':'old'}],
           'findings':'VERDICT: UNVERIFIED\nPlease expose evidence.', 'candidate_dir':str(original),
           'publication':1,'inspections':1,'prior_actor_iters':3,'prior_actor_wall_secs':20,
           'prior_verifier_iters':4,'prior_verifier_wall_secs':10}
    # 恢复状态：双角色历史、UNVERIFIED 发现、候选目录、发布序号 1、已做 1 次
    # 检查、双角色先验预算（Actor 3 迭代 20 秒 / Verifier 4 迭代 10 秒）。
    kwargs=dict(hooks=SimpleNamespace(),vm=object(),lineage=tmp_path,episode_dir=ep,project_index=2,
        project='Update the supplied document.',fixture_dir=ep/'fixtures',target='Update the supplied document.',
        actor_cfg=cfg,verifier_cfg=cfg,memory_dir=tmp_path/'memory',emit=lambda *a,**kw:None,
        agentic_verifier_cfg=control,resume_state=saved)
    # 组装 run_practice_with_evidence 的全部关键字参数。
    return ep,kwargs,calls,sessions
    # 返回 episode 目录、参数、记录容器。


def test_unverified_resumes_same_actor_and_verifier_then_returns_real_fail(tmp_path,monkeypatch):
    """UNVERIFIED 续跑：同一 Actor/同一 Verifier 会话，最终返回真实 FAIL。"""
    ep,kw,calls,sessions=evidence_setup(tmp_path,monkeypatch,iter(['unverified','wrong']))
    # Verifier 脚本：再次 UNVERIFIED，然后给出 wrong（FAIL）。
    out=R.run_practice_with_evidence(**kw)
    # 执行证据恢复续跑。
    assert out['terminal_outcome']=='FAIL' and calls==['actor','verify','actor','verify']
    # 证据请求触发"Actor→Verifier"再走一轮；最终判定是真实 FAIL。
    assert sessions[0] is sessions[1]
    # 关键：两次检查共用同一 Verifier 会话（独立上下文持续，不重开）。
    assert (ep/'candidate/publication_001/artifact').read_bytes()==b'original candidate'
    assert (ep/'actor_handoff.md').read_text()=='original handoff'
    # 候选工件与交接在续跑全程原样保留（逐字节）。
    assert not (ep/'outcome.json').exists() and not (ep/'memory_distillation').exists()
    # 不写终局 outcome、不启动记忆蒸馏（判定未定之前学习不开始）。
    assert (ep/'unverified_report_002.md').read_text()=='VERDICT: UNVERIFIED'
    # 两次 UNVERIFIED 中第一次的发现按发布序号（002）归档留证。


def test_evidence_budget_exhaustion_never_becomes_success(tmp_path,monkeypatch):
    """证据预算耗尽永远不能变成成功（fail-closed，不写报告）。"""
    ep,kw,calls,_=evidence_setup(tmp_path,monkeypatch,iter(['unverified']))
    # Verifier 脚本：只返回 UNVERIFIED（请求证据）。
    kw['resume_state']['prior_actor_iters']=kw['actor_cfg'].max_iters-1
    # 注入：Actor 先验迭代已耗到上限-1（剩余预算不足续跑）。
    with pytest.raises(E.PracticeInfrastructureError,match='remaining budget exhausted'):
        R.run_practice_with_evidence(**kw)
        # 必须以"剩余预算耗尽"的基础设施异常停止。
    assert calls==['actor','verify'] and not (ep/'verifier_report.md').exists()
    # 恰好走了原有一轮（Actor→Verifier 各一次），且没有写 verifier_report.md
    # （绝不能把耗尽伪装成完成）。


def test_verifier_transport_absence_never_triggers_an_actor_retry(tmp_path,monkeypatch):
    """Verifier 传输缺失（基础设施缺席）绝不触发 Actor 重试。"""
    import core.verifier as verifier
    ep,kw,calls,_=evidence_setup(tmp_path,monkeypatch,iter([]))
    # Verifier 脚本为空（本测试单独替换 verify_agentic）。
    monkeypatch.setattr(verifier,'verify_agentic',lambda *a,**kw:('unverified','VM connection failed'))
    # 注入：Verifier 因"VM 连接失败"返回 UNVERIFIED（传输层缺席，不是证据请求）。
    with pytest.raises(E.PracticeInfrastructureError,match='infrastructure absence'):
        R.run_practice_with_evidence(**kw)
        # 必须识别为基础设施缺席并抛错（与真实证据请求区分）。
    assert calls==['actor'] and not (ep/'verifier_report.md').exists()
    # Actor 不被重试；不写报告。


def stopped_first_practice(tmp_path):
    """把 stopped_practice 重排成"第一个练习"场景（ep001/turn_001，零既往）。"""
    f = stopped_practice(tmp_path)
    # 先搭"第二个练习"底稿。
    shutil.rmtree(f.cycle / 'episodes/ep001')
    (f.cycle / 'episodes/ep002').rename(f.cycle / 'episodes/ep001')
    ep = f.cycle / 'episodes/ep001'
    # 删掉已完成的 ep001，把挂起的 ep002 重命名为 ep001。
    (ep / 'verifier/iter_02').rename(ep / 'verifier/iter_01')
    # Verifier 复核轮次同步改号（iter_02 → iter_01）。
    shutil.rmtree(f.cycle / 'curriculum/turn_001')
    (f.cycle / 'curriculum/turn_002').rename(f.cycle / 'curriculum/turn_001')
    # 删掉旧课程轮，turn_002 改号为 turn_001。
    E._atomic_install_memory(str(f.root / 'active_memory'), f.active)
    E._atomic_install_memory(str(f.cycle / 'memory'), f.active)
    # 活动记忆与周期记忆都安装为 f.active（目标记忆）。
    state = json.loads((f.cycle / 'state.json').read_text())
    state.update(projects=1, learning_experiences=0, memory_manifest=E._manifest(f.active),
                 memory_tree_sha256=R.V._memory_tree_sha256(f.active))
    # 周期状态：1 个项目打开、0 条学习经历、记忆清单/哈希与目标记忆一致。
    save(f.cycle / 'state.json', state)
    (f.cycle / 'trigger/outcome.md').write_text('Same target Actor diagnosis and verifier report.')
    # 触发 markdown 原文。
    event = {'event': 'EVOLUTION_VERIFIER_PHASE_STARTED', 'payload': {
        'payload': {'project_index': 1}, 'state': {'project_open': True, 'memory_phase_open': False}}}
    (f.root / 'events.jsonl').write_text(json.dumps(event) + '\n')
    # 事件流重写为单一中断点：项目 1 的 Verifier 阶段开启。
    manifest = json.loads((f.root / 'manifest.json').read_text())
    manifest['protocol'] = {'curriculum_memory_access': 'none'}
    save(f.root / 'manifest.json', manifest)
    # 协议承诺：课程记忆访问 none（首个练习阶段课程读不到 Actor 记忆）。
    plan = json.loads(f.plan.read_text())
    plan.update(completed_projects=0, curriculum_memory_access='none',
                source_transcript=plan['source_transcript'].replace('turn_002', 'turn_001'),
                curriculum_publication_program=plan['curriculum_publication_program'].replace('turn_002', 'turn_001'),
                input_sha256={str(f.root / 'manifest.json'): sha(f.root / 'manifest.json')})
    # 计划改写：0 个已完成项目、记忆访问 none、两处 turn_002 路径改号、
    # manifest 输入哈希刷新。
    save(f.plan, plan)
    return f


def test_first_practice_admission_preserves_target_memory_and_memory_off(tmp_path):
    """首个练习受理：保留目标记忆、课程记忆关闭，validate_evolution 通过且零写入。"""
    f = stopped_first_practice(tmp_path)
    before = {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 受理前快照。
    b = admit(f)
    # 受理。
    assert b.projects == 0 and b.completed_curriculum_turns == 1 and b.project_records == []
    # 零已完成项目、课程轮 1、无既往项目记录。
    assert b.initial_evolution_memory == f.active
    # 进化初始记忆 = 目标记忆（首个练习没有练习前记忆）。
    assert b.pending_practice['actor_history'][0]['content'] == 'same actor context'
    assert b.pending_practice['verifier_history'][0]['content'] == 'independent context'
    # Actor/Verifier 双历史原样带入。
    b.validate_evolution(f.cycle, f.target, f.active, b.trigger['verifier_report'],
                         b.trigger['actor_learning_diagnosis'], 'phase2_outcome_protocol', 'PASS')
    # 触发对账：周期/目标/记忆/报告/诊断/权威/PASS 全部自洽（不抛错）。
    assert before == {str(p): sha(p) for p in tmp_path.rglob('*') if p.is_file()}
    # 关键：受理零盘上写入。


@pytest.mark.parametrize('fault', ['access', 'count', 'early_learning', 'memory', 'notes_size'])
def test_first_practice_rejects_condition_drift_and_partial_learning(tmp_path, fault):
    """首练习的条件漂移与部分学习一律拒绝。"""
    # 参数化故障：记忆访问漂移（read_only）、完成计数漂移、提前学习残留、
    # 记忆被未提交编辑污染、笔记字节见证不匹配。
    f = stopped_first_practice(tmp_path)
    plan = json.loads(f.plan.read_text())
    if fault == 'access':
        plan['curriculum_memory_access'] = 'read_only'
        # 故障 1：计划的记忆访问与 manifest 的 none 漂移。
    elif fault == 'count':
        plan['completed_projects'] = 1
        # 故障 2：完成项目计数与状态文件漂移。
    elif fault == 'early_learning':
        save(f.cycle / 'episodes/ep001/outcome.json', {'terminal_outcome': 'PASS'})
        # 故障 3：首个练习就存在终局 outcome（提前学习残留）。
    elif fault == 'memory':
        (f.cycle / 'memory/lesson.md').write_text('uncommitted')
        # 故障 4：周期记忆被未提交编辑污染。
    else:
        publication = Path(plan['curriculum_publication_program'])
        publication.write_text(publication.read_text() + "open('/home/user/curriculum_notes.md', 'w').write(notes)\n")
        (publication.parent / 'trace.txt').write_text('curriculum_notes.md: 1 bytes\n[exit 0]')
        # 故障 5：程序含笔记写入语句但 trace 见证 1 字节（与实际 18 字节笔记
        # 不符——字节见证被篡改）。
    save(f.plan, plan)
    with pytest.raises(E.PracticeInfrastructureError):
        admit(f)
        # 受理必须逐一拒绝。


def test_literal_notes_byte_witness_is_accepted_without_executing_program(tmp_path):
    """笔记字节见证一致时受理通过，且绝不执行程序本身。"""
    f = stopped_first_practice(tmp_path)
    plan = json.loads(f.plan.read_text())
    publication = Path(plan['curriculum_publication_program'])
    publication.write_text(publication.read_text() + "open('/home/user/curriculum_notes.md', 'w').write(notes)\n")
    (publication.parent / 'trace.txt').write_text('curriculum_notes.md: 18 bytes\n[exit 0]')
    # 程序含笔记写入语句，trace 见证 18 字节 = len('exact latest notes')（一致）。
    assert admit(f).notes == 'exact latest notes'
    # 受理通过且笔记从程序字面量机械提取（程序从未被执行——执行会真写盘，
    # 而受理是零写入的）。


def test_first_practice_resume_learns_once_before_memory_off_curriculum(tmp_path, monkeypatch):
    """首练习续跑在记忆关闭的课程轮之前恰好学习一次。"""
    f = stopped_first_practice(tmp_path); boundary = admit(f); vm = _VM(); prompts = []; calls = []
    # 受理并准备伪 VM/钩子。
    hooks = _hooks(vm, ['DECISION: READY_FOR_TARGET\nReturn control.'], prompts)
    # 课程脚本化回复：一轮后交还。
    session = U.UnifiedCurriculumSession(history=boundary.history, notes=boundary.notes, turns=1)
    # 课程会话：从轮次 1 接续。
    def practice(**kw):
        # 伪练习执行：校验恢复参数后返回 FAIL 终局。
        assert not prompts and kw['project_index'] == 1 and kw['resume_state'] is boundary.pending_practice
        # 课程未先运行；续跑项目 1、恢复状态即受理产物。
        calls.append('practice1')
        return dict(terminal_outcome='FAIL', verifier_report='VERDICT: FAIL\nIndependent finding.',
                    actor_handoff='STATUS: SUBMIT', actor_history=kw['resume_state']['actor_history'],
                    before_memory=f.active)
        # 返回真实 FAIL 报告与原历史。
    def learning(**kw):
        # 伪学习晋升：FAIL 如实入学习环。
        assert calls == ['practice1'] and kw['experience_index'] == 1 and kw['terminal_outcome'] == 'FAIL'
        # 学习发生在练习后；经验序号 1、终局 FAIL。
        calls.append('learn1'); return f.active, 'same Actor diagnosis', kw['actor_history']
        # 记忆不变、诊断沿用、历史原样。
    monkeypatch.setattr(U, '_run_practice_attempt', practice)
    monkeypatch.setattr(U, '_promote_learning', learning)
    # 替换统一进化内部入口。
    result = U.evolve_until_ready(vm, str(f.cycle), f.target, boundary.trigger['verifier_report'],
        boundary.trigger['actor_learning_diagnosis'], boundary.initial_evolution_memory,
        load(None), load(None), load(None), load(None), hooks=hooks, session=session,
        trigger_authority='phase2_outcome_protocol', triggering_outcome='PASS',
        resume_boundary=boundary, curriculum_memory_access='none')
    # 从恢复边界续跑：课程记忆访问 none。
    assert result.status == 'ready_for_retry', result.reason
    # 最终就绪可重试。
    assert result.projects == 1 and session.turns == 2 and calls == ['practice1', 'learn1']
    # 项目推进到 1、课程轮推进到 2；调用序列恰为"练习→学习"各一次。
    metadata = json.loads((f.cycle / 'curriculum/turn_002/memory_access.json').read_text())
    assert metadata['access'] == 'none' and metadata['attached_files'] == metadata['attached_bytes'] == 0
    # 下一课程轮的记忆访问记录：none 被机械执行（附件文件数与字节数全零）。
    assert 'practice project 1: FAIL' in prompts[0]
    # 课程提示词如实记录"练习项目 1 失败"。
