# 【中文注释·模块定位】Phase-1 波次起草（wave authoring）的续跑与终止契约测试
# （explore/phase1_wave._author_wave + explore/practice_loop 的
# PracticeHooks/PracticeBoundaryError/PracticeInfrastructureError + core/loop
# 的 LoopResult + core/actor 的 PLAIN_JSON_TRANSPORT_NOTE）。场景：课程 Agent
# 在 guest 侧起草"下一个进化波次"——反复尝试直到它发布交接决策（WAVE/
# SATURATED/STALLED）。被钉死的行为承诺：
# 1) 重试保留工作与上下文：陈旧交接必须失效、部分草稿文件跨段保留、先验
#    历史不可变（深拷贝对账）、系统提示契约前缀与 JSON 传输提示不丢；
# 2) 终局探针只认"已声明决策 + 已落盘的非空 fixture 树"；
# 3) 转写越界（boundary audit 命中）→ 隔离并写 boundary_audit.json，
#    状态 quarantined 且绝不含决策；
# 4) 连续无动作段落按基础设施故障停止（不超过 _MAX_ACTIONLESS_WAVE_SEGMENTS），
#    草稿归档进 blocked_draft、逐段转写保留，但"有产出的早段"不被误erase；
# 5) 有实际执行的段落重置无动作连击；所有段落共享同一份配置预算（累计扣减，
#    超出累计上限报 cumulative transport ceiling）；
# 6) 语义终局决策（SATURATED/STALLED）只能由 Agent 自己发布；
# 7) 草稿捕获失败绝不掩盖原始基础设施故障（draft_capture 记录错误）。
# LocalWaveVM 只执行 harness 的真实 fixture 探测命令（bash），其余钩子伪替。
"""A Curriculum retry must preserve work and cannot renew an empty loop forever."""
# 模块 docstring：课程重试必须保留工作，且不能无限续起一个空转循环。

import copy
# 导入 copy：深拷贝历史做"输入不可变"对账。
import json
# 导入 json：读写交接 handoff 与 authoring_state。
from pathlib import Path
# 导入 Path：盘上路径构造。
import shutil
# 导入 shutil：捕获钩子里复制草稿树。
import subprocess
# 导入 subprocess：LocalWaveVM 真实执行 bash 探测命令。

import pytest
# 导入 pytest：fixture、参数化与异常断言。

from config.settings import Config
# 导入配置 dataclass：构造各测试的迭代/墙钟预算。
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
# 导入纯 JSON 传输提示：断言它出现在 Phase-1 起草的系统提示里。
from core.loop import LoopResult
# 导入循环结果容器：伪 attempt 的返回值形状。
from explore import phase1_wave as wave
# 导入被测模块：Phase-1 波次起草（_author_wave）。
from explore.practice_loop import PracticeBoundaryError, PracticeHooks, PracticeInfrastructureError
# 导入练习循环构件：钩子集合、越界异常与基础设施异常。


class LocalWaveVM:
    """Execute only the harness's fixture probes against pytest-owned paths."""

    # 类 docstring：只执行 harness 的 fixture 探测命令（针对 pytest 持有的路径）。
    # 伪 VM：真实 bash 执行（探测 fixture 树的存在性/非空性），
    # 但被探测的路径全部在 pytest 的 tmp_path 下。

    def run_command(self, command, timeout=30):
        # VM 句柄唯一需要的方法：bash -c 执行并返回 stdout。
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            timeout=timeout, check=True)
        # 真实子进程执行（失败即抛错，测试立即失败）。
        return result.stdout
        # 返回标准输出。


@pytest.fixture
def surface(tmp_path, monkeypatch):
    """搭建 guest 侧波次根/交接文件，并把模块常量重定向到 tmp_path。"""
    # 返回（波次根, 交接文件, 起草输出根）三元组。
    root = tmp_path / "guest/evolution_wave"
    # guest 侧波次草稿根（对应生产常量 /home/user/evolution_wave）。
    handoff = tmp_path / "guest/phase1_wave.json"
    # 交接决策文件（对应生产常量 /home/user/phase1_wave.json）。
    root.mkdir(parents=True)
    # 预创建波次根目录。
    monkeypatch.setattr(wave, "WAVE_ROOT", str(root))
    monkeypatch.setattr(wave, "WAVE_HANDOFF", str(handoff))
    # 把被测模块的两个 guest 路径常量重定向到临时目录。
    return root, handoff, tmp_path / "curriculum/wave_002"
    # 第三个元素：本课程轮次（wave_002）的起草输出根。


def _publish(surface, decision="WAVE"):
    """向交接文件发布一条课程决策（默认 WAVE + 一个项目）。"""
    root, handoff, _ = surface
    record = {
        "decision": decision, "rationale": "the Curriculum's own evidence",
        "projects": ([{"id": "p", "instruction":
                       "Keep all work in /home/user/evolution_project."}]
                     if decision == "WAVE" else []),
    }
    # 决策记录：决策词、课程自有证据作理由；WAVE 才带项目（SATURATED/
    # STALLED 是语义终局，不开新项目）。
    handoff.write_text(json.dumps(record))
    # 决策落盘。
    return record
    # 返回记录供断言。


def _draft(surface, text="original partial input"):
    """在波次根的项目草稿区写一个部分完成的 input.txt。"""
    path = surface[0] / "p/evolution_project/input.txt"
    # 项目 p 的草稿文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 建目录树。
    path.write_text(text)
    # 写入草稿内容。
    return path
    # 返回路径。


def _hooks(surface, attempt):
    """构造一套 PracticeHooks：真实捕获草稿树，其余钩子轻量伪替。"""
    def capture(_vm, _key, destination, *, guest_dirs, attempts):
        # 伪捕获项目工件钩子：校验 guest 目录名与尝试次数后复制草稿树。
        assert guest_dirs == [surface[0].name] and attempts == 1
        # 捕获只针对 evolution_wave 这一个 guest 目录，且限一次尝试。
        if not surface[0].exists():
            return {"ok": False, "files": 0}
        # 根不存在时如实报告失败。
        shutil.copytree(surface[0], Path(destination) / "evolution_wave")
        # 整树复制到目标（模拟真实归档捕获）。
        return {"ok": True}
        # 捕获成功。

    return PracticeHooks(
        run_attempt=attempt,
        read_guest_text=lambda _vm, path: (
            Path(path).read_text() if Path(path).is_file() else ""),
        audit_text=lambda *_args, **_kwargs: [],
        audit_transcripts=lambda *_args, **_kwargs: [],
        capture_project=capture)
    # 钩子集合：尝试函数、guest 文本读取（真实读临时文件）、文本/转写审计
    # 默认放行、草稿捕获如上。


def _author(surface, attempt, *, cfg=None, history=None):
    """以标准参数调用被测的 wave._author_wave。"""
    return wave._author_wave(
        hooks=_hooks(surface, attempt), vm=LocalWaveVM(),
        cfg=cfg or Config(max_iters=20, wall_clock_secs=60),
        target_direction="public task instruction", previous_wave_outcomes="",
        history=history or [], sink_root=surface[2],
        target_query_conditioned=True)
    # 公开任务指令 + 空先验结果 + 指定历史 + 输出根 + 目标查询条件化开启。


def test_retry_preserves_partial_files_and_complete_context(surface):
    """重试必须保留部分草稿与完整上下文，且不污染任何输入。"""
    stale = surface[0] / "old-wave.txt"
    stale.write_text("previous wave must not survive the fresh-wave boundary")
    # 上一波的陈旧文件：fresh-wave 边界必须清掉它。
    prior = [{"role": "user", "content": "prior project outcome"},
             {"role": "assistant", "content": "prior lesson", "reasoning": "kept"}]
    # 先验历史：一对带 reasoning 字段的消息。
    original = copy.deepcopy(prior)
    # 先验历史的深拷贝基准（用于证明输入不可变）。
    cfg = Config(max_iters=20, wall_clock_secs=60, system_extra="existing contract")
    # 配置：带一个既有系统提示附加契约。
    histories = []
    # 记录每次尝试收到的初始历史。

    def attempt(prompt, _vm, phase_cfg, sink, **kwargs):
        # 伪尝试函数：第一段起草失败（stalled），第二段重试后发布决策。
        histories.append(copy.deepcopy(kwargs["initial_history"]))
        # 深拷贝记录本次收到的历史。
        assert PLAIN_JSON_TRANSPORT_NOTE in phase_cfg.system_extra
        # 系统提示必须带纯 JSON 传输提示。
        assert phase_cfg.system_extra.startswith("existing contract")
        # 既有契约前缀排在最前（用户的 system_extra 不被吞掉）。
        assert not stale.exists()
        # fresh-wave 边界已清掉陈旧文件（每次尝试都成立）。
        if len(histories) == 1:
            # 第一段：起草一份部分输入后 stalled。
            _draft(surface)
            # 写部分草稿文件。
            surface[1].write_text('{"decision": "WAVE",')
            # 写一份截断（损坏）的交接文件。
            assert not kwargs["terminal_handoff_ready"]()
            # 截断交接不算就绪。
            history = prior + [{"role": "user", "content": prompt},
                               {"role": "assistant", "content": "drafted input"}]
            # 本段历史 = 先验 + 本段问答。
            sink.save_transcript("system", history)
            # 保存段转写。
            return LoopResult(status="stalled", iters=1, programs_run=1,
                              wall_secs=2), history
            # 返回 stalled（消耗 1 迭代 2 秒）。
        assert not surface[1].exists(), "a retry must invalidate the stale handoff"
        # 重试开始时陈旧/损坏交接已被清除。
        assert (surface[0] / "p/evolution_project/input.txt").read_text() == \
            "original partial input"
        # 第一段的部分草稿文件原样保留。
        assert "draft fixtures" in prompt and "remain on the same machine" in prompt
        # 重试提示词告知：草稿 fixture 在原机保留（不要求重新起草）。
        assert kwargs["continue_context"] is True
        # 续跑上下文开关开启。
        _publish(surface)
        # 第二段发布完整 WAVE 决策。
        assert kwargs["terminal_handoff_ready"]()
        # 决策就绪后终止探针放行。
        return LoopResult(status="done", iters=1, programs_run=1), histories[-1]
        # 返回 done。

    decision, returned = _author(surface, attempt, cfg=cfg, history=prior)
    # 执行起草：传入先验历史与配置。
    assert decision.decision == "WAVE"
    # 最终解析出的决策是 WAVE。
    assert returned == histories[-1]
    # 返回的历史 = 最后一段的历史。
    assert histories[1][:2] == original and len(histories[1]) == 4
    # 第二段历史 = 先验 2 条 + 两段问答共 4 条（上下文完整衔接）。
    assert prior == original and cfg.system_extra == "existing contract"
    # 关键：先验历史与配置在起草全程未被修改（输入不可变）。
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    # 读取起草状态文件。
    assert state["status"] == "published" and state["segments"] == 2
    # 状态 published、共 2 段。


def test_terminal_probe_waits_for_the_declared_fixture_tree(surface):
    """终局探针只认"已声明决策 + 已落盘的非空 fixture 树"三要素齐全。"""
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        # 伪尝试函数：逐步补齐终局条件并观察探针。
        _publish(surface)
        # 先发布决策。
        assert not kwargs["terminal_handoff_ready"]()
        # 只有决策、没有 fixture 树：不就绪。
        (surface[0] / "p/evolution_project").mkdir(parents=True)
        # 创建（空的）fixture 目录。
        assert not kwargs["terminal_handoff_ready"](), "empty fixtures are incomplete"
        # 空 fixture 树仍不就绪（必须非空）。
        _draft(surface)
        # 写入草稿文件。
        assert kwargs["terminal_handoff_ready"]()
        # 决策 + 非空 fixture 树：就绪。
        return LoopResult(status="done", iters=1, programs_run=1), []
        # 返回 done。

    assert _author(surface, attempt)[0].decision == "WAVE"
    # 起草成功解析出 WAVE 决策。


def test_wave_transcript_quarantine_updates_authoring_state(surface):
    """转写越界命中 → 隔离：状态 quarantined、写 boundary_audit、绝不含决策。"""
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        # 伪尝试函数：起草并发布后正常 done。
        _draft(surface)
        _publish(surface)
        return LoopResult(status="done", iters=1, programs_run=1), []

    hooks = _hooks(surface, attempt)
    # 取默认钩子集。
    hooks.audit_transcripts = lambda *_args, **_kwargs: [
        {"file": "trace.txt", "hits": [{"kind": "repo-url", "match": "blocked"}]}]
    # 注入越界审计命中：trace.txt 命中 repo-url 类封锁项。
    with pytest.raises(PracticeBoundaryError):
        wave._author_wave(
            hooks=hooks, vm=LocalWaveVM(), cfg=Config(),
            target_direction="public task instruction", previous_wave_outcomes="",
            history=[], sink_root=surface[2], target_query_conditioned=True)
        # 起草必须以越界异常失败。

    state = json.loads((surface[2] / "authoring_state.json").read_text())
    # 读取起草状态。
    assert state["status"] == "quarantined"
    # 状态是隔离。
    assert "boundary_audit.json" in state["reason"]
    # 隔离原因指向 boundary_audit.json。
    assert "decision" not in state
    # 关键：被隔离的起草绝不能携带决策（语义终局不能从越界段发布）。
    assert (surface[2] / "segment_000/boundary_audit.json").is_file()
    # 审计报告落盘在对应段目录。


def test_repeated_narration_stops_as_infrastructure_and_archives_drafts(surface):
    """连续无动作（纯叙述）段落按基础设施故障停止，并归档草稿与转写。"""
    calls = []
    # 记录尝试调用次数。

    def attempt(prompt, _vm, _cfg, sink, **kwargs):
        # 伪尝试函数：第一段有产出（起草），之后三段全部无动作纯叙述。
        calls.append(sink.root)
        # 记录输出段根。
        # An earlier productive segment must not be erased by later dry retries.
        if len(calls) == 1:
            _draft(surface)
            # 第一段起草（有产出）。
            return LoopResult(status="stalled", programs_run=1, iters=1), []
            # 返回 stalled（programs_run=1 表示有实际执行）。
        history = list(kwargs["initial_history"])
        # 之后各段从初始历史出发。
        for index in range(1, 4):
            reply = "Tool channel explicitly closed this turn (tool_choice=none)."
            sink.save_turn(index, reply)
            history += [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": reply}]
            # 三轮纯叙述问答（无 Program/Look 动作）。
        sink.save_transcript("system", history)
        # 保存段转写。
        return LoopResult(status="stalled", turns=3, wall_secs=1), history
        # 返回 stalled（无动作段落）。

    with pytest.raises(PracticeInfrastructureError, match="no Program or Look actions"):
        _author(surface, attempt)
        # 连续无动作超过上限（_MAX_ACTIONLESS_WAVE_SEGMENTS）→ 基础设施异常。
    assert len(calls) == 4
    # 共尝试 4 段：1 个有产出段 + 3 个无动作段。
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    # 读取状态。
    assert state["status"] == "infra" and state["actionless_segments"] == 3
    # 状态 infra、无动作段计数为 3。
    assert "decision" not in state
    # infra 停止绝不携带决策。
    archived = surface[2] / "blocked_draft/evolution_wave/p/evolution_project/input.txt"
    assert archived.read_text() == "original partial input"
    # 草稿被归档进 blocked_draft 且内容无损。
    transcripts = sorted(surface[2].glob("segment_*/transcript.json"))
    assert len(transcripts) == 3
    # 三个无动作段各留一份转写。
    assert len(json.loads(transcripts[-1].read_text())["messages"]) == 18
    # 末段转写 18 条消息（3 轮问答 × 3 段累计的历史长度：6×3=18）。
    assert len(list(surface[2].glob("segment_*/result.json"))) == 4
    # 4 个段 result 全部保留（含第一段有产出段）——早段不被误删。


def test_executed_work_resets_the_actionless_retry_streak(surface):
    """有实际执行的段落重置无动作连击（允许"干活-停顿"交替长存）。"""
    remaining = iter([0, 0, 1, 0, 0, 1])
    # 六段的动作脚本：0=无动作、1=有执行。
    calls = []

    def attempt(_prompt, _vm, _cfg, _sink, **_kwargs):
        # 伪尝试函数：按脚本交替"无动作/有执行"，最后一段发布决策。
        work = next(remaining)
        calls.append(work)
        if work:
            _draft(surface)
            # 有执行的段落起草文件。
        if len(calls) == 6:
            _publish(surface)
            return LoopResult(status="done", programs_run=work, iters=work), []
            # 第六段发布决策并 done。
        return LoopResult(status="stalled", programs_run=work, iters=work), []
        # 其余段 stalled。

    assert _author(surface, attempt)[0].decision == "WAVE"
    # 最终成功解析出 WAVE 决策。
    assert len(calls) == 6
    # 关键：六段全部被允许——连击在每次有执行后归零，从未触发 infra 停止。


@pytest.mark.parametrize("ceiling", ["wall", "iterations"])
def test_segments_share_one_configured_emergency_ceiling(surface, ceiling):
    """所有段落共享同一份配置的应急预算上限（累计扣减，超出即停）。"""
    # 参数化：分别用墙钟上限与迭代上限触发。
    cfg = Config(max_iters=3 if ceiling == "iterations" else 20,
                 wall_clock_secs=5 if ceiling == "wall" else 60)
    # 两种紧预算配置：3 迭代 / 5 秒。
    calls = []

    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        # 伪尝试函数：第一段消耗 2 迭代 3 秒，之后每段消耗 1 迭代 2 秒。
        calls.append((kwargs["iters_budget"], kwargs["wall_budget"]))
        # 记录本段拿到的预算。
        _draft(surface)
        # 每段都有执行（避免误入无动作路径）。
        return LoopResult(status="stalled", programs_run=1,
                          iters=2 if len(calls) == 1 else 1,
                          wall_secs=3 if len(calls) == 1 else 2), []
        # 返回 stalled 并如实报告消耗。

    with pytest.raises(PracticeInfrastructureError, match="cumulative transport ceiling"):
        _author(surface, attempt, cfg=cfg)
        # 第二段拿到的预算 = 3-2=1 迭代 / 57 秒（仍可执行），第三段开局
        # 剩余预算为 0 → 累计上限耗尽被阻塞。
    assert len(calls) == 2
    # 只发生了 2 段尝试。
    assert calls[1] == (cfg.max_iters - 2, cfg.wall_clock_secs - 3)
    # 关键：第二段拿到的预算 = 配置预算 - 第一段实际消耗（逐项扣减）。
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    # 读取状态。
    assert state["status"] == "infra" and state["segments"] == 2
    # 状态 infra、共 2 段。


@pytest.mark.parametrize("decision", ["SATURATED", "STALLED"])
def test_only_the_agent_can_publish_a_semantic_terminal_decision(surface, decision):
    """语义终局决策（SATURATED/STALLED）只能来自 Agent 自己发布的交接。"""
    def attempt(_prompt, _vm, _cfg, _sink, **kwargs):
        # 伪尝试函数：Agent 在段内发布语义终局并 done。
        _publish(surface, decision)
        # 发布参数化决策。
        assert kwargs["terminal_handoff_ready"]()
        # 决策就绪（SATURATED/STALLED 无需项目 fixture）。
        return LoopResult(status="done", iters=1, programs_run=1), []
        # 返回 done。

    assert _author(surface, attempt)[0].decision == decision
    # 起草结果如实返回该决策。
    assert json.loads((surface[2] / "authoring_state.json").read_text())["decision"] == decision
    # 决策被写入 authoring_state（published 段才允许携带 decision）。


def test_capture_failure_does_not_hide_the_original_infrastructure_failure(surface):
    """草稿捕获失败绝不能掩盖原始基础设施故障，错误被如实记录。"""
    def attempt(*_args, **_kwargs):
        # 伪尝试函数：直接以 infra 状态失败。
        return LoopResult(status="infra"), []

    hooks = _hooks(surface, attempt)
    # 取默认钩子集。
    def broken_capture(*_args, **_kwargs):
        raise OSError("archive transport unavailable")
        # 故障注入：捕获钩子抛出传输不可用异常。
    hooks.capture_project = broken_capture
    # 替换捕获钩子。
    with pytest.raises(PracticeInfrastructureError, match="infrastructure status infra"):
        wave._author_wave(
            hooks=hooks, vm=LocalWaveVM(), cfg=Config(),
            target_direction="public task instruction", previous_wave_outcomes="",
            history=[], sink_root=surface[2], target_query_conditioned=True)
        # 异常必须报告原始的 infra 状态（而不是捕获异常）。
    state = json.loads((surface[2] / "authoring_state.json").read_text())
    # 读取状态。
    assert state["draft_capture"] == {
        "ok": False, "error": "archive transport unavailable"}
    # 捕获失败被降级记录进 draft_capture 字段（诊断信息不丢、不喧宾夺主）。
