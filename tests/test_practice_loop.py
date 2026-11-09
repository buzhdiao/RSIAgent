# 【中文注释·模块定位】可选 RSI 生命周期的聚焦状态机测试（explore/
# practice_loop.evolve_practice + explore/target_learning 的审计面 +
# explore/provisioning 的捕获产物 + llm.client 的持久消息 +
# tools.exam_fence.audit_text）。全部用 FakeVM/ScriptedAgents/FakeHarness
# 驱动真实状态机，不起 VM/LLM。被钉死的承诺：
# 1) 转写越界隔离：审计命中落盘 host-only 报告（0600 权限、细节不进异常）、
#    诊断写失败绝不降级隔离；
# 2) 课程视觉只在本项目边界内保留像素（归档清单 + "losslessly stored" 提示 +
#    Reissue a Look 指引，后续轮次像素不可见）；重入幂等；
# 3) guest 文本读取：确认缺失不重试抓取、在册则无上限无损抓取、不确定时
#    回退抓取；停摆续行只修 JSON 传输（无判定/状态/决策词）；
# 4) 记忆边界：工作期非法编辑被丢弃、草稿不晋升、只在整库对账后晋升；
#    Actor/Verifier 上下文每 episode 全新、课程独享跨 episode 持久 +
#    黑盒 Verifier 证据（看不到 Actor 交接或私有记忆内容）；
# 5) 终局语义：FAIL/PASS 都是终局且只判一次、PASS 无需新鲜确认、FAIL 以
#    "未验证失败"蒸馏；应急上限是基础设施而非学习结果；
# 6) Actor 执行证据：确切的累积视图、无推理字段、manifest 哈希确定性、
#    附加篡改/替换篡改都会失效已发布判定；传输失败在 Verifier 之前 fail-closed；
# 7) 交接令牌：VERDICT 唯一且在代码围栏外、发布原子（stalled 边界上的有效
#    发布胜出）；Actor 只认中性 SUBMIT 并在上下文内修复传输；畸形交接续跑
#    同一 Agent 上下文且无轮数上限；
# 8) 记忆对账在 Actor done 之前不限轮续跑（segment 递增、无终局提示则续）；
#    整库拒绝时隔离且持久记忆库无损；
# 9) Phase-1 探索：预算按完整项目计数、按检查点提交精确记忆、课程可提前
#    SATURATED、Actor 自有记忆以一次性副本供给课程、只从已闭合项目恢复且
#    带课程上下文、失败恢复不改写既有状态、键盘中断持久化 infra 边界；
# 10) Agent 化验证器只见候选不见 Actor 私有证据；practice 主循环源码级无
#    评分/预检/评估器面（inspect 源码与签名断言）。
"""Focused state-machine tests for the opt-in RSI lifecycle."""
# 模块 docstring：可选 RSI 生命周期的聚焦状态机测试。

from __future__ import annotations

import copy
# 导入 copy：深拷贝树/历史做前后对账。
import hashlib
# 导入 hashlib：manifest sha256 断言。
import inspect
# 导入 inspect：源码级与签名级的"无评分面"断言。
import json
# 导入 json：读取落盘的 outcome/state 产物。
from pathlib import Path
# 导入 Path：盘上产物路径。
import subprocess
# 导入 subprocess：LocalCommandVM 真实执行 shell 命令。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造配置/循环结果等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import load as load_config
# 导入配置加载：读取真实 target.yaml 作 Agent 化验证器控制配置。
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
# 导入纯 JSON 传输提示：断言续行提示只修传输。
from explore import practice_loop
# 导入被测主模块：练习循环状态机及其内部辅助。
from explore import target_learning
# 导入目标学习：目标审计面的构造。
from explore import provisioning
# 导入供给模块：捕获产物的 manifest/materials/roots 文件名。
from llm.client import DURABLE_IMAGES_FIELD, durable_user_message
# 导入持久消息：多模态图像字段与构造器。
from tools.exam_fence import audit_text
# 导入考试围栏文本审计：可见资产名授权的断言对象。


TARGET = "Reproduce the disclosed long-horizon editing task from the supplied inputs."
# 北极星目标（本文件所有测试的授权指令）。
PROJECT_1 = (
    "Work only in /home/user/evolution_project and create the requested editable "
    "deliverable from the supplied raw input."
)
# 练习项目 1：在项目根内创建可编辑交付物。
PROJECT_2 = (
    "Work only in /home/user/evolution_project and transfer the learned method "
    "to the supplied contrasting input."
)
# 练习项目 2：把学到的方法迁移到对照输入。


def test_transcript_quarantine_persists_host_only_hit_details(tmp_path):
    """转写越界隔离：host-only 报告持久化命中细节，异常不泄漏细节。"""
    sink_dir = tmp_path / "curriculum/segment_000"
    sink_dir.mkdir(parents=True)
    # 产物目录。
    hits = [{"file": str(sink_dir / "iter_04/trace.txt"), "hits": [
        {"kind": "repo-url", "match": "blocked repository evidence"}]}]
    # 审计命中：trace.txt 命中 repo-url 封锁项。
    calls = []

    def audit(root, **kwargs):
        calls.append((root, kwargs))
        return hits
        # 伪转写审计：记录调用并返回命中。

    hooks = SimpleNamespace(audit_transcripts=audit)
    with pytest.raises(practice_loop.PracticeBoundaryError) as exc:
        practice_loop._audit_agent_artifacts(hooks, str(sink_dir), TARGET)
        # 审计命中 → 越界异常。

    report_path = sink_dir / "boundary_audit.json"
    report = json.loads(report_path.read_text())
    assert report["hits"] == hits and report["mode"] == "practice"
    # 报告完整持久化命中（host-only 文件）与 practice 模式。
    assert report["status"] == "quarantined"
    # 状态 quarantined。
    assert calls == [(str(sink_dir), {
        "mode": "practice", "authorized_instruction": TARGET})]
    # 审计以 practice 模式与授权指令调用。
    assert str(report_path) in str(exc.value)
    # 异常指向报告路径（供人查看）。
    assert "blocked repository evidence" not in str(exc.value)
    # 关键：命中细节绝不进异常字符串（可能被回传给模型）。
    assert report_path.stat().st_mode & 0o777 == 0o600
    # 报告权限 0600（host-only，guest 不可读）。


def test_failed_diagnostic_write_does_not_downgrade_quarantine(monkeypatch, tmp_path):
    """诊断报告写失败绝不把隔离降级为放行（fail-closed）。"""
    hooks = SimpleNamespace(audit_transcripts=lambda *_args, **_kwargs: [
        {"file": "trace.txt", "hits": [{"kind": "repo-url", "match": "blocked"}]}])
    # 审计命中。

    def fail_write(*_args, **_kwargs):
        raise PermissionError("read-only diagnostic directory")
        # 注入：原子写抛权限错误。

    monkeypatch.setattr(practice_loop, "_atomic_json", fail_write)
    with pytest.raises(practice_loop.PracticeBoundaryError,
                       match="audit report could not be written"):
        practice_loop._audit_agent_artifacts(hooks, str(tmp_path), TARGET)
        # 仍抛越界异常并说明"审计报告无法落盘"。


def test_curriculum_visuals_scope_only_pixels_at_project_boundary():
    """课程视觉只在本项目边界内保留像素：归档 + 指引 + 重入幂等。"""
    pixels = b"exact prior project pixels"
    history = [
        durable_user_message("the complete Look result", pixels),
        {"role": "assistant", "content": "my complete visual reasoning"},
        {"role": "user", "content": "the next observation"},
        {"role": "assistant", "content": "the next action"},
    ]
    # 历史：带像素的 Look 结果 + 三条纯文本消息。

    scoped, archived = practice_loop._scope_prior_curriculum_visuals(history)
    # 执行像素限定。

    assert len(scoped) == len(history)
    assert [message["role"] for message in scoped] == \
        [message["role"] for message in history]
    # 消息条数与角色序列不变。
    assert scoped[0]["content"].startswith("the complete Look result")
    assert "losslessly stored" in scoped[0]["content"]
    assert "Reissue a Look" in scoped[0]["content"]
    # 首条文本保留并注明"像素已无损存档"+"需要就重新 Look"。
    assert scoped[1:] == history[1:]
    # 其余消息原样。
    assert all(DURABLE_IMAGES_FIELD not in message for message in scoped)
    assert DURABLE_IMAGES_FIELD in history[0]
    # 关键：限定后的历史不再携带像素字段（跨边界不可见）。
    assert archived == [{
        "message_index": 0,
        "images": 1,
        "sha256": [history[0][DURABLE_IMAGES_FIELD][0]["sha256"]],
    }]
    # 归档清单：消息序号、图像数、sha256。
    scoped_again, archived_again = \
        practice_loop._scope_prior_curriculum_visuals(scoped)
    assert scoped_again == scoped
    assert archived_again == []
    # 重入幂等：已限定的历史不再产生归档。


class FakeVM:
    """伪 VM：内存文件/树/记忆/执行证据 + 全部 RSI 协议命令的模拟。"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        # guest 文件表（交接/报告等路径 → 字节）。
        self.tree = None
        # 项目树快照（None 表示无项目）。
        self.memory: dict[str, bytes] = {}
        # guest 内记忆树。
        self.execution_evidence: dict[str, bytes] = {}
        # guest 内执行证据视图。
        self.execution_evidence_expected: dict[str, bytes] = {}
        # 推送时记录的期望证据（比对篡改）。
        self.evidence_verify_ok = True
        # 注入开关：证据校验失败。
        self.target_shape_safe = True
        # 注入开关：目标形状安全。
        self.original_fixture_tree = None
        # 原始 fixture 树（暂存/恢复协议的内存模拟）。

    def run_command(self, command, **_kwargs):
        if "RSI_REMOVE_TREE_RC" in command:
            assert practice_loop.PROJECT_ROOT in command
            self.tree = None
            return "RSI_REMOVE_TREE_RC=0"
            # 删除项目树：命令必须指向 PROJECT_ROOT。
        if "RSI_REMOVE_RC" in command:
            for path in (practice_loop.CURRICULUM_HANDOFF,
                         practice_loop.ACTOR_HANDOFF,
                         practice_loop.VERIFIER_REPORT,
                         practice_loop.LEARNING_DIAGNOSIS):
                if path in command:
                    self.files.pop(path, None)
            return "RSI_REMOVE_RC=0"
            # 删除交接/报告/诊断文件（只删这四个协议路径）。
        if "RSI_MEMORY_FILES" in command:
            return (f"RSI_MEMORY_FILES={len(self.memory)}\n"
                    "RSI_MEMORY_DIR_RC=0")
            # 记忆文件计数协议。

        if "RSI_ORIGINAL_FIXTURE_STAGE_RC" in command:
            self.original_fixture_tree = copy.deepcopy(self.tree)
            return "RSI_ORIGINAL_FIXTURE_STAGE_RC=0"
            # 暂存原始 fixture 树。
        if "RSI_ORIGINAL_FIXTURE_RESTORE_RC" in command:
            self.tree = copy.deepcopy(self.original_fixture_tree)
            return "RSI_ORIGINAL_FIXTURE_RESTORE_RC=0"
            # 恢复原始 fixture 树。
        if "RSI_TARGET_SHAPE_RC" in command:
            return ("RSI_TARGET_SHAPE_RC=0" if self.target_shape_safe
                    else "RSI_TARGET_SHAPE_RC=1")
            # 目标形状探测：按开关成功/失败。
        if "RSI_PROJECT_SHAPE_RC" in command:
            return ("RSI_PROJECT_SHAPE_RC=0" if self.tree is not None
                    else "RSI_PROJECT_SHAPE_RC=1")
            # 项目形状探测：树存在即成功。
        if "RSI_TARGET_STAGE_RC" in command:
            return "RSI_TARGET_STAGE_RC=0"
            # 目标暂存协议。
        if "RSI_TARGET_OVERLAY_RC" in command:
            return "RSI_TARGET_OVERLAY_RC=0"
            # 目标叠加协议。
        if "RSI_ACTOR_EVIDENCE_RC" in command:
            intact = self.execution_evidence == \
                self.execution_evidence_expected
            return ("RSI_ACTOR_EVIDENCE_RC=0"
                    if self.evidence_verify_ok and intact
                    else "RSI_ACTOR_EVIDENCE_RC=1")
            # Actor 执行证据校验：开关失败或证据被篡改（与期望不符）都报错。
        return ""
        # 其他命令无输出。


class GuestTextVM:
    """guest 文本协议的伪 VM：固定 run_command 状态与 fetch_file 载荷。"""

    def __init__(self, state: str, payload=None):
        self.state = state
        # run_command 的固定回复（文件状态探测结果）。
        self.payload = payload
        # fetch_file 的固定载荷。
        self.commands = []
        self.fetches = []
        # 记录命令与抓取。

    def run_command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return self.state
        # 记录并返回状态。

    def fetch_file(self, path, **kwargs):
        self.fetches.append((path, kwargs))
        return self.payload
        # 记录并返回载荷。


class LocalCommandVM:
    """真实本地执行 VM：shell 跑命令（用于哈希校验的真实性）。"""
    def run_command(self, command, **kwargs):
        result = subprocess.run(
            command, shell=True, text=True, capture_output=True,
            timeout=kwargs.get("timeout"))
        return result.stdout + result.stderr
        # 真实子进程执行，返回 stdout+stderr。









def test_guest_text_confirmed_absent_skips_retrying_fetch():
    """确认缺失的文件跳过抓取（一次探测即定，绝不重试）。"""
    vm = GuestTextVM("RSI_FILE_STATE=ABSENT", b"must not be fetched")
    # 探测回复 ABSENT；载荷若被取走即测试失败。

    assert practice_loop._read_guest_text(vm, "/home/user/verifier_report.md") == ""
    # 读取结果为空串。
    assert vm.fetches == []
    # 关键：没有发生任何抓取。
    assert "test -f /home/user/verifier_report.md" in vm.commands[0][0]
    # 探测命令用 test -f 检查该路径。


def test_guest_text_present_fetches_long_report_losslessly():
    """在册文件无上限无损抓取（max_bytes=None）。"""
    report = ("# free-form report\n" + "evidence\n" * 1000
              + "VERDICT: FAIL\n").encode()
    # 7KB 级报告。
    vm = GuestTextVM("RSI_FILE_STATE=PRESENT", (report, ""))

    actual = practice_loop._read_guest_text(vm, "/home/user/verifier_report.md")

    assert actual.encode() == report
    # 逐字节无损。
    assert vm.fetches == [(
        "/home/user/verifier_report.md", {"max_bytes": None})]
    # 关键：抓取带 max_bytes=None（永不截断）。


def test_guest_text_indeterminate_presence_falls_back_to_fetch():
    """探测不确定（通道错误）时回退为直接抓取。"""
    report = b"# report\nVERDICT: PASS\n"
    vm = GuestTextVM(
        "[channel error: Timeout - the machine did not answer]", report)
    # 探测返回通道错误。

    assert practice_loop._read_guest_text(
        vm, "/home/user/verifier_report.md") == report.decode()
    # 回退抓取拿到内容。
    assert len(vm.fetches) == 1
    # 恰好一次抓取。


def test_same_context_stall_continuation_repairs_plain_json_transport_only():
    """停摆续行提示只修纯 JSON 传输：无判定/状态/决策词（不引导答案）。"""
    path = "/home/user/arbitrary_handoff.md"
    prompt = practice_loop._continue_after_stall("GENERIC", path)
    # 构造续行提示。

    assert PLAIN_JSON_TRANSPORT_NOTE in prompt
    # 纯 JSON 传输提示在场。
    assert "SAME-CONTEXT CONTINUATION" in prompt
    assert path in prompt
    # 同上下文续行与路径明示。
    assert "not treated as a submission, verdict" in " ".join(prompt.split())
    # 明说这不是提交/判定。
    assert "VERDICT:" not in prompt
    assert "STATUS:" not in prompt
    assert "DECISION:" not in prompt
    # 关键：三种令牌都不出现（续行绝不替 Agent 作答）。


class ScriptedAgents:
    """脚本化四角色 Agent：按步骤驱动 FakeVM 并记录每次调用的现场。"""

    def __init__(self, vm: FakeVM, steps):
        self.vm = vm
        self.steps = list(steps)
        # 剩余步骤脚本。
        self.calls = []
        # 每次调用的完整现场记录。

    def __call__(self, prompt, _vm, _cfg, sink, *, initial_history=None,
                 continue_context=False, allow_noop_done=False, **_kwargs):
        assert _vm is self.vm
        assert self.steps, f"unexpected Agent call: {prompt[:120]}"
        # 调用必须指向本 VM 且脚本未耗尽（多余调用即测试失败）。
        step = self.steps.pop(0)
        history_in = copy.deepcopy(initial_history or [])
        call = {
            "name": step["name"],
            "prompt": prompt,
            "history_in": history_in,
            "continue_context": continue_context,
            "allow_noop_done": allow_noop_done,
            "tree_before": copy.deepcopy(self.vm.tree),
            "memory_before": dict(self.vm.memory),
            "execution_evidence_before": dict(
                self.vm.execution_evidence),
            "sink": sink.root,
        }
        # 记录：提示词、入向历史、续跑/空完成开关、调用前的树/记忆/证据、sink 根。
        self.calls.append(call)
        if "check" in step:
            checked = step["check"](self.vm, call)
            assert checked is not False
            # 执行步骤自带断言（返回 False 即失败）。
        if "tree" in step:
            self.vm.tree = copy.deepcopy(step["tree"])
            # 步骤动作：设置项目树。
        if "target_shape_safe" in step:
            self.vm.target_shape_safe = bool(step["target_shape_safe"])
            # 步骤动作：设置目标形状开关。
        if "memory" in step:
            self.vm.memory = dict(step["memory"])
            # 步骤动作：设置 guest 记忆。
        if step.get("path"):
            self.vm.files[step["path"]] = step["text"].encode()
            # 步骤动作：写入协议文件（交接/报告/诊断）。
        if "program" in step:
            sink.save_program(1, "bash", step["program"])
            sink.save_trace(1, SimpleNamespace(
                stdout=step.get("trace", ""), exit_code=0,
                secs=0.1, timed_out=False))
            # 步骤动作：保存程序与轨迹（Actor 执行证据的素材）。
        terminal_probe = _kwargs.get("terminal_handoff_ready")
        call["terminal_handoff_ready"] = bool(
            terminal_probe and terminal_probe())
        # 记录终局探针在本次调用时刻的判定。
        history = history_in + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": step["name"]},
        ]
        call["history_out"] = copy.deepcopy(history)
        # 出向历史 = 入向 + 问答对。
        result = SimpleNamespace(
            status=step.get("status", "done"), iters=1, turns=1,
            wall_secs=1.0)
        return result, history
        # 返回循环结果（默认 done）与历史。


class FakeHarness:
    """伪 harness：捕获/重放/校验/推送/拉取/日志/审计/事件的完整内存实现。"""

    def __init__(self, vm: FakeVM, driver: ScriptedAgents):
        self.vm = vm
        self.driver = driver
        self.snapshots = {}
        # host 目录 → 树快照（capture 写、replay/verify 读）。
        self.journals = []
        # 记忆日志记录。
        self.pulls = []
        # 拉取的记忆树序列。
        self.memory_audits = []
        # 记忆审计输入序列。
        self.installs = []
        # 安装的记忆树序列。
        self.audit_calls = []
        # 文本/转写/捕获审计调用记录。
        self.events = []
        # 事件流。
        self.evidence_pushes = []
        # 执行证据推送记录。
        self.evidence_push_ok = True
        # 注入开关：证据推送失败。

    def reset(self, vm, target):
        assert vm is self.vm
        assert target == TARGET
        vm.files.clear()
        vm.tree = None
        vm.memory = {}
        vm.execution_evidence = {}
        vm.execution_evidence_expected = {}
        vm.target_shape_safe = True
        return {"ok": True}
        # 重置 VM 到干净状态（目标必须是北极星）。

    def read_text(self, vm, path):
        data = vm.files.get(path)
        return data.decode() if data is not None else ""
        # 读取 guest 协议文件。

    def capture(self, vm, _key, host_dir, guest_dirs):
        assert guest_dirs in ([practice_loop.PROJECT_ROOT_NAME],
                              [practice_loop.PROJECT_ROOT_NAME])
        # 关键：捕获只针对项目根这一个 guest 目录。
        if vm.tree is None:
            return {"ok": False, "files": 0, "bytes": 0}
            # 无树时如实报告失败。
        Path(host_dir).mkdir(parents=True, exist_ok=True)
        (Path(host_dir) / provisioning.MANIFEST).write_text(
            "fake captured candidate manifest\n")
        (Path(host_dir) / provisioning.MATERIALS).write_bytes(
            b"fake captured candidate archive")
        (Path(host_dir) / provisioning.ROOTS).write_text(
            '{"roots":["fake"]}\n', encoding="utf-8")
        self.snapshots[host_dir] = copy.deepcopy(vm.tree)
        # 写出三件捕获产物并记录树快照。
        return {"ok": True, "files": 1, "bytes": 1}

    def replay(self, vm, host_dir):
        if host_dir not in self.snapshots:
            return {"ok": False, "error": "missing snapshot"}
        vm.tree = copy.deepcopy(self.snapshots[host_dir])
        return {"ok": True, "mismatches": []}
        # 重放：从快照恢复树（逐字节一致）。

    def verify_project(self, vm, host_dir):
        if host_dir not in self.snapshots:
            return {"ok": False, "error": "missing snapshot"}
        if vm.tree != self.snapshots[host_dir]:
            return {"ok": False, "error": "candidate tree differs"}
        return {"ok": True, "mismatches": []}
        # 校验：当前树必须与快照一致（候选未被 mutation）。

    @staticmethod
    def _tree(path):
        root = Path(path)
        if not root.exists():
            return {}
        return {
            str(item.relative_to(root)): item.read_bytes()
            for item in root.rglob("*") if item.is_file()
        }
        # 把 host 目录读成 {相对路径: 字节} 树。

    def push_memory(self, vm, memory_dir):
        vm.memory = self._tree(memory_dir)
        return True
        # 推送记忆：host 目录树装进 guest。

    def push_dir(self, vm, host_dir, guest_path):
        assert guest_path == practice_loop.ACTOR_EXECUTION_EVIDENCE
        if not self.evidence_push_ok:
            return False
        evidence = self._tree(host_dir)
        vm.execution_evidence = evidence
        vm.execution_evidence_expected = dict(evidence)
        self.evidence_pushes.append(dict(evidence))
        return True
        # 推送执行证据：只允许推到专用 guest 路径；记录期望值（供篡改检测）。

    def pull_memory(self, vm):
        pulled = dict(vm.memory)
        self.pulls.append(pulled)
        return pulled
        # 拉取记忆并记录。

    def journal(self, journal_dir, episode, files, meta):
        self.journals.append({
            "dir": journal_dir, "episode": episode,
            "files": dict(files), "meta": dict(meta)})
        # 记录记忆日志（终局等元数据）。

    def install(self, memory_dir, files):
        installed = dict(files)
        self.installs.append(installed)
        practice_loop._atomic_install_memory(memory_dir, installed)
        # 安装记忆：记录并真实原子落盘。

    def audit_text(self, text, mode, authorized_instruction):
        self.audit_calls.append(("text", text, mode, authorized_instruction))
        return []
        # 文本审计：放行并记录。

    def audit_transcripts(self, root, mode, authorized_instruction):
        self.audit_calls.append(
            ("transcript", root, mode, authorized_instruction))
        return []
        # 转写审计：放行并记录。

    def audit_captured(self, root, authorized_instruction):
        self.audit_calls.append(
            ("capture", root, "practice", authorized_instruction))
        return []
        # 捕获审计：practice 模式放行并记录。

    def hooks(self, audit_memory=None):
        """组装 PracticeHooks（可注入自定义记忆审计行为）。"""
        def tracked_memory_audit(files, *args, **kwargs):
            candidate = dict(files)
            self.memory_audits.append(candidate)
            if audit_memory is not None:
                return audit_memory(candidate, *args, **kwargs)
            return candidate
            # 记录审计输入；可注入拒绝行为。

        return practice_loop.PracticeHooks(
            run_attempt=self.driver,
            reset_vm=self.reset,
            read_guest_text=self.read_text,
            capture_project=self.capture,
            replay_project=self.replay,
            verify_project=self.verify_project,
            audit_captured=self.audit_captured,
            push_memory=self.push_memory,
            pull_memory=self.pull_memory,
            audit_memory=tracked_memory_audit,
            journal_memory=self.journal,
            install_memory=self.install,
            validate_corpus=lambda _path: {"ok": True},
            audit_text=self.audit_text,
            audit_transcripts=self.audit_transcripts,
            push_dir=self.push_dir,
        )
        # 全部钩子接到本 harness 的实现。

    def append_event(self, event_type, *, status, state, payload):
        self.events.append({
            "event_type": event_type,
            "status": status,
            "state": dict(state),
            "payload": dict(payload),
        })
        # 事件收集器。


def cfg():
    # The fake Agent ignores model settings. These fields pin the runtime's
    # generic config overlay without importing production model clients.
    return SimpleNamespace(
        practice_mode=False, independent_verify=True,
        agent_decided_stop=False, practice_done_requires="",
        history_keep_pairs=80, max_resumes=3,
        max_iters=2000, wall_clock_secs=86400)
    # 四个角色共用的运行时配置形状：伪 Agent 忽略模型设置；这些字段钉住
    # 运行时的通用配置叠加，且不导入生产模型客户端。


def project_step(name, project, tree, status="done", check=None):
    """课程步骤：写项目决策交接 + 设置 fixture 树。"""
    step = {
        "name": name,
        "path": practice_loop.CURRICULUM_HANDOFF,
        "text": "DECISION: PROJECT\n" + project,
        "tree": tree,
        "status": status,
    }
    if check is not None:
        step["check"] = check
    return step
    # 课程写 DECISION: PROJECT + 项目任务书；树是本项目的 fixture。


def actor_step(name, status, tree, memory=None, check=None):
    """Actor 步骤：写 STATUS 交接 + 程序/轨迹证据 + 可选（非法）工作期记忆。"""
    step = {
        "name": name,
        "path": practice_loop.ACTOR_HANDOFF,
        "text": f"STATUS: {status}\n{name} handoff evidence",
        "tree": tree,
        "program": f"echo {name}",
        "trace": f"completed {name}",
    }
    if memory is not None:
        step["memory"] = memory
    if check is not None:
        step["check"] = check
    return step
    # Actor 写 STATUS 交接；程序/轨迹构成执行证据；memory 是工作期记忆
    # （若为非法编辑，应被边界丢弃）。


def verifier_step(name, verdict, check=None, tree=None):
    """Verifier 步骤：写 VERDICT 报告（可选树设置，用于突变测试）。"""
    step = {
        "name": name,
        "path": practice_loop.VERIFIER_REPORT,
        "text": f"VERDICT: {verdict}\n{name} independently observed evidence",
    }
    if tree is not None:
        step["tree"] = tree
    if check is not None:
        step["check"] = check
    return step
    # Verifier 写 VERDICT 报告；tree 用于"唯一验证器突变候选"测试。


def memory_step(name, files, check=None, status="done"):
    """记忆步骤：设置 guest 记忆树（草稿/对账两种）。"""
    step = {"name": name, "memory": files, "status": status}
    if check is not None:
        step["check"] = check
    return step
    # 记忆阶段把 files 装进 guest 记忆。


def diagnosis_step(name, text, check=None):
    """诊断步骤：写学习诊断文件。"""
    step = {
        "name": name,
        "path": practice_loop.LEARNING_DIAGNOSIS,
        "text": text,
    }
    if check is not None:
        step["check"] = check
    return step
    # Actor 学习诊断写入 LEARNING_DIAGNOSIS。


def terminal_step(name="curriculum-converged", token="CONVERGED"):
    """终局步骤：课程发布 DECISION 令牌（CONVERGED/STALLED/SATURATED）。"""
    return {
        "name": name,
        "path": practice_loop.CURRICULUM_HANDOFF,
        "text": f"DECISION: {token}\nEvidence-based outer-loop decision.",
    }
    # 课程终局决策。


def run(tmp_path, steps, audit_memory=None):
    """标准驱动：FakeVM + 脚本 + harness 跑完 evolve_practice 并校验脚本耗尽。"""
    vm = FakeVM()
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)
    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(audit_memory=audit_memory),
        event_sink=harness.append_event)
    assert not driver.steps
    # 脚本必须被完整消费（状态机不多不少地按脚本走）。
    return result, driver, harness
























def test_target_task_visible_asset_names_are_authorized_without_weakening_fence(monkeypatch):
    """目标任务的可见资产名获得授权，且围栏不被削弱。"""
    from tools import exam_fence
    inputs = ("/home/user/source_document.pdf", "/home/user/source_table.csv")
    # This unit test exercises authorization, not external denylist extraction.
    monkeypatch.setattr(exam_fence, "_load_constants", lambda: {
        "056": list(inputs),
    })
    # 注入常量表：任务 056 的可见输入。
    visible_names = " ".join(inputs)

    assert audit_text(
        visible_names, mode="exam", authorized_instruction=TARGET)
    # 对照组：对北极星指令，可见资产名命中（未授权面）。
    assert audit_text(
        visible_names, mode="exam",
        authorized_instruction=target_learning._target_audit_surface(TARGET, inputs)) == []
    # 用目标审计面（任务+可见输入）授权后：可见资产名放行。
    assert audit_text(
        "/home/example/OSWorld-V2/evaluation_examples/task_class/task_056.py",
        mode="exam",
        authorized_instruction=target_learning._target_audit_surface(TARGET, inputs))










    # 基准仓库内部路径仍命中（围栏不被资产授权削弱）。








def test_actor_execution_view_is_exact_cumulative_and_reasoning_free(
        tmp_path):
    """Actor 执行证据视图：确切、累积、无推理字段、manifest 哈希确定。"""
    actor = tmp_path / "actor"
    first = actor / "attempt_001/segment_000/iter_01"
    second = actor / "attempt_002/segment_001/iter_09"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    long_trace = b"x" * 20_001
    (first / "program.sh").write_bytes(b"echo first\n")
    (first / "trace.txt").write_bytes(long_trace)
    (first / "trace_meta.json").write_bytes(b'{"exit_code": 0}\n')
    (first / "look.json").write_bytes(b'{"path": "/tmp/a"}\n')
    (first / "turn.txt").write_bytes(b"private model turn")
    (actor / "attempt_001/segment_000/transcript.json").write_bytes(
        b"private transcript")
    (second / "program.py").write_bytes(b"print('second')\n")
    (second / "trace.txt").write_bytes(b"second trace")
    (second / "worklog.txt").write_bytes(b"private reasoning summary")
    (second / "checks.json").write_bytes(b"host judgement")
    # 素材：两个 attempt 的程序/轨迹/元数据/look + 私有文件（turn/transcript/
    # worklog/checks）。

    view_one = tmp_path / "view-one"
    view_two = tmp_path / "view-two"
    record_one = practice_loop._build_actor_execution_evidence(
        actor, 2, view_one)
    record_two = practice_loop._build_actor_execution_evidence(
        actor, 2, view_two)
    record_reused = practice_loop._build_actor_execution_evidence(
        actor, 2, view_one)
    # 同一素材构建三个视图（两新一复用）。

    expected = {
        "attempt_001/segment_000/iter_01/look.json",
        "attempt_001/segment_000/iter_01/program.sh",
        "attempt_001/segment_000/iter_01/trace.txt",
        "attempt_001/segment_000/iter_01/trace_meta.json",
        "attempt_002/segment_001/iter_09/program.py",
        "attempt_002/segment_001/iter_09/trace.txt",
        "SHA256SUMS",
    }
    actual = {
        str(path.relative_to(view_one))
        for path in view_one.rglob("*") if path.is_file()
    }
    assert actual == expected
    # 关键：视图确切包含程序/轨迹/元数据/look + SHA256SUMS，共 7 个文件。
    assert (view_one /
            "attempt_001/segment_000/iter_01/trace.txt").read_bytes() == \
        long_trace
    # 超长轨迹逐字节无损（20KB）。
    assert not any(name.endswith((
        "turn.txt", "transcript.json", "worklog.txt", "checks.json"))
                   for name in actual)
    # 关键：四种私有/推理文件绝不进视图。
    assert (view_one / "SHA256SUMS").read_bytes() == \
        (view_two / "SHA256SUMS").read_bytes()
    assert record_one["manifest_sha256"] == \
        record_two["manifest_sha256"]
    # manifest 哈希跨视图确定（与构建顺序无关）。
    assert record_reused == record_one
    # 复用视图返回同一记录（确定性）。
    assert record_one["files"] == 6
    # 记录的文件数是 6（不含 SHA256SUMS 自身）。
    assert record_one["bytes"] == sum(
        (view_one / name).stat().st_size for name in expected
        if name != "SHA256SUMS")
    # 字节数 = 6 个文件大小之和。
    assert record_one["manifest_sha256"] == hashlib.sha256(
        (view_one / "SHA256SUMS").read_bytes()).hexdigest()
    # manifest 哈希 = SHA256SUMS 文件的 sha256。

    (view_one / "unmanifested-empty-directory").mkdir()
    with pytest.raises(
            practice_loop.PracticeInfrastructureError, match="does not match"):
        practice_loop._build_actor_execution_evidence(actor, 2, view_one)
        # 视图被加了未入册的空目录 → 复用校验失败（"does not match"）。

    bad_actor = tmp_path / "bad-actor"
    bad_iter = bad_actor / "attempt_001/segment_000/iter_01"
    bad_iter.mkdir(parents=True)
    (bad_iter / "program.sh").symlink_to(first / "program.sh")
    with pytest.raises(
            practice_loop.PracticeInfrastructureError, match="file symlink"):
        practice_loop._build_actor_execution_evidence(
            bad_actor, 1, tmp_path / "bad-view")
        # 素材含文件符号链接 → 拒绝构建（逃逸防护）。


def test_guest_execution_evidence_verification_rejects_additive_tamper(
        tmp_path):
    """guest 侧证据校验拒绝附加式篡改（新增未入册文件）。"""
    actor = tmp_path / "actor"
    iteration = actor / "attempt_001/segment_000/iter_01"
    iteration.mkdir(parents=True)
    (iteration / "program.sh").write_bytes(b"echo exact\n")
    (iteration / "trace.txt").write_bytes(b"exact trace\n")
    view = tmp_path / "guest evidence with spaces"
    # 视图路径带空格（shell 引用正确性）。
    record = practice_loop._build_actor_execution_evidence(actor, 1, view)
    vm = LocalCommandVM()
    # 真实本地执行（sha256sum 真跑）。

    practice_loop._verify_actor_execution_evidence(
        vm, record, guest_path=str(view))
    # 初次校验通过。
    (view / "unmanifested.txt").write_bytes(b"fabricated")
    # 注入未入册文件。
    with pytest.raises(
            practice_loop.PracticeInfrastructureError, match="hash verification"):
        practice_loop._verify_actor_execution_evidence(
            vm, record, guest_path=str(view))
        # 关键：附加篡改必须被抓（清单+哈希双重校验）。


def test_curriculum_contract_keeps_all_deliverables_in_replayed_root():
    """课程契约：所有持久/最终交付物都在被快照/重放的项目根内。"""
    prompt = " ".join(practice_loop._curriculum_runtime_contract().split())
    # 空白归一后的运行时契约文本。

    assert "Every persistent or final Actor Agent deliverable required by the " \
        "natural-language project" in prompt
    assert "including any additional saved or exported copy" in prompt
    # 覆盖全部持久/最终交付物，含额外保存/导出的副本。
    assert "must have its destination inside /home/user/evolution_project" \
        in prompt
    # 目的地必须在项目根内。
    assert "never require a persistent or final deliverable at any path " \
        "outside that root" in prompt
    # 绝不要求根外路径的交付物。
    assert "free to choose every filename and layout within the root" in prompt
    # 根内文件名与布局自由。
    assert "harness snapshots and replays only this root" in prompt
    # harness 只快照/重放该根。


def test_context_lifecycle_isolation_and_verifier_authority(tmp_path):
    """上下文生命周期隔离与验证器权威：两 episode 的完整正向剧本。"""
    def memory_one_sees_failed_submission(vm, _call):
        assert vm.tree == {"state": "candidate-one"}
        assert vm.memory == {}  # prohibited work-phase edit was discarded
        # 记忆草稿阶段：看到失败提交的候选树；工作期非法编辑已被丢弃。

    def reconciliation_one_sees_unpromoted_draft(vm, call):
        assert vm.tree == {"state": "candidate-one"}
        assert vm.memory == {"draft.md": b"first-pass draft"}
        assert not (tmp_path / "lineage/memory/draft.md").exists()
        assert call["history_in"][-1] == {
            "role": "assistant", "content": "memory-one-draft"}
        # 对账阶段：看到草稿记忆（同一会话续跑）；草稿未晋升到持久库；
        # 历史衔接草稿段的出向。

    def actor_two_loads_promoted_memory(vm, _call):
        assert vm.tree == {"state": "fixtures-two"}
        assert vm.memory == {"lesson.md": b"grounded failure lesson"}
        # 第二个 Actor：装载第二套 fixtures 与已晋升的第一课。

    steps = [
        project_step("curriculum-project-one", PROJECT_1,
                     {"state": "fixtures-one"}),
        actor_step("actor-one-submit", "SUBMIT", {"state": "candidate-one"},
                   memory={"illegal-work-edit.md": b"discard me"}),
        verifier_step("verifier-one-fail", "FAIL",
                      check=lambda vm, _c: vm.tree ==
                      {"state": "candidate-one"}),
        memory_step("memory-one-draft", {"draft.md": b"first-pass draft"},
                    check=memory_one_sees_failed_submission),
        memory_step("memory-one-reconciled",
                    {"lesson.md": b"grounded failure lesson"},
                    check=reconciliation_one_sees_unpromoted_draft),
        project_step("curriculum-project-two", PROJECT_2,
                     {"state": "fixtures-two"}),
        actor_step("actor-two-submit", "SUBMIT", {"state": "candidate-two"},
                   check=actor_two_loads_promoted_memory),
        verifier_step("verifier-two-pass", "PASS",
                      check=lambda vm, _c: vm.tree ==
                      {"state": "candidate-two"}),
        memory_step("memory-two-draft", {
            "lesson.md": b"grounded failure lesson",
            "draft-transfer.md": b"first-pass transfer claim",
        }),
        memory_step("memory-two-reconciled", {
            "lesson.md": b"grounded failure lesson",
            "transfer.md": b"verified transfer",
        }),
        terminal_step(),
    ]
    # 完整剧本：项目1（提交→FAIL→草稿→对账出教训）→ 项目2（装载教训→
    # 提交→PASS→迁移草稿→对账出迁移）→ 收敛。
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    assert result.projects == 2
    # 收敛、两个项目。
    calls = {call["name"]: call for call in driver.calls}

    # Actor and Verifier contexts are fresh for every episode. Each episode has
    # exactly one submission and one authoritative verdict.
    assert calls["actor-one-submit"]["history_in"] == []
    assert calls["actor-two-submit"]["history_in"] == []
    assert calls["verifier-one-fail"]["history_in"] == []
    assert calls["verifier-two-pass"]["history_in"] == []
    assert all(not call["continue_context"] for call in (
        calls["actor-one-submit"], calls["actor-two-submit"],
        calls["verifier-one-fail"], calls["verifier-two-pass"]))
    # 关键：每个 episode 的 Actor/Verifier 上下文全新（无续跑、空历史）。

    # Only the Verifier receives the reasoning-free execution evidence view.
    evidence_one = calls["verifier-one-fail"]["execution_evidence_before"]
    evidence_two = calls["verifier-two-pass"]["execution_evidence_before"]
    assert "attempt_001/segment_000/iter_01/program.sh" in evidence_one
    assert b"actor-one-submit" in evidence_one[
        "attempt_001/segment_000/iter_01/program.sh"]
    assert b"actor-two-submit" in evidence_two[
        "attempt_001/segment_000/iter_01/program.sh"]
    assert b"actor-one-submit" not in b"".join(evidence_two.values())
    # 关键：证据视图逐 episode 累积（第二个 Verifier 能看到第一个的程序），
    # 但只在 Verifier 的调用里出现。
    assert len(harness.evidence_pushes) == 2
    # 两次证据推送（每 episode 一次）。
    for name, call in calls.items():
        if name.startswith("verifier-"):
            assert practice_loop.ACTOR_EXECUTION_EVIDENCE in call["prompt"]
        else:
            assert practice_loop.ACTOR_EXECUTION_EVIDENCE not in call["prompt"]
    # 关键：只有 Verifier 的提示词引用执行证据路径。

    # Both memory phases retain the submitting Actor context and promote only
    # after whole-corpus reconciliation.
    assert calls["memory-one-draft"]["continue_context"] is True
    assert calls["memory-one-reconciled"]["continue_context"] is True
    assert calls["memory-one-reconciled"]["history_in"] == \
        calls["memory-one-draft"]["history_out"]
    assert calls["memory-one-draft"]["allow_noop_done"] is True
    assert calls["memory-one-reconciled"]["allow_noop_done"] is True
    # 关键：两个记忆段都续跑提交 Actor 的上下文；对账段历史衔接草稿段；
    # 两者都允许空完成（无新可学内容时合法收尾）。

    # Curriculum alone persists across episodes and sees black-box Verifier
    # evidence, not the Actor handoff or private memory contents.
    assert calls["curriculum-project-one"]["history_in"] == []
    assert len(calls["curriculum-project-two"]["history_in"]) == 2
    assert len(calls["curriculum-converged"]["history_in"]) == 4
    # 关键：只有课程跨 episode 持久（第 2 次开项目带 2 条历史、终局带 4 条）。
    second_prompt = calls["curriculum-project-two"]["prompt"]
    assert "TERMINAL OUTCOME: FAIL" in second_prompt
    assert "verifier-one-fail independently observed evidence" in second_prompt
    assert "actor-one-submit handoff evidence" not in second_prompt
    assert "grounded failure lesson" not in second_prompt
    # 关键：课程看到黑盒 Verifier 证据（终局+独立证据），看不到 Actor 交接
    # 原文或私有记忆内容。

    assert [entry["meta"]["terminal_outcome"]
            for entry in harness.journals] == ["FAIL", "PASS"]
    # 两次记忆日志的终局如实（FAIL/PASS）。
    assert harness.pulls == [
        {"lesson.md": b"grounded failure lesson"},
        {
            "lesson.md": b"grounded failure lesson",
            "transfer.md": b"verified transfer",
        },
    ]
    # 只拉取对账后的记忆（草稿从不被拉取）。
    assert harness.memory_audits == harness.pulls
    assert harness.installs == harness.pulls
    # 审计与安装的正是拉取的内容（整库对账后才入库）。

    assert [event["event_type"] for event in harness.events] == [
        "PROJECT_OPENED", "ACTOR_PHASE_STARTED", "VERIFIER_PHASE_STARTED",
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED",
        "PROJECT_OPENED", "ACTOR_PHASE_STARTED", "VERIFIER_PHASE_STARTED",
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED",
    ]
    # 事件序列：每 episode 恰好 开项目→Actor→Verifier→记忆→闭项目。
    assert not any(event["event_type"] ==
                   "VERIFIER_PASS_CONFIRMATION_STARTED"
                   for event in harness.events)
    # 关键：没有 PASS 确认阶段（PASS 是终局）。
    verifier_events = [event for event in harness.events
                       if event["event_type"] == "VERIFIER_PHASE_STARTED"]
    assert len(verifier_events) == 2
    assert all(len(event["payload"]["actor_execution_manifest_sha256"]) == 64
               for event in verifier_events)
    # Verifier 阶段事件携带 64 位证据 manifest 哈希。
    assert harness.audit_calls
    assert all(call[-1] == TARGET for call in harness.audit_calls)
    # 全部审计都以北极星为授权指令。


def test_fail_is_terminal_and_distils_as_unverified_failure(tmp_path):
    """FAIL 是终局且只判一次；以"未验证失败"蒸馏进记忆。"""
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-submit", "SUBMIT", {"state": "partial"}),
        verifier_step("verifier-fail", "FAIL"),
        memory_step("memory-fail", {
            "unresolved.md": b"unverified limitation, not success"}),
        memory_step("memory-fail-reconciled", {
            "unresolved.md": b"unverified limitation, not success"}),
        terminal_step("curriculum-stalled", "STALLED"),
    ]
    # 剧本：提交部分结果 → FAIL → 记忆把"未解决的局限"蒸馏（不是成功）→
    # 课程 STALLED。
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "stalled"
    assert [call["name"] for call in driver.calls].count(
        "verifier-fail") == 1
    # 关键：FAIL 只判一次（无重判）。
    assert harness.journals[0]["meta"]["terminal_outcome"] == "FAIL"
    # 记忆日志终局 FAIL。
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["terminal_outcome"] == "FAIL"
    assert outcome["verifier_reports"][0].startswith("VERDICT: FAIL")
    # episode 终局 FAIL + 报告原样。


def test_pass_is_terminal_without_fresh_confirmation(tmp_path):
    """PASS 是终局且无需新鲜确认（无二次验证周期）。"""
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-submit", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-pass", {"verified.md": b"verified experience"}),
        memory_step("memory-pass-reconciled",
                    {"verified.md": b"verified experience"}),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert [name for name in calls if name.startswith("verifier-")] == [
        "verifier-pass"]
    # 关键：只有一个 Verifier 调用（PASS 无需确认）。
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["terminal_outcome"] == "PASS"
    assert outcome["verification_cycles"] == 1
    assert len(outcome["actor_handoffs"]) == 1
    assert len(outcome["verifier_reports"]) == 1
    # 终局 PASS、一个验证周期、各一份交接/报告。
    assert (tmp_path / "lineage/episodes/ep001/handoffs/"
            "verifier_001.md").read_text().startswith("VERDICT: PASS")
    # 报告交接文件落盘。
    assert harness.journals[0]["meta"]["terminal_outcome"] == "PASS"
    assert len(harness.evidence_pushes) == 1
    assert all(event["event_type"] != "VERIFIER_PASS_CONFIRMATION_STARTED"
               for event in harness.events)
    # 日志终局 PASS；一次证据推送；无 PASS 确认事件。


@pytest.mark.parametrize("failure", ["push", "hash"])
def test_execution_evidence_transport_fails_closed_before_verifier(
        tmp_path, failure):
    """执行证据传输失败在 Verifier 之前 fail-closed（不评分、不记忆）。"""
    vm = FakeVM()
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
    ]
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)
    if failure == "push":
        harness.evidence_push_ok = False
    else:
        vm.evidence_verify_ok = False
    # 参数化：推送失败 / guest 内哈希校验失败。

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "execution evidence" in result.reason
    # infra 状态且原因指向执行证据。
    assert not driver.steps
    assert [call["name"] for call in driver.calls] == [
        "curriculum-project", "actor-ready"]
    # 课程与 Actor 已跑；Verifier 从未被调用。
    assert harness.pulls == []
    assert harness.memory_audits == []
    assert harness.journals == []
    assert harness.installs == []
    # 零记忆拉取/审计/日志/安装。
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": False,
        "recovery_open": False,
    }
    # 停在 Verifier 阶段开始处（项目开着、记忆阶段未开）。
    assert all(event["event_type"] not in {
        "MEMORY_PHASE_STARTED", "PROJECT_CLOSED"}
               for event in harness.events)
    # 无记忆阶段、无闭项目。
    assert not (tmp_path / "lineage/episodes/ep001/handoffs/"
                "verifier_001.md").exists()
    # 无验证报告交接（基础设施故障绝不评分）。


@pytest.mark.parametrize("tamper_kind", ["replace", "add"])
def test_execution_evidence_mutation_invalidates_published_verdict(
        tmp_path, tamper_kind):
    """执行证据被篡改（替换/附加）即失效已发布的 PASS 判定。"""
    def tamper(vm, _call):
        path = "attempt_001/segment_000/iter_01/program.sh"
        assert path in vm.execution_evidence
        if tamper_kind == "replace":
            vm.execution_evidence[path] = b"fabricated replacement"
        else:
            vm.execution_evidence["unmanifested/claim.txt"] = \
                b"fabricated additive evidence"
        # Verifier 调用前篡改 guest 证据：替换已入册文件 / 追加未入册文件。

    vm = FakeVM()
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass-after-tamper", "PASS", check=tamper),
    ]
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "hash verification" in result.reason
    # 发布后复检发现篡改 → infra（判定失效）。
    assert not driver.steps
    assert harness.pulls == []
    assert harness.journals == []
    assert harness.installs == []
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"
    assert not (tmp_path / "lineage/episodes/ep001/handoffs/"
                "verifier_001.md").exists()
    # 零记忆操作、无报告交接（被篡改的判定绝不生效）。


def test_candidate_mutation_by_sole_verifier_fails_closed(tmp_path):
    """唯一验证器突变候选 → fail-closed（快照完整性校验失败）。"""
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step(
            "verifier-mutated-candidate", "PASS",
            tree={"state": "verifier-mutation"}),
    ]
    # Verifier 在验证期间修改候选树。
    vm = FakeVM()
    driver = ScriptedAgents(vm, steps)
    harness = FakeHarness(vm, driver)

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event)

    assert result.status == "infra"
    assert "candidate snapshot integrity verification failed" in result.reason
    # 候选快照完整性校验失败。
    assert not driver.steps
    assert harness.pulls == []
    assert harness.journals == []
    assert harness.installs == []
    handoffs = tmp_path / "lineage/episodes/ep001/handoffs"
    assert not (handoffs / "verifier_001.md").exists()
    assert not (handoffs / "verifier_pass_proposal_001.md").exists()
    # 无报告交接、无 PASS 提案（被突变的判定作废）。
    assert harness.events[-1]["event_type"] == "VERIFIER_PHASE_STARTED"
    # 停在 Verifier 阶段。


def test_malformed_handoff_resumes_same_agent_context_without_round_cap(
        tmp_path):
    """畸形交接续跑同一 Agent 上下文且无轮数上限（修复后继续）。"""
    malformed = verifier_step("verifier-malformed", "FAIL")
    malformed["text"] = "I am not using the transport token yet."
    # 第一次 Verifier 报告不用传输令牌（畸形）。
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-yield", "SUBMIT", {"state": "partial"}),
        malformed,
        verifier_step("verifier-corrected", "FAIL"),
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step(),
    ]
    result, driver, _ = run(tmp_path, steps)
    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert len(calls["verifier-corrected"]["history_in"]) == 2
    assert calls["verifier-corrected"]["continue_context"] is True
    # 修正段续跑同一上下文（入向历史 2 条）。
    assert "not contain an allowed" in \
        calls["verifier-corrected"]["prompt"]
    # 提示词指出上次交接不含合法令牌。


def test_actor_accepts_only_neutral_submit_and_repairs_transport_in_context(
        tmp_path):
    """Actor 只认中性 SUBMIT；废弃令牌在上下文内修复传输。"""
    obsolete = actor_step(
        "actor-obsolete-ready", "SUBMIT", {"state": "candidate"})
    obsolete["text"] = "STATUS: READY\nobsolete success-claiming token"
    # 第一次交接用废弃的 READY（宣称成功）令牌。
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        obsolete,
        actor_step("actor-submit", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-fail", "FAIL"),
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step(),
    ]
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    assert len(calls["actor-submit"]["history_in"]) == 2
    assert calls["actor-submit"]["continue_context"] is True
    assert "did not contain an allowed" in calls["actor-submit"]["prompt"]
    # 修复段续跑同一上下文，提示词指出令牌不被接受。
    assert [event["event_type"] for event in harness.events].count(
        "ACTOR_PHASE_STARTED") == 1
    # 关键：仍是同一个 Actor 阶段（修复不重开阶段）。
    assert practice_loop._parse_handoff(
        "STATUS: SUBMIT\ncurrent state", practice_loop._ACTOR_TOKEN).token == \
        "SUBMIT"
    assert practice_loop._parse_handoff(
        "STATUS: READY\nold token", practice_loop._ACTOR_TOKEN) is None
    assert practice_loop._parse_handoff(
        "STATUS: YIELD\nold token", practice_loop._ACTOR_TOKEN) is None
    # 解析器只认 SUBMIT；READY/YIELD 都是 None。


def test_verifier_heading_then_unique_verdict_publishes_atomically(tmp_path):
    """标题 + 唯一判定原子发布：stalled 边界上的有效发布胜出。"""
    report = (
        "# Independent report\n\nVERDICT: FAIL\n\n"
        "The required application workflow is contradicted by the evidence."
    )
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-yield", "SUBMIT", {"state": "candidate"}),
        {
            "name": "verifier-published",
            "path": practice_loop.VERIFIER_REPORT,
            "text": report,
            # A valid publication wins even if a generic loop boundary is
            # reported at the same time; no second stochastic verdict is drawn.
            "status": "stalled",
        },
        memory_step("memory", {}),
        memory_step("memory-reconciled", {}),
        terminal_step("curriculum-stalled", "STALLED"),
    ]
    # Verifier 段状态 stalled 但已发布有效报告（发布胜过边界状态）。
    result, driver, _ = run(tmp_path, steps)

    assert result.status == "stalled"
    call = next(c for c in driver.calls
                if c["name"] == "verifier-published")
    assert call["terminal_handoff_ready"] is True
    # 调用时刻终局探针已就绪。
    outcome = json.loads(
        (tmp_path / "lineage/episodes/ep001/outcome.json").read_text())
    assert outcome["verifier_reports"] == [report]
    assert outcome["terminal_outcome"] == "FAIL"
    # 报告原子入库；终局 FAIL；不抽第二次随机判定。


def test_verifier_report_token_must_be_unique_and_outside_code_fences():
    """VERDICT 令牌必须唯一且在代码围栏/缩进之外。"""
    parse = practice_loop._parse_unique_handoff
    token = practice_loop._VERIFIER_TOKEN

    accepted = parse("# title\n\nVERDICT: FAIL\nEvidence", token)
    assert accepted is not None and accepted.token == "FAIL"
    assert accepted.text.startswith("# title")
    # 顶层唯一令牌可解析，正文保留。
    assert parse("VERDICT: FAIL\nVERDICT: FAIL", token) is None
    # 幂等重复在此解析器下也不行？（唯一性比报告解析器更严）
    assert parse("VERDICT: FAIL\nVERDICT: PASS", token) is None
    # 冲突拒绝。
    assert parse("```text\nVERDICT: FAIL\n```", token) is None
    # 代码围栏内的令牌不算数。
    assert parse("    VERDICT: FAIL", token) is None
    # 缩进（非顶层）的令牌不算数。


def test_memory_reconciliation_continues_uncapped_until_actor_done(tmp_path):
    """记忆对账在 Actor done 之前不限轮续跑（budget 状态不算失败）。"""
    draft = {"draft.md": b"first-pass draft"}
    final = {"lesson.md": b"reconciled durable lesson"}
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", draft),
        memory_step("reconciliation-incomplete", draft, status="budget"),
        memory_step("reconciliation-done", final),
        terminal_step(),
    ]
    # 对账第一次以 budget 状态结束（未完成），第二次才对出最终课。
    result, driver, harness = run(tmp_path, steps)

    assert result.status == "converged"
    calls = {call["name"]: call for call in driver.calls}
    first = calls["reconciliation-incomplete"]
    second = calls["reconciliation-done"]
    assert first["history_in"] == calls["memory-draft"]["history_out"]
    assert second["history_in"] == first["history_out"]
    assert first["continue_context"] is True
    assert second["continue_context"] is True
    assert first["allow_noop_done"] is True
    assert second["allow_noop_done"] is True
    # 对账段链式续跑（历史逐段衔接）、允许空完成。
    assert first["sink"].endswith("memory_reconciliation/segment_000")
    assert second["sink"].endswith("memory_reconciliation/segment_001")
    # 对账段目录递增。
    assert "without your terminal decision" in second["prompt"]
    # 续段提示词指出"还没有你的终局决定"（继续对账）。
    assert harness.pulls == [final]
    assert harness.memory_audits == [final]
    assert harness.journals[0]["files"] == final
    assert harness.installs == [final]
    # 只拉取/审计/入账/安装对账完成的最终记忆。


def test_memory_boundary_rejects_whole_candidate_and_preserves_durable_bank(
        tmp_path):
    """记忆边界拒绝整批候选：隔离且持久记忆库逐字节无损。"""
    lineage_memory = tmp_path / "lineage/memory"
    lineage_memory.mkdir(parents=True)
    (lineage_memory / "old.md").write_bytes(b"known-good memory")
    # 预置一课已知良好的持久记忆。
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"unpromoted draft"}),
        memory_step("memory-leak", {"leak.md": b"forbidden grader content"}),
    ]
    # 对账试图带入被封禁的评分器内容。

    def reject(_files, *_args, **_kwargs):
        return {}
        # 注入：记忆审计拒绝整批（返回空）。

    result, _, harness = run(tmp_path, steps, audit_memory=reject)
    assert result.status == "quarantined"
    # 状态隔离。
    assert harness.journals == []
    # 零日志（被拒内容不入账）。
    assert (lineage_memory / "old.md").read_bytes() == b"known-good memory"
    assert not (lineage_memory / "leak.md").exists()
    # 关键：持久库原样保留；泄漏内容未落盘。
    assert harness.events[-1]["event_type"] == "MEMORY_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": True,
        "recovery_open": False,
    }
    # 停在记忆阶段（项目与记忆阶段都开着）。
    assert all(event["event_type"] != "PROJECT_CLOSED"
               for event in harness.events)
    # 项目未被闭合（隔离而非完成）。


def test_emergency_ceiling_is_infrastructure_not_learning_outcome(tmp_path):
    """应急上限是基础设施故障而非学习结果（原因里无 PASS/CONVERGED）。"""
    steps = [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        {"name": "actor-emergency-ceiling", "status": "safety_ceiling"},
    ]
    # Actor 段撞上安全上限。
    result, _, harness = run(tmp_path, steps)
    assert result.status == "infra"
    assert "safety_ceiling" in result.reason
    # infra 状态 + 原因指向上限。
    assert harness.journals == []
    # 零记忆日志。
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["status"] == "infra"
    assert "PASS" not in state["reason"]
    assert "CONVERGED" not in state["reason"]
    # 状态文件 infra，原因不含学习结果词。
    assert harness.events[-1]["event_type"] == "ACTOR_PHASE_STARTED"
    assert harness.events[-1]["state"] == {
        "project_open": True,
        "memory_phase_open": False,
        "recovery_open": False,
    }
    # 停在 Actor 阶段开始处。


def test_practice_has_no_host_grade_preflight_reward_or_evaluator_surface():
    """源码级断言：练习主循环没有宿主评分/预检/奖励/评估器面。"""
    source = inspect.getsource(practice_loop)
    assert "from explore import reward" not in source
    assert "grade_instance(" not in source
    assert "run_dry_gate(" not in source
    assert "preflight" not in source.lower().replace(
        "project-specific preflight", "")
    assert "OSWorldEvaluator" not in source
    # 无奖励导入、无评分调用、无干跑门、无预检（除项目特定预检措辞）、
    # 无 OSWorld 评估器。
    assert "terminal_outcome = verifier_decision.token" in source
    # 终局只来自验证器判定令牌。
    assert "VERIFIER_PASS_CONFIRMATION_STARTED" not in source
    # 无 PASS 确认阶段。
    assert "self_evolving_actor_repair_msg" not in source
    # 无 Actor 修复消息（练习不做修复轮）。
    assert "verifier_decision.body" not in source
    # 主循环不消费验证器正文（正文是给课程的黑盒证据）。
    signature = inspect.signature(practice_loop.evolve_practice)
    assert "evaluator" not in signature.parameters
    assert "score" not in signature.parameters
    # evolve_practice 签名无 evaluator/score 参数（结构上密封）。


def test_phase1_budget_counts_complete_projects_and_checkpoints_exact_memory(
        tmp_path):
    """Phase-1 预算按完整项目计数，检查点提交精确记忆。"""
    final_memory = {
        "office/layout.md": b"reusable verified layout procedure",
        "media/timing.md": b"frame-accurate timing lesson",
    }
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"uncommitted"}),
        memory_step("memory-reconciled", final_memory),
    ])
    harness = FakeHarness(vm, driver)

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=1, checkpoint_projects=(0, 1),
        phase1_exploration=True)
    # Phase-1 探索：预算 1 项目、在项目 0 与 1 边界做检查点。

    assert not driver.steps
    assert result.status == "budget_exhausted"
    assert result.projects == 1
    # 预算耗尽收尾、1 个完整项目。
    assert practice_loop._read_memory_tree(result.memory_dir) == final_memory
    # 结果记忆 = 对账后的最终记忆。
    assert practice_loop._read_memory_tree(str(
        tmp_path / "lineage/checkpoints/project_000/memory")) == {}
    assert practice_loop._read_memory_tree(str(
        tmp_path / "lineage/checkpoints/project_001/memory")) == final_memory
    # 关键：项目 0 检查点是空记忆（尚未学习）、项目 1 检查点是完整最终记忆。
    checkpoints = [
        event for event in harness.events
        if event["event_type"] == "MEMORY_CHECKPOINT_COMMITTED"]
    assert [event["payload"]["project"] for event in checkpoints] == [0, 1]
    assert all(event["state"] == {
        "project_open": False,
        "memory_phase_open": False,
        "recovery_open": False,
    } for event in checkpoints)
    # 检查点事件按项目 0/1 提交，且提交时各阶段已闭合。
    state = json.loads((tmp_path / "lineage/state.json").read_text())
    assert state["phase1_exploration"] is True
    assert state["project_budget"] == 1
    assert state["status"] == "budget_exhausted"
    # 状态文件如实记录探索模式/预算/终态。


def test_phase1_curriculum_can_stop_early_with_saturated(tmp_path):
    """Phase-1 课程可提前以 SATURATED 收尾（0 个项目也有终局文本）。"""
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        terminal_step("curriculum-saturated", "SATURATED"),
    ])
    harness = FakeHarness(vm, driver)

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=8, checkpoint_projects=(0, 2, 4, 8),
        phase1_exploration=True)
    # 预算 8 但课程第一轮就 SATURATED。

    assert not driver.steps
    assert result.status == "saturated"
    assert result.projects == 0
    assert "DECISION: SATURATED" in result.terminal_text
    # 饱和收尾、0 项目、终局文本带决策。
    assert "TARGET TASK DISTRIBUTION" in driver.calls[0]["prompt"]
    # Phase-1 课程提示词带目标任务分布（探索模式）。


def test_phase1_curriculum_inspects_actor_owned_memory_as_disposable_copy(
        tmp_path):
    """课程以一次性副本查看 Actor 自有记忆（可见但不可持久持有）。"""
    learned = {"skills/grounded.md": b"Actor-owned grounded experience"}
    terminal = terminal_step("curriculum-saturated", "SATURATED")
    terminal["check"] = lambda vm, call: (
        vm.memory == learned
        and call["memory_before"] == learned
        and "CURRENT ACTOR-OWNED DURABLE MEMORY" in call["prompt"])
    # 终局步骤断言：课程调用时 guest 记忆与调用前记忆都是学到的内容，
    # 且提示词标注"当前 Actor 自有持久记忆"。
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        verifier_step("verifier-pass", "PASS"),
        memory_step("memory-draft", {"draft.md": b"uncommitted"}),
        memory_step("memory-reconciled", learned),
        terminal,
    ])
    harness = FakeHarness(vm, driver)

    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=8, checkpoint_projects=(0, 2, 4, 8),
        phase1_exploration=True)

    assert not driver.steps
    assert result.status == "saturated"
    assert practice_loop._read_memory_tree(result.memory_dir) == learned
    # 饱和收尾且结果记忆就是学到的内容（副本读不改变所有权）。


def test_phase1_resumes_only_from_closed_project_with_curriculum_context(
        tmp_path):
    """Phase-1 只从已闭合项目恢复，且恢复的课程上下文原样衔接。"""
    lineage = tmp_path / "lineage"
    memory_one = {"lesson.md": b"verified generation one"}
    vm_one = FakeVM()
    driver_one = ScriptedAgents(vm_one, [
        project_step("curriculum-project-1", PROJECT_1,
                     {"state": "fixtures-1"}),
        actor_step("actor-1", "SUBMIT", {"state": "candidate-1"}),
        verifier_step("verifier-1", "PASS"),
        memory_step("memory-1-draft", {"draft.md": b"draft"}),
        memory_step("memory-1-reconciled", memory_one),
        project_step("curriculum-project-2-lost", PROJECT_2,
                     {"state": "fixtures-2"}),
    ])
    # 第一次运行：完成项目 1 并做检查点，然后在项目 2 中途崩溃。
    harness_one = FakeHarness(vm_one, driver_one)
    hooks_one = harness_one.hooks()
    ordinary_capture = hooks_one.capture_project
    capture_calls = 0

    def fail_third_capture(*args, **kwargs):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 3:
            raise FileNotFoundError("simulated lossless capture defect")
        return ordinary_capture(*args, **kwargs)
        # 注入：第三次捕获抛"无损捕获缺陷"（模拟盘上损失）。

    hooks_one.capture_project = fail_third_capture
    first = practice_loop.evolve_practice(
        vm_one, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=hooks_one, event_sink=harness_one.append_event,
        project_budget=2, checkpoint_projects=(0, 2),
        phase1_exploration=True)

    assert first.status == "infra" and first.projects == 1
    assert practice_loop._read_memory_tree(first.memory_dir) == memory_one
    # 第一次运行 infra 收尾、1 个完整项目、记忆是第一代。
    recovered_history = [
        {"role": "user", "content": "prior persistent curriculum turn"},
        {"role": "assistant", "content": "prior uncaptured project decision"},
    ]
    transcript = lineage / "curriculum/turn_002/segment_000/transcript.json"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({
        "system": "test curriculum system", "messages": recovered_history,
    }), encoding="utf-8")
    # 手工放置项目 2 的课程转写（恢复源：未捕获项目决策的上下文）。

    memory_two = {"lesson.md": b"verified generations one and two"}
    vm_two = FakeVM()
    driver_two = ScriptedAgents(vm_two, [
        project_step("curriculum-project-2-rebuilt", PROJECT_2,
                     {"state": "fixtures-2-rebuilt"}),
        actor_step("actor-2", "SUBMIT", {"state": "candidate-2"}),
        verifier_step("verifier-2", "PASS"),
        memory_step("memory-2-draft", memory_one),
        memory_step("memory-2-reconciled", memory_two),
    ])
    harness_two = FakeHarness(vm_two, driver_two)
    resumed = practice_loop.evolve_practice(
        vm_two, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness_two.hooks(), event_sink=harness_two.append_event,
        project_budget=2, checkpoint_projects=(0, 2),
        phase1_exploration=True, resume_completed_boundary=True)
    # 第二次运行：从已闭合边界恢复，重建项目 2 并学出第二代记忆。

    assert resumed.status == "budget_exhausted"
    assert resumed.projects == 2
    assert practice_loop._read_memory_tree(resumed.memory_dir) == memory_two
    # 恢复后预算耗尽、2 个项目、记忆是两代合并。
    first_resumed_call = driver_two.calls[0]
    assert first_resumed_call["history_in"] == recovered_history
    assert first_resumed_call["continue_context"] is True
    assert "HARNESS RECOVERY NOTICE" in first_resumed_call["prompt"]
    assert "not a grade or convergence signal" in first_resumed_call["prompt"]
    # 关键：恢复的课程调用带原上下文 + harness 恢复通知（明说这不是评分
    # 或收敛信号）。
    assert (lineage / "episodes/ep001/outcome.json").is_file()
    assert (lineage / "episodes/ep002/outcome.json").is_file()
    # 两个 episode 的 outcome 都在（第一次的成果保留、第二次续写）。
    assert [event["event_type"] for event in harness_two.events[:2]] == [
        "PHASE_RESUMED", "PROJECT_OPENED"]
    # 恢复事件序列：先 PHASE_RESUMED 再开项目。
    assert not any(
        event["event_type"] == "MEMORY_CHECKPOINT_COMMITTED"
        and event["payload"]["project"] == 0
        for event in harness_two.events)
    # 关键：恢复运行不重复提交项目 0 的检查点（第一次已提交）。


def test_failed_phase1_resume_does_not_overwrite_existing_state(tmp_path):
    """失败的 Phase-1 恢复不改写既有状态文件（校验失败即原样保留）。"""
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    prior_state = {
        "schema_version": 1,
        "status": "infra",
        "projects": 5,
        "last_project": 5,
        # Deliberately incomplete: validation must fail without rewriting it.
        "sentinel": "preserve-me",
    }
    state_path = lineage / "state.json"
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")
    # 预置一份故意不完整（缺字段）的状态文件。

    vm = FakeVM()
    harness = FakeHarness(vm, ScriptedAgents(vm, []))
    result = practice_loop.evolve_practice(
        vm, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), project_budget=8,
        checkpoint_projects=(0, 2, 4, 8), phase1_exploration=True,
        resume_completed_boundary=True)
    # 尝试从该状态恢复（校验必然失败）。

    assert result.status == "infra"
    assert result.projects == 5 and result.last_project == 5
    # 恢复读取了既有进度（5 个项目）但以 infra 收尾。
    assert json.loads(state_path.read_text(encoding="utf-8")) == prior_state
    # 关键：状态文件逐字节原样（失败恢复绝不改写）。


def test_phase1_keyboard_interrupt_persists_infra_boundary(tmp_path):
    """键盘中断持久化 infra 边界（外部中断可安全恢复）。"""
    vm = FakeVM()
    harness = FakeHarness(vm, ScriptedAgents(vm, []))
    hooks = harness.hooks()

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt
        # 注入：Agent 调用即抛键盘中断。

    hooks.run_attempt = interrupt
    lineage = tmp_path / "lineage"
    result = practice_loop.evolve_practice(
        vm, str(lineage), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=hooks, project_budget=8,
        checkpoint_projects=(0, 2, 4, 8), phase1_exploration=True)

    assert result.status == "infra"
    assert result.projects == 0
    state = json.loads((lineage / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "infra"
    assert state["projects"] == 0
    assert state["reason"] == "KeyboardInterrupt: external interruption"
    # infra 边界落盘：0 项目、原因标明外部中断。


def test_phase1_agentic_verifier_sees_candidate_not_actor_private_evidence(
        tmp_path, monkeypatch):
    """Phase-1 的 Agent 化验证器只见候选，不见 Actor 私有证据。"""
    final_memory = {"lesson.md": b"grounded lesson"}
    vm = FakeVM()
    driver = ScriptedAgents(vm, [
        project_step("curriculum-project", PROJECT_1,
                     {"state": "fixtures"}),
        actor_step("actor-ready", "SUBMIT", {"state": "candidate"}),
        memory_step("memory-draft", {"draft.md": b"draft"}),
        memory_step("memory-reconciled", final_memory),
    ])
    # 注意：没有 verifier_step——验证由 Agent 化验证器承担。
    harness = FakeHarness(vm, driver)
    observed = {}

    def verify_agentic(project, active_vm, control_cfg, **kwargs):
        observed["project"] = project
        observed["tree"] = copy.deepcopy(active_vm.tree)
        observed["memory"] = dict(active_vm.memory)
        observed["execution_evidence"] = dict(
            active_vm.execution_evidence)
        observed["context"] = kwargs["context"]
        assert control_cfg.verifier_stage_lifecycle is False
        assert control_cfg.verifier_hide_actor_memory is True
        # 记录验证器收到的现场并校验控制配置（阶段生命周期关、隐藏 Actor 记忆）。
        return "pass", "independent candidate evidence\nVERDICT: PASS\n"

    monkeypatch.setattr("core.verifier.verify_agentic", verify_agentic)
    control = load_config(str(
        Path(__file__).resolve().parents[1]
        / "config/roles/target.yaml"))
    # 真实 target.yaml 作为控制配置。
    result = practice_loop.evolve_practice(
        vm, str(tmp_path / "lineage"), TARGET,
        cfg(), cfg(), cfg(), cfg(), corpus_path=str(tmp_path / "corpus"),
        hooks=harness.hooks(), event_sink=harness.append_event,
        project_budget=1, checkpoint_projects=(1,),
        phase1_exploration=True, agentic_verifier_cfg=control)

    assert result.status == "budget_exhausted"
    assert observed["project"] == PROJECT_1
    # 验证器收到的是项目任务书。
    assert observed["tree"] == {"state": "candidate"}
    assert observed["memory"] == {}
    assert observed["execution_evidence"] == {}
    # 关键：验证器 VM 只有候选树——记忆与执行证据都被剥离。
    assert "Actor-private memory, handoff prose, reasoning" in \
        observed["context"]
    # 上下文明示"Actor 私有记忆/交接文本/推理不可见"。
    assert len(harness.evidence_pushes) == 1
    # 执行证据只推送过一次（进隔离沙箱，不进验证器上下文）。
