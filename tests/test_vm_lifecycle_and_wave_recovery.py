# 【中文注释·模块定位】停止批次（stopped batch）回归测试：被中断的批次绝不
# 伪造评分、也绝不丢失已提交的记忆。覆盖两大块：
# 1) Phase-1 边界恢复（explore/phase1_boundary_recovery.py + phase1_wave.py）：
#    边界校验保留失败与成功两方面的学习、预算不重置、归档凭据可审计；记忆漂移/
#    部分学习/更晚阶段/预算篡改一律拒绝且零写入；被隔离（quarantined）的边界
#    即使保存的转写干净也不许恢复；损坏的客户机只允许一次干净重试（归档未计分
#    尝试），已有判定/边界违规永不重试；并行失败要排干并关闭成功兄弟分支。
# 2) VM 生命周期（benchmarks/osworld/provider.py）与 dnsmasq 重试（tools/）：
#    启动适配器缓存端口分配、放宽等待；guest 清理连匿名卷一起删；冷启动就绪
#    重试有上限、发生在任何任务安装之前，且只对归属明确的客体归档证据；
#    失败的启动绝不能删除他人（foreign owner）的 VM；dnsmasq 只对瞬态 inotify
#    失败重试且需显式选择加入；容器删除失败时保留句柄供清理重试。
"""Stopped-batch regressions: no fabricated grades or lost committed memory."""
# 模块 docstring：停止批次回归——不伪造评分、不丢已提交记忆。

import hashlib
# 导入 hashlib：目标指令的 sha256 指纹。
import json
# 导入 json：状态文件/凭据/trace 元数据的读写。
from pathlib import Path
# 导入 Path：路径对象处理。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 provider/container/VM 等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from explore import phase1_boundary_recovery as recovery
# 导入 Phase-1 边界恢复模块：validate_boundary/archive_pending。
from explore import phase1_wave as wave
# 导入并行波次主循环：分支执行/基础设施重试/决策数据类。
from explore.practice_loop import PracticeBoundaryError, PracticeInfrastructureError, _manifest
# 导入练习循环的边界错误、基础设施错误与记忆 manifest 工具。


def write(path, value):
    # 测试工具：先建父目录再写 JSON。
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def boundary(tmp_path, count=1):
    # 现场搭建：一份"phase1 在波次边界因基础设施停止"的完整磁盘现场。
    root = tmp_path / "run/phase1"
    # phase1 血统根目录。
    memory = {"lesson.md": b"preserved learned lesson"} if count else {}
    # 已提交记忆（count=0 时为空：模拟零项目启动失败）。
    manifest = _manifest(memory)
    # 记忆 manifest（state.json 登记）。
    for folder in ("memory", "memory_frozen"):
        (root / folder).mkdir(parents=True)
        for name, data in memory.items():
            (root / folder / name).write_bytes(data)
    # 工作记忆与冻结快照同步放置。
    state = {"status": "infra", "projects": count, "last_project": count, "waves": count + 1,
             "phase1_parallel_waves": True, "project_budget": 8,
             "checkpoint_projects": [0, 4, 8], "max_parallel": 4,
             "target_query_conditioned": True,
             "target_sha256": hashlib.sha256(b"public instruction").hexdigest(),
             "memory_manifest": manifest}
    # 状态文件：infra 停止、计数器、预算/检查点/并行度、目标指纹与记忆 manifest。
    for name in ("state.json", "result.json", "manifest.json"):
        write(root / name, state)
    # 三份核心状态文件同内容落盘。
    if count:
        write(root / "episodes/ep001/outcome.json", {
            "project_index": 1, "project_id": "p", "terminal_outcome": "FAIL",
            "wave_index": 1, "memory_before": {}, "memory_after": manifest})
        (root / "episodes/ep001/outcome.md").write_text("unaltered FAIL evidence")
        # 已完成项目的 FAIL 结果（含记忆前后 manifest）与原文。
        write(root / "waves/wave_001/curriculum_decision.json", {
            "decision": "WAVE", "rationale": "practice needed",
            "projects": [{"id": "p", "instruction": "Create /home/user/evolution_project/output"}]})
        (root / "waves/wave_001/outcomes.md").write_text("complete wave feedback")
        # wave_001 的课程决策与完整反馈。
        write(root / "curriculum/wave_001/segment_000/transcript.json", {
            "messages": [{"role": "user", "content": "original context"},
                         {"role": "assistant", "content": "original action"}]})
        write(root / "events.jsonl", {"event_type": "PHASE1_WAVE_COMPLETED",
              "payload": {"wave": 1, "total_projects": 1}})
        # wave_001 的课程转写与完成事件。
    write(root / "curriculum/wave_002/segment_000/transcript.json", {
        "messages": [{"role": "assistant", "content": "failed draft"}]})
    # wave_002 有一份未完成的课程草稿转写（待归档的悬空轮）。
    return root


def validate(root):
    # 测试工具：对现场做边界校验（与 state.json 登记参数一致）。
    return recovery.validate_boundary(
        root, "public instruction", project_budget=8, checkpoint_projects=[0, 4, 8],
        max_parallel=4, target_query_conditioned=True)


def test_resume_preserves_failed_and_successful_learning_without_resetting_budget(tmp_path):
    # 测试：恢复既要保留失败项目的学习也要保留成功记忆，且预算不重置——
    # 归档凭据里显式登记 total_budget_unchanged。
    root = boundary(tmp_path)
    # 搭建现场。
    old_state = (root / "state.json").read_bytes()
    # 记录恢复前 state.json 的字节快照。
    plan = validate(root)
    # 边界校验。
    assert plan["projects"] == 1 and plan["wave"] == 1
    # 计划确认：1 个已完成项目、停在第 1 波。
    assert plan["history"][0]["content"] == "original context"
    # 课程历史从原始上下文续起。
    archive = recovery.archive_pending(root, plan)
    # 归档悬空的 wave_002 草稿轮，返回归档目录。
    assert (archive / "state.json").read_bytes() == old_state
    # 归档里保存了恢复前的 state 快照。
    assert (archive / "curriculum/wave_002/segment_000/transcript.json").is_file()
    # 悬空草稿转写被完整归档。
    assert (root / "memory/lesson.md").read_bytes() == b"preserved learned lesson"
    # 成功提交的记忆原样保留。
    assert json.loads((root / "episodes/ep001/outcome.json").read_text())["terminal_outcome"] == "FAIL"
    # 失败项目的判定也原样保留（不洗成 PASS）。
    assert json.loads((archive / "receipt.json").read_text())["total_budget_unchanged"]
    # 归档凭据登记：总预算未变。


@pytest.mark.parametrize("fault", ["memory", "partial", "later_phase", "counter"])
def test_resume_rejects_memory_drift_partial_learning_and_later_phases(tmp_path, fault):
    # 测试（4 类破坏参数化）：记忆漂移、部分学习、已进入更晚阶段、预算计数被
    # 篡改——边界校验都必须拒绝，且不产生任何归档目录（零写入）。
    root = boundary(tmp_path)
    # 先搭合法现场再注入破坏。
    if fault == "memory":
        (root / "memory/lesson.md").write_text("uncommitted alteration")
    # 破坏 1：记忆内容与 state 登记的 manifest 不符。
    elif fault == "partial":
        (root / "episodes/ep002/memory_distillation").mkdir(parents=True)
    # 破坏 2：悬空的部分学习目录（记忆阶段进行中）。
    elif fault == "later_phase":
        (root.parent / "phase2").mkdir()
    # 破坏 3：phase2 目录已存在（批次已推进到更晚阶段）。
    else:
        state = json.loads((root / "state.json").read_text())
        state["project_budget"] = 16
        write(root / "state.json", state)
    # 破坏 4：state.json 的预算被改成 16（与校验参数 8 不一致）。
    with pytest.raises(PracticeInfrastructureError):
        validate(root)
    # 校验必须拒绝。
    assert not (root / "boundary_recovery").exists()
    # 拒绝路径零写入：没有归档目录。


def test_zero_project_boot_failure_needs_no_nonexistent_wave_event(tmp_path):
    # 测试：零项目的启动失败现场（无任何项目/波次工件）同样可校验并归档
    # ——恢复逻辑不依赖"必然存在的波次完成事件"。
    root = boundary(tmp_path, count=0)
    # count=0：空现场。
    plan = validate(root)
    # 校验通过。
    assert plan["projects"] == 0 and plan["history"] == []
    # 计划：零项目、空课程历史。
    recovery.archive_pending(root, plan)
    # 归档悬空轮：没有波次事件也能正常执行。


@pytest.mark.parametrize("count", [0, 1])
def test_quarantined_boundary_cannot_resume_after_clean_transcript_audits(
        tmp_path, monkeypatch, count):
    # 测试（count=0/1 参数化）：被隔离的边界（客户机交接未通过目标边界审计）
    # 绝不能因"保存下来的转写看起来干净"而恢复——隔离是原始边界失败的
    # 权威记录，续跑必须拒绝且不启动 VM、不改任何文件。
    from config.settings import Config
    from explore.practice_loop import PracticeHooks

    root = boundary(tmp_path, count=count)
    # 搭建现场（有/无项目两种）。
    state = json.loads((root / "state.json").read_text())
    # A rejected guest handoff is audited before it is saved on the host.
    # A clean saved transcript cannot clear that original boundary failure.
    # 被拒的 guest 交接在保存到主机前就已被审计；干净的已保存转写
    # 无法洗掉原始的边界失败。
    state.update(status="quarantined",
                 reason="Curriculum wave handoff failed the target boundary")
    write(root / "state.json", state)
    # 把状态改为 quarantined 并登记隔离原因。
    preserved = {p.relative_to(root): p.read_bytes()
                 for p in root.rglob("*") if p.is_file()}
    # 给全目录拍字节快照。
    started = []
    # 记录 VM 是否被启动。
    hooks = PracticeHooks(validate_corpus=lambda *a: None,
                     audit_text=lambda *a, **k: [],
                     audit_transcripts=lambda *a, **k: [])
    # 空审计钩子：转写审计本身干净。
    decision = wave.WaveDecision(
        "SATURATED", "mock next decision", (), '{"decision":"SATURATED"}')
    # 预置一个干净的课程决策（如果续跑走到作者环节也会是干净的）。
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    monkeypatch.setattr(wave, "_author_wave", lambda **k: (decision, []))
    # 屏蔽记忆推送、替换波次作者。
    def vm_factory():
        started.append(True)
        return SimpleNamespace(close=lambda: None), object()
    # VM 工厂：一旦被调用就记录（本测试要求绝不调用）。
    with pytest.raises(PracticeInfrastructureError, match="quarantined"):
        wave.evolve_parallel_phase1(
            root=str(root), target_direction="public instruction",
            actor_cfg=Config(), curriculum_cfg=Config(), memory_cfg=Config(),
            verifier_control_cfg=Config(), corpus_path="unused", hooks=hooks,
            vm_factory=vm_factory, project_budget=8,
            checkpoint_projects=(0, 4, 8), max_parallel=4,
            target_query_conditioned=True, resume_completed_boundary=True)
    # 续跑必须以 "quarantined" 基础设施错误拒绝。
    assert started == []
    # VM 从未被启动。
    assert not (root / "boundary_recovery").exists()
    # 没有归档目录产生。
    assert {p.relative_to(root): p.read_bytes()
            for p in root.rglob("*") if p.is_file()} == preserved
    # 全目录逐字节不变：拒绝路径零写入。




def test_broken_guest_restarts_once_and_archives_unscored_attempt(tmp_path, monkeypatch):
    # 测试：损坏的客户机（只读文件系统）触发一次有界的干净重试——首次失败
    # 时归档未计分尝试、删除污染的 actor 工作目录（夹具保留），第二次从同一
    # 不可变夹具重启成功；重试凭据登记 prior_memory_promoted=False。
    episode = tmp_path / "ep001"
    # episode 目录。
    write(episode / "fixtures/manifest.json", {"immutable": True})
    # 不可变夹具清单。
    calls = []
    expected = object()
    def branch(**kwargs):
        # 伪分支执行：第一次触发客户机损坏，第二次验证现场已清理后成功。
        calls.append(kwargs)
        if len(calls) == 1:
            write(episode / "actor/iter_01/trace_meta.json", {"infra_fail": True})
            (episode / "actor/iter_01/trace.txt").write_text("mktemp: Read-only file system")
            # 留下物理故障证据：infra_fail 标记 + 只读文件系统错误文本。
            raise PracticeInfrastructureError("ACTOR phase ended at infrastructure status infra")
            # 第一次以基础设施错误失败。
        assert not (episode / "actor").exists()
        # 第二次进入前：被污染的 actor 目录已被删除（不带入污染状态）。
        assert (episode / "fixtures/manifest.json").is_file()
        # 不可变夹具仍在。
        return expected
    monkeypatch.setattr(wave, "_execute_branch", branch)
    # 替换底层分支执行。
    assert wave._execute_branch_with_infra_retry(episode_dir=episode) is expected
    # 带重试的包装器返回第二次的结果（同一对象，证明无重复计分）。
    assert len(calls) == 2
    # 恰好两次调用：一次失败 + 一次干净重试（上限一次）。
    receipt = json.loads((episode / "infra_attempts/attempt_001/retry_receipt.json").read_text())
    # 读取重试凭据。
    assert receipt["prior_memory_promoted"] is False
    # 凭据登记：失败尝试没有提升任何记忆（未计分尝试不产生学习）。


@pytest.mark.parametrize("committed", [False, True])
def test_boundary_and_committed_verdict_are_never_retried(tmp_path, monkeypatch, committed):
    # 测试（committed=False/True 参数化）：边界违规永不重试；已有 Verifier
    # 判定（committed）也永不重试——重试只能用于"无判定"的物理故障。
    episode = tmp_path / "ep001"
    if committed:
        path = episode / "handoffs/verifier_001.md"
        path.parent.mkdir(parents=True)
        path.write_text("VERDICT: FAIL")
        # committed=True：预置一份已落盘的 Verifier 判定。
    def branch(**kwargs):
        raise TimeoutError("physical fault") if committed else PracticeBoundaryError("boundary")
        # committed 分支抛物理故障（但有判定在身）；否则抛边界违规。
    monkeypatch.setattr(wave, "_execute_branch", branch)
    with pytest.raises((TimeoutError, PracticeBoundaryError)):
        wave._execute_branch_with_infra_retry(episode_dir=episode)
    # 两种情况都必须原样上抛（绝不触发重试）。
    assert not (episode / "infra_attempts").exists()
    # 没有任何重试归档产生。




def test_parallel_failure_drains_and_closes_successful_sibling(tmp_path, monkeypatch):
    # 测试：并行波次中一个分支基础设施失败 → 等待（排干）并关闭成功兄弟分支
    # 的 VM、关闭课程 VM；批次以 infra 状态收尾、项目计数为 0（整波不收账）。
    import time
    from explore.practice_loop import PracticeHooks
    from config.settings import Config
    closed = []
    # 记录被关闭的 VM（课程与分支）。
    projects = tuple(wave.WaveProject(str(i), "public instruction") for i in (1, 2))
    # 两个并行项目。
    decision = wave.WaveDecision("WAVE", "agent rationale", projects, "{}")
    # 课程决策：开波。
    monkeypatch.setattr(wave, "_author_wave", lambda **kw: (decision, []))
    monkeypatch.setattr(wave, "_capture_wave_fixtures", lambda **kw: None)
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    # 替换作者/夹具采集/记忆推送。
    def branch(**kw):
        # 伪分支执行：项目 1 基础设施失败；项目 2 稍慢但成功。
        if kw["project_index"] == 1:
            raise PracticeInfrastructureError("failed sibling")
        time.sleep(0.02)
        return SimpleNamespace(project_index=2, project=projects[1], terminal_outcome="FAIL",
                               desktop=SimpleNamespace(close=lambda: closed.append(2)))
    monkeypatch.setattr(wave, "_execute_branch_with_infra_retry", branch)
    # 替换带重试的分支执行。
    hooks = PracticeHooks(validate_corpus=lambda *a: None, audit_text=lambda *a, **k: [])
    result = wave.evolve_parallel_phase1(
        root=str(tmp_path / "lineage"), target_direction="public instruction",
        actor_cfg=Config(), curriculum_cfg=Config(), memory_cfg=Config(),
        verifier_control_cfg=Config(), corpus_path="unused", hooks=hooks,
        vm_factory=lambda: (SimpleNamespace(close=lambda: closed.append("curriculum")), object()),
        project_budget=8, max_parallel=2)
    # 并行度 2 跑一波。
    assert result.status == "infra" and result.projects == 0
    # 结果：infra 状态、零项目入账（成功兄弟不单独收账）。
    assert closed == ["curriculum", 2]
    # 课程 VM 与成功兄弟（项目 2）的 VM 都被关闭。




def test_boot_adapter_caches_only_one_allocation_and_extends_waits(monkeypatch):
    # 测试：启动适配器补丁（_prepare_boot_lifecycle）——端口分配在启动窗口内
    # 只查询一次（缓存复用）；就绪等待放宽到 600 秒；补丁幂等且锁超时改为 300。
    from contextlib import nullcontext
    import benchmarks.osworld.provider as adapter
    queried, waits = [], []
    class Provider:
        container = None
        def _get_used_ports(self):
            queried.append(True)
            return {5000}
        # 每次真实端口扫描都记录。
        def _wait_for_vm_ready(self, timeout=300):
            waits.append(timeout)
        # 记录就绪等待的超时参数。
        def start_emulator(self):
            for _ in range(4):
                assert self._get_used_ports() == {5000}
            self._wait_for_vm_ready()
        # 启动过程中连续 4 次取端口 + 一次等待就绪（模拟上游行为）。
    module = SimpleNamespace(DockerProvider=Provider, LOCK_TIMEOUT=10)
    # 伪模块：提供者类与初始锁超时。
    monkeypatch.setattr(adapter, "_boot_slot", nullcontext)
    # 启动槽位用空上下文（不真正持锁）。
    adapter._prepare_boot_lifecycle(module)
    adapter._prepare_boot_lifecycle(module)
    # 打两次补丁（验证幂等）。
    p = Provider()
    p.start_emulator()
    p.start_emulator()
    # 两次真实启动。
    assert len(queried) == 2 and waits == [600, 600]
    # 每次启动只做一次真实端口扫描（分配窗口内缓存生效），等待超时放宽为 600。
    assert module.LOCK_TIMEOUT == 300
    # 锁超时被补丁改为 300。
    p._get_used_ports()
    assert len(queried) == 3
    # 分配窗口外再取端口走原始扫描（缓存不溢出窗口）。


def test_guest_cleanup_removes_its_anonymous_volume_even_when_stop_times_out(monkeypatch):
    # 测试：guest 清理在守护 stop 超时后仍要继续 remove（force + 连匿名卷一起删），
    # 并清空 provider 上的容器/端口句柄。
    from contextlib import nullcontext
    import benchmarks.osworld.provider as adapter
    calls = []
    class Container:
        def stop(self, timeout):
            raise TimeoutError("daemon stop timed out")
        # stop 永远超时。
        def remove(self, **kwargs):
            calls.append(kwargs)
        # remove 被调用时记录参数。
    class Provider:
        container = Container()
        def start_emulator(self): pass
        def stop_emulator(self): pytest.fail("unpatched stop")
        def _wait_for_vm_ready(self, timeout=300): pass
        def _get_used_ports(self): return set()
    monkeypatch.setattr(adapter, "_boot_slot", nullcontext)
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    # 打补丁。
    p = Provider()
    p.stop_emulator()
    # 走补丁后的停止流程。
    assert calls == [{"force": True, "v": True}]
    # remove 参数恰为 force=True、v=True：强制删除并移除匿名卷。
    assert p.container is None and p.server_port is None
    # 容器与端口句柄被清空。


@pytest.mark.parametrize("failures,owner,error,expected", [
    (1, "example-study/worker-a/phase1", TimeoutError, 2),
    (2, "example-study/worker-a/phase1", TimeoutError, 2),
    (1, "", TimeoutError, 1),
    (1, "example-study/worker-a/phase1", RuntimeError, 1),
])
def test_cold_readiness_retry_is_bounded_and_before_any_task_setup(
        tmp_path, monkeypatch, failures, owner, error, expected):
    # 测试（四组参数化）：冷启动就绪重试有界（至多 1 次干净重试=2 次尝试）、
    # 发生在任何任务安装之前；只对"归属明确（有 owner）且超时类错误"的失败
    # 归档证据（receipt + 容器日志）；无归属或非超时错误则不归档、不重试。
    from contextlib import contextmanager
    import os
    import benchmarks.osworld.provider as adapter
    monkeypatch.setenv("RSIAGENT_VM_OWNER", owner)
    # 设置客体归属者（空串 = 无归属）。
    monkeypatch.setattr(adapter, "__file__", str(tmp_path / "benchmarks/osworld/provider.py"))
    # 让证据归档目录落在临时区。
    calls, removed, slots = [], [], []
    @contextmanager
    def slot():
        slots.append("acquire")
        try: yield
        finally: slots.append("release")
    # 伪启动槽位：记录获取/释放配对。
    class Container:
        def __init__(self, number):
            self.id = str(number)
            self.labels = {"rsiagent.owner": owner, "rsiagent.host_pid": str(os.getpid()),
                           "rsiagent.launch_token": str(number)}
            self.attrs = {"State": {"Status": "running"}}
        # 带归属标签的容器（owner/宿主 pid/启动令牌）。
        def reload(self): pass
        def logs(self, **kwargs): return b"cold boot diagnostics"
        # 容器日志内容（将被归档）。
        def remove(self, **kwargs): removed.append((self.id, kwargs))
    class Provider:
        container = None
        def _get_used_ports(self): return set()
        def _wait_for_vm_ready(self, timeout=300):
            if len(calls) <= failures:
                raise error("VM failed to become ready within timeout period")
            # 前 failures 次尝试抛指定错误（超时或其他）。
        def start_emulator(self):
            calls.append(True)
            self.container = Container(len(calls))
            self._wait_for_vm_ready()
            return "ready for setup"
        # 每次启动：创建新容器并等待就绪。
    monkeypatch.setattr(adapter, "_boot_slot", slot)
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    # 打补丁。
    provider = Provider()
    if failures == 1 and expected == 2:
        assert provider.start_emulator() == "ready for setup"
        assert provider.container.id == "2"
        # 单次超时 + 有归属：第一次失败被归档并重试，第二次成功（容器编号 2）。
    else:
        with pytest.raises(error): provider.start_emulator()
        # 其余组合：错误如实上抛（无重试）。
    assert len(calls) == expected
    # 启动尝试次数恰为预期（有界）。
    assert slots == ["acquire", "release"] * expected
    # 每次尝试都在槽位内（acquire/release 配对）。
    assert all(opts == {"force": True, "v": True} for _, opts in removed)
    # 失败容器一律被强制删除并清理匿名卷。
    receipts = list(tmp_path.rglob("receipt.json"))
    # 收集归档的证据凭据。
    if not owner or error is RuntimeError:
        assert not receipts
        # 无归属或非超时错误：不归档证据。
    else:
        assert len(receipts) == min(failures, 2)
        # 有归属的超时失败：每次失败都归档（上限 2 次）。
        for path in receipts:
            receipt = json.loads(path.read_text())
            assert receipt["removed_with_anonymous_volume"]
            assert path.with_name("container.log").read_bytes() == b"cold boot diagnostics"
            # 凭据登记匿名卷清理，容器日志原样归档。


def test_failed_boot_cannot_remove_a_foreign_owner(tmp_path, monkeypatch):
    # 测试：失败启动的归档/清理绝不触碰他人（owner 不匹配）的容器——
    # 所有权不匹配时抛 QemuRollbackError 且绝不调用 remove。
    import benchmarks.osworld.provider as adapter
    from env.qemu_rollback import QemuRollbackError
    class Container:
        labels = {"rsiagent.owner": "unrelated"}
        def reload(self): pass
        def remove(self, **kwargs): pytest.fail("foreign VM removed")
        # 容器归属者是别人；remove 一旦被调用即失败。
    with pytest.raises(QemuRollbackError, match="ownership mismatch"):
        adapter._archive_failed_boot(SimpleNamespace(container=Container()),
            "example-study/worker-a/phase1", 600)
    # 归档失败启动时发现归属不匹配：抛错且不删除。


@pytest.mark.parametrize("failure,failures,expected_calls,expected_status", [
    ("failed to create inotify: Too many open files", 2, 3, 0),
    ("failed to create inotify: Too many open files", 40, 30, 3),
    ("bad command line options", 40, 1, 3),
])
def test_dnsmasq_retries_only_transient_inotify_start_failure(
        tmp_path, failure, failures, expected_calls, expected_status):
    # 测试（三组参数化）：dnsmasq 启动重试包装只对瞬态 inotify 失败重试——
    # 前 2 次失败第 3 次成功（返回 0）；失败达上限（约 30 次尝试）以原码 3 退出；
    # 参数错误（bad command line options）不重试、立即失败；原始错误保持可见；
    # 启动参数原样传递（含带空格的参数）。
    import os
    import subprocess
    fake = tmp_path / "dnsmasq"
    fake.write_text('''#!/bin/bash
n=$(cat "$PROBE_COUNT" 2>/dev/null || echo 0)
n=$((n+1))
echo "$n" > "$PROBE_COUNT"
printf '%s\\n' "$@" > "$PROBE_ARGS"
if [ "$n" -le "$PROBE_FAILURES" ]; then
    echo "dnsmasq: $PROBE_FAILURE" >&2
    exit 3
fi
''')
    fake.chmod(0o755)
    # 伪 dnsmasq：计数探测次数、记录参数，按脚本决定失败次数与错误文本。
    source = Path(__file__).resolve().parents[1] / "tools/dnsmasq_start_retry.sh"
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(source.read_text().replace('/usr/sbin/dnsmasq', str(fake))
                       .replace('sleep 2', 'sleep 0'))
    # 用仓库真实包装脚本生成测试副本：指向伪 dnsmasq、去掉重试间隔等待。
    args = ['--dhcp-option=option:router,20.20.20.1', '--host-record=two words']
    # 启动参数样例（含带空格的 --host-record）。
    result = subprocess.run(['bash', str(wrapper), *args], text=True, capture_output=True,
        env={**os.environ, 'PROBE_COUNT':str(tmp_path/'count'), 'PROBE_ARGS':str(tmp_path/'args'),
             'PROBE_FAILURE':failure, 'PROBE_FAILURES':str(failures)}, timeout=15)
    # 运行包装脚本。
    assert result.returncode == expected_status
    # 返回码：成功 0 / 耗尽重试 3 / 参数错误立即 3。
    assert int((tmp_path/'count').read_text()) == expected_calls
    # dnsmasq 实际启动次数恰为预期（重试有界且不重试参数错误）。
    assert (tmp_path/'args').read_text().splitlines() == args
    # 每次重试参数原样传递（含空格参数）。
    assert failure in result.stderr  # Original errors remain visible.
    # 原始错误文本保留在 stderr（排障可见）。


def test_dnsmasq_start_retry_requires_explicit_opt_in(monkeypatch):
    # 测试：dnsmasq 启动重试必须显式选择加入（RSIAGENT_DNSMASQ_START_RETRY=1
    # 且 VM 归属者合法）——未开启时容器环境/挂载原样；开启后注入只读的
    # 重试挂载点，且不覆盖调用方既有环境变量。
    from benchmarks.osworld.provider import _LoopbackContainers
    calls = []
    collection = SimpleNamespace(run=lambda *a, **kw: calls.append(kw))
    # 伪容器集合：只记录 run 参数。
    monkeypatch.delenv('RSIAGENT_DNSMASQ_START_RETRY', raising=False)
    monkeypatch.setenv('RSIAGENT_VM_OWNER', 'unrelated_experiment')
    # 未开启重试 + 非法归属者。
    _LoopbackContainers(collection).run('image', environment={'KEEP':'unchanged'}, volumes={})
    # 第一次启动。
    assert calls[-1]['environment'] == {'KEEP':'unchanged'} and calls[-1]['volumes'] == {}
    # 未选择加入：环境与挂载原样透传。
    monkeypatch.setenv('RSIAGENT_DNSMASQ_START_RETRY', '1')
    monkeypatch.setenv('RSIAGENT_VM_OWNER', 'example-study/worker-b/phase1')
    # 显式选择加入 + 合法归属者。
    _LoopbackContainers(collection).run('image', environment={'KEEP':'unchanged'},
        volumes={'system':{'bind':'/System.qcow2','mode':'ro'}})
    # 第二次启动。
    assert calls[-1]['environment'] == {'KEEP':'unchanged', 'DNSMASQ':'/run/rsiagent-dnsmasq-start'}
    # 环境注入 DNSMASQ 指针（既有 KEEP 保留）。
    assert calls[-1]['volumes']['system'] == {'bind':'/System.qcow2','mode':'ro'}
    # 既有挂载不受影响。
    mounts = [v for v in calls[-1]['volumes'].values() if v['bind']=='/run/rsiagent-dnsmasq-start']
    assert mounts == [{'bind':'/run/rsiagent-dnsmasq-start','mode':'ro'}]
    # 新增恰一个只读的重试挂载点。


def test_failed_container_removal_retains_handle_for_cleanup_retry(monkeypatch):
    # 测试：停止流程中 remove 失败（守护不可用）时，句柄必须保留在 provider
    # 上——供后续清理重试；错误原样上抛；stop 已成功不重复执行。
    import benchmarks.osworld.provider as adapter
    class Container:
        def stop(self, timeout): pass
        def remove(self, **kwargs): raise ConnectionError("daemon unavailable")
        # stop 成功、remove 失败。
    class Provider:
        container = Container()
        def start_emulator(self): pass
        def stop_emulator(self): pass
        def _wait_for_vm_ready(self, timeout=300): pass
        def _get_used_ports(self): return set()
    adapter._prepare_boot_lifecycle(SimpleNamespace(DockerProvider=Provider))
    # 打补丁。
    provider = Provider()
    owned = provider.container
    # 记录句柄身份。
    with pytest.raises(ConnectionError):
        provider.stop_emulator()
    # remove 失败原样上抛。
    assert provider.container is owned
    # 句柄仍是原容器对象：失败后未清空（可重试清理）。
