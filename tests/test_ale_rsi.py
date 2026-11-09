# 【中文注释·模块定位】ALE 基准与 RSIAgent 探索层的交界契约测试：阶段转换、
# 记忆边界与有界波次 VM 所有权（benchmarks/ale 的 protocol/windows/practice/
# learning/report/host/lifecycle/capacity + explore.phase1_wave 并行进化 +
# explore.practice_loop/explore.commit/explore.target_learning 的共享原语）。
# 被钉死的承诺：
# 1) 公开批处理恰好覆盖 67 个钉死任务各一次，且 select_tasks 恒等返回；
# 2) 冻结记忆检测任何字节变化（fail-closed）并拒绝符号链接逃逸；
# 3) Windows 宿主路径映射保持 guest 原生任务路径不动、幂等；
# 4) 审计只授权目标任务的文本，其他任务/私有来源（GitHub URL）一律命中；
# 5) 停放已验证分支（park）：VM 超容量上限时 Actor 上下文不丢——先释放 VM
#    再串行提交，候选重放 + 记忆提交按自著顺序进行，峰值并发受池约束；
# 6) ALE Phase-2 保留 Actor 学习产物但每次循环拿全新 Verifier 会话，
#    FAIL/PASS 触发的进化复核共用同一 session，输入记忆树不被污染；
# 7) 报告层拒绝同任务双 RSI 分数与非有限/越界分数；
# 8) 冻结源码完整且不含 .env 凭据（指纹对齐）；
# 9) 重置服务只接受自有 guest 的并发请求并清理套接字；
# 10) 容量核算把已附着的容器名只计一次；
# 11) 审计能解码转写里的多行字符串（换行打断 shingle 也逃不掉）；
# 12) 宿主记忆/指令协议与 RSIAgent 主实现逐字节一致（sha256/manifest/shingle）。
"""ALE phase transitions, memory boundaries, and bounded wave VM ownership."""
# 模块 docstring：ALE 阶段转换、记忆边界与有界波次 VM 所有权。

import asyncio
# 导入 asyncio：驱动 ResetService/Capacity 的异步服务。
import json
# 导入 json：构造语料/转写/分数文件与读取租约。
from pathlib import Path
# 导入 Path：盘上路径构造。
import queue
# 导入 queue：VM 槽位池（模拟容量约束）。
import threading
# 导入 threading：并发守卫与峰值并发统计。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 VM/容器/池等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from benchmarks.ale.protocol import protocol, memory_record, verify_memory
# 导入 ALE 协议层：任务锁协议、记忆记录与冻结校验（本文件多处复用）。


def test_public_batch_covers_each_pinned_task_once():
    """公开批处理恰好覆盖 67 个钉死任务各一次，select_tasks 恒等返回。"""
    from benchmarks.ale.protocol import select_tasks
    # 导入任务选择函数（公开批处理没有留出划分）。
    lock = protocol()
    # 加载任务锁协议（钉死的任务清单与元数据）。
    assert len(lock['tasks']) == len(set(lock['tasks'])) == 67
    # 任务总数 67 且无重复（每个任务恰好出现一次）。
    assert select_tasks(lock) == lock['tasks']
    # 公开批处理选择 = 全部任务（顺序保持）。
    assert not {'assignments', 'existing_baseline_tasks'} & lock.keys()
    # 协议里没有波次指派表与既有基线任务键（公开批处理无这些概念）。


def test_frozen_memory_detects_changes_and_symlinks(tmp_path):
    """冻结记忆检测任何字节变化，并拒绝符号链接逃逸（fail-closed）。"""
    bank = tmp_path / 'memory'; bank.mkdir()
    # 记忆库目录。
    (bank / 'lesson.md').write_bytes(b'original')
    # 写入原始记忆字节。
    record = memory_record(bank)
    # 生成记忆记录（树哈希 + 文件清单）。
    verify_memory(record)
    # 校验通过：记录与盘上内容一致。
    (bank / 'lesson.md').write_bytes(b'changed')
    # 篡改记忆字节。
    with pytest.raises(RuntimeError, match='Frozen memory changed'):
        verify_memory(record)
        # 校验必须检测到变化并抛错。
    (bank / 'lesson.md').unlink()
    (bank / 'lesson.md').symlink_to(tmp_path / 'outside')
    # 把记忆文件替换成指向库外目标的符号链接。
    with pytest.raises(RuntimeError, match='symlink'):
        memory_record(bank)
        # 记录生成必须拒绝符号链接（防记忆边界逃逸）。


def test_windows_host_paths_leave_native_task_paths_intact():
    """Windows 宿主路径映射：guest 原生任务路径保持不动且映射幂等。"""
    from benchmarks.ale.windows import WindowsVM
    # 导入 Windows 宿主 VM 适配层。
    vm = object.__new__(WindowsVM); vm.home = 'C:/Users/User'
    # 绕过 __init__ 直接实例化并设置宿主家目录。
    assert vm.host_path('/home/user/.memory/ü.md') == 'C:/Users/User/.memory/ü.md'
    # 记忆路径映射到宿主家目录下（Unicode 文件名保留）。
    assert vm.host_path('/tmp/proj.tgz') == 'C:/Users/User/.ale/tmp/proj.tgz'
    # /tmp 映射到 .ale 暂存区。
    assert vm.host_path(vm.host_path('/tmp/proj.tgz')) == 'C:/Users/User/.ale/tmp/proj.tgz'
    # 幂等：已映射的宿主路径再次映射不变。
    assert vm.host_path('E:/agenthle/task/input.csv') == 'E:/agenthle/task/input.csv'
    # 宿主原生路径原样返回（guest 任务数据在别的盘也不改写）。


def test_audit_authorizes_target_but_rejects_other_task_and_private_source(tmp_path):
    """审计只授权目标任务文本；其他任务与私有来源（GitHub URL）一律命中。"""
    from explore.commit import _shingles
    # 导入 shingle 分词器（与主实现共享的相似度原语）。
    from benchmarks.ale.practice import Audit
    # 导入 ALE 练习审计器。
    target = 'Create one spreadsheet that sums all rows from the supplied financial table.'
    # 被授权的目标任务指令。
    other = 'Segment every individual cell in the provided microscope image series.'
    # 另一个（未授权）任务的指令。
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps(sorted(_shingles(target) | _shingles(other))))
    # 语料 = 两个任务 shingle 的并集（模拟"语料里见过"的内容库）。
    audit = Audit(corpus)
    # 构造审计器。
    assert audit.text(target, authorized_instruction=target) == []
    # 目标任务文本对授权指令：零命中（放行）。
    assert audit.text(other, authorized_instruction=target)
    # 其他任务文本对授权指令：命中（不该出现的任务内容）。
    assert audit.text('https://github.com/rdi-berkeley/agents-last-exam/tree/main/tasks')
    # 基准仓库私有来源 URL：命中（禁止泄漏基准出处）。


def test_parked_wave_can_exceed_vm_capacity_without_losing_actor_context(tmp_path, monkeypatch):
    """停放已验证分支：VM 超容量时上下文不丢，提交按自著顺序串行完成。"""
    from explore import phase1_wave as wave
    # 导入 Phase-1 并行波次进化主实现。
    from explore.practice_loop import PracticeHooks, _atomic_install_memory, _manifest, _read_memory_tree
    # 导入练习钩子与记忆原子安装/读取原语。
    slots = queue.Queue(); slots.put(0); slots.put(1)
    # 两个 VM 槽位（模拟容量 = 2 的池）。
    live = set(); guard = threading.Lock(); peak = 0
    # 在用槽位集合、互斥锁与峰值并发记录。
    histories = {}; committed = []; replayed = []
    # 各分支的 Actor 历史、已提交分支序号、已重放候选路径。
    def factory():
        # 伪 VM 工厂：从槽位池取号，记录峰值并发，关闭时归还槽位。
        nonlocal peak
        slot = slots.get(timeout=2)
        # 取槽位（容量 2，超出会阻塞/超时）。
        with guard:
            assert slot not in live
            # 同一槽位绝不被两个活分支同时持有。
            live.add(slot); peak = max(peak, len(live))
            # 登记在用并更新峰值。
        def close():
            with guard:
                live.remove(slot)
            slots.put(slot)
            # 关闭 = 归还槽位。
        return SimpleNamespace(close=close), SimpleNamespace(slot=slot)
        # 返回（desktop, vm）形状的双对象。
    decision = wave.WaveDecision('WAVE', 'independent practice', tuple(
        wave.WaveProject(f'p{i}', '/home/user/evolution_project') for i in range(5)), '{}')
    # 课程决策：WAVE + 5 个独立练习项目（固定指令路径）。
    monkeypatch.setattr(wave, '_author_wave', lambda **kw: (decision, []))
    # 伪起草：直接给出上述决策。
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *args: None)
    # 跳过规范记忆推送。
    monkeypatch.setattr(wave, '_capture_wave_fixtures', lambda **kw: None)
    # 跳过波次夹具捕获。
    monkeypatch.setattr(wave, '_replay', lambda hooks, vm, path: replayed.append(path))
    # 伪重放：记录被重放的候选路径。
    def execute(**kw):
        # 伪分支执行：拿 VM、留私有上下文，返回已验证分支。
        desktop, vm = kw['vm_factory']()
        i = kw['project_index']
        history = [{'role': 'assistant', 'content': f'branch-{i}-private-context'}]
        histories[i] = history
        return wave._BranchRuntime(i, kw['project'], kw['episode_dir'], desktop, vm,
            history, 'STATUS: SUBMIT', 'VERDICT: PASS', 'PASS', {})
    monkeypatch.setattr(wave, '_execute_branch', execute)
    # 替换真实分支执行（每个分支都"已验证 PASS"）。
    def commit(**kw):
        # 伪记忆提交：断言停放分支上下文仍在，随后原子安装新记忆。
        branch = kw['branch']
        assert branch.actor_history is histories[branch.project_index]
        # 关键：分支对象携带的正是执行期的同一份 Actor 上下文（身份断言）。
        assert branch.vm is not None and branch.desktop is not None
        # 提交前已为停放分支恢复了 VM。
        before = _read_memory_tree(str(kw['canonical_memory_dir']))
        after = {**before, f'{branch.project_index}.md': b'Actor-owned lesson'}
        # 新记忆 = 波前记忆 + 本分支的课程（Actor 自有课）。
        _atomic_install_memory(str(kw['canonical_memory_dir']), after)
        # 原子安装进规范记忆目录。
        committed.append(branch.project_index)
        # 记录提交顺序。
        return {'before': _manifest(before), 'after': _manifest(after), 'changes': {}}
        # 返回提交记录形状。
    monkeypatch.setattr(wave, '_commit_branch_memory', commit)
    # 替换真实记忆提交。
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction='public target',
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=factory, corpus_path='unused', project_budget=1, max_parallel=2,
        hooks=PracticeHooks(validate_corpus=lambda p: {}, audit_text=lambda *a, **k: []),
        park_verified_branches=True)
    # 执行并行 Phase-1：容量 2、预算 1 波、开启分支停放。
    assert result.status == 'budget_exhausted' and result.projects == 5
    # 以预算耗尽收尾，5 个项目全部完成。
    assert committed == [1, 2, 3, 4, 5]
    # 提交严格按自著（assignments 1 基）顺序进行。
    assert len(replayed) == 5 and peak <= 2 and not live
    # 5 个停放分支提交前各重放一次候选；峰值并发不超过池容量；结束时槽位全部归还。
    assert len(_read_memory_tree(result.memory_dir)) == 5
    # 规范记忆最终含 5 条分支课程。


def test_ale_phase2_preserves_actor_learning_and_fresh_verifier_sessions(tmp_path, monkeypatch):
    """ALE Phase-2：保留 Actor 学习产物，每次循环全新 Verifier 会话。"""
    from benchmarks.ale import learning
    # 导入 ALE 的 Phase-2 学习编排层。
    from core import loop
    # 导入主循环：LoopResult 与 run_with_resume 入口。
    from explore import target_learning, unified_evolution
    # 导入目标学习与统一进化（被 monkeypatch 的晋升/进化入口）。
    from explore.practice_loop import PracticeHooks, _atomic_install_memory, _read_memory_tree
    # 导入钩子与记忆原语。
    initial = tmp_path / 'initial'
    _atomic_install_memory(str(initial), {'seed.md': b'phase1'})
    # Phase-1 传入的初始记忆（一颗种子文件）。
    work = tmp_path / 'phase2'; work.mkdir()
    # Phase-2 工作目录。
    pools = []; session_ids = []; history_ids = []; learned_ids = []; reviews = []
    # 记录：池实例、各轮 Verifier 会话、各轮 Actor 历史、学习输入历史、进化复核。
    class Pool:
        """记录获取次数与关闭状态的伪干净沙箱池。"""
        def __init__(self, sandboxes, practice, reset_endpoint):
            self.practice = practice; self.acquisitions = 0; self.closed = False
            pools.append(self)
            # 登记池实例供断言。
        def acquire(self):
            self.acquisitions += 1
            return SimpleNamespace(close=lambda: None), SimpleNamespace(visible_roots=())
            # 每次获取返回一个可关闭的沙箱与空可见根。
        def close(self): self.closed = True
        # 关闭标记。
    monkeypatch.setattr(learning, 'CleanPool', Pool)
    # 用伪池替换真实沙箱池。
    monkeypatch.setattr(learning, 'hooks_for', lambda *a: PracticeHooks(push_memory=lambda *a: True))
    # 钩子：推送记忆恒真。
    def actor(direction, vm, cfg, sink, **kw):
        # 伪 Actor 循环：第一轮 FAIL（触发进化），第二轮 PASS（终局）。
        session_ids.append(kw['verifier_sessions'])
        # 记录本轮收到的 Verifier 会话对象身份。
        history = [{'role': 'user', 'content': 'task'}, {'role': 'assistant', 'content': 'candidate'}]
        history_ids.append(history)
        kw['runtime_state']['active_history'] = history
        # 把本轮历史写入运行状态（断点续跑协议）。
        if len(history_ids) == 1:
            return loop.LoopResult(status='evolve', verifier_route='EVOLVE', verifier_report='VERDICT: FAIL\nMissing rows'), history
            # 第一轮：进化路线 + FAIL 报告。
        return loop.LoopResult(status='done', verifier_route='HANDOFF', verifier_report='VERDICT: PASS\nAll rows checked'), history
        # 第二轮：交接路线 + PASS 报告。
    monkeypatch.setattr(loop, 'run_with_resume', actor)
    # 替换真实可续跑循环。
    def learn(**kw):
        # 伪学习晋升：以终局文本生成新课程并安装进记忆。
        learned_ids.append(kw['actor_history'])
        memory = {**kw['before_memory'], f'learn{len(learned_ids)}.md': kw['terminal_outcome'].encode()}
        _atomic_install_memory(str(kw['memory_dir']), memory)
        return memory, 'Improve row coverage', kw['actor_history']
        # 返回（新记忆, 诊断, 原历史）。
    monkeypatch.setattr(target_learning, '_promote_learning', learn)
    # 替换真实学习晋升。
    def evolve(vm, root, direction, report, diagnosis, memory, *configs, **kw):
        # 伪统一进化：记录触发的终局与会话身份，把进化记忆落到目标记忆目录。
        reviews.append((kw['triggering_outcome'], kw['session']))
        destination = Path(root) / 'memory'
        _atomic_install_memory(str(destination), memory)
        return SimpleNamespace(status='ready_for_retry', memory_dir=str(destination),
            projects=1 if len(reviews) == 1 else 0, reason='Ready')
        # 第一次复核做 1 个项目，第二次直接就绪。
    monkeypatch.setattr(unified_evolution, 'evolve_until_ready', evolve)
    # 替换真实进化入口。
    result = learning.phase2({'work_dir': str(work), 'input_memory': memory_record(initial),
        'sandbox': {}, 'practice_sandboxes': [{}], 'corpus': 'unused', 'instruction': 'public task', 'reset_endpoint': 'fake'})
    # 执行 ALE Phase-2。
    assert result['transition_ready'] and result['official_evaluator_calls'] == 0
    # 就绪可进入 Phase-3，且全程零官方评分调用。
    assert result['target_cycles'] == 2 and result['learning_updates'] == 2
    # 两个目标周期、两次学习更新。
    assert session_ids[0] is not session_ids[1]
    # 关键：两轮拿到的是不同的（全新）Verifier 会话——验证器无状态残留。
    assert learned_ids[0] is history_ids[0] and learned_ids[1] is history_ids[1]
    # 学习输入正是各轮的 Actor 历史（身份断言，不是拷贝）。
    assert reviews[0][0] == 'FAIL' and reviews[1][0] == 'PASS'
    # 第一次复核由 FAIL 触发、第二次由 PASS 触发。
    assert reviews[0][1] is reviews[1][1]
    # 两次进化复核共用同一 Verifier 会话（周期内会话持续）。
    assert _read_memory_tree(str(initial)) == {'seed.md': b'phase1'}
    # 输入记忆目录零污染（Phase-2 不改写来源）。
    assert len(_read_memory_tree(result['memory']['path'])) == 3
    # 最终记忆 = 种子 + 两条学习课程。
    assert all(p.closed for p in pools)
    # 沙箱池全部关闭（资源回收）。


def test_report_rejects_two_rsi_scores_for_the_same_task(tmp_path):
    """报告收集拒绝同一任务出现两个 RSI 分数（重复即腐败）。"""
    from benchmarks.ale.report import collect
    # 导入报告收集函数。
    task = protocol()['tasks'][0]
    # 取第一个钉死任务。
    for run, score in [('one', 0.25), ('two', 0.75)]:
        path = tmp_path / run / task / 'phase3/result.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'score': score}))
        # 两个 run 各给同一任务写一个 Phase-3 分数。
    with pytest.raises(RuntimeError, match='Duplicate rsi'):
        collect([tmp_path / 'one', tmp_path / 'two'])
        # 收集必须报"重复 RSI 分数"。


@pytest.mark.parametrize('score', [float('nan'), float('inf'), -0.1, 1.1, True])
def test_report_rejects_invalid_official_scores(score):
    """官方分数校验拒绝 NaN/inf/越界/布尔等非法值（fail-closed）。"""
    from benchmarks.ale.report import valid_score
    # 导入分数校验函数。
    with pytest.raises(ValueError, match='finite number'):
        valid_score(score)
        # 每种非法值都必须抛"必须是有限数"错误。


def test_frozen_source_is_complete_and_omits_credentials(tmp_path):
    """冻结源码完整且不含凭据；指纹能检测任何内容变化。"""
    from benchmarks.ale.host import freeze_source
    # 导入源码冻结函数。
    from benchmarks.ale.protocol import source_fingerprint
    # 导入源码指纹函数。
    release = tmp_path / 'release'
    freeze_source(release)
    # 冻结当前仓库源码到发布目录。
    assert not (release / '.env').exists()
    # 凭据文件绝不能进发布。
    assert source_fingerprint(release) == source_fingerprint()
    # 发布指纹与当前源码指纹一致（冻结完整无缺）。
    (release / 'run_ale.py').write_text('changed launcher')
    # 篡改启动器。
    assert source_fingerprint(release) != source_fingerprint()
    # 指纹立刻失配（任何内容变化都可检测）。


def test_reset_service_handles_independent_guests_and_rejects_foreign_ids():
    """重置服务并发处理自有 guest，拒绝外来 ID，并清理套接字文件。"""
    from benchmarks.ale.lifecycle import ResetService, request_reset
    # 导入重置服务与客户端请求函数。
    async def exercise():
        # 异步驱动：模拟两个自有 guest 的并发重置 + 一个外来 ID。
        owned = {'first', 'second'}
        async def reset(sandbox_id):
            if sandbox_id not in owned:
                raise RuntimeError('Not an owned guest')
            owned.remove(sandbox_id)
            await asyncio.sleep(0.01)
            return {'id': sandbox_id + '-fresh'}
            # 伪底层重置：仅认自有 ID，返回全新沙箱 ID。
        service = ResetService(reset)
        # 启动重置服务。
        try:
            replies = await asyncio.gather(*(asyncio.to_thread(request_reset, service.path, name)
                                              for name in ('first', 'second')))
            # 两个线程并发请求重置。
            assert replies == [{'id': 'first-fresh'}, {'id': 'second-fresh'}]
            # 各自拿到独立的全新 guest。
            with pytest.raises(RuntimeError, match='Not an owned guest'):
                await asyncio.to_thread(request_reset, service.path, 'foreign')
                # 外来 ID 必须被拒绝。
        finally:
            await service.close()
            # 无论成败都关闭服务。
        assert not Path(service.path).exists()
        # 套接字文件被清理。
    asyncio.run(exercise())
    # 运行异步场景。


def test_capacity_counts_attached_container_names_once(tmp_path, monkeypatch):
    """容量核算把已附着的容器名只计一次（租约跨进程共享）。"""
    import docker
    import shutil
    from benchmarks.ale.capacity import Capacity
    # 导入容量管理器与 docker/shutil（被 monkeypatch 的依赖）。
    guest = SimpleNamespace(id='full-container-id', name='ale-qemu-example', attrs={})
    # 一个已运行的容器（完整 ID + 短名）。
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda: [guest], get=lambda name: guest),
                             close=lambda: None)
    # 伪 docker 客户端。
    monkeypatch.setattr(docker, 'from_env', lambda: client)
    # 替换 docker 客户端构造。
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024**4))
    # 磁盘余量恒为 1TB（容量检查不因磁盘失败）。
    read_text = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        'MemAvailable: 104857600 kB\n' if str(path) == '/proc/meminfo'
        else read_text(path, *a, **kw))
    # 只伪造 /proc/meminfo（100GB 可用内存），其余文件真实读取。
    async def exercise():
        # 异步驱动：两个 Capacity 实例共享同一租约目录。
        first = Capacity(tmp_path / 'pool', tmp_path, limit=2)
        second = Capacity(tmp_path / 'pool', tmp_path, limit=2)
        stop = asyncio.Event()
        try:
            await asyncio.wait_for(first.acquire(1, 0, stop), timeout=1)
            # 第一个实例获取一个容量租约。
            first.attach(guest.name)
            # 附着一个容器（容量应把该容器计入本租约）。
            await asyncio.wait_for(second.acquire(1, 0, stop), timeout=1)
            # 第二个实例仍能获取（2 上限 - 1 已用 = 还有余量）。
            leases = [json.loads(p.read_text()) for p in (tmp_path / 'pool').glob('*.json')]
            assert len(leases) == 2
            # 两个租约文件并存。
            assert any(row['containers'] == [guest.id] for row in leases)
            # 关键：附着容器以完整 ID 记进租约（只计一次，不按名重复计数）。
        finally:
            first.release(); second.release()
            # 释放租约。
    asyncio.run(exercise())
    # 运行异步场景。


def test_audit_decodes_multiline_transcript_strings(tmp_path):
    """审计能解码转写里的多行字符串：换行打断 shingle 也逃不过命中。"""
    from benchmarks.ale.practice import Audit
    from explore.commit import _shingles
    other = 'Segment every individual cell in the provided microscope image series.'
    # 未授权任务的指令文本。
    corpus = tmp_path / 'corpus.json'
    corpus.write_text(json.dumps(sorted(_shingles(other))))
    # 语料 = 该文本的 shingle 集。
    (tmp_path / 'transcript.json').write_text(json.dumps([
        {'role': 'assistant', 'content': other.replace(' ', '\n')}]))
    # 转写里同一文本但空格换成换行（试图用多行字符串躲过审计）。
    assert Audit(corpus).transcripts(tmp_path)
    # 审计必须命中（多行字符串被解码归一后仍匹配语料）。


def test_host_memory_and_instruction_protocol_match_rsiagent(tmp_path):
    """宿主记忆/指令协议与 RSIAgent 主实现逐字节一致（哈希/清单/shingle）。"""
    from benchmarks.ale.protocol import instruction_shingles
    # ALE 侧指令 shingle。
    from explore.practice_loop import _manifest
    # RSIAgent 侧记忆清单。
    from explore.target_learning import _memory_tree_sha256
    # RSIAgent 侧记忆树哈希。
    from explore.commit import _shingles
    # RSIAgent 侧 shingle。
    memory = {'ü.md': b'hello', 'binary.bin': bytes(range(256))}
    # 测试记忆：Unicode 文件名 + 全值域二进制内容。
    for name, content in memory.items():
        (tmp_path / name).write_bytes(content)
    # 落盘。
    record = memory_record(tmp_path)
    # ALE 侧生成记忆记录。
    assert record['sha256'] == _memory_tree_sha256(memory)
    # 树哈希两侧一致。
    assert record['files'] == _manifest(memory)
    # 文件清单两侧一致。
    instruction = 'Create a table with at least ten rows, then save the UTF-8 output.'
    assert instruction_shingles(instruction) == _shingles(instruction)
    # 指令 shingle 两侧一致（同一套相似度语义）。
