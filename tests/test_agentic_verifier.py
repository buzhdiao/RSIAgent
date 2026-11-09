#!/usr/bin/env python3
# 【中文注释·模块定位】可选（opt-in）效果隔离 Agent 验证器的传输与连续性契约
# 测试（core/verifier.verify_agentic + core/verifier_runtime 的
# AgenticVerifierNoProgressError + core/loop.run_attempt 的钩子协议 +
# core.trace.ArtifactSink + explore/charter 的验证器章程 + llm.client 的持久
# 用户消息）。被钉死的承诺：
# 1) 报告解析器只认"权威传输行"：围栏/引用内的 VERDICT 不算数；幂等重复可
#    接受、互相冲突返回 None；UNVERIFIED 只有在 allow_unverified 开启时才算
#    配置化传输（不是旧别名）；ROUTE: HANDOFF/REVISE/EVOLVE/VERIFY_MORE 的
#    解析同理；字面转义 \n 也被当作传输行；
# 2) 复发重放（recurrent replay）把"权威信封"补回提示词，直到第一个真实
#    动作把它锚进检查点（binary/evidence/route 三种模式都要过）；
# 3) 全 Agent 报告无损且会话跨调用持久（含基础设施暂停遥测、上下文压缩
#    配置原样下传）；
# 4) 无动作验证器在"连续 3 段无实质进展"时停止（任何段状态都不重试到无穷），
#    且基础设施绝不评分（不写 verify2.json）；有实质动作或真实发布会重置
#    空段连击（发布在枯竭边界上仍有权威）；
# 5) EVOLVE/UNVERIFIED 是纯路由/传输判定：运行时权威不变（隐藏 Actor 记忆
#    的机制隐藏提示保留）、会话跨证据请求持久、Actor 证据先导进入提示词；
# 6) 调度只在 opt-in 时发生；orientation 阶段保留上下文但绝不创建评估器
#    生命周期（无 private_evaluator、无 run.sh、无 VERIFIER_EVALUATOR_* 事件）；
# 7) 章程（charter）把调查与报告形态交给 Agent：无预设清单/schema/探针数、
#    code-as-policy 自由、VERIFIER_SCRATCH、输出截断警示；UNVERIFIED 章程
#    请求"不可信的可复现先导"；rollback 章程如实描述活命名空间镜像与恢复；
#    EVOLVE 路由是路由判断不是教学请求；VERIFY_MORE 保留验证器权威（候选
#    未变、课程无权否决本地判定）；
# 8) 实质检查点丢枯竭尾部但返回观察；语义重放保留每个动作与结果、剥掉干话；
# 9) 恢复保留报告并回滚无动作尾（丢弃对计数、挂起观察字节/图像数如实入
#    recovery.json）；look 观察与像素重附而不重放干回复（t099 回归）；
#    首段全干也不得抹掉任务权威（SAME-CONTEXT TRANSPORT RECOVERY 信封）。
"""Opt-in effect-isolated Agent verifier transport and continuity tests."""
# 模块 docstring：可选的效果隔离 Agent 验证器传输与连续性测试。

import json
# 导入 json：读取落盘的 verify2/recovery 产物。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造控制配置/循环结果等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import Config
# 导入配置 dataclass：构造验证器角色配置。
from core import loop as L
# 导入主循环：run_attempt 的钩子协议（被 monkeypatch）。
from core import verifier as V
# 导入被测模块：verify_agentic/verify_independent/会话/解析器/章程辅助。
from core.trace import ArtifactSink
# 导入工件池：真实的盘上产物落盘。
from core.verifier_runtime import AgenticVerifierNoProgressError
# 导入无进展异常：无动作停止的预期错误类型。
from explore.charter import (
    agent_harness_verifier_charter,
    agent_harness_verifier_orientation_charter,
)
# 导入验证器章程生成器：调查章程与 orientation 章程。
from llm.client import durable_user_message
# 导入持久用户消息：携带图像字节的多模态消息。


def test_report_parser_is_transport_only_and_unambiguous():
    """报告解析只认权威传输行；幂等重复可接受，冲突返回 None。"""
    report = """# Investigation
```text
VERDICT: FAIL
```
> VERDICT: FAIL

Evidence was gathered independently.
VERDICT: PASS
"""
    # 一个围栏里的 FAIL + 引用行里的 FAIL + 正文最后的 PASS。
    assert V._parse_agentic_verifier_report(report) == "pass"
    # 只有正文顶层的 VERDICT 是权威 → pass。
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: PASS\n") == "pass"
    # 幂等重复（PASS 两次）可接受。
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: FAIL\n") is None
    # 互相冲突 → None（拒绝解析）。
    assert V._parse_agentic_verifier_report("no token") is None
    # 无判定令牌 → None。


@pytest.mark.parametrize("mode", ["binary", "evidence", "route"])
def test_recurrent_replay_rehydrates_authority_until_an_action_anchors_it(
        monkeypatch, tmp_path, mode):
    """The first real action can follow a dry reply to the task opening."""
    # 复发重放：权威信封在第一个真实动作锚定前，每次都要补回提示词。
    # 参数化：binary（VERDICT）/evidence（UNVERIFIED）/route（ROUTE）三种判定形态。
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    # 验证器配置走 _verifier_cfg。
    task = "Preserve unrelated appointments. Move only the named review to Tuesday."
    # 权威任务原文。
    context = "Candidate: /candidate. Frozen original: /original."
    # 候选与冻结原件的上下文。
    calls = []
    action = '{"program":{"lang":"bash","code":"cat /candidate"}}'
    dry = "I will inspect the candidate."
    observation = "Exact independent probe result: original value is unchanged."
    # 四个脚本要素：真实动作、干话、探针观察。

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append((prompt, list(history)))
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": dry},
                {"role": "user", "content": "Emit one real action now."},
                {"role": "assistant", "content": action},
                {"role": "user", "content": observation},
                {"role": "assistant", "content": dry},
            ]
            # 第一段：干话 → 真实动作 → 观察 → 又干话（stalled）。
        elif len(calls) == 2:
            # This repeated dry response activates semantic replay, which removes
            # the first user/dry-assistant pair containing the authority envelope.
            history += [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": dry}]
            # 第二段又干话：触发语义重放，剥掉含权威信封的首个 user/干话对。
        else:
            assert task in prompt
            assert context in prompt
            assert "AUTHORITATIVE TASK — verbatim" in prompt
            assert observation in prompt
            assert [m["content"] for m in history
                    if m["role"] == "assistant"] == [action]
            assert kwargs["continue_context"] is True
            # 第三段起：权威任务/上下文/观察全部回到提示词；历史只剩真实动作；
            # 续跑开关开启。
            if len(calls) == 3:
                # Another dry segment must not mark the restored opening as
                # anchored until a real action includes it in the checkpoint.
                history += [{"role": "user", "content": prompt},
                            {"role": "assistant", "content": dry}]
                # 第三段再干话：恢复的开场尚未被锚定（还会重放）。
            else:
                report = ("Independent findings.\nROUTE: HANDOFF\n"
                          if mode == "route" else
                          "Independent findings.\nVERDICT: PASS\n")
                kwargs["program_executor"]("verifier-report", report)
                assert kwargs["terminal_handoff_ready"]()
                # 第四段：按参数化发布判定报告，终局就绪。
                history += [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content":
                     '{"program":{"lang":"verifier-report","code":"publish"}}'},
                ]
                sink.save_transcript("system", history)
                return SimpleNamespace(status="done"), history
                # 返回 done。
        sink.save_transcript("system", history)
        return SimpleNamespace(status="stalled"), history
        # 其余段返回 stalled。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    # 替换真实循环。
    control = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=mode == "route",
        verifier_unverified_evidence=mode == "evidence")
    # 控制配置按参数化开启 route/evidence 能力。
    verdict, _ = V.verify_agentic(
        task, _VM(), control, context=context,
        sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)
    # 执行 Agent 化验证。
    assert verdict == "pass" and len(calls) == 4
    # 最终 pass、共四段。
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_001" /
                           "recovery.json").read_text())
    assert recovery["semantic_replay_activated"] is True
    # 第二段触发过语义重放（落盘记录）。


def test_unverified_verdict_is_configured_transport_not_legacy_alias():
    """UNVERIFIED 是配置化传输判定：默认不解析，显式开启才接受。"""
    report = "# Evidence gap\nVERDICT: UNVERIFIED\n"
    assert V._parse_agentic_verifier_report(report) is None
    # 默认：UNVERIFIED 不是可解析判定（None）。
    assert V._parse_agentic_verifier_report(
        report, allow_unverified=True) == "unverified"
    # 开启 allow_unverified：解析为 unverified。
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\nVERDICT: UNVERIFIED\n",
        allow_unverified=True) is None
    # 与 PASS 冲突 → None。


def test_unified_report_parser_accepts_idempotent_duplicates_not_conflicts():
    """ROUTE 解析：HANDOFF→pass、REVISE→wrong、EVOLVE→evolve；冲突即拒绝。"""
    assert V._parse_agentic_verifier_report(
        "# Evidence\nROUTE: HANDOFF\n", allow_evolve=True) == "pass"
    # HANDOFF 路由 → pass。
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\n", allow_evolve=True) == "wrong"
    # REVISE 路由 → wrong。
    assert V._parse_agentic_verifier_report(
        "ROUTE: EVOLVE\n", allow_evolve=True) == "evolve"
    # EVOLVE 路由 → evolve。
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\nEvidence.\nROUTE: REVISE\n",
        allow_evolve=True) == "wrong"
    # 幂等重复可接受。
    assert V._parse_agentic_verifier_report(
        "ROUTE: REVISE\nROUTE: EVOLVE\n", allow_evolve=True) is None
    # 路由冲突 → None。
    assert V._parse_agentic_verifier_report(
        "VERDICT: PASS\n", allow_evolve=True) is None
    # allow_evolve 下 VERDICT 行不再是权威（只认 ROUTE）。


def test_orientation_parser_accepts_idempotent_duplicates_not_conflicts():
    """orientation 解析：STAGE 令牌幂等重复可接受，无令牌返回 None。"""
    assert V._parse_agentic_orientation_report(
        "STAGE: ORIENTATION_READY\nSTAGE: ORIENTATION_READY\n") == \
        "orientation_ready"
    # 幂等重复 → orientation_ready。
    assert V._parse_agentic_orientation_report(
        r"Orientation complete.\n\nSTAGE: ORIENTATION_READY") == \
        "orientation_ready"
    # 字面转义 \n 形态也可解析。
    assert V._parse_agentic_orientation_report("no stage") is None
    # 无 STAGE 令牌 → None。


def test_report_parser_treats_literal_escaped_newlines_as_transport_lines():
    """字面转义 \n / \r\n 也被当作传输行分隔（供应商回显形态）。"""
    assert V._parse_agentic_verifier_report(
        r"Evidence complete.\nVERDICT: PASS") == "pass"
    # 字面 \n 后的 VERDICT 行有效。
    assert V._parse_agentic_verifier_report(
        r"Evidence complete.\r\nROUTE: REVISE", allow_evolve=True) == "wrong"
    # 字面 \r\n 后的 ROUTE 行有效。


class _VM:
    """伪 VM：内存文件表 + rm/test -s/fetch_file 的可信包装协议模拟。"""

    def __init__(self):
        self.files = {}
        # 内存文件表。
        self.removals = []
        # 记录删除的路径（供恢复测试断言"没有误删"）。

    def run_command(self, command, timeout=30, cap=4000):
        if command.startswith("rm -f -- "):
            path = command[len("rm -f -- "):]
            self.removals.append(path)
            self.files.pop(path, None)
            return ""
            # rm -f -- 只记录并删除内存文件。
        if command.startswith("test -s "):
            path = command.split(" && echo ", 1)[0][len("test -s "):]
            return ("__RSIAGENT_REPORT_READY__\n"
                    if self.files.get(path) else "")
            # 报告就绪探测：文件非空则回就绪令牌。
        return ""
        # 其他命令无输出。

    def fetch_file(self, path, max_bytes=None):
        data = self.files.get(path)
        return ((data, "") if data else (None, "missing"))
        # 抓取内存文件（无上限）。


def _verifier_cfg():
    """构造最小验证器角色配置（8 迭代级预算、无独立验证、无视觉）。"""
    cfg = Config()
    cfg.model = "verifier-model"
    cfg.vision_model = ""
    cfg.agent_decided_stop = True
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 100
    return cfg
    # 验证器专用形状：Agent 自决停止、练习模式、零历史折叠。


def test_full_agent_report_is_lossless_and_session_persists(
        monkeypatch, tmp_path):
    """全 Agent 报告无损（>2000 字符）且会话/遥测跨调用持久。"""
    long_fail = "# Report\nVERDICT: FAIL\n" + ("detailed evidence\n" * 400)
    long_pass = "# Reinspection\n" + ("new evidence\n" * 400) + "VERDICT: PASS\n"
    # 两份超长报告（各 400 行证据）。
    reports = iter((long_fail, long_pass))
    calls = []

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        assert kwargs["instruction_is_complete_opening"] is True
        # 指令以完整开场形式下发。
        report = next(reports)
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布报告后终局就绪。
        history = list(kwargs.get("initial_history") or [])
        history += [{"role": "user", "content": prompt},
                    {"role": "assistant", "content":
                     '{"program":{"lang":"verifier-report","code":"publish report"}}'}]
        calls.append({"prompt": prompt,
                      "continue": kwargs.get("continue_context"),
                      "history_before": len(kwargs.get("initial_history") or [])})
        # 记录提示词、续跑开关与初始历史长度。
        return SimpleNamespace(
            status="done", infra_pause_secs=4, infra_pauses=1), history
        # 返回 done + 基础设施暂停遥测（4 秒 1 次）。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    outer_cfg = SimpleNamespace(agentic_verifier_config="verifier.yaml")
    vm = _VM()
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict1, findings1 = V.verify_agentic(
        "task", vm, outer_cfg, sink=sink, turn_no=5, session=session,
        context="mechanical candidate revision one", wall_budget=50)
    verdict2, findings2 = V.verify_agentic(
        "task", vm, outer_cfg, sink=sink, turn_no=6, session=session,
        context="mechanical candidate revision two", wall_budget=50)
    # 同一会话连续验证两次（turn 5 与 6）。

    assert verdict1 == "wrong" and str(findings1) == long_fail
    assert verdict2 == "pass" and str(findings2) == long_pass
    # 两次判定与报告逐字无损。
    assert len(str(findings1)) > 2000 and len(str(findings2)) > 2000
    # 长报告没有被截断。
    assert session.inspections == 2 and len(session) == 4
    # 会话：2 次检查、4 条历史（跨调用累积）。
    assert session.infra_pause_secs == 8 and session.infra_pauses == 2
    # 基础设施暂停遥测跨调用累计（8 秒 2 次）。
    assert calls[0]["continue"] is False
    assert calls[1]["continue"] is True and calls[1]["history_before"] == 2
    # 第一次全新开场；第二次续跑且初始历史是上次的 2 条。
    assert "mechanical candidate revision two" in calls[1]["prompt"]
    # 第二次的上下文进入提示词。

    saved = json.loads(
        (tmp_path / "run" / "iter_06" / "verify2.json").read_text())
    assert saved["findings"] == long_pass
    # 落盘产物无损（iter_06 保存完整 PASS 报告）。


def test_agentic_verifier_honors_loaded_context_compaction(monkeypatch, tmp_path):
    """加载的上下文压缩配置被原样下传（不被 Agent 化路径覆写）。"""
    loaded = _verifier_cfg()
    loaded.history_keep_pairs = 20
    loaded.ctx_high_water = 500000
    observed = {}
    monkeypatch.setattr("config.settings.load", lambda _path: loaded)
    # 磁盘配置：保留 20 对历史、高水位 500000。

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        observed["history_keep_pairs"] = cfg.history_keep_pairs
        observed["ctx_high_water"] = cfg.ctx_high_water
        # 记录到达 run_attempt 的实际压缩配置。
        report = "Evidence complete.\nVERDICT: PASS\n"
        kwargs["program_executor"]("verifier-report", report)
        return SimpleNamespace(status="done"), [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "published"},
        ]
        # 发布 PASS 并 done。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    verdict, _ = V.verify_agentic(
        "task", _VM(), SimpleNamespace(agentic_verifier_config="fixed.yaml"),
        sink=ArtifactSink(str(tmp_path / "run")), wall_budget=50)

    assert verdict == "pass"
    assert observed == {"history_keep_pairs": 20, "ctx_high_water": 500000}
    # 关键：磁盘加载的压缩配置逐键原样生效。


@pytest.mark.parametrize("status", ["stalled", "stalled_quiescent", "budget", "done"])
@pytest.mark.parametrize("repeated_reply", [True, False])
def test_actionless_verifier_stops_without_a_candidate_verdict(
        monkeypatch, tmp_path, status, repeated_reply):
    """无动作验证器：连续 3 段无实质进展即停，且基础设施绝不评分。"""
    # 参数化：四种段状态 × 重复/相异两种干话形态（重复形态触发语义重放）。
    loaded = _verifier_cfg()
    monkeypatch.setattr("config.settings.load", lambda _path: loaded)
    original = [
        {"role": "user", "content": "original inspection task"},
        {"role": "assistant", "content":
         '{"program":{"lang":"bash","code":"echo evidence"}}'},
    ]
    # 原始检查点：开场 + 一个真实动作。
    session = V.VerifierSession(original)
    session.pending_observation = "complete prior evidence"
    session.pending_turn = V.parse_turn(original[-1]["content"])
    # 会话带挂起观察与挂起轮次（模拟中断现场）。
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        calls.append(prompt)
        assert len(calls) <= 3, "an empty checkpoint must not retry indefinitely"
        # 空检查点绝不无限重试。
        assert cfg.history_keep_pairs == 0, "the guard must respect context policy"
        # 守卫尊重上下文策略（不偷偷改压缩配置）。
        assert "complete prior evidence" in prompt
        # 挂起观察每次都回到提示词（权威不丢）。
        history = list(kwargs["initial_history"]) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": (
                "(empty reply)" if repeated_reply else
                f"Different actionless narrative {len(calls)}"),
             "reasoning": "complete raw reasoning"},
        ]
        # 干话（重复或相异）+ 原始推理字段（防止清洗）。
        sink.save_transcript("system", history)
        return SimpleNamespace(status=status), history
        # 返回参数化的段状态。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    root = tmp_path / "run"
    with pytest.raises(AgenticVerifierNoProgressError,
                       match="3 consecutive segments"):
        V.verify_agentic(
            "original inspection task", _VM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=ArtifactSink(str(root)), session=session, wall_budget=50)
        # 连续 3 段无进展 → 抛无进展异常。

    assert len(calls) == 3
    # 恰好三段。
    assert list(session) == original
    assert session.pending_observation == "complete prior evidence"
    # 会话回滚到原始检查点（挂起观察保留）。
    assert not list(root.rglob("verify2.json")), "infrastructure cannot grade the task"
    # 关键：基础设施绝不评分（没有 verify2.json）。
    segments = sorted((root / "verifier_agent/inspection_001").glob("segment_*"))
    for index, segment in enumerate(segments, 1):
        raw = json.loads((segment / "transcript.json").read_text())
        assert raw["messages"][-1]["reasoning"] == "complete raw reasoning"
        # 每段转写保留原始推理（不被清洗）。
        recovery = json.loads((segment / "recovery.json").read_text())
        assert recovery["actionless_segments"] == index
        assert recovery["checkpoint_advanced"] is False
        # 每段恢复记录：无动作计数递增、检查点未推进。
    assert recovery["stop_reason"] == "no_substantive_progress"
    assert recovery["status"] == "infra"
    assert recovery["segment_status"] == status
    # 末段恢复记录：停止原因/infra 状态/段状态与参数化一致。


def test_verifier_progress_resets_empty_streak_and_publication_wins(
        monkeypatch, tmp_path):
    """有实质动作重置空段连击；真实发布在枯竭边界上仍有权威。"""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    steps = iter(["empty", "empty", "program", "empty", "empty", "publish"])
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        step = next(steps)
        calls.append(step)
        content = "(empty reply)"
        if step == "program":
            content = '{"program":{"lang":"bash","code":"echo new evidence"}}'
        if step == "publish":
            kwargs["program_executor"]("verifier-report", "Evidence.\nVERDICT: PASS\n")
            # A real publication has authority even at a coincident dry boundary.
            assert kwargs["terminal_handoff_ready"]()
            # 第六段：发布 PASS，终局就绪（恰逢第三次空段边界）。
        history = list(kwargs["initial_history"]) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="stalled"), history
        # 按脚本每段返回 stalled。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    root = tmp_path / "run"
    verdict, _ = V.verify_agentic(
        "task", _VM(), SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=ArtifactSink(str(root)), session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass" and len(calls) == 6
    # 六段后以 pass 结束（没有被 3 连击提前掐断）。
    records = [json.loads(p.read_text()) for p in sorted(root.rglob("recovery.json"))]
    assert [record["actionless_segments"] for record in records] == [1, 2, 0, 1, 2, 3]
    # 关键：空段连击序列 1,2,→0（program 重置）→1,2,3（发布段恰在边界）。
    assert all("stop_reason" not in record for record in records)
    # 没有任何段因无进展停止。


def test_full_agent_can_route_to_evolution_without_changing_runtime_authority(
        monkeypatch, tmp_path):
    """EVOLVE 是纯路由判定：运行时权威不变（机制隐藏提示保留）。"""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    captured = {}

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        report = "# Evidence\nunmet target requirement\nROUTE: EVOLVE\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布 EVOLVE 路由报告。
        captured.update(
            prompt=prompt,
            system=kwargs["system_prompt"],
            reminder=kwargs["user_message_transform"]("observation"),
        )
        # 捕获提示词/系统提示/观察变换后的提醒消息。
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml", verifier_evolve_route=True,
        verifier_hide_actor_memory=True)
    # 开启 EVOLVE 路由与"隐藏 Actor 记忆"。

    verdict, findings = V.verify_agentic(
        "task", _VM(), cfg, sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)

    assert verdict == "evolve"
    # 判定如实为 evolve。
    assert str(findings).endswith("ROUTE: EVOLVE\n")
    # 报告无损。
    assert "ROUTE: EVOLVE" in captured["prompt"]
    assert "ROUTE: EVOLVE" in captured["system"]
    # 路由词汇进提示词与系统提示。
    assert "mechanically hidden" in captured["system"]
    # 关键：机制隐藏（Actor 记忆不可见）的说明保留在系统提示里——运行时
    # 权威边界不因路由能力而改变。
    assert "HANDOFF, REVISE, or EVOLVE" in captured["reminder"]
    # 观察提醒里带三种合法路由。


def test_full_agent_can_request_evidence_without_losing_its_session(
        monkeypatch, tmp_path):
    """UNVERIFIED 证据请求保留会话与 Actor 证据先导。"""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    captured = {}

    def fake_run_attempt(prompt, vm, cfg, sink, **kwargs):
        report = "# Unsettled material claim\nVERDICT: UNVERIFIED\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布 UNVERIFIED 报告。
        captured.update(prompt=prompt, system=kwargs["system_prompt"])
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_unverified_evidence=True)
    session = V.VerifierSession()

    verdict, findings = V.verify_agentic(
        "task", _VM(), cfg, sink=ArtifactSink(str(tmp_path / "run")),
        session=session, wall_budget=50,
        actor_evidence_leads="Exact probe: curl localhost")
    # 带 Actor 证据先导执行。

    assert verdict == "unverified"
    assert str(findings).endswith("VERDICT: UNVERIFIED\n")
    # 判定与报告无损。
    assert session.stage == "EVIDENCE_REQUESTED"
    # 关键：会话进入"证据已请求"阶段（跨调用持久）。
    assert "VERDICT: UNVERIFIED" in captured["system"]
    # UNVERIFIED 判定词进系统提示。
    assert "ACTOR-SUPPLIED EVIDENCE LEADS" in captured["prompt"]
    # Actor 证据先导进入提示词。


def test_verify_independent_dispatches_only_when_opted_in(monkeypatch):
    """调度只在 opt-in 时发生：配置存在时 verify_independent 转发给 Agent 臂。"""
    sentinel = ("wrong", V.Findings("full report"))
    monkeypatch.setattr(V, "verify_agentic", lambda *a, **kw: sentinel)
    # 用哨兵替换 Agent 臂。
    cfg = SimpleNamespace(agentic_verifier_config="verifier.yaml")
    assert V.verify_independent("task", object(), cfg) is sentinel
    # 配置存在 → 转发（返回同一对象）。


def test_orientation_preserves_context_without_creating_an_evaluator_lifecycle(
        monkeypatch):
    """orientation 保留上下文但绝不创建评估器生命周期。"""
    from core import loop as actor_loop
    from core.verifier_runtime import AgenticVerifierExecutor
    # 导入循环与执行器（后者用于证明无评估器方法）。

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    prompts = []

    def fake_attempt(prompt, vm, cfg, sink, **kwargs):
        prompts.append(prompt)
        kwargs["program_executor"](
            "verifier-report",
            "Private S0 observations only.\nSTAGE: ORIENTATION_READY\n")
        # 发布"仅私有 S0 观察 + ORIENTATION_READY"。
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"ready"}}'},
        ]
        return SimpleNamespace(
            status="done", infra_pause_secs=0, infra_pauses=0), history
        # 无暂停遥测的 done。

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    events = []
    session = V.VerifierSession(
        on_event=lambda event, payload: events.append((event, payload)))
    # 带事件收集器的会话。
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
        verifier_stage_lifecycle=True,
        verifier_persist_scratch=True,
    )
    # 开启路由/阶段生命周期/scratch 持久化（能力齐全也不该建评估器）。

    verdict, findings = V.verify_agentic(
        "task", object(), cfg, session=session,
        stage="orientation", wall_budget=50)
    # 以 orientation 阶段执行。

    assert verdict == "orientation_ready"
    assert "STAGE: ORIENTATION_READY" in str(findings)
    # 判定与报告如实。
    assert "STAGE: ORIENTATION" in prompts[0]
    assert "No Actor Agent for this target cycle has been created" in prompts[0]
    assert "private_evaluator" not in prompts[0]
    assert "future candidate is correct" in prompts[0]
    # orientation 提示词：阶段标记、无 Actor 现状、无 private_evaluator、
    # 禁止预判未来候选。
    assert session.stage == "ORIENTATION_READY"
    assert session.orientation_runs == 1
    assert session.candidate_generation == 0
    # 会话阶段与计数：1 次 orientation、0 次候选生成。
    assert not hasattr(session, "current_evaluator")
    assert not hasattr(AgenticVerifierExecutor, "run_frozen_private_evaluator")
    # 关键：没有创建评估器对象，执行器上也没有冻结评估器方法。
    assert all(not event.startswith("VERIFIER_EVALUATOR_")
               for event, _payload in events)
    # 全程无评估器生命周期事件。


def test_candidate_verification_uses_only_explicit_agent_actions(monkeypatch):
    """候选验证只依赖 Agent 显式选择的动作（无自动仪器）。"""
    from core import loop as actor_loop

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    prompts = []

    def fake_attempt(prompt, vm, cfg, sink, **kwargs):
        prompts.append(prompt)
        kwargs["program_executor"](
            "verifier-report",
            "Direct inspection affirmatively supports the task.\nROUTE: HANDOFF\n")
        # 发布"直接检查正面支持 + HANDOFF"。
        history = list(kwargs.get("initial_history") or []) + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"published"}}'},
        ]
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(actor_loop, "run_attempt", fake_attempt)
    events = []
    session = V.VerifierSession(
        on_event=lambda event, payload: events.append((event, payload)))
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
        verifier_stage_lifecycle=True,
        verifier_persist_scratch=True,
    )
    # 能力齐全的候选验证配置。

    verdict, findings = V.verify_agentic(
        "task", object(), cfg, session=session, wall_budget=50)

    assert verdict == "pass"
    assert "affirmatively supports" in str(findings)
    # pass 判定与报告无损。
    assert "private_evaluator" not in prompts[0]
    assert "freely create, adapt, or discard whatever probes" in prompts[0]
    # 提示词：无 private_evaluator、探针由 Agent 自由创建/调整/丢弃。
    assert all(not event.startswith("VERIFIER_EVALUATOR_")
               for event, _payload in events)
    # 无评估器生命周期事件。


def test_agentic_charter_leaves_investigation_and_report_shape_to_agent():
    """调查章程：调查与报告形态交给 Agent（无清单/schema/探针数）。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "/tmp/report.md", "file changed")
    # 生成调查章程。
    assert "full investigation Agent" in prompt
    # 验证器是完整调查 Agent。
    assert "no prescribed checklist, report schema, probe count" in prompt
    # 无预设清单/报告 schema/探针数。
    assert "code-as-policy freedom" in prompt
    # code-as-policy 自由。
    assert "mechanically\neffect-isolated action surface" in prompt
    # 效果隔离动作面（保留换行位置的精确断言）。
    assert "Freely author Python or Bash" in prompt
    # 自由写 Python/Bash。
    assert "VERIFIER_SCRATCH" in prompt
    # 专用 scratch。
    assert "output truncation" in prompt
    # 输出截断警示。
    assert "verifier-read" not in prompt
    # 不出现 verifier-read 通道（读取走动作）。
    assert "verifier-report" in prompt
    # 报告发布语言。
    assert "VERDICT: PASS" in prompt and "VERDICT: FAIL" in prompt
    # 两种终局判定词。


def test_unverified_charter_requests_untrusted_reproducible_leads():
    """UNVERIFIED 章程请求"不可信的可复现先导"并逐字包含它们。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel",
        allow_unverified=True,
        actor_evidence_leads=(
            "Actor-stated claim: service is live\n"
            "Exact reproducible probe proposed by Actor: curl localhost"))
    # 开启 UNVERIFIED 并附 Actor 证据先导。

    assert "VERDICT: UNVERIFIED" in prompt
    # UNVERIFIED 判定词在章程里。
    assert "same persistent Verifier Agent" in prompt
    # 同一持久验证器 Agent 续办。
    assert "credible instruments or observation channels disagree" in prompt
    # 证据请求的触发语义（可信仪器/观察通道不一致）。
    assert "ACTOR-SUPPLIED EVIDENCE LEADS" in prompt
    # 先导段头。
    assert "untrusted leads" in prompt
    # 明示"不可信先导"（Actor 的主张不作数）。
    assert "curl localhost" in prompt
    # 先导原文逐字包含。


def test_rollback_charter_truthfully_exposes_live_namespaces_then_restore():
    """rollback 章程如实描述"活命名空间的事务镜像 + 判定前恢复"。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel",
        execution_mode="rollback_mirror")

    assert "transactional\nmirror of the exact live Actor machine" in prompt
    # 事务镜像（保留换行位置）。
    assert "processes, localhost services, network, GUI, and IPC" in prompt
    # 镜像覆盖：进程/本地服务/网络/GUI/IPC。
    assert "restores that checkpoint before grading" in prompt
    # 判定前恢复检查点。
    assert "`$HOME` remains `/home/user`" in prompt
    # 家目录保持不变。
    assert "without access to the Actor's processes" not in prompt
    # 旧版（错误）的"无法访问 Actor 进程"措辞已移除——镜像里能访问。


def test_agentic_charter_restores_universal_falsification_principles():
    """章程恢复"普适证伪原则"（空白归一后断言多句原则原文）。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)
    flat = " ".join(prompt.split())
    # 空白归一。

    assert "Verification is method-neutral unless the authoritative task makes a " \
        "method, application, native editable state, behavior, workflow, or " \
        "provenance material" in flat
    # 方法中立原则（除非任务让某渠道成为实质）。
    assert "compatible file, backend render, or visually similar export is not by " \
        "itself proof" in flat
    # 兼容文件/后端渲染/视觉相似导出本身不是证明。
    assert "evidence that distinguishes the required result from a nearby plausible " \
        "but incorrect substitute" in flat
    # 要求区分性证据。
    assert "keep investigating through a distinguishing channel" in flat
    # 不足时继续通过区分性渠道调查。
    assert "no prescribed checklist, report schema, probe count" in flat
    # 无预设清单/schema/探针数。


def test_orientation_is_free_and_creates_no_automatic_instrument():
    """orientation 章程：自由熟悉环境、只产私有报告、绝不建自动仪器。"""
    prompt = agent_harness_verifier_orientation_charter(
        "authoritative task", "host channel", "trusted baseline")
    flat = " ".join(prompt.split())

    assert "Freely get familiar with this environment" in prompt
    # 自由熟悉环境。
    assert "task-start observations and hypotheses" in flat
    # 产出：任务起始观察与假设。
    assert "Orientation produces only your private report" in flat
    # 只产私有报告。
    assert "Only actions you explicitly choose to run are executed" in flat
    # 只有显式选择的动作才执行。
    assert "does not discover, freeze, rerun, score, or privilege files" in flat
    # 不发现/冻结/重跑/评分/特权文件。
    assert "private_evaluator" not in prompt
    assert "run.sh" not in prompt
    # 无评估器、无 run.sh 自动仪器。
    assert "STAGE: ORIENTATION_READY" in prompt
    # 就绪阶段令牌。
    assert "HANDOFF, REVISE, or EVOLVE" in prompt
    # 阶段外仍提示三种路由。


def test_rollback_orientation_describes_its_actual_home_and_transaction():
    """rollback orientation 如实描述事务镜像与其家目录。"""
    prompt = agent_harness_verifier_orientation_charter(
        "authoritative task", "host channel", "trusted baseline",
        execution_mode="rollback_mirror")
    flat = " ".join(prompt.split())

    assert "transactional mirror of the exact live S0 machine" in flat
    # 活 S0 机器的事务镜像。
    assert "restores it when orientation ends" in flat
    # orientation 结束时恢复。
    assert "`$HOME` remains `/home/user`" in flat
    # 家目录不变。
    assert "effect-isolated and read-only outside" not in flat
    # 旧版（错误）的"效果隔离、外部只读"措辞已移除。


def test_candidate_verification_uses_fresh_agent_chosen_probes_without_checklist():
    """候选验证章程：全新 Agent 自选探针，无清单、无自动、无旧守卫词。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)

    assert "S0 observations, old hypotheses" in prompt
    # 上下文：S0 观察与旧假设。
    assert "freely create, adapt, or discard whatever probes" in prompt
    # 探针自由。
    assert "private_evaluator" not in prompt
    assert "automatically" not in prompt
    # 无评估器、无"自动"仪器。
    assert "no prescribed checklist, report schema, probe count" in prompt
    # 无预设清单/schema/探针数。
    assert "HANDOFF_FALSIFICATION" not in prompt
    # 旧版守卫标记已移除。


def test_curriculum_verify_more_preserves_verifier_authority_on_same_candidate():
    """VERIFY_MORE 章程：候选未变、课程无权否决验证器的本地判定。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", candidate_generation=4,
        curriculum_review=(
            "The report omitted evidence for one requirement.\n"
            "ROUTE: VERIFY_MORE\n"))
    # 候选第 4 代 + 课程复查消息。

    assert "This is candidate generation 4" in prompt
    # 代次如实质入章程。
    assert "candidate has not changed" in prompt
    # 候选未变。
    assert "cannot see the candidate" in prompt
    # 课程看不到候选。
    assert "has not overruled your local correctness judgment" in prompt
    # 关键：课程没有否决验证器的本地正确性判定。
    assert "publish PASS again" in prompt and "or FAIL" in prompt
    # 允许再次 PASS 或改判 FAIL。


def test_unified_charter_adds_agent_owned_evolve_route_without_teaching():
    """统一章程加入 Agent 自有的 EVOLVE 路由，但绝不含教学请求。"""
    prompt = agent_harness_verifier_charter(
        "authoritative task", "host channel", allow_evolve=True)

    assert "ROUTE: HANDOFF" in prompt
    assert "ROUTE: REVISE" in prompt
    assert "ROUTE: EVOLVE" in prompt
    # 三种路由词。
    assert "EVOLVE is a routing judgment" in prompt
    # EVOLVE 是路由判断。
    assert "not a request to teach" in prompt
    # 不是教学请求。
    assert "no prescribed checklist, report schema, probe count" in prompt
    # 无预设清单/schema/探针数。
    assert "VERDICT: PASS" not in prompt and "VERDICT: FAIL" not in prompt
    # 路由模式下无 VERDICT 判定词（二值判定让位给路由）。


def test_substantive_checkpoint_drops_dry_tail_but_returns_observation():
    """实质检查点丢弃干话尾部但返回挂起观察（Look 轮次原样保留）。"""
    prior = [{"role": "user", "content": "opening"},
             {"role": "assistant", "content":
              '{"program":{"lang":"bash","code":"echo first"}}'}]
    # 已锚定检查点：开场 + 程序动作。
    raw = prior + [
        {"role": "user", "content": "first trace"},
        {"role": "assistant", "content":
         '{"look":{"path":"/tmp/a.png","question":"inspect"}}'},
        {"role": "user", "content": "the look result, losslessly"},
        {"role": "assistant", "content": "Now publishing the report:"},
        {"role": "user", "content": "strict format nudge"},
        {"role": "assistant", "content": "Now publishing the report:"},
    ]
    # 原始历史：锚定段 + look 动作/观察 + 两轮干话（被 nudges 追问）。

    checkpoint, observation, images, turn = \
        V._agentic_substantive_checkpoint(prior, raw)
    # 计算实质检查点。

    assert len(checkpoint) == 4
    assert checkpoint[-1]["content"].startswith('{"look"')
    # 检查点 4 条：锚定 2 条 + look 动作（干话尾部被丢）。
    assert observation == "the look result, losslessly"
    # 挂起观察 = look 结果。
    assert images == []
    # 无图像载荷。
    assert isinstance(turn, V.Look)
    # 挂起轮次是 Look。
    assert all("Now publishing" not in m["content"] for m in checkpoint)
    # 关键：干话（"Now publishing"）不进检查点。


def test_semantic_replay_keeps_every_action_and_result_without_dry_turns():
    """语义重放保留每个动作与结果、剥掉全部干话。"""
    raw = [
        {"role": "user", "content": "full opening"},
        {"role": "assistant", "content":
         'analysis then {"program":{"lang":"bash","code":"probe"}}'},
        {"role": "user", "content": "complete probe trace"},
        {"role": "assistant", "content": "I will look now"},
        {"role": "user", "content": "strict nudge"},
        {"role": "assistant", "content":
         '{"look":{"path":"/tmp/a.png","question":"inspect"}}'},
        {"role": "user", "content": "complete look observation"},
        {"role": "assistant", "content": "I will publish now"},
    ]
    # 原始历史：开场 → 程序（带前导分析）→ 轨迹 → 干话 → 提醒 → look →
    # 观察 → 干话。

    replay, observation, images, turn = V._agentic_semantic_replay(raw)
    # 计算语义重放。

    assert len(replay) == 4
    # 重放 4 条（干话与提醒被剥）。
    assert replay[0]["content"] == "full opening"
    # 开场保留。
    assert "complete probe trace" in replay[2]["content"]
    assert "strict nudge" in replay[2]["content"]
    # 观察消息合并了轨迹与提醒（无损上下文）。
    assert [type(V.parse_turn(replay[i]["content"])).__name__
            for i in (1, 3)] == ["Program", "Look"]
    # 位置 1/3 是程序与 Look（动作语义保留）。
    assert all("I will" not in message["content"] for message in replay)
    # 关键：干话（"I will ..."）全部剥除。
    assert observation == "complete look observation"
    assert images == []
    assert isinstance(turn, V.Look)
    # 挂起观察/图像/轮次如实返回。


def test_agentic_recovery_preserves_report_and_rolls_back_actionless_tail(
        monkeypatch, tmp_path):
    """恢复保留报告并回滚无动作尾：丢弃对与挂起观察如实入账。"""
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _VM()
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({"prompt": prompt, "history": list(history),
                      "continue": kwargs.get("continue_context")})
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content":
                 '{"program":{"lang":"bash","code":"echo evidence"}}'},
                {"role": "user", "content": "full program trace"},
                {"role": "assistant", "content": "Now writing the report:"},
                {"role": "user", "content": "strict nudge"},
                {"role": "assistant", "content": "Now writing the report:"},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history
            # 第一段：真实动作 + 轨迹 + 两轮干话（stalled）。

        assert len(history) == 2
        assert "Now writing" not in str(history)
        assert "full program trace" in prompt
        # 第二段：历史回滚到 2 条（锚定 + 动作），干话不在历史里，观察在提示词里。
        report = "# Independent report\nVERDICT: FAIL\ncomplete evidence\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布 FAIL 报告。
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"write report"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=sink, turn_no=9, session=session, wall_budget=50)

    assert verdict == "wrong"
    assert "complete evidence" in str(findings)
    # 判定与证据无损。
    assert len(vm.removals) == 0
    # 没有误删任何报告文件。
    assert len(calls) == 2 and calls[1]["continue"] is True
    # 两段；第二次续跑。
    assert all("Now writing" not in m["content"] for m in session)
    # 会话里没有干话。
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_advanced"] is True
    assert recovery["discarded_tail_pairs"] == 2
    assert recovery["pending_observation_chars"] == len("full program trace")
    # 关键：检查点推进、丢弃 2 对干话尾、挂起观察字节数如实在账。


def test_agentic_recovery_reattaches_look_without_replaying_dry_reply(
        monkeypatch, tmp_path):
    """Regression for the live t099 Look -> actionless-promise attractor."""
    # t099 回归：look 之后被"我保证要写报告"吸引而空转——恢复必须重附
    # look 观察与像素而不重放干回复。
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    calls = []
    pixels = b"\x89PNG\r\n\x1a\nexact-verifier-observation"
    # PNG 头 + 观察字节（持久消息的图像载荷）。

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({
            "prompt": prompt,
            "history": list(history),
            "opening_image": kwargs.get("opening_image"),
        })
        if len(calls) == 1:
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content":
                 '{"look":{"path":"/tmp/candidate.png",'
                 '"question":"identify it"}}'},
                durable_user_message("complete visual result", pixels),
                {"role": "assistant", "content":
                 "I will now run the exact format check."},
                {"role": "user", "content": "emit one action now"},
                {"role": "assistant", "content": ""},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history
            # 第一段：look 动作 + 带像素的观察 + 干话 + 提醒 + 空回复。

        assert len(history) == 2
        assert all("I will now" not in str(message.get("content", ""))
                   for message in history)
        # 第二段：历史回滚到 2 条，干回复不在其中。
        assert calls[-1]["opening_image"] == [pixels]
        # 关键：像素以开场图像重附（多模态无损）。
        assert "complete visual result" in prompt
        assert "exact pixels" in prompt
        # 观察文本与像素说明进提示词。
        report = "# Independent report\nVERDICT: PASS\nvisual and format evidence\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布 PASS。
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    session = V.VerifierSession()
    sink = ArtifactSink(str(tmp_path / "run"))

    verdict, findings = V.verify_agentic(
        "task", _VM(),
        SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=sink, turn_no=3, session=session, wall_budget=50)

    assert verdict == "pass"
    assert "visual and format evidence" in str(findings)
    # 判定与报告无损。
    assert len(calls) == 2
    # 两段完成。
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_history_pairs"] == 1
    assert recovery["discarded_tail_pairs"] == 2
    assert recovery["pending_observation_images"] == 1
    # 关键：检查点 1 对、丢弃 2 对干话尾、挂起观察带 1 张图像。


def test_dry_first_segment_rehydrates_full_inspection_envelope(
        monkeypatch, tmp_path):
    """A transport stall before the first action must not erase task authority."""
    # 首个动作之前传输停摆绝不抹掉任务权威（全干首段→全量信封重注水）。
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    calls = []

    def fake_run_attempt(prompt, _vm, cfg, sink, **kwargs):
        history = list(kwargs.get("initial_history") or [])
        calls.append({
            "prompt": prompt,
            "history": list(history),
            "continue": kwargs.get("continue_context"),
        })
        if len(calls) == 1:
            # Three dry decoder turns reproduce the live t019 recovery boundary:
            # no Program/Look exists, so there is no substantive checkpoint yet.
            history += [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "I will investigate."},
                {"role": "user", "content": "emit one action"},
                {"role": "assistant", "content": "I will investigate."},
                {"role": "user", "content": "emit one action now"},
                {"role": "assistant", "content": ""},
            ]
            sink.save_transcript("system", history)
            return SimpleNamespace(status="stalled"), history
            # 第一段：三轮干话（复现 t019 现场边界——无任何 Program/Look）。

        assert history == []
        assert "AUTHORITATIVE TASK — verbatim" in prompt
        assert "the complete authoritative task" in prompt
        assert "current Actor candidate handoff" not in prompt
        assert "self-checks" in prompt and "conclusions" in prompt
        assert "SAME-CONTEXT TRANSPORT RECOVERY" in prompt
        # 第二段：历史清空；权威任务原文回注；无候选交接措辞（orientation
        # 形态）；自检/结论要求保留；同上下文传输恢复信封在场。
        report = "# Independent evidence\nROUTE: HANDOFF\n"
        kwargs["program_executor"]("verifier-report", report)
        assert kwargs["terminal_handoff_ready"]()
        # 发布 HANDOFF 路由报告。
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"publish"}}'},
        ]
        sink.save_transcript("system", history)
        return SimpleNamespace(status="done"), history

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)
    cfg = SimpleNamespace(
        agentic_verifier_config="verifier.yaml",
        verifier_evolve_route=True,
    )
    verdict, _findings = V.verify_agentic(
        "the complete authoritative task", _VM(), cfg,
        sink=ArtifactSink(str(tmp_path / "run")),
        session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass"
    assert len(calls) == 2
    # 两段完成，最终 pass。
    assert calls[0]["continue"] is False
    assert calls[1]["continue"] is True
    # 第一次全新开场、第二次续跑。
    recovery = json.loads((tmp_path / "run" / "verifier_agent" /
                           "inspection_001" / "segment_000" /
                           "recovery.json").read_text())
    assert recovery["checkpoint_history_pairs"] == 0
    assert recovery["checkpoint_advanced"] is False
    # 无实质检查点：0 对历史、检查点未推进（权威由信封重注水而非检查点）。
