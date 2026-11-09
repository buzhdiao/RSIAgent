# 【中文注释·模块定位】统一进化（explore/unified_evolution.py）契约测试：一个
# 跨目标重试持续暂停/恢复的"目标域"课程 Agent（UnifiedCurriculumSession）如何
# 仲裁目标失败/目标通过后的路线，以及进化到"可重试"为止的循环（evolve_until_ready）。
# 测试策略：
# 1) 章程契约：三类提示词（统一重试、失败路线、通过路线）只暴露合法决策词，
#    并写明权限边界（不能改 PASS、不能看候选、没有清单）；
# 2) 会话连续性：同一课程上下文可以先挑战 PASS 再授权交接、可以 REVISE 后转
#    EVOLVE——历史逐条延续、路线摘要累积、证据原文进入历史与提示；
# 3) 主循环行为：READY_FOR_RETRY 可零项目返回记忆；PASS 后课程可读记忆并跳过
#    额外练习；项目收尾先更新记忆再回到课程（诊断入提示）；STALLED 绝不被
#    重解读为就绪；跨进化波次暂停/恢复时历史前缀完整保留。
# 全部用伪 VM（_VM）与脚本化钩子（_hooks），不起真实 LLM/VM。
"""Contracts for the Curriculum-only READY_FOR_RETRY adapter."""

# 模块 docstring：只面向课程 Agent 的 READY_FOR_RETRY 适配器契约。

import json
# 导入 json：读取 state.json 断言登记状态。
from pathlib import Path
# 导入 Path：路径对象处理。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 run_attempt 的返回对象。

from config.settings import load
# 导入配置加载器：load(None) 取默认配置。
from explore import unified_evolution as U
# 以 U 别名导入被测主体：统一进化模块。
from explore.charter import (
    self_evolving_curriculum_charter,
    unified_curriculum_pass_charter,
    unified_curriculum_route_charter,
)
# 导入三个章程生成器：统一重试/目标通过/目标失败路线的提示词契约。
from explore.practice_loop import CURRICULUM_HANDOFF, PracticeHooks
# 导入课程交接路径常量与钩子容器。


class _VM:
    # 伪 VM：内存字典模拟 guest 文件系统，支持清理/项目形状探测命令。
    def __init__(self):
        self.files = {}
        # guest 上的文件（交接文件等）。
        self.memory = {}
        # 已推送到 guest 的记忆树。

    def run_command(self, command, timeout=30, cap=4000):
        if command.startswith("rm -f -- "):
            path = command.split(";", 1)[0][len("rm -f -- "):].strip("'\"")
            self.files.pop(path, None)
            return "RSI_REMOVE_RC=0"
        # 单文件删除命令：真删内存字典并返回成功标记。
        if "RSI_PROJECT_SHAPE_RC" in command:
            return "RSI_PROJECT_SHAPE_RC=0"
        # 项目目录形状探测：返回合规标记。
        return ""
        # 其余命令空输出。


def _hooks(vm, decisions, seen_prompts, handoff_path=CURRICULUM_HANDOFF):
    # 钩子工厂：脚本化课程决策序列，记录收到的提示；每次尝试把决策写进交接文件。
    pending = iter(decisions)
    # 决策迭代器：按顺序吐出预置的课程决策文本。

    def run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        # 伪 run_attempt：一次课程模型调用。
        assert passed_vm is vm
        # 收到的 VM 必须就是传入的那一个。
        seen_prompts.append(prompt)
        # 记录提示供断言。
        passed_vm.files[handoff_path] = next(pending)
        # 把下一个决策写入交接文件（Agent 发布令牌的唯一出口）。
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": '{"done": null}'},
        ]
        # 返回扩展后的历史（用户提示 + 自主完成）。
        return SimpleNamespace(status="done"), history

    def push_memory(passed_vm, memory_dir):
        # 伪记忆推送：读取记忆树存入 VM。
        passed_vm.memory = U._read_memory_tree(memory_dir)
        return True

    return PracticeHooks(
        run_attempt=run_attempt,
        sink_factory=lambda _path: object(),
        reset_vm=lambda _vm, _target: {"ok": True},
        read_guest_text=lambda passed_vm, path: passed_vm.files.get(path, ""),
        capture_project=lambda *args, **kwargs: {"ok": True},
        audit_captured=lambda *args, **kwargs: [],
        audit_memory=lambda files, *args, **kwargs: dict(files),
        validate_corpus=lambda *args, **kwargs: {},
        audit_text=lambda *args, **kwargs: [],
        audit_transcripts=lambda *args, **kwargs: [],
        push_memory=push_memory,
    )
    # 组装全套钩子：其余环节一律空实现（本文件聚焦课程行为）。


def test_unified_curriculum_contract_returns_only_ready_for_retry():
    # 测试：统一重试章程只允许 READY_FOR_RETRY 决策——不得出现回到目标测试
    # 或收敛终止的决策词；章程写明持久目标 Verifier 与官方评测器的边界。
    prompt = self_evolving_curriculum_charter(
        "target query", latest_outcome="target verifier report",
        unified_retry=True)
    # 生成统一重试章程。
    assert "DECISION: READY_FOR_RETRY" in prompt
    # 唯一合法决策词在章程中。
    assert "DECISION: READY_FOR_TARGET_TEST" not in prompt
    # 不允许课程直接放行目标测试。
    assert "DECISION: CONVERGED" not in prompt
    # 不允许课程自行宣布收敛终止。
    assert "persistent target Verifier Agent" in prompt
    # 章程写明持久目标 Verifier Agent 的存在。
    assert "official evaluator" in prompt
    # 章程写明官方评测器的边界（学习阶段不可触碰）。


def test_target_failure_route_contract_separates_local_and_high_level_authority():
    # 测试：目标失败路线章程区分两级权限——局部修补（REVISE）与进化搜索
    # （EVOLVE）都是合法路线；章程写明课程不能把 FAIL 改成 PASS，也不能
    # 接触官方评测器，且授权后要自创练习项目。
    prompt = unified_curriculum_route_charter(
        "target query", "evidence\nVERDICT: FAIL\n")
    # 生成失败路线章程。
    assert "ROUTE: REVISE" in prompt
    # 局部修补路线。
    assert "ROUTE: EVOLVE" in prompt
    # 进化搜索路线。
    assert "cannot change it to PASS" in prompt
    # 权限边界：课程不能改判定。
    assert "official evaluator" in prompt
    # 评测器边界写进章程。
    assert "author a practice" in prompt and "project" in prompt
    # EVOLVE 路线要求课程亲自书写练习项目。


def test_target_pass_contract_separates_correctness_from_terminal_authority():
    # 测试：目标通过路线章程把"正确性"与"终局授权"分开——支持不足时
    # VERIFY_MORE，支持充分时 HANDOFF；课程不能看候选、不能直接改判、
    # 没有评测清单、接触不到官方评测器。
    prompt = unified_curriculum_pass_charter(
        "target query", "orientation evidence",
        "candidate evidence\nVERDICT: PASS\n")
    # 生成通过路线章程（定向证据 + 候选证据）。
    assert "ROUTE: HANDOFF" in prompt
    # 交接路线（授权目标测试）。
    assert "ROUTE: VERIFY_MORE" in prompt
    # 追加验证路线。
    assert "cannot inspect the candidate" in prompt
    # 课程不能亲自检查候选（保持角色隔离）。
    assert "cannot directly" in prompt and "REVISE or EVOLVE" in prompt
    # 课程不能直接修补或进化（PASS 场景的权限边界）。
    assert "official evaluator" in prompt
    # 评测器边界。
    assert "no checklist" in prompt.lower()
    # 课程没有评测清单（判定权在目标 Verifier）。


def test_same_curriculum_context_can_challenge_pass_then_authorize_handoff(
        tmp_path):
    # 测试：同一课程会话里先质疑 PASS（VERIFY_MORE）再在证据补强后授权
    # 交接（HANDOFF）——历史延续、通过计数累计、路线摘要按序记录、
    # 两次证据分别进入历史与提示。
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        [
            "One requirement lacks support.\nROUTE: VERIFY_MORE\n",
            "The new report resolves it.\nROUTE: HANDOFF\n",
        ],
        prompts,
        handoff_path=U.CURRICULUM_ROUTE_HANDOFF,
    )
    # 课程决策脚本：先追加验证，后授权交接（写进路线交接文件）。
    session = U.UnifiedCurriculumSession()
    # 全新课程会话。

    first = U.route_target_pass(
        vm, str(tmp_path / "routes"), "target query", "orientation evidence",
        "first support\nVERDICT: PASS\n", load(None),
        hooks=hooks, session=session)
    first_history = list(session.history)
    # 第一次通过审查：取当时的历史快照。
    second = U.route_target_pass(
        vm, str(tmp_path / "routes"), "target query", "orientation evidence",
        "stronger support\nVERDICT: PASS\n", load(None),
        hooks=hooks, session=session)
    # 第二次通过审查：证据更强。

    assert first.route == "VERIFY_MORE"
    assert second.route == "HANDOFF"
    # 两次路线依次为追加验证、授权交接。
    assert session.history[:len(first_history)] == first_history
    # 第二次的历史以第一次为前缀：会话连续不重置。
    assert session.target_passes == 2
    # 通过审查计数累计到 2。
    assert session.route_summaries == [
        "target PASS review 1: VERIFY_MORE",
        "target PASS review 2: HANDOFF",
    ]
    # 路线摘要按序累积（审查 1/2 + 路线）。
    assert "first support" in session.history[0]["content"]
    # 第一次的证据原文留在历史首条。
    assert "stronger support" in prompts[1]
    # 第二次的提示包含新证据原文。


def test_same_curriculum_context_routes_revise_then_evolve(tmp_path):
    # 测试：同一课程会话在连续目标失败间先选局部修补（REVISE）再转向进化
    # （EVOLVE）——与通过路线同构：历史延续、失败计数累计、摘要记录。
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        [
            "Local repair has highest value.\nROUTE: REVISE\n",
            "Practice search now has highest value.\nROUTE: EVOLVE\n",
        ],
        prompts,
        handoff_path=U.CURRICULUM_ROUTE_HANDOFF,
    )
    # 课程决策脚本：先 REVISE 后 EVOLVE。
    session = U.UnifiedCurriculumSession()

    first = U.route_target_failure(
        vm, str(tmp_path / "routes"), "target query",
        "first concrete failure\nVERDICT: FAIL\n", load(None),
        hooks=hooks, session=session)
    first_history = list(session.history)
    # 第一次失败路线：取历史快照。
    second = U.route_target_failure(
        vm, str(tmp_path / "routes"), "target query",
        "second concrete failure\nVERDICT: FAIL\n", load(None),
        hooks=hooks, session=session)
    # 第二次失败路线。

    assert first.route == "REVISE"
    assert second.route == "EVOLVE"
    # 路线依次为局部修补、进化搜索。
    assert session.history[:len(first_history)] == first_history
    # 历史前缀保留：同一上下文跨失败延续。
    assert session.target_failures == 2
    # 失败路线计数累计到 2。
    assert session.route_summaries == [
        "target failure 1: REVISE", "target failure 2: EVOLVE"]
    # 失败路线摘要按序累积。
    assert "first concrete failure" in session.history[0]["content"]
    # 第一次失败证据进入历史。
    assert "second concrete failure" in prompts[1]
    # 第二次失败证据进入提示。


def test_evolution_can_return_memory_for_retry_without_running_target(tmp_path):
    # 测试：课程判定 READY_FOR_RETRY 时，进化循环可以零练习项目直接返回
    # 现有记忆——不跑目标、不跑项目、记忆原样保留；状态与 state.json 一致。
    vm = _VM()
    prompts = []
    hooks = _hooks(
        vm,
        ["DECISION: READY_FOR_RETRY\nCurrent memory is ready to falsify."],
        prompts,
    )
    # 课程决策：现有记忆已可证伪，直接重试。
    initial = {"skill.md": b"existing memory"}
    # 初始记忆。

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        initial, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)
    # 跑进化主循环（带失败证据、Actor 诊断与路线报告）。

    assert result.status == "ready_for_retry"
    # 状态：可重试。
    assert result.projects == 0
    # 零练习项目：课程没有要求新项目。
    assert U._read_memory_tree(result.memory_dir) == initial
    # 记忆树与初始一致（未被改动）。
    assert "READY_FOR_RETRY" in prompts[0]
    # 提示包含合法决策词。
    assert "READY_FOR_TARGET_TEST" not in prompts[0]
    # 提示不含目标测试决策词（适配器只出 READY_FOR_RETRY）。
    state = json.loads(
        (Path(result.root) / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "ready_for_retry"
    # state.json 登记状态与结果一致（落盘可恢复）。


def test_phase2_pass_curriculum_reads_memory_and_can_skip_extra_practice(
        tmp_path):
    # 测试：Phase-2 PASS 后，课程在提示里读到 Actor 自有持久记忆全文，
    # 并可以判定"当前记忆无需对照练习"直接放行——记忆被推送进 guest、
    # 状态登记触发结果为 PASS。
    vm = _VM()
    prompts = []
    initial = {"skills/target.md": b"same Actor grounded PASS lesson"}
    # Actor 已提交的接地记忆。
    hooks = _hooks(
        vm,
        ["DECISION: READY_FOR_TARGET\nNo contrast now has higher value."],
        prompts,
    )
    # 课程决策：现在没有更高价值的对照练习。

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "independent candidate evidence\nVERDICT: PASS\n",
        "same Actor retained a scoped successful procedure",
        initial, load(None), load(None), load(None), load(None),
        trigger_authority="phase2_outcome_protocol",
        triggering_outcome="PASS",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)
    # 以 Phase-2 结果协议触发、PASS 触发结果跑主循环。

    assert result.status == "ready_for_retry"
    # 状态：可重试（回到目标）。
    assert result.projects == 0
    # 零额外练习：课程有权跳过。
    assert vm.memory == initial
    # 记忆被推送进 guest（VM 侧可见）。
    assert "CURRENT ACTOR-OWNED DURABLE MEMORY" in prompts[0]
    # 提示里出现记忆全文段落标题：课程读到了 Actor 自有记忆。
    assert "DECISION: REWRITE" not in prompts[0]
    # 提示不含重写决策词（PASS 场景不重写章程）。
    state = json.loads(
        (Path(result.root) / "state.json").read_text(encoding="utf-8"))
    assert state["triggering_outcome"] == "PASS"
    # state.json 登记触发结果为 PASS。


def test_project_outcome_updates_memory_before_ready_for_retry(
        monkeypatch, tmp_path):
    # 测试：练习项目收尾时必须先把学习提交进记忆，再回到课程——第二个
    # 课程提示里包含失败报告与因果诊断；最终记忆树是提升后的版本。
    vm = _VM()
    prompts = []
    hooks = _hooks(vm, [
        (
            "DECISION: PROJECT\nCreate a robust artifact entirely beneath "
            "/home/user/evolution_project."
        ),
        "DECISION: READY_FOR_RETRY\nThe practice evidence now justifies retry.",
    ], prompts)
    # 课程决策脚本：先开一个练习项目，后放行重试。
    initial = {"skill.md": b"v0"}
    # 初始记忆 v0。

    monkeypatch.setattr(U, "_run_practice_attempt", lambda **kwargs: {
        "terminal_outcome": "FAIL",
        "verifier_report": "VERDICT: FAIL\nmissing property",
        "actor_handoff": "STATUS: SUBMIT\npartial artifact",
        "actor_history": [{"role": "user", "content": "project"},
                          {"role": "assistant", "content": "attempt"}],
        "before_memory": initial,
    })
    # 伪练习尝试：FAIL 收尾，带验证报告、Actor 交接、历史与提交前记忆。

    def promote(**kwargs):
        # 伪学习提升：把 v1 记忆安装进记忆目录，返回诊断与历史。
        learned = {"skill.md": b"v1 grounded by failure"}
        kwargs["hooks"].install_memory(str(kwargs["memory_dir"]), learned)
        return learned, "causal diagnosis", kwargs["actor_history"]

    monkeypatch.setattr(U, "_promote_learning", promote)
    # 替换学习提升。

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        initial, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)
    # 跑主循环：项目 → 学习 → 课程。

    assert result.status == "ready_for_retry"
    # 最终放行重试。
    assert result.projects == 1
    # 恰好跑了一个练习项目。
    assert U._read_memory_tree(result.memory_dir) == {
        "skill.md": b"v1 grounded by failure"}
    # 记忆树已是 v1：项目成果先于就绪状态落进记忆。
    assert "missing property" in prompts[1]
    # 第二个课程提示包含失败报告原文。
    assert "causal diagnosis" in prompts[1]
    # 第二个课程提示包含因果诊断：课程基于项目学习做决策。


def test_curriculum_stall_is_not_reinterpreted_as_readiness(tmp_path):
    # 测试：课程明确判 STALLED 时，主循环必须如实返回 stalled——绝不把
    # 课程停滞重解读为"可以重试"（语义决策只能来自课程本身）。
    vm = _VM()
    hooks = _hooks(
        vm, ["DECISION: STALLED\nNo productive hypothesis remains."], [])
    # 课程决策：停滞，无生产性假设。

    result = U.evolve_until_ready(
        vm, str(tmp_path / "cycle"), "target query",
        "unmet requirement evidence\nVERDICT: FAIL\n",
        "same Actor identified a transferable weakness",
        {}, load(None), load(None), load(None), load(None),
        routing_report="Curriculum chose search\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks)
    # 跑主循环。

    assert result.status == "stalled"
    # 状态：停滞。
    assert result.status != "ready_for_retry"
    # 且绝不等价于"可重试"。


def test_curriculum_context_is_paused_and_resumed_across_evolve_waves(tmp_path):
    # 测试：课程上下文跨两次进化波次暂停/恢复——第二次波次以第一次的历史
    # 前缀起步（含第一次的目标证据原文），波次计数累计为 2。
    vm = _VM()
    prompts = []
    received_histories = []
    decisions = iter((
        "DECISION: READY_FOR_RETRY\nFirst wave is ready.",
        "DECISION: READY_FOR_RETRY\nSecond wave is ready.",
    ))
    # 两波课程决策脚本。

    def run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        # 专用伪 run_attempt：记录每次收到的提示与初始历史。
        del cfg, sink
        assert passed_vm is vm
        prompts.append(prompt)
        received_histories.append(list(kwargs.get("initial_history") or []))
        passed_vm.files[CURRICULUM_HANDOFF] = next(decisions)
        # 写入本波决策。
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": '{"done": null}'},
        ]
        return SimpleNamespace(status="done"), history

    hooks = _hooks(vm, [], [])
    hooks.run_attempt = run_attempt
    # 基础钩子 + 专用 run_attempt。
    session = U.UnifiedCurriculumSession()
    # 全新课程会话（跨波次共享）。
    cfg = load(None)

    first = U.evolve_until_ready(
        vm, str(tmp_path / "cycle1"), "target query",
        "first target evidence\nVERDICT: FAIL\n", "first diagnosis",
        {}, cfg, cfg, cfg, cfg,
        routing_report="first Curriculum route\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks,
        session=session)
    second = U.evolve_until_ready(
        vm, str(tmp_path / "cycle2"), "target query",
        "second target evidence\nVERDICT: FAIL\n", "second diagnosis",
        {}, cfg, cfg, cfg, cfg,
        routing_report="second Curriculum route\nROUTE: EVOLVE\n",
        corpus_path=str(tmp_path / "corpus.json"), hooks=hooks,
        session=session)
    # 用同一个会话连续跑两波进化（不同周期目录、不同证据）。

    assert first.status == second.status == "ready_for_retry"
    # 两波都以可重试收尾。
    assert received_histories[0] == []
    # 第一波从空历史起步。
    assert received_histories[1] == session.history[:2]
    # 第二波收到的初始历史恰为会话历史的前两条（暂停点快照）。
    assert "first target evidence" in received_histories[1][0]["content"]
    # 前缀里带着第一波的目标证据原文。
    assert "second target evidence" in prompts[1]
    # 第二波的提示包含第二次证据原文。
    assert session.waves == 2
    # 会话波次计数累计为 2。
