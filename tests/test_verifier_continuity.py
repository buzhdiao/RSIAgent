#!/usr/bin/env python3
# 【中文注释·模块定位】E4-A1 验证器连续性（verifier continuity）回归：uv 接受
# 谓词 + 消息变体（core/loop.uv_accept_ok + core/verifier 的
# verify_independent/VerifierSession/unverified_message/_mk_findings/
# items_punchlist/carry_findings/build_verifier_system/_opening）。被钉死的承诺：
# 1) uv_accept_ok 三档策略：遗留"第 N 次未验证即接受"（E3 逃生门）、
#    budget_conditional_accept（只在收官且记录干净时接受）、
#    verifier_continuity（一次 WRONG 永久撤销疑点利益）；全 Agent 验证器臂
#    缺报告绝不能被洗白成接受；
# 2) UNVERIFIED 消息两种变体：默认版逐字节稳定（含"写到交付物旁"旧句、无
#    INDEPENDENT CHANNEL），证据请求版换成独立通道语言并移除病灶句；
# 3) 真实 run_attempt 的 UNVERIFIED 续跑：同一验证器第二次检查携带
#    actor_evidence_leads（Actor 证据先行的检查清单，剥除私有叙述），
#    最终真实 PASS 走 HANDOFF 路由；
# 4) 会话连续性跨调用累积历史（CONTINUING INSPECTION 桥接），但候选内容
#    证据每轮刷新（CURRENT candidate）；doubts 在会话内由同一验证器解决；
# 5) 弹性深度（elastic depth）按 VERDICT 终结而非 6 轮计数；关闭时旧计数
#    依旧封顶；开场白按弹性与否声明预算；
# 6) items[] 结构化：畸形项丢弃、状态归一、punch list 只列未决项且 VIOLATED
#    在前、已满足项只计总数；消息携带 punch list；items schema 只在开关
#    开启时写入系统提示；
# 7) carry_findings 是改写 findings 的唯一合法方式（字符串拼接会丢
#    .items/.doubts 侧信道——t034 回归）；内容守卫与 doubts 降级都保留 items；
# 8) 会话关闭时验证器保持旧版逐字节行为（session 列表零触碰）。
# 文件可被 pytest 运行，也可独立执行（__main__ 块顺序跑全部 test_ 函数）。
"""E4-A1 tests: verifier continuity + the uv-accept predicate + message variants.

Run: python3 -m pytest tests/test_verifier_continuity.py -q
(also executable standalone: python3 tests/test_verifier_continuity.py)
"""
# 模块 docstring：E4-A1 测试——验证器连续性 + uv 接受谓词 + 消息变体；
# 可用 pytest 运行，也可独立执行。

import sys
# 导入 sys：把仓库根插入模块搜索路径。
import types
# 导入 types：（保留导入，供独立运行环境使用）。

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 把仓库根（tests/ 的上一级）加入 sys.path，保证可直接导入 core 包。

from core.loop import uv_accept_ok                    # noqa: E402
# 导入被测的 uv 接受谓词（E4-A1 抽出的纯函数）。
from core import verifier as V                        # noqa: E402
# 导入独立验证模块：会话/消息/findings/punchlist 等被测面。


class Cfg:
    """最小验证器配置：只定义被 mock 路径触碰的字段（其余用真实 Config 默认）。"""
    unverified_accept = 2
    # 第 2 次 could-not-confirm 即接受（遗留阈值）。
    budget_conditional_accept = False
    # 预算条件接受开关（默认关）。
    verifier_continuity = False
    # 验证器连续性开关（默认关）。
    # verify_independent surface (only what the mocked path touches)
    verifier_model = "m"
    model = "m"
    verifier_probes = 3
    verifier_max_tokens = 100
    temperature = 0.0
    reasoning_effort = "low"
    verifier_destination_note = False
    verifier_appearance_attach = False
    verifier_content_guard = True
    vision_verify = False
    reasoning_in_history = False
    verifier_probe_timeout = 30
    verifier_elastic_depth = False
    # 上述字段构成 verify_independent 所需的最小表面（只列 mock 路径触碰的）。


def test_uv_predicate_legacy_accepts_regardless_of_wrongs():
    """遗留策略：两个开关都关时，第 N 次未验证就接受，无论记录脏不脏。"""
    c = Cfg()
    assert uv_accept_ok(c, unverified=2, commit=False, wrongs=1)   # the E3 exit-door
    # 关键：wrongs=1 也接受——E3 逃生门（多个负分案例的根源，被本测试钉住）。


def test_uv_predicate_budget_conditional_blocks_precommit_and_wrongs():
    """budget_conditional_accept：只在收官阶段且记录干净时接受。"""
    c = Cfg(); c.budget_conditional_accept = True
    assert not uv_accept_ok(c, 2, commit=False, wrongs=0)   # pre-commit
    # 未到收官阶段：即使记录干净也拒绝。
    assert not uv_accept_ok(c, 2, commit=True, wrongs=1)    # dirty record
    # 收官但记录有 WRONG：拒绝。
    assert uv_accept_ok(c, 2, commit=True, wrongs=0)        # clean + commit
    # 收官且干净：接受。


def test_uv_predicate_continuity_revokes_trust_after_wrong():
    """verifier_continuity：一次 WRONG 撤销本运行剩余的疑点利益。"""
    c = Cfg(); c.verifier_continuity = True
    assert not uv_accept_ok(c, 2, commit=True, wrongs=1)    # WRONG on record -> never
    # 有 WRONG 在案：永不接受（用证据而非耗尽的耐心结案）。
    assert uv_accept_ok(c, 2, commit=True, wrongs=0)        # clean record unchanged
    # 干净记录：行为不变。
    assert uv_accept_ok(c, 5, commit=False, wrongs=0)       # (bca off) legacy path
    # bca 关闭时非收官阶段也按遗留阈值接受（5 >= 2）。


def test_agentic_verifier_never_launders_missing_report_into_acceptance():
    """全 Agent 验证器臂：缺失/无效报告是基础设施缺席，绝不能被洗白成接受。"""
    c = Cfg(); c.agentic_verifier_config = "config/roles/practice_verifier.yaml"
    assert not uv_accept_ok(c, 2, commit=True, wrongs=0)
    # 配置了全 Agent 验证器：2 次未验证不接受。
    assert not uv_accept_ok(c, 200, commit=True, wrongs=0)
    # 无论累积多少次（200 次）都不接受——二值终局契约。


def test_unverified_message_default_byte_stable():
    """默认 UNVERIFIED 消息逐字节稳定（旧句保留、无独立通道语言）。"""
    m = V.unverified_message("X")
    assert "write it to a file next to the deliverable" in m      # legacy verbatim
    # 旧版句子逐字保留（下游可能有对账依赖）。
    assert "INDEPENDENT CHANNEL" not in m
    # 默认版没有独立通道措辞（那是证据请求版专属）。


def test_unverified_message_evidence_request():
    """证据请求版消息：独立通道语言 + 病灶句被移除。"""
    m = V.unverified_message("X", evidence_request=True)
    assert "INDEPENDENT CHANNEL" in m
    # 要求走独立验证通道。
    assert "a file you author about your own work proves nothing" in m
    # 明说"自著文件证明不了什么"（防自我背书）。
    assert "write it to a file next to the deliverable" not in m  # disease removed
    # 病灶句（诱导写到交付物旁）被移除。


def test_agentic_unverified_returns_reproducible_checks_to_same_verifier(
        monkeypatch, tmp_path):
    """真实 run_attempt 的 UNVERIFIED 续跑：同一验证器拿到可复现检查清单。"""
    from config.settings import Config
    from core import loop as actor_loop
    from core.trace import ArtifactSink
    from env.vm import Trace
    # 导入真实循环与 Trace/Sink（全真实路径，只 mock LLM 两个入口）。

    cfg = Config()
    cfg.max_iters = 8
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.verifier_continuity = True
    cfg.verifier_unverified_evidence = True
    cfg.verifier_evolve_route = True
    cfg.verifier_local_verdict_only = True
    cfg.strict_one_action = False
    # 配置：独立验证 + 连续性 + 未验证证据 + 进化路由 + 本地判定，8 迭代。

    replies = iter((
        '{"program":{"lang":"bash","code":"make candidate"}}',
        ('{"done":{"checks":[{"desc":"candidate is live",'
         '"probe":"test -e /tmp/candidate && echo PASS"}]}}'),
        ('{"program":{"lang":"bash","code":"independently inspect candidate; '
         'private actor narration marker"}}'),
        ('{"done":{"checks":[{"desc":"candidate is independently live",'
         '"probe":"test -e /tmp/candidate && echo PASS"}]}}'),
    ))
    # 四轮脚本化回复：做候选 → 首次 done 声明 → 独立检查（夹带私有叙述）→
    # 证据版 done 声明。
    actor_users = []
    # 收集每轮发给 Actor 的用户消息。

    def fake_chat(*args, **kwargs):
        actor_users.append(args[2])
        return next(replies)
        # 伪 Actor LLM：记录用户消息并按脚本回复。

    inspections = []
    # 收集验证器检查调用。

    def fake_verify(*args, **kwargs):
        inspections.append(kwargs)
        if len(inspections) == 1:
            return "unverified", "service liveness was not reproducible"
        return "pass", "independently reproduced\nVERDICT: PASS\n"
        # 伪独立验证：第一次判 unverified（不可复现），第二次真实 PASS。

    monkeypatch.setattr(actor_loop, "chat", fake_chat)
    monkeypatch.setattr(actor_loop, "verify_independent", fake_verify)
    # 替换循环的两个 LLM 入口。

    class _EvidenceVM:
        """伪 VM：脚本总是成功，fetch_file 恒缺失（证据走 checks 而非文件）。"""
        def run_script(self, lang, code, timeout=600, **kwargs):
            return Trace(stdout="worked\n[exit 0]", exit_code=0, secs=0.1)
            # 脚本执行成功。

        def run_command(self, command, timeout=30, **kwargs):
            if command == actor_loop._FIND:
                return ""
            return "PASS"
            # find 命令返回空（无文件），checks 探针返回 PASS。

        def fetch_file(self, path, max_bytes=0):
            return None, "missing"
            # 文件抓取恒缺失。

    result, _history = actor_loop.run_attempt(
        "authoritative task", _EvidenceVM(), cfg,
        ArtifactSink(str(tmp_path / "run")))
    # 跑真实的尝试循环。

    assert result.status == "done"
    # 最终以 done 结束。
    assert result.verifier_route == "HANDOFF"
    # 判定真实 PASS 后走 HANDOFF 路由（不是 EVOLVE）。
    assert len(inspections) == 2
    # 独立验证恰好被调用两次（第一次 UNVERIFIED 后重检）。
    assert "actor_evidence_leads" not in inspections[0]
    # 第一次检查没有证据先导（Actor 还没提供 checks）。
    leads = inspections[1]["actor_evidence_leads"]
    assert "candidate is independently live" in leads
    assert "test -e /tmp/candidate && echo PASS" in leads
    assert "private actor narration marker" not in leads
    # 关键：第二次检查的证据先导携带 Actor 声明的检查描述与探针命令，
    # 但 Actor 的私有叙述被剥除（只传可复现证据）。
    assert any("could not CONFIRM" in user for user in actor_users)
    # UNVERIFIED 反馈以"could not CONFIRM"话术回到 Actor。


def _mock_chat_factory(replies):
    """构造按脚本依次回复的伪 V.chat 工厂（测试后自行恢复原函数）。"""
    it = iter(replies)
    def _chat(model, system, user, **kw):
        return next(it)
    return _chat


def test_session_continuity_accumulates(monkeypatch=None):
    """Two inspections sharing one session: the second sees the first's history."""
    # 两次检查共用一个会话：第二次能看到第一次的历史（连续性累积）。
    replies = ['{"verdict": "wrong", "findings": "bad", "doubts": ""}',
               '{"verdict": "unverified", "findings": "cannot confirm", "doubts": ""}']
    # 两轮脚本化判定：先 wrong 后 unverified。
    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    # 暂时替换模块级 chat。
    try:
        c = Cfg(); c.verifier_continuity = True
        session = V.VerifierSession()
        v1, _ = V.verify_independent("task", vm=None, cfg=c, session=session)
        n1 = len(session)
        v2, _ = V.verify_independent("task", vm=None, cfg=c, session=session)
        # 同一会话连续两次检查。
        assert v1 == "wrong" and v2 == "unverified"
        # 两次判定如实返回。
        assert n1 == 2 and len(session) == 4          # history grew across calls
        # 关键：历史跨调用增长（第一次后 2 条，第二次后 4 条）。
        # the second call's opening carried the continuity bridge
        assert "CONTINUING INSPECTION" in session[2]["content"]
        # 第二次的开场白携带连续性桥接标记。
    finally:
        V.chat = orig
        # 恢复真实 chat（模块级状态不外泄）。


def test_session_keeps_context_but_refreshes_candidate_content_evidence():
    """Prior evidence guides the same Agent but cannot bless a revised candidate."""
    # 会话保留上下文，但候选内容证据每轮刷新（旧证据不能为修订候选背书）。
    replies = [
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "content confirmed", "doubts": ""}',
        '{"verdict": "pass", "findings": "prior evidence still settles it", '
        '"doubts": ""}',
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "current content confirmed", '
        '"doubts": ""}',
    ]
    # 五轮脚本：第一轮探针+PASS；第二轮想靠旧证据 PASS（被拒，要求重新取证）；
    # 第三轮重新探针+以当前内容 PASS。

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"
            # 探针命令返回固定输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_continuity = True
        session = V.VerifierSession()
        v1, _ = V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert v1 == "pass" and session.content_probes == 1
        # 第一轮 PASS，内容探针计数 1。
        v2, _ = V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert v2 == "pass"
        assert session.content_probes == 2
        assert session.inspections == 2
        # 第二轮 PASS；内容探针与检查计数都累积到 2。
        assert any("CURRENT candidate" in message["content"]
                   for message in session if message["role"] == "user")
        # 关键：会话里有"CURRENT candidate"提示——旧证据不能为新候选背书。
    finally:
        V.chat = orig


def test_verifier_system_requires_pre_pass_falsification():
    """验证器系统提示必须要求先证伪后 pass，且终局只有一次。"""
    system = V.build_verifier_system(Cfg())
    assert "Before emitting `pass`, try to FALSIFY" in system
    # 发 pass 前必须先尝试证伪。
    assert "one terminal `pass`" in system
    # 终局 pass 只有一次。


def test_pass_doubts_are_resolved_by_same_verifier_agent():
    """Incidental caveats are clarified in-session, never bounced to the Actor."""
    # 附带的疑点在会话内由同一验证器解决，绝不弹回给 Actor。
    replies = [
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "task met", '
        '"doubts": "an unrelated process exists"}',
        '{"verdict": "pass", "findings": "task met; unrelated process noted", '
        '"doubts": ""}',
    ]
    # 三轮脚本：探针 → 带 doubts 的 PASS（被 doubts 门拦截要求澄清）→
    # 澄清后的干净 PASS。

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"
            # 探针固定输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_continuity = True; c.doubts_block_pass = True
        # 开启 doubts 拦截。
        session = V.VerifierSession()
        verdict, findings = V.verify_independent(
            "task", vm=VM(), cfg=c, session=session)
        assert verdict == "pass" and findings.doubts == ""
        # 最终 PASS 且 doubts 清空（在会话内解决）。
        assert any("Resolve that contradiction" in message["content"]
                   for message in session if message["role"] == "user")
        # 会话里出现了"解决那个矛盾"的追问（同一验证器自清）。
    finally:
        V.chat = orig


def test_elastic_depth_runs_past_the_legacy_cap():
    """E4-A1b: with elastic depth the inspection ends on the VERDICT, not on the
    6-round counter — 9 probe rounds then a verdict must all be honored."""
    # 弹性深度：检查按 VERDICT 终结而非 6 轮计数——9 轮探针 + 1 轮判定全部兑现。
    replies = (['{"probes": ["ls /tmp"]}'] * 9
               + ['{"verdict": "pass", "findings": "ok", "doubts": ""}'])
    # 十轮脚本：9 轮探针 + 第 10 轮 PASS。

    class VM:
        def run_command(self, p, timeout=None):
            return "real output line"
            # 探针返回真实形状的输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_elastic_depth = True; c.verifier_content_guard = False
        # 开弹性深度、关内容守卫（本测试聚焦轮数）。
        v, _ = V.verify_independent("task", vm=VM(), cfg=c)
        assert v == "pass"            # reached its own verdict on round 10
        # 关键：第 10 轮的自发判定被接受（没有被 6 轮计数砍断）。
    finally:
        V.chat = orig


def test_capped_depth_still_stops_at_budget():
    """Flag OFF: the legacy counter still terminates the inspection (byte-exact)."""
    # 开关关闭时旧计数依旧封顶（逐字节旧行为）。
    replies = ['{"probes": ["ls /tmp"]}'] * 20 + ['{"verdict": "pass", "findings": "x"}']
    # 20 轮探针 + 1 轮 PASS（远超旧上限）。

    class VM:
        def run_command(self, p, timeout=None):
            return "real output line"
            # 探针输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg()                      # elastic OFF, verifier_probes = 3
        c.verifier_probes = 3
        # 弹性关闭、探针上限 3。
        v, _ = V.verify_independent("task", vm=VM(), cfg=c)
        assert v in ("pass", "unverified")   # forced demand path, not 20 rounds
        # 到达上限后走强制索判路径（绝不会跑满 20 轮）。
    finally:
        V.chat = orig


def test_opening_advertises_no_budget_when_elastic():
    """开场白按弹性与否声明预算：有预算报轮数，弹性则声明无轮数预算。"""
    o_cap = V._opening("t", 6, elastic=False)
    o_ela = V._opening("t", 6, elastic=True)
    assert "You have 6 probe rounds" in o_cap
    # 非弹性开场白声明 6 轮预算。
    assert "NO round budget" in o_ela and "probe rounds." not in o_ela
    # 弹性开场白声明无轮数预算（且不再出现轮数句）。


def test_items_parsed_normalised_and_punchlisted():
    """E4-A1c: items[] become data; partial/looses statuses normalise; the
    punch list names ONLY the unsettled ones, violated first."""
    # items[] 结构化为数据；部分满足等状态归一；punch list 只列未决项且
    # 违反项在前。
    f = V._mk_findings("prose", "", [
        {"req": "clips exist", "status": "met", "evidence": "three on Desktop"},
        {"req": "watermark on ep2", "status": "PARTIALLY_MET", "evidence": "never seen"},
        {"req": "merged in order", "status": "violated", "evidence": "concat shows 1→3→2"},
        {"req": "junk entry"},                       # malformed -> dropped
        "not-a-dict",                                # malformed -> dropped
    ])
    # 五个条目：满足/部分满足/违反/缺状态畸形/非字典畸形。
    assert len(f.items) == 3                          # two malformed dropped
    # 畸形两被丢弃，剩 3 条。
    assert dict((r, s) for r, s, _ in f.items)["watermark on ep2"] == "could-not-confirm"
    # PARTIALLY_MET 归一为 could-not-confirm（未决）。
    pl = V.items_punchlist(f)
    assert pl.index("VIOLATED") < pl.index("COULD NOT CONFIRM")   # violated first
    # punch list 中 VIOLATED 段在 COULD NOT CONFIRM 段之前。
    assert "clips exist" not in pl                    # met items are not punch-listed
    # 已满足项不进 punch list。
    assert "1 other requirement(s) it judged met" in pl
    # 已满足项只以总数形式提及。


def test_punchlist_empty_without_items_and_messages_still_work():
    """无 items 时 punch list 为空串，而两条消息仍包含 prose 发现。"""
    f = V._mk_findings("prose only", "")
    # 只有 prose、无 items。
    assert V.items_punchlist(f) == ""
    # punch list 为空串。
    assert "prose only" in V.disagreement_message(f)
    assert "prose only" in V.unverified_message(f, evidence_request=True)
    # 分歧消息与证据请求消息都携带 prose 发现。


def test_messages_carry_the_punchlist_when_items_exist():
    """有 items 时两条消息都携带 punch list（未决项逐条点名）。"""
    f = V._mk_findings("prose", "", [
        {"req": "the deck is at the named path", "status": "violated",
         "evidence": "found only in /tmp"}])
    # 一个违反项。
    for msg in (V.disagreement_message(f),
                V.unverified_message(f, evidence_request=True)):
        assert "THE UNSETTLED ITEMS" in msg
        # 消息带"未决项"清单头。
        assert "the deck is at the named path" in msg
        # 违反项的要求原文进消息。


def test_items_schema_only_advertised_when_flag_on():
    """items schema 只在 verifier_items 开启时写入系统提示。"""
    class C:
        vision_model = "k3"; vision_verify = True; verifier_vision_model = ""
        verifier_destination_note = False; verifier_items = True
    class Coff(C):
        verifier_items = False
    assert '"items"' in V.build_verifier_system(C())
    # 开启：schema 出现在系统提示里。
    assert '"items"' not in V.build_verifier_system(Coff())
    # 关闭：schema 完全不出现（不浪费提示词预算）。


def test_carry_findings_preserves_side_channels():
    """E4-A1c REGRESSION (t034, 14 punch lists lost): rewriting findings text
    must not strip .items/.doubts. String concatenation returns a plain str;
    carry_findings() is the only legal way to rewrite."""
    # t034 回归：改写 findings 文本绝不能剥掉 .items/.doubts 侧信道；
    # 字符串拼接返回普通 str（丢失），carry_findings() 是唯一合法改写方式。
    src = V._mk_findings("orig", "a doubt", [
        {"req": "the named file exists at the named path", "status": "violated",
         "evidence": "found only in /tmp"}])
    # 原始 findings：prose + doubt + 一个违反项。
    plain = "[downgraded] " + src                     # the OLD (buggy) shape
    assert getattr(plain, "items", ()) == ()          # proves the loss
    # 关键证据：拼接后的普通 str 已丢失 items（旧 bug 形态）。
    fixed = V.carry_findings("[downgraded] " + str(src), src)
    assert fixed.items == src.items and fixed.doubts == src.doubts
    # carry_findings 重建：items 与 doubts 完整保留。
    assert "THE UNSETTLED ITEMS" in V.unverified_message(fixed, evidence_request=True)
    # 重建后的消息仍携带 punch list（14 个丢失案例的教训）。


def test_content_guard_same_agent_reinspection_keeps_items():
    """A blind PASS stays with the Verifier until it reads current content."""
    # 盲目 PASS 被内容守卫扣留在验证器处，直到读到当前内容。
    item = '"items": [{"req": "r1", "status": "met", "evidence": "e"}]'
    replies = [
        '{"verdict": "pass", "findings": "old evidence", ' + item + '}',
        '{"probes": ["cat /etc/hosts"]}',
        '{"verdict": "pass", "findings": "current evidence", ' + item + '}',
    ]
    # 三轮脚本：带旧证据的 PASS（被守卫拦下要求取证）→ 探针 → 以当前证据
    # 的 PASS。

    class VM:
        def run_command(self, p, timeout=None):
            return "x"
            # 探针输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg(); c.verifier_content_guard = True
        v, f = V.verify_independent("task", vm=VM(), cfg=c)
        assert v == "pass"
        # 最终 PASS。
        assert len(getattr(f, "items", ())) == 1
        # 关键：重检后的 findings 保留 1 个证据条目（内容守卫不丢 items）。
    finally:
        V.chat = orig


def test_doubts_gate_downgrade_keeps_items():
    """Downgrading a pass carrying doubts must preserve its evidence items."""
    # doubts 降级（pass 带未决疑点被降级为 unverified）必须保留证据条目。
    src = V._mk_findings("findings text", "an unresolved doubt", [
        {"req": "budget cap has a source", "status": "could-not-confirm",
         "evidence": "field blank in the source document"}])
    # 原 findings：prose + 未决疑点 + 一个未决项。
    rebuilt = V.carry_findings(
        "[inspection passed but listed unresolved doubts — downgraded] "
        "Its doubts: " + src.doubts + "\nIts findings: " + str(src), src)
    # 按降级话术重建文本。
    assert rebuilt.items == src.items
    # 关键：items 原样保留。
    msg = V.unverified_message(rebuilt, evidence_request=True)
    assert "budget cap has a source" in msg and "COULD NOT CONFIRM" in msg
    # 证据请求消息仍点名未决项与其状态段。


def test_session_off_stays_fresh():
    """会话关闭（传普通列表）时保持旧版逐字节行为：列表零触碰。"""
    replies = ['{"probes": ["cat /etc/hosts"]}',
               '{"verdict": "pass", "findings": "ok", "doubts": ""}']
    # 两轮脚本：探针 + PASS。

    class VM:
        def run_command(self, p, timeout=None):
            return "127.0.0.1 localhost"
            # 探针输出。

    orig = V.chat
    V.chat = _mock_chat_factory(replies)
    try:
        c = Cfg()                                     # continuity OFF
        session = []
        V.verify_independent("task", vm=VM(), cfg=c, session=session)
        assert session == []                          # untouched -> byte-exact legacy
        # 关键：普通列表会话完全不被写入（旧版行为不变）。
    finally:
        V.chat = orig


if __name__ == "__main__":
    # 独立运行入口：按名字顺序执行全部 test_ 函数并逐个打印 PASS。
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("ALL E4-A1 TESTS PASS")
