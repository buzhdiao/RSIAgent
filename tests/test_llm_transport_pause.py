# 【中文注释·模块定位】LLM 传输层暂停（llm/client.py + core/loop.py）回归测试：
# 供应商故障必须让 harness 暂停等待，绝不伪造"空的 Agent 回合"。测试策略：
# 1) llm.client.chat 的错误分类：402（欠费）等供应商状态错误 → recoverable=True
#    （重试 4 次后抛出，状态码保留）；400（请求参数错）→ 不可恢复；本地 SDK
#    编程错误（TypeError）→ 不可恢复且无状态码——只有"外部恢复可能"才值得暂停。
# 2) core.loop.run_attempt 的暂停记账：可恢复错误触发一次 7 秒暂停后同一上下文续跑，
#    暂停秒数计入 infra_pause_secs 并从工作时钟（wall_secs）剔除，不进对话历史；
# 3) 相邻行为的守卫：累积 nudge 不会误杀有产出的 Agent；JSON 重试只在下一请求
#    开启 json_object 且保留上下文；用户通道故障按基础设施错误中止并留痕；
#    嵌套 Verifier 的暂停要向上汇总进外层 Actor 的时钟。
# 全部用 monkeypatch 伪造 OpenAI 客户端与时间函数，不起真实网络。
"""Provider failures pause the harness; they are never synthetic Agent turns."""
# 模块 docstring：供应商故障让 harness 暂停，绝不合成假的 Agent 回合。

import json
# 导入 json：读取用户通道故障的审计记录。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 OpenAI 客户端对象树。

import pytest
# 导入 pytest：异常断言。

from config.settings import load
# 导入配置加载器：load(None) 取默认配置再就地调整。
from core import loop as L
# 以 L 别名导入主循环：被测主体 run_attempt（暂停记账与守卫）。
from core.trace import ArtifactSink
# 导入工件汇：为每次运行提供磁盘落盘目录。
from env.vm import Trace
# 导入 Trace：伪 VM run_script 的返回类型（stdout/exit_code/secs）。
from llm import client as C
# 以 C 别名导入 LLM 客户端：被测主体 chat（错误分类与重试）。
from llm.client import LLMTransportError
# 导入传输错误类型：断言其 recoverable/status_code 字段。


class _StatusError(Exception):
    # 伪造 OpenAI SDK 的 APIStatusError：带状态码与响应体。
    def __init__(self, status_code, body):
        super().__init__(body)
        self.status_code = status_code
        self.body = body


class _FailingCompletions:
    # 伪造 completions 接口：每次 create 都抛出预设错误并计数。
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.error


def test_402_raises_recoverable_transport_instead_of_empty_turn(monkeypatch):
    # 测试：402（欠费）这类供应商状态错误必须以"可恢复传输错误"抛出，
    # 而不是静默生成一条空助手消息；重试 4 次耗尽后才抛。
    completions = _FailingCompletions(
        _StatusError(402, {"message": "Insufficient credits"}))
    # 伪接口：4 次调用全部抛 402。
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    # 替换懒加载的 OpenAI 客户端：不发起真实网络请求。
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)
    # 抹掉重试间退避睡眠：测试立即完成。

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")
    # 调用 chat：必须以 LLMTransportError 收场。

    assert raised.value.recoverable is True
    # recoverable=True：欠费是外部状态，稍后重试有意义。
    assert raised.value.status_code == 402
    # 状态码 402 保留在错误对象上：调用方可按状态分类。
    assert completions.calls == 4
    # 恰好尝试 4 次（for attempt in range(4)）：耗尽重试后才放弃。


def test_bad_request_is_explicit_fatal_transport(monkeypatch):
    # 测试：400（请求参数错误）是不可恢复的致命传输错误——重试没有意义。
    completions = _FailingCompletions(
        _StatusError(400, {"message": "unsupported parameter"}))
    # 伪接口：抛 400。
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    # 替换 OpenAI 客户端。
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)
    # 抹掉退避睡眠。

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")
    # chat 必须抛传输错误。

    assert raised.value.recoverable is False
    # recoverable=False：修参数之前重试同请求永远失败。
    assert raised.value.status_code == 400
    # 状态码保留：明确"是请求本身的问题"。


def test_local_sdk_programming_error_does_not_pause_forever(monkeypatch):
    # 测试：本地 SDK 编程错误（如 TypeError 传错关键字）同样不可恢复，
    # 且没有 HTTP 状态码——这类错误靠修代码解决，不该触发暂停等待。
    completions = _FailingCompletions(TypeError("unexpected keyword"))
    # 伪接口：抛 TypeError。
    monkeypatch.setattr(
        C, "_client",
        SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    # 替换 OpenAI 客户端。
    monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)
    # 抹掉退避睡眠。

    with pytest.raises(LLMTransportError) as raised:
        C.chat("model", "system", "user")
    # chat 把非 HTTP 异常也归一为传输错误。

    assert raised.value.recoverable is False
    # recoverable=False：编程错误不会自愈。
    assert raised.value.status_code is None
    # status_code 为 None：不是 HTTP 层错误。


class _VM:
    # 伪 VM：run_script 一律成功，供 run_attempt 的主循环消费。
    def __init__(self):
        self.codes = []
        # 记录收到的每段脚本代码。

    def run_command(self, _command, **_kwargs):
        return ""
        # 探测类命令一律空输出。

    def run_script(self, _lang, code, **_kwargs):
        self.codes.append(code)
        return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        # 记录并返回成功输出（结尾 [exit 0] 标记退出码）。

    def fetch_file(self, _path, max_bytes=0):
        return None, "missing"
        # 文件抓取一律缺失。


def _practice_cfg():
    # 构造练习模式配置：关独立验证/用户决定停止，预算收紧到可控范围。
    cfg = load(None)
    # 默认配置打底。
    cfg.model = "actor"
    # 角色模型名（伪 chat 不看它）。
    cfg.practice_mode = True
    # 练习模式：走练习专用收尾规则。
    cfg.independent_verify = False
    # 关闭独立验证（本配置默认不带 agentic verifier）。
    cfg.agent_decided_stop = True
    # 允许 Agent 以 {"done":null} 自主停止。
    cfg.history_keep_pairs = 0
    # 不截断历史（伪 chat 全量可见，便于断言）。
    cfg.max_iters = 20
    # 迭代预算 20。
    cfg.wall_clock_secs = 100
    # 墙钟预算 100 秒。
    cfg.max_consec_dry = 3
    # 连续无产出上限 3。
    cfg.max_consec_degen = 3
    # 连续退化上限 3。
    cfg.llm_infra_retry_secs = 7
    # 可恢复传输错误后的暂停时长 7 秒（断言用）。
    return cfg


class _Clock:
    # 伪时钟：time/monotonic 同源可编程推进，sleep 真正前移时间——
    # 让"暂停不计入工作时钟"的记账可以被精确断言。
    def __init__(self):
        self.now = 1000.0
        # 起始时刻（任意正数）。

    def time(self):
        return self.now
        # 挂钟时间。

    def monotonic(self):
        return self.now
        # 单调时钟与挂钟同源（简化）。

    def sleep(self, seconds):
        self.now += seconds
        # 睡眠直接推进时钟（不真等）。


def test_recoverable_transport_pauses_same_context_and_work_clock(
        monkeypatch, tmp_path):
    # 测试：可恢复传输错误触发一次暂停（infra_pauses=1、infra_pause_secs=7），
    # 暂停后同一上下文续跑成功；暂停不进对话历史，也不计入工作时钟。
    cfg = _practice_cfg()
    # 练习配置。
    clock = _Clock()
    # 伪时钟。
    replies = iter((
        LLMTransportError("actor", _StatusError(402, "credits"),
                          recoverable=True, status_code=402, detail="credits"),
        '{"program":{"lang":"bash","code":"echo work"}}',
        '{"done":null}',
    ))
    # 脚本化回复序列：第 1 次抛可恢复错误，第 2 次给出程序，第 3 次 done。

    def fake_chat(*_args, **_kwargs):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply
        # 伪 chat：按序列抛错或返回回复。

    monkeypatch.setattr(L, "chat", fake_chat)
    # 替换主循环里的 chat。
    monkeypatch.setattr(L.time, "time", clock.time)
    monkeypatch.setattr(L.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(L.time, "sleep", clock.sleep)
    # 主循环的时间三件套全部接到伪时钟上。

    result, history = L.run_attempt(
        "practice", _VM(), cfg, ArtifactSink(str(tmp_path / "pause")))
    # 跑一次完整练习尝试。
    assert result.status == "done"
    # Agent 自主 done：暂停没有破坏回合的语义完整性。
    assert result.turns == 2 and result.programs_run == 1
    # 2 个对话回合（程序 + done）、执行 1 次程序：错误回合不计入对话。
    assert result.infra_pauses == 1 and result.infra_pause_secs == 7
    # 恰好 1 次基础设施暂停、累计 7 秒（cfg.llm_infra_retry_secs）。
    assert result.wall_secs == 0
    # 工作时钟为 0：供应商停机时间被完全剔除（0.1 秒的脚本耗时被伪 VM 记账豁免）。
    assert all("empty reply" not in str(message["content"]) for message in history)
    # 历史里没有任何"空回复"占位：供应商故障绝不合成空 Agent 回合。


def test_cumulative_nudges_never_terminate_productive_agent(monkeypatch, tmp_path):
    # 测试：累积 nudge（催促行动）计数只针对"连续无产出"——只要 Agent 交替
    # 产出旁白与程序，即使 nudge 计数达到旧版上限也不会被终止。
    cfg = _practice_cfg()
    cfg.max_nudges = 1  # 旧版数值必须保持惰性（不得再起作用）
    # 故意设旧版 max_nudges=1：它必须是惰性的（被连续计数取代）。
    replies = iter((
        "I will act next.",
        '{"program":{"lang":"bash","code":"echo first"}}',
        "I will take another action next.",
        '{"program":{"lang":"bash","code":"echo second"}}',
        '{"done":null}',
    ))
    # 旁白→程序→旁白→程序→done 的交替序列：两次程序之间夹着 nudge 旁白。
    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))
    # 伪 chat 按序列吐回复。

    vm = _VM()
    result, history = L.run_attempt(
        "practice", vm, cfg, ArtifactSink(str(tmp_path / "nudges")))
    # 跑完整尝试。
    assert result.status == "done"
    # 有产出的 Agent 走到自主 done，未被 nudge 误杀。
    assert result.turns == 5 and result.programs_run == 2
    # 5 个回合（旁白/程序×2/done）、2 次程序执行。
    assert vm.codes == ["echo first", "echo second"]
    # 伪 VM 恰好收到两段脚本：程序确实被执行。
    assert sum(m["role"] == "assistant" and "I will" in str(m["content"])
               for m in history) == 2
    # 历史里保留了两条 nudge 旁白：旁白照常入史，只是不终止回合。


def test_json_format_retry_keeps_context_and_only_executes_parsed_action(monkeypatch, tmp_path):
    # 测试（RSIAGENT_JSON_ACTION_RETRY=1）：模型输出无法解析为行动时，
    # 下一请求开启 json_object 格式重试并保留完整上下文；且每个回合
    # 只执行被成功解析的那一次行动，不会重复执行。
    monkeypatch.setenv("RSIAGENT_JSON_ACTION_RETRY", "1")
    # 打开 JSON 行动重试开关。
    requests = []
    # 收集每次请求的 kwargs（含历史快照）。
    replies = iter(("Tool choice is none, so I cannot act.",
                    '{"program":{"lang":"bash","code":"echo actual work"}}',
                    '{"done":null}'))
    # 旁白（解析失败）→ 程序 → done。
    def fake_chat(*args, **kwargs):
        requests.append({**kwargs, "history": list(kwargs.get("history", []))})
        return next(replies)
        # 记录请求再按序列返回。
    monkeypatch.setattr(L, "chat", fake_chat)
    # 替换主循环 chat。
    vm = _VM()
    result, history = L.run_attempt("practice", vm, _practice_cfg(), ArtifactSink(str(tmp_path)))
    # 跑完整尝试。
    assert result.status == "done" and vm.codes == ["echo actual work"]
    # 收尾 done 且只执行了一次程序：重试没有造成重复执行。
    assert "json_object" not in requests[0]
    # 第一次请求不带 json_object：正常回合不强制 JSON 模式。
    assert requests[1]["json_object"] is True
    # 重试请求开启 json_object：强制结构化输出以便解析。
    assert requests[1]["history"][-1]["content"] == "Tool choice is none, so I cannot act."
    # 重试历史末尾是上一条失败回复：上下文完整保留，模型看得见自己说过什么。
    assert "json_object" not in requests[2]
    # 之后的正常请求又关闭 json_object：重试开关只作用于紧邻的那一请求。


def test_configured_user_channel_failure_aborts_infra_invalid(monkeypatch, tmp_path):
    # 测试：Agent 发起 ask（向用户提问）而用户通道配置不可用时，
    # 尝试必须以 UserChannelInfrastructureError（infra-invalid）中止——
    # 不能把"没人回答"当成"Agent 自己决定继续"；提问与错误留痕落盘。
    cfg = _practice_cfg()
    # 练习配置。
    monkeypatch.setattr(
        L, "chat", lambda *_args, **_kwargs:
        '{"ask":{"question":"Please provide the missing evidence."}}')
    # 伪 chat：Agent 第一回合就请求用户输入。
    sink = ArtifactSink(str(tmp_path / "user-channel-failure"))
    # 工件汇：审计记录的落盘位置。

    def unavailable(_question):
        raise RuntimeError("provider unavailable")
        # 用户通道后端不可用。

    with pytest.raises(L.UserChannelInfrastructureError, match="infra-invalid"):
        L.run_attempt("task", _VM(), cfg, sink, ask_user=unavailable)
    # run_attempt 必须以"基础设施无效"错误中止。

    record = json.loads(
        (tmp_path / "user-channel-failure" / "iter_01" / "ask.json")
        .read_text(encoding="utf-8"))
    # 读取该迭代的 ask 审计记录。
    assert record["ok"] is False
    # 记录标记为失败。
    assert record["question"] == "Please provide the missing evidence."
    # Agent 的问题原文被留痕。
    assert "USER CHANNEL ERROR" in record["answer"]
    # 回答字段记录通道错误文本：事后可审计"为什么这一轮没人回答"。


def test_nested_verifier_pause_also_pauses_outer_actor_clock(
        monkeypatch, tmp_path):
    # 测试：独立 Verifier（嵌套运行时）内的供应商暂停要向上汇总——
    # 外层 Actor 结果的 infra_pauses/infra_pause_secs 必须包含嵌套会话的暂停，
    # 且同样从外层工作时钟剔除。
    cfg = _practice_cfg()
    cfg.practice_mode = False
    cfg.independent_verify = True
    cfg.agentic_verifier_config = "verifier.yaml"
    cfg.done_witness_gates = False
    cfg.split_done_gate = False
    # 任务模式 + 独立 agentic Verifier（关闭 witness/拆分门以聚焦暂停记账）。
    clock = _Clock()
    # 伪时钟。
    replies = iter((
        '{"program":{"lang":"bash","code":"echo build"}}',
        ('{"done":{"checks":[{"desc":"ACTOR_PRIVATE_SELF_CHECK_SENTINEL",'
         '"probe":"test -e /tmp && echo PASS || echo FAIL"}]}}'),
    ))
    # Actor 先执行构建程序，再提交 done（带一个私有自检探针）。

    class PassVM(_VM):
        def run_command(self, _command, **_kwargs):
            return "PASS"
        # 扩展伪 VM：所有探测命令返回 PASS（探针恒过）。

    def fake_verify(_instruction, _vm, _cfg, sink=None, turn_no=0,
                    context="", session=None, wall_budget=None):
        # 伪独立验证：校验入参后模拟一次嵌套运行时内部的供应商停机。
        assert isinstance(session, L.VerifierSession)
        # 必须传入真正的 VerifierSession（暂停记账的载体）。
        assert wall_budget == 100
        # 嵌套验证共享外层墙钟预算（100 秒）。
        assert "ACTOR_PRIVATE_SELF_CHECK_SENTINEL" not in context
        # 验证上下文不得包含 Actor 的私有自检描述（角色边界）。
        # 模拟一次由嵌套 Verifier Agent 运行时内部处理的供应商停机。
        clock.sleep(9)
        # 模拟嵌套运行时暂停 9 秒（真实时钟被推进）。
        session.infra_pause_secs += 9
        session.infra_pauses += 2
        # 在 Verifier 会话上登记 9 秒/2 批暂停（模拟两批可恢复错误）。
        return "pass", "independently confirmed"
        # 验证结论：pass + 独立确认语。

    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))
    # 伪 chat。
    monkeypatch.setattr(L, "verify_independent", fake_verify)
    # 替换独立验证入口。
    monkeypatch.setattr(L.time, "time", clock.time)
    monkeypatch.setattr(L.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(L.time, "sleep", clock.sleep)
    # 时间三件套接到伪时钟。

    result, _ = L.run_attempt(
        "task", PassVM(), cfg, ArtifactSink(str(tmp_path / "nested-pause")))
    # 跑一次完整任务尝试。

    assert result.status == "done"
    # 正常收尾。
    assert result.infra_pause_secs == 9
    # 外层结果吸收嵌套会话的 9 秒暂停。
    assert result.infra_pauses == 2
    # 外层结果吸收嵌套会话的 2 批暂停。
    assert result.wall_secs == 0
    # 嵌套暂停同样不计入外层工作时钟：供应商停机全程不在账。
