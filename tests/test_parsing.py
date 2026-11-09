# 【中文注释·模块定位】回合解析离线回归（core/actor.parse_turn + core/checks.validate
# + core/loop 的上下文折叠/表面增量/传输失败归一化 + core/imagery 视觉管线）。
# 覆盖 M3 风格输出的全部形态：prose 前导、markdown 围栏、裸对象、先草稿后修正、
# 供应商原生文本工具调用——全部必须能在不起真实模型的情况下解析。
# 测试策略按版本演进组织：
# v16 检查门（引号内比较不算改写、/tmp 草稿可写、内容检查不被结构代理挤掉、
# 容量溢出只丢弃不否决）；v9.2/v11/v14 上下文折叠（尺寸触发、地板守卫、字符
# 预算 keep 窗口）；v13 look 多图/区域/瓦片；v17 基础设施检测与恢复；v18 零增量
# 转向（strategy pivot）；v21 严格单动作/验证预算/上下文钉住/模型升级；v22-v30
# 视觉链路（VLM 委派、多轮视觉 Agent、验证者视觉探针、屏幕捕获、自动渲染）。
# 所有涉及 LLM 的测试都通过替换 L.chat / L.verify_independent 脚本化回复。
"""Offline tests for turn parsing — M3-style output shapes (prose preambles, markdown
fences, bare objects, draft-then-correct) must parse without a live model."""
# 模块 docstring：回合解析离线测试——M3 风格输出形态必须无需真实模型即可解析。

import os
# 导入 os：路径与 sys.path 处理。
import sys
# 导入 sys：把仓库根插入模块搜索路径。

import pytest
# 导入 pytest：参数化与异常断言。

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 把仓库根（tests/ 的上一级）加入 sys.path，保证可直接导入 core 包。

from core.checks import validate                      # noqa: E402
# 导入检查门验证器：Done 检查的合法性与容量管理。
from core.actor import (Ask, Done, Look, Program, build_system,  # noqa: E402
                         extract_plan, parse_turn, trace_message)
# 导入解析层构件：四种回合类型（Ask/Done/Look/Program）、系统提示构建、
# 计划抽取、回合解析与轨迹消息渲染。


def test_program_with_prose():
    # 测试：prose 前导 + JSON 程序对象的混合回合能解析出 Program。
    t = parse_turn('I will scan the disk first.\n'
                   '{"program": {"lang": "bash", "code": "ls -la ~"}}')
    # 前导一句自然语言，后跟标准 program 对象。
    assert isinstance(t, Program) and t.lang == "bash" and t.code == "ls -la ~"
    # 解析结果：bash 程序、代码原样。


def test_provider_native_text_tool_call_is_not_a_dry_turn():
    # 测试：供应商原生文本工具调用（<|open|>...<|close|> 标记）不能被当成
    # 无行动回合——必须解析出等价的 bash Program。
    raw = ('I will inspect now.<|open|>tools<|sep|>'
           '<|open|>call tool="bash" index="1"<|sep|>'
           '<|open|>argument key="code" type="string"<|sep|>'
           'printf "evidence\\n"\nls /home/user/Desktop'
           '<|close|>argument<|sep|><|close|>call<|sep|><|close|>tools')
    # 供应商私有格式的 bash 工具调用（含多行代码参数）。

    t = parse_turn(raw)

    assert isinstance(t, Program)
    # 解析为程序对象（不是 None/空回合）。
    assert t.lang == "bash"
    # 语言取自 tool 名。
    assert t.code == 'printf "evidence\\n"\nls /home/user/Desktop'
    # 代码参数原样保留（含转义与换行）。


def test_provider_native_look_call_is_not_a_dry_turn():
    # 测试：供应商原生 look 工具调用解析为 Look（路径 + 问题）。
    raw = ('<|open|>call tool="look" index="1"<|sep|>'
           '<|open|>argument key="path" type="string"<|sep|>'
           '/tmp/evidence.png<|close|>argument<|sep|>'
           '<|open|>argument key="question" type="string"<|sep|>'
           'Read the exact label.<|close|>argument<|sep|>'
           '<|close|>call')
    # look 工具调用的原生文本形态。

    t = parse_turn(raw)

    assert isinstance(t, Look)
    # 解析为 Look。
    assert t.path == "/tmp/evidence.png"
    assert t.question == "Read the exact label."
    # 路径与问题逐字保留。


def test_markdown_fenced():
    # 测试：markdown ```json 围栏包裹的对象能解析。
    t = parse_turn('```json\n{"program": {"lang": "python", "code": "print(1)"}}\n```')
    assert isinstance(t, Program) and t.code == "print(1)"
    # 围栏剥掉后取出程序。


def test_bare_program_object():
    # 测试：裸的 {"lang","code"} 对象（无 program 外壳）也能解析为 Program。
    t = parse_turn('{"lang": "python", "code": "print(2)"}')
    assert isinstance(t, Program) and t.code == "print(2)"


def test_draft_then_correct_takes_last():
    # 测试：先草稿后修正——同一回合里两个 program 取最后一个。
    t = parse_turn('{"program": {"lang": "bash", "code": "old"}}\n'
                   'Wait, better:\n{"program": {"lang": "bash", "code": "new"}}')
    assert isinstance(t, Program) and t.code == "new"
    # 修正版胜出。


def test_done_with_checks():
    # 测试：带 checks 的 done 对象解析为 Done。
    t = parse_turn('{"done": {"checks": [{"desc": "d", "probe": "ls / && echo PASS"}]}}')
    assert isinstance(t, Done) and len(t.checks) == 1
    # 一个检查项。


def test_bare_checks_object():
    # 测试：裸 checks 对象（无 done 外壳）同样解析为 Done。
    t = parse_turn('{"checks": [{"desc": "d", "probe": "ls ~/x && echo PASS"}]}')
    assert isinstance(t, Done) and len(t.checks) == 1


def test_program_wins_over_done():
    # 测试：优先级——同一回合里 program > done。
    t = parse_turn('{"program": {"lang": "bash", "code": "x"}, "done": {"checks": []}}')
    assert isinstance(t, Program)
    # 有程序就不收尾。


def test_program_then_hedged_done_two_objects():
    # seed2 杀手用例：好程序后面跟着同一回合内的"对冲" done 对象。
    t = parse_turn('{"program": {"lang": "bash", "code": "ls -la ~"}}\n'
                   '{"done": {"checks": []}}')
    assert isinstance(t, Program) and t.code == "ls -la ~"
    # program 胜出：done 对冲不劫持回合。


def test_program_then_done_null():
    # seed3 杀手用例：尾随 {"done": null}（M3 的口头禅）不改变 program 胜出。
    t = parse_turn('{"program": {"lang": "bash", "code": "ls"}}\n{"done": null}')
    assert isinstance(t, Program) and t.code == "ls"


def test_done_null_alone_is_explicit_done():
    # 测试：单独的 {"done": null} 是显式收尾（无检查）。
    t = parse_turn('{"done": null}')
    assert isinstance(t, Done) and t.checks == []


def test_dry_turns():
    # 测试：四种"无行动回合"解析为 None——空串、纯旁白、无关对象、空代码程序。
    assert parse_turn("") is None
    assert parse_turn("thinking out loud, no action") is None
    assert parse_turn('{"note": "irrelevant object"}') is None
    assert parse_turn('{"program": {"lang": "python", "code": ""}}') is None
    # 空代码的 program 不是行动。


def test_multiline_code_survives():
    # 测试：多行代码（含 \n 转义）在解析后原样存活。
    t = parse_turn('{"program": {"lang": "python", '
                   '"code": "import os\\nfor f in os.listdir(\'.\'):\\n    print(f)"}}')
    assert isinstance(t, Program) and "\n" in t.code and "listdir" in t.code
    # 换行与代码内容都在。


def test_nested_look_tolerance():
    # seed12 杀手用例：M3 把 look 包进 program 对象里——容忍并解出 Look。
    t = parse_turn('{"program": {"look": {"path": "/tmp/grid.png", '
                   '"question": "compare styles"}}}')
    assert isinstance(t, Look) and t.path == "/tmp/grid.png"


def test_look_parsing():
    # 测试：look 解析、空路径拒绝、以及 look/program/done 的优先级。
    t = parse_turn('{"look": {"path": "/tmp/x/img.png", "question": "what date?"}}')
    assert isinstance(t, Look) and t.path == "/tmp/x/img.png" and "date" in t.question
    # 标准 look。
    t = parse_turn('{"look": {"path": ""}}')               # empty path -> not a look
    # 空路径不算 look。
    assert t is None
    # precedence: program > look > done
    # 优先级链。
    t = parse_turn('{"look": {"path": "/a.png"}}\n'
                   '{"program": {"lang": "bash", "code": "ls"}}')
    assert isinstance(t, Program)
    # program 压过 look。
    t = parse_turn('{"look": {"path": "/a.png"}}\n{"done": {"checks": []}}')
    assert isinstance(t, Look)
    # look 压过 done。


def test_ask_parsing_and_precedence():
    # 测试：ask 解析与优先级——ask 压过 done、被 program 压过。
    t = parse_turn('{"ask": {"question": "Could you provide the missing file?"}}')
    assert isinstance(t, Ask)
    assert t.question == "Could you provide the missing file?"
    # 标准 ask。
    t = parse_turn('{"ask": {"question": "Need input"}}\n'
                   '{"done": {"checks": []}}')
    assert isinstance(t, Ask)
    # ask 压过 done。
    t = parse_turn('{"program": {"lang": "bash", "code": "ls"}}\n'
                   '{"ask": {"question": "Need input"}}')
    assert isinstance(t, Program)
    # program 压过 ask。


def test_ask_is_advertised_only_when_channel_is_attached():
    # 测试：系统提示只在用户通道接入时宣传 "ask" 工具——没有通道就不许 Agent 问。
    assert '"ask"' not in build_system()
    # 默认（无通道）：提示不含 ask。
    assert '"ask"' in build_system(ask_enabled=True)
    # 开启后：提示宣传 ask。


def test_extract_plan():
    # 测试：PLAN 行抽取——首行前缀、最新修订胜出、无计划/空串返回空、长度有界。
    assert extract_plan("PLAN: recon 4, extract 8, decide 2, place 3, verify 3\n"
                        '{"program": {"lang": "bash", "code": "ls"}}'
                        ).startswith("recon 4")
    # 标准 PLAN 行被抽出。
    two = extract_plan("PLAN: old plan\nsome prose\nPLAN: revised plan\n{}")
    assert two == "revised plan"                      # latest revision wins
    # 最新修订胜出。
    assert extract_plan("no plan line here") == ""
    # 无 PLAN 行返回空。
    assert extract_plan("") == ""
    # 空串返回空。
    assert len(extract_plan("PLAN: " + "x" * 999)) == 300   # bounded
    # 超长计划截断到 300 字符。


def test_trace_message_carries_plan_and_commit():
    # 测试：轨迹消息携带计划与剩余回合数；提交阶段有 COMMIT PHASE 标记。
    class _T:                                          # minimal Trace stand-in
        # 最小 Trace 替身。
        stdout, exit_code, secs, timed_out = "out", 0, 1.0, False
    m = trace_message(_T(), 12, plan="recon then place", commit=False)
    assert "YOUR PLAN: recon then place" in m and "12 action turn(s) remain." in m
    # 计划与剩余回合数都渲染进消息。
    assert "COMMIT PHASE" not in m
    # 非提交阶段无该标记。
    m2 = trace_message(_T(), 3, plan="p", commit=True)
    assert "COMMIT PHASE" in m2
    # 提交阶段出现标记。


def test_trace_message_is_lossless_by_default():
    # 测试：默认轨迹消息无损——12000+12000 字符的输出完整进入消息，
    # 没有"省略字符"的提示。
    marker = "MEMORY-MIDDLE-MUST-SURVIVE"
    payload = "a" * 12000 + marker + "z" * 12000
    # 带中段标记的大载荷。
    class _T:
        stdout, exit_code, secs, timed_out = payload, 0, 1.0, False

    rendered = trace_message(_T())
    assert payload in rendered
    # 载荷完整保留。
    assert marker in rendered
    # 中段标记在（无中段截断）。
    assert "chars omitted" not in rendered
    # 无截断提示。


def test_check_gate():
    # 测试：检查门——真实检查通过；常量回显/改写/非草稿区重定向/无探针/
    # 非字典逐项拒绝；无害的 2>/dev/null 不误伤；空检查列表必须带指导拒绝。
    accepted, rejections, dropped = validate([
        {"desc": "real", "probe": "ls ~/target && echo PASS || echo FAIL"},   # ok
        {"desc": "constant", "probe": "echo PASS"},                            # rejected
        {"desc": "mutates", "probe": "rm -rf /home/x && echo PASS"},           # rejected
        {"desc": "redir-home", "probe": "ls / > ~/out && echo PASS"},          # rejected (non-scratch)
        {"desc": "no env", "probe": "true"},                                   # rejected
        "not-a-dict",                                                          # rejected
    ])
    # 六种候选：1 个合法 + 5 个应拒（常量/破坏性/主目录重定向/无操作/类型错）。
    assert len(accepted) == 1 and accepted[0].desc == "real", [c.desc for c in accepted]
    # 只有"real"通过。
    assert len(rejections) == 5 and not dropped
    # 拒绝 5 个、无容量丢弃。
    a2, r2, d2 = validate([{"desc": "harmless redir",
                            "probe": "cat ~/f 2>/dev/null | head -1 && echo PASS || echo FAIL"}])
    assert len(a2) == 1 and not r2 and not d2, f"harmless 2>/dev/null wrongly rejected: {r2}"
    # 无害的 stderr 重定向不误伤。
    a3, r3, d3 = validate([])
    assert not a3 and r3, "empty checks must be rejected with guidance"
    # 空检查列表：必须拒绝（零证据的收尾不可信）。


def test_v16_quoted_comparison_not_mutating():
    # v16 修复：引号内的 python '>' 比较不得被当成 shell 重定向——
    # 这一误报曾在一次运行里拒掉 8 个合法读探针中的 7 个。
    a, r, d = validate([{"desc": "zipread",
        "probe": "python3 -c \"import zipfile; z=zipfile.ZipFile('/home/u/f.pptx'); "
                 "print('PASS' if len(z.read('ppt/slides/slide1.xml')) > 100 else 'FAIL')\""}])
    assert len(a) == 1 and not r, f"quoted '>' wrongly rejected: {r}"
    # 引号内的 '>' 不触发改写拒绝。
    # and a genuine unquoted shell redirect to a non-scratch path is still caught
    a2, r2, _ = validate([{"desc": "real redir", "probe": "cat ~/f > ~/out && echo PASS"}])
    assert not a2 and r2
    # 真正的（未加引号）非草稿区重定向仍然被抓住。


def test_v16_scratch_write_allowed():
    # v16：检查允许先渲染到 /tmp 草稿区再检查它（render-and-compare 模式）。
    a, r, d = validate([{"desc": "render+compare",
        "probe": "solvespace-cli --render /tmp/ck.png ~/part.slvs && "
                 "python3 -c \"print('PASS')\" && test -f /tmp/ck.png && echo PASS"}])
    assert len(a) == 1 and not r, f"/tmp scratch write wrongly rejected: {r}"
    # /tmp 草稿写入不触发拒绝。


def test_v16_eviction_keeps_content_over_proxies():
    # t056 bug：结构代理（存在性检查）挤满容量时，内容检查必须存活。
    checks = ([{"desc": f"exist{i}", "probe": f"test -f ~/f{i} && echo PASS || echo FAIL"}
               for i in range(14)]
              + [{"desc": "content-psnr",
                  "probe": "python3 ~/psnr.py /tmp/out.mp4 && echo PASS || echo FAIL"},
                 {"desc": "content-grep", "probe": "grep -q OSWorld ~/out.txt && echo PASS"}])
    # 14 个存在性检查 + 2 个内容检查（总量超容量 12）。
    accepted, rejections, dropped = validate(checks, max_checks=12)
    assert len(accepted) == 12 and not rejections
    # 收 12 个、零拒绝（溢出走丢弃）。
    kept = {c.desc for c in accepted}
    assert "content-psnr" in kept and "content-grep" in kept, f"content evicted! {kept}"
    # 两个内容检查都存活：内容优先于代理。
    assert sum(1 for c in accepted if c.desc.startswith("exist")) == 10
    # 只挤掉 10 个存在性检查（14→10）。


def test_capacity_overflow_drops_but_does_not_reject():
    # seed9 bug：超过 max_checks 的溢出必须走"丢弃"，不得否决整个收尾声明。
    many = [{"desc": f"c{i}", "probe": f"ls ~/f{i} && echo PASS || echo FAIL"}
            for i in range(15)]
    # 15 个合法检查。
    accepted, rejections, dropped = validate(many, max_checks=12)
    assert len(accepted) == 12
    # 收满容量。
    assert not rejections, "capacity overflow must not be a soundness rejection"
    # 零拒绝：溢出不是健全性失败。
    assert len(dropped) == 3
    # 恰丢弃 3 个。


def test_look_in_program_envelope_with_lang():
    # t004 s21/s22 杀手用例：program 外壳里 lang="look" 的 look 字段解出 Look。
    t = parse_turn('{"program": {"lang": "look", "path": "/tmp/w/after-19.png", '
                   '"question": "what font?"}}')
    assert isinstance(t, Look) and t.path == "/tmp/w/after-19.png"


def test_bare_look_fields():
    # 测试：裸 path/question 字段（无 look 外壳）解析为 Look。
    t = parse_turn('{"path": "/tmp/w/s.png", "question": "layout?"}')
    assert isinstance(t, Look) and t.path == "/tmp/w/s.png"


def test_program_envelope_with_code_still_program():
    # 测试：带 code 的 program 即使顺带提到 path 键也仍是程序（不是 look）。
    t = parse_turn('{"program": {"lang": "python", "code": "print(1)", '
                   '"path": "/tmp/x.png"}}')
    assert isinstance(t, Program) and t.code == "print(1)"



def test_size_triggered_fold_v92():
    # v9.2：折叠由上下文"尺寸"触发而非对数——低于高水位不折叠（即使窗口外
    # 还有 2 对未折叠）；高于高水位折叠到低水位以下；keep 窗口永不折叠；
    # 高水位为 0 时旧版计数触发仍有效。
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.history_keep_pairs = 2
    cfg.fold_batch = 2
    cfg.ctx_high_water = 5000
    cfg.ctx_low_water = 3000
    # keep 2 对、批 2 对、高/低水位 5000/3000 字符。
    old_chat = L.chat
    L.chat = lambda *a, **k: "WORK LOG: folded"
    # 替换总结模型调用。
    try:
        h = []
        for i in range(10):                      # 10 pairs, ~800 chars each
            h += [{"role": "user", "content": "u" + "x" * 400},
                  {"role": "assistant", "content": "a" + "y" * 400}]
        # 构造 10 对、每对约 800 字符的历史。
        # BELOW high water: no fold, even with 2 unfolded pairs beyond the window
        # (count mode would have folded here — that is the v9.2 difference)
        s1, c1 = L._fold_summary(h[:8], 0, "", cfg, None, 1)
        assert c1 == 0 and s1 == "", (c1, s1)
        # 8 对（约 6400 字符）仍低于高水位判断口径：不折叠（计数模式会折叠——
        # 这正是 v9.2 的差异）。
        # ABOVE high water (~8k chars): fold oldest pairs down under low water
        s2, c2 = L._fold_summary(h, 0, "", cfg, None, 2)
        assert c2 > 0
        # 10 对（约 8000 字符）超水位：折叠发生。
        size = len(s2) + sum(len(m["content"]) for m in h[c2 * 2:])
        assert size <= cfg.ctx_low_water, size
        # 折叠后剩余尺寸（摘要 + 未折叠内容）降到低水位以下。
        # the keep window is never folded
        assert c2 <= len(h) // 2 - cfg.history_keep_pairs
        # 折叠对数不超过"总对数 - keep 窗口"。
        # legacy count trigger still works when high water is 0
        cfg.ctx_high_water = 0
        s3, c3 = L._fold_summary(h[:8], 0, "", cfg, None, 3)
        assert c3 == 2 and s3 == "WORK LOG: folded", (c3, s3)
        # 高水位归 0：旧版计数触发接管，按批折叠 2 对。
    finally:
        L.chat = old_chat
        # 恢复原始 chat。



def test_multi_program_extras_counted():
    # s15 杀手用例：一条回复 7 个 program 对象只有最后一个执行——丢弃数要
    # 记账（extras），供退化检测使用。
    t = parse_turn('{"program": {"lang": "bash", "code": "a"}} '
                   '{"program": {"lang": "bash", "code": "b"}} '
                   '{"program": {"lang": "bash", "code": "c"}}')
    assert isinstance(t, Program) and t.code == "c" and t.extras == 2
    # 取最后一个、extras=2。
    t2 = parse_turn('{"program": {"lang": "bash", "code": "only"}}')
    assert t2.extras == 0
    # 单程序 extras=0。


def test_fold_floor_guard_v11():
    # seed13 抖动：仅 keep 窗口本身超水位时不得每轮都折叠——可折叠对数少于
    # 一批时守卫拒绝折叠；凑满一批才折一次。
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.history_keep_pairs = 4
    cfg.fold_batch = 3
    cfg.ctx_high_water = 1000     # keep-window (4 pairs x ~800) alone exceeds this
    # keep 窗口（4 对 × ~800 字符）本身就超水位。
    cfg.ctx_low_water = 500
    old_chat = L.chat
    L.chat = lambda *a, **k: "LOG"
    try:
        h = []
        for i in range(6):
            h += [{"role": "user", "content": "u" + "x" * 400},
                  {"role": "assistant", "content": "a" + "y" * 400}]
        # 6 对历史。
        # 6 pairs, 2 foldable (< fold_batch=3): guard refuses the 1-2-pair thrash fold
        s1, c1 = L._fold_summary(h, 0, "", cfg, None, 1)
        assert c1 == 0, c1
        # 6 对只有 2 对可折叠（< 一批 3）：守卫拒绝小批量抖动折叠。
        # 7 pairs -> 3 foldable = exactly one batch: folds once, then stops at guard
        h += [{"role": "user", "content": "u" + "x" * 400},
              {"role": "assistant", "content": "a" + "y" * 400}]
        s2, c2 = L._fold_summary(h, 0, "", cfg, None, 2)
        assert c2 == 3, c2
        # 7 对 → 3 对可折叠恰一批：折一次即止（守卫挡住第二次）。
    finally:
        L.chat = old_chat



def test_look_paths_and_region_v13():
    # v13：look 支持多路径 paths + region 裁剪区域；单 path 归一为 paths 列表。
    t = parse_turn('{"look": {"paths": ["/a.png", "/b.png", "/c.png", '
                   '"/d.png", "/e.png"], '
                   '"region": [10, 20, 300, 400], "question": "compare views"}}')
    assert isinstance(t, Look) and t.paths == [
        "/a.png", "/b.png", "/c.png", "/d.png", "/e.png"]
    # 五个路径按序保留。
    assert t.region == [10, 20, 300, 400]
    # 裁剪区域原样。
    t2 = parse_turn('{"look": {"path": "/one.png", "question": "q"}}')
    assert t2.paths == ["/one.png"] and t2.region is None and t2.path == "/one.png"
    # 单路径：paths 归一为列表、region 为 None、path 兼容字段保留。


def test_look_normalizes_screen_prefix_accidentally_joined_to_absolute_path():
    # 测试：误拼的 "screen: /abs/path"（前缀与绝对路径粘连）归一为纯绝对路径；
    # 真正的屏幕引用 "screen:1.0" 原样保留。
    turn = parse_turn(
        '{"look": {"path": "screen: /mnt/verifier/reference.png", '
        '"question": "inspect reference"}}')

    assert isinstance(turn, Look)
    assert turn.paths == ["/mnt/verifier/reference.png"]
    # 粘连形式被拆开归一。

    live_screen = parse_turn(
        '{"look": {"path": "screen:1.0", "question": "inspect desktop"}}')
    assert live_screen.paths == ["screen:1.0"]
    # 真屏幕引用不动。


def test_imagery_tiling_v13():
    # v13：图像瓦片——大图切 1 总览 + 4 瓦片；小图整图直传；指定 region 只回
    # 裁剪区；多图对比逐一保留（瓦片不得吞掉后续路径的附件预算）。
    import io
    try:
        from PIL import Image
    except ImportError:
        return                       # graceful-degrade env: helper passes through
    # 无 PIL 的降级环境：直接通过（优雅降级）。
    from core.imagery import prepare_look_images
    big = Image.new("RGB", (3000, 2200), (200, 200, 200))
    buf = io.BytesIO(); big.save(buf, format="PNG")
    atts, note = prepare_look_images([buf.getvalue()])
    assert len(atts) == 5, len(atts)             # overview + 4 tiles
    # 大图产出 5 个附件（总览 + 4 瓦片）。
    assert "native-resolution" in note and "top-left" in note
    # 说明含原生分辨率与瓦片方位。
    small = Image.new("RGB", (600, 400), (10, 10, 10))
    buf2 = io.BytesIO(); small.save(buf2, format="JPEG")
    atts2, note2 = prepare_look_images([buf2.getvalue()])
    assert len(atts2) == 1 and "full view" in note2
    # 小图单附件整图。
    atts3, note3 = prepare_look_images([buf.getvalue()], region=[100, 100, 900, 700])
    assert len(atts3) == 1 and "requested region" in note3
    # 带 region：只回裁剪区单附件。
    # Multi-image comparisons preserve every requested item exactly once. Large-image
    # tiling must not consume an attachment budget and silently drop later paths.
    # 多图对比逐项恰好保留一次；大图瓦片不得吞掉附件预算而悄悄丢路径。
    atts4, note4 = prepare_look_images([buf.getvalue()] * 8)
    assert len(atts4) == 8, len(atts4)
    assert "image 8: full view" in note4, note4
    # 8 张同图逐张保留（第 8 张也有说明）。



def test_char_budget_keep_window_v14():
    # v14：keep 窗口的字符预算版——预算/每对字符数向下取整后再被 keep 对数
    # 地板抬高（40k/10k=4 → 地板 6）；瘦历史仍用满 30 对；keep_chars=0 退回
    # 固定对数行为。
    import core.loop as L
    from config.settings import load as _load
    cfg = _load(None)
    cfg.compaction = "summary"; cfg.history_keep_pairs = 30
    cfg.fold_batch = 2; cfg.ctx_high_water = 100_000; cfg.ctx_low_water = 62_000
    cfg.keep_chars = 40_000
    # 摘要压缩、keep 30 对、高/低水位 100k/62k、keep 预算 40k 字符。
    old = L.chat; L.chat = lambda *a, **k: "LOG"
    try:
        fat = []
        for i in range(20):                     # 20 pairs x ~10k chars = 200k total
            fat += [{"role": "user", "content": "u" + "x" * 5000},
                    {"role": "assistant", "content": "a" + "y" * 5000}]
        # 20 对、每对约 10k 字符（共 200k）。
        # effective keep = 40k budget / 10k per pair = 4 -> floored to 6
        assert L._effective_keep(fat, cfg) == 6
        # 有效 keep = 预算算出的 4 被 6 对地板抬高。
        s1, c1 = L._fold_summary(fat, 0, "", cfg, None, 1)
        size = len(s1) + sum(len(m["content"]) for m in fat[c1 * 2:])
        assert size <= cfg.ctx_low_water, size   # reachable now (was not
        # 折叠后尺寸可达低水位以下（旧的成对窗口做不到）。
        assert c1 == 14, c1                      # under pair-window: 30>20 kept all)
        # 折叠 14 对（keep 6 → 20-6=14）。
        # thin traces: full 30-pair window behavior unchanged
        thin = []
        for i in range(40):
            thin += [{"role": "user", "content": "u" + "x" * 300},
                     {"role": "assistant", "content": "a" + "y" * 300}]
        assert L._effective_keep(thin, cfg) == 30
        # 瘦历史（每对 600 字符）：仍用满 30 对窗口。
        # legacy: keep_chars=0 -> fixed pairs
        cfg.keep_chars = 0
        assert L._effective_keep(fat, cfg) == 30
        # 预算关闭：回到固定 30 对。
    finally:
        L.chat = old



def test_large_program_trace_is_archived_losslessly_but_bounded_in_live_context():
    # 测试：大程序输出的双轨处理——Trace 对象无损保留全部 1.2MB（供归档），
    # 活动上下文消息截到 500k 以内（保留头尾与省略说明）。
    from env.vm import Trace

    raw = "HEAD" + "x" * 1_200_000 + "TAIL"
    trace = Trace(stdout=raw, exit_code=0)
    message = trace_message(trace, context_max_chars=500_000)

    assert trace.stdout == raw
    # Trace 本体无损。
    assert len(message) < 510_000
    # 消息长度受控。
    assert "HEAD" in message and "TAIL" in message
    # 头尾都在。
    assert "chars omitted; full output archived" in message
    # 消息带"已省略、全量已归档"说明。


def test_unchanged_baseline_keeps_complete_live_program_output():
    # 测试：baseline.yaml 的 trace 配置保持完整活动输出——300k+证据+300k
    # 的输出在消息里零省略（基线行为不被悄悄收紧）。
    from pathlib import Path
    from config.settings import load
    from env.vm import Trace

    repo = Path(__file__).resolve().parents[1]
    cfg = load(str(repo / "config/osworld/baseline.yaml"))
    # 用仓库真实基线配置。
    raw = "H" * 300_000 + "MIDDLE_EVIDENCE" + "T" * 300_000
    message = trace_message(
        Trace(stdout=raw, exit_code=0), head=cfg.trace_head, tail=cfg.trace_tail,
        context_max_chars=cfg.trace_context_max_chars)

    assert raw in message
    assert "chars omitted" not in message
    # 全量在、无省略。


def test_v17_infra_detection():
    # v17：基础设施失败检测——Trace 只在显式 infra_fail 标记时才算基础设施
    # 失败；正常 exit 0 的输出不算。
    from env.vm import Trace
    ro = ("mktemp: failed to create file via template '/tmp/rsiagent_XXXXXX.py': "
          "Read-only file system")
    assert "Read-only file system" in ro and "mktemp" in ro
    # 典型的只读文件系统错误文本。
    t = Trace(stdout="hello\n[exit 0]", exit_code=0)
    assert not t.infra_fail
    # 正常退出不是基础设施失败。


def test_actor_loop_normalizes_unclassified_run_wrapper_failure():
    # 测试：运行包装器（rsiagent_run）级别的失败要归一为 infra_fail——
    # exit_code=None + stderr 里的 mktemp/日志创建失败识别为基础设施；
    # 程序自己的输出里出现同类错误文本（exit 0）则不算。
    import core.loop as L
    from env.vm import Trace

    wrapper_failure = Trace(
        stdout=(
            "[exit 2]\n[stderr] mktemp: failed to create file via template "
            "‘/tmp/rsiagent_XXXXXX.sh’: Read-only file system\n"
            "/bin/sh: cannot create /tmp/rsiagent_run_abc123.log: "
            "Read-only file system"),
        exit_code=None,
        infra_fail=False,
    )
    # 包装器失败形态：无退出码 + stderr 双重只读错误。
    assert L._normalize_program_transport_failure(wrapper_failure) is True
    assert wrapper_failure.infra_fail is True
    # 归一为基础设施失败。

    genuine_program_output = Trace(
        stdout=(
            "mktemp: failed to create file via template "
            "‘/tmp/rsiagent_XXXXXX.sh’: Read-only file system\n[exit 0]"),
        exit_code=0,
        infra_fail=False,
    )
    # 程序自己打印了同类文本但正常退出：不算包装器失败。
    assert L._normalize_program_transport_failure(genuine_program_output) is False
    assert genuine_program_output.infra_fail is False
    # 不误标。


@pytest.mark.parametrize("no_progress", [False, True])
@pytest.mark.parametrize("flush_failure", [False, True])
def test_verifier_infra_retry_survives_dead_scratch_and_rolls_back_before_retry(
        monkeypatch, tmp_path, no_progress, flush_failure):
    # 测试（2×2 参数化）：Verifier 首次验证因基础设施失败时——回滚死掉的
    # scratch 执行器（close）、以 continue_candidate=True 在重建的镜像上重试；
    # no_progress 类失败保留现场供恢复（不 close）、转写落盘、无 result.json；
    # 转写刷盘失败也只影响断言路径不影响重试行为。
    import json
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from core.verifier_runtime import (
        AgenticVerifierInfrastructureError, AgenticVerifierNoProgressError,
    )
    # 导入两类 Verifier 运行时错误：基础设施失败与无实质进展。
    from env.vm import Trace

    cfg = load(None)
    cfg.model = "actor"
    cfg.max_iters = 3
    cfg.wall_clock_secs = 60
    cfg.agent_decided_stop = True
    cfg.history_keep_pairs = 0
    cfg.independent_verify = True
    cfg.agentic_verifier_config = "verifier.yaml"
    cfg.verifier_continuity = True
    cfg.verifier_persist_scratch = True
    cfg.verifier_infra_retries = 1
    # 任务模式 + 独立验证 + 持久 scratch + 1 次基础设施重试。
    replies = iter((
        '{"program":{"lang":"bash","code":"printf work"}}',
        '{"done":{"checks":[{"desc":"artifact exists",'
        '"probe":"test -f /tmp/artifact && echo PASS"}]}}',
    ))
    # Actor 脚本：先干活再收尾（带一个存在性探针）。

    class VM:
        # 伪 VM：脚本成功、探针全 PASS。
        def run_script(self, *_args, **_kwargs):
            return Trace(stdout="work\n[exit 0]", exit_code=0)

        def run_command(self, command, **_kwargs):
            return "PASS" if "echo PASS" in command else ""

    class Executor:
        # 伪 scratch 执行器：export 永远失败，close 可观察。
        def __init__(self, vm):
            self.vm = vm
            self.closed = False

        def export_scratch(self):
            raise AgenticVerifierInfrastructureError(
                "controller unavailable while exporting scratch")
            # 导出 scratch 时控制器不可用。

        def close(self):
            self.closed = True

    vm = VM()
    failed_executor = Executor(vm)
    verify_calls = []

    def fake_verify(*_args, **kwargs):
        # 伪独立验证：第一次把死执行器挂到会话上并抛错；第二次通过。
        verify_calls.append(kwargs)
        if len(verify_calls) == 1:
            kwargs["session"]._program_executor = failed_executor
            if no_progress:
                raise AgenticVerifierNoProgressError("no substantive progress")
            raise AgenticVerifierInfrastructureError("trusted mirror unavailable")
            # 按参数抛基础设施失败或无进展错误。
        return "pass", "candidate verified after rebuilt mirror"
        # 第二次：重建镜像后通过。

    monkeypatch.setattr(L, "chat", lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr(L, "verify_independent", fake_verify)
    # 替换 chat 与独立验证。
    if flush_failure and no_progress:
        def fail_flush(*_args):
            raise OSError('diagnostic disk full')
        monkeypatch.setattr(ArtifactSink, 'save_transcript', fail_flush)
        # 可选注入：诊断转写刷盘失败（磁盘满）。
    if no_progress:
        with pytest.raises(AgenticVerifierNoProgressError):
            L.run_attempt("make artifact", vm, cfg, ArtifactSink(str(tmp_path / "run")))
        # no_progress：错误如实上抛（不重试——无进展不是基础设施）。
        assert len(verify_calls) == 1
        # 只验证一次。
        assert failed_executor.closed is False, "preserve the boundary for recovery"
        # 死执行器不 close：现场保留给恢复流程。
        if not flush_failure:
            transcript = json.loads((tmp_path / 'run/transcript.json').read_text())
            assert transcript['system']
            assert any('artifact exists' in str(m) for m in transcript['messages'])
            assert any('work' in str(m) for m in transcript['messages'])
            assert not (tmp_path / 'run/result.json').exists()
            # 转写完整落盘（系统提示/探针/工作输出都在）且无 result.json。
        return
    result, _ = L.run_attempt(
        "make artifact", vm, cfg, ArtifactSink(str(tmp_path / "run")))
    # infra 分支：跑完整个尝试。

    assert result.status == "done"
    # 重试后完成。
    assert len(verify_calls) == 2
    # 验证恰好两次（失败 + 重试）。
    assert verify_calls[1]["continue_candidate"] is True
    # 重试标记 continue_candidate=True：候选在同一会话上继续。
    assert failed_executor.closed is True
    # 死执行器在重试前被回滚关闭。
    assert result.infra_pauses == 1
    # 记账一次基础设施暂停。


def test_channel_restart_recovers_same_actor_without_replaying_action(tmp_path):
    # 测试：通道错误（控制器重启）触发 wait_for_controller 恢复——同一个
    # Actor 会话继续，提示写明动作"未重放"且带恢复证据；程序序列不重复执行。
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.max_iters = 3
    cfg.wall_clock_secs = 60
    cfg.history_keep_pairs = 0
    cfg.independent_verify = False
    cfg.practice_mode = True
    # 练习模式配置。
    replies = [
        '{"program":{"lang":"bash","code":"python heavy.py"}}',
        '{"program":{"lang":"bash","code":"echo resumed"}}',
        '{"done":null}',
    ]
    # Actor 脚本：重活 → 恢复后的续作 → 收尾。
    actor_users = []
    # 记录发给 Actor 的每条提示。

    def fake_chat(_model, _system, user, **_kwargs):
        actor_users.append(user)
        return replies.pop(0)
        # 记录提示并按脚本回复。

    class VM:
        # 伪 VM：第一个脚本通道错误，之后正常；控制器恢复可观察。
        def __init__(self):
            self.codes = []
            self.recoveries = 0

        def run_command(self, _cmd, **_kwargs):
            return ""

        def run_script(self, _lang, code, **_kwargs):
            self.codes.append(code)
            if len(self.codes) == 1:
                return Trace(
                    stdout="[channel error: ConnectionError — controller restarting]",
                    secs=2.0, infra_fail=True)
            # 第一个脚本：通道错误（infra_fail）。
            return Trace(stdout="resumed\n[exit 0]", exit_code=0, secs=0.1)
            # 第二个脚本正常。

        def wait_for_controller(self, **_kwargs):
            self.recoveries += 1
            return True, "controller recovered; 2 consecutive probes passed"
            # 控制器恢复：两次探针通过。

    vm = VM()
    old_chat = L.chat
    L.chat = fake_chat
    try:
        result, _history = L.run_attempt(
            "practice", vm, cfg, ArtifactSink(str(tmp_path / "recovery")))
        # 跑完整尝试。
    finally:
        L.chat = old_chat
        # 恢复 chat。

    assert result.status == "done"
    # 恢复后正常收尾。
    assert vm.recoveries == 1
    # 恰一次控制器恢复。
    assert vm.codes == ["python heavy.py", "echo resumed"]
    # 两个脚本各执行一次（重活不重放）。
    assert "NOT replayed" in actor_users[1]
    # 恢复后的提示写明动作未重放。
    assert "controller recovered" in actor_users[1]
    # 提示带恢复证据原文。


def test_v17_surface_delta():
    # v17：表面增量——把本轮新增/变化的 guest 文件面（size+路径）与基线快照
    # 对比；新文件标 "CREATED this run"；无变化时增量为空串。
    import core.loop as L
    class FakeVM:
        # 伪 VM：run_command 按序列返回 find 输出。
        def __init__(self, seq): self.seq = seq; self.i = 0
        def run_command(self, *a, **k):
            out = self.seq[self.i]; self.i = min(self.i + 1, len(self.seq) - 1); return out
    base = "1000.0 /home/user/Desktop/form.pdf\n1000.0 /home/user/Desktop/ref.jpg"
    after = ("1000.0 /home/user/Desktop/form.pdf\n1000.0 /home/user/Desktop/ref.jpg\n"
             "2000.0 /home/user/Desktop/form-filled.pdf")
    # 基线两文件；运行后多了 form-filled.pdf。
    vm = FakeVM([base, after])
    baseline = L._snapshot(vm)
    assert "/home/user/Desktop/form.pdf" in baseline
    # 基线快照含已有文件。
    delta = L._surface_delta(vm, baseline)
    assert "CREATED this run" in delta and "form-filled.pdf" in delta
    # 增量标出本轮新建的文件。
    vm2 = FakeVM([base, base])
    assert L._surface_delta(vm2, L._snapshot(vm2)) == ""
    # 无变化：增量为空。


def test_unified_surface_delta_hides_actor_private_path_names():
    # 测试：统一表面增量隐藏 Actor 私有路径（excluded_paths 前缀下的文件
    # 不进增量报告），只暴露交付面文件。
    import core.loop as L

    class FakeVM:
        def run_command(self, *args, **kwargs):
            assert kwargs["cap"] == 0
            # 断言枚举命令不设输出上限（全量枚举）。
            return (
                "2000.0 /home/user/work/private_strategy.md\n"
                "2000.0 /home/user/Desktop/candidate.xlsx")

    delta = L._surface_delta(
        FakeVM(), {"/home/user/Desktop/input.xlsx": "1000.0"},
        excluded_paths=("/home/user/work",))
    # 私有目录 /home/user/work 被排除。

    assert "candidate.xlsx" in delta
    # 公开交付文件在增量里。
    assert "private_strategy" not in delta
    # 私有文件名被隐藏。


def _v18_harness(find_seq, pivot_enabled=True):
    """Shared rig for the v18 restart tests: an actor that repeats one program until
    the repeat-cap stalls each attempt, a verifier that never rescues, and a VM whose
    find output follows ``find_seq`` (baseline, stall-exit, resume-baseline, ...)."""
    # v18 重启测试共用台架：Actor 重复同一程序直到重复上限导致每次尝试停滞；
    # Verifier 从不救场；VM 的 find 输出按 find_seq 序列推进。
    import tempfile

    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.strategy_pivot = pivot_enabled
    cfg.max_iters = 40
    cfg.max_consec_repeat = 3
    cfg.independent_verify = True
    cfg.max_resumes = 1
    cfg.wall_clock_secs = 3600
    cfg.history_keep_pairs = 0
    # 转向开关可配；重复上限 3；最多恢复 1 次。

    class VM:
        def __init__(self): self.i = 0
        def run_command(self, cmd, timeout=30, **kw):
            if cmd.startswith("find /home/user"):
                out = find_seq[min(self.i, len(find_seq) - 1)]
                self.i += 1
                return out
            # find 命令按序列返回（每次推进一格，尾部驻留）。
            return "PASS"
            # 其余探测命令恒 PASS。
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
            # 脚本输出永远相同（制造重复停滞）。
        def fetch_file(self, path, max_bytes=0):
            return None, "n"

    calls = {"review": 0}
    PROG = '{"program": {"lang": "bash", "code": "echo same"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if "post-mortem" in system:
            calls["review"] += 1
            return "review-text-XYZ: tried A via B; stuck at C; try D instead"
            # 复盘调用：返回固定的复盘文本。
        return PROG
        # Actor 调用：永远重复同一程序。
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        return "wrong", "not there"
        # Verifier 恒 wrong：从不救场。

    old_chat, old_verify = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        sink = ArtifactSink(tempfile.mkdtemp(prefix="v18_"))
        res, hist = L.run_with_resume("t", VM(), cfg, sink)
        # 跑带恢复的主循环。
    finally:
        L.chat, L.verify_independent = old_chat, old_verify
    return res, hist, calls


def test_v18_pivot_on_zero_delta():
    # v18：零表面增量的停滞触发"转向"（pivot）而非纯恢复——恰好一次复盘
    # 调用；检查标签是 pivot1 不是 resume1；复盘文本进入新上下文的用户消息。
    base = "1000.0 /home/user/Desktop/a.txt"
    res, hist, calls = _v18_harness([base, base, base, base])   # nothing ever changes
    # find 输出永远不变：零增量。
    assert res.resumes == 1
    # 恰一次重启。
    tags = [v for _, v in res.inspections]
    assert "pivot1" in tags and "resume1" not in tags, tags
    # 重启标记为转向（不是恢复）。
    assert calls["review"] == 1                       # exactly one post-mortem call
    # 恰一次复盘调用。
    openings = [m["content"] for m in hist if m["role"] == "user"
                and "MATERIALLY DIFFERENT" in str(m["content"])]
    assert openings and "review-text-XYZ" in openings[0]
    # 转向后的开场消息带复盘文本（新上下文知道自己为何转向）。


def test_v18_ab_toggle_off_forces_continuation():
    # v18 对照组：strategy_pivot 关闭时走纯 v15 恢复路径——resume1 标签、
    # 无复盘调用（对照臂行为保留）。
    base = "1000.0 /home/user/Desktop/a.txt"
    res, hist, calls = _v18_harness([base, base, base, base], pivot_enabled=False)
    assert res.resumes == 1
    tags = [v for _, v in res.inspections]
    assert "resume1" in tags and "pivot1" not in tags, tags   # control arm = pure v15
    # 对照臂：纯恢复。
    assert calls["review"] == 0
    # 无复盘调用。


def test_v18_continuation_when_work_banked():
    # v18：已有工作入账（文件增长）时走恢复而非转向——无复盘调用，恢复
    # 提示写明保留（RETAINS）已入账工作。
    base = "1000.0 /home/user/Desktop/a.txt"
    grown = base + "\n2000.0 /home/user/Desktop/out.csv"
    res, hist, calls = _v18_harness([base, grown, grown, grown])  # attempt banked a file
    # 第一次尝试入账了一个新文件。
    assert res.resumes == 1
    tags = [v for _, v in res.inspections]
    assert "resume1" in tags and "pivot1" not in tags, tags
    # 有工作就恢复：不转向。
    assert calls["review"] == 0                       # no post-mortem on banked work
    # 有入账工作就不做复盘。
    assert any(m["role"] == "user" and "RETAINS" in str(m["content"]) for m in hist)
    # 恢复提示带 RETAINS（保留工作）说明。


def test_phase0_interpreter_body_mutation_caught():
    # Phase-0 安全修复：python/perl 体内删除/覆盖交付物的检查必须被拒绝
    # ——检查跑在被打分的活机器上；体内容读合法、/tmp 草稿写合法。
    from core.checks import _nonscratch_mutations as M
    assert M("python3 -c \"import shutil; shutil.rmtree('/home/user/Documents'); print('PASS')\"")
    assert M("python3 -c \"import os; os.remove('/home/user/Desktop/out.pdf'); print('PASS')\"")
    assert M("python3 -c \"open('/home/user/f','w').write('x'); print('PASS')\"")
    assert M("perl -e \"unlink '/home/u/x'; print 'PASS'\"")
    # 四种体内的非草稿区破坏（rmtree/remove/write/unlink）全部识别。
    # legit content READS inside a body stay allowed (no false positive)
    assert not M("python3 -c \"import zipfile; z=zipfile.ZipFile('/home/u/f.pptx'); "
                 "print('PASS' if len(z.read('ppt/slides/slide1.xml')) > 100 else 'FAIL')\"")
    assert not M("python3 -c \"import openpyxl; wb=openpyxl.load_workbook('/home/u/x.xlsx'); "
                 "print('PASS' if wb.active['A1'].value else 'FAIL')\"")
    # 体内的内容读（zipfile/openpyxl）不误报。
    # a write to /tmp scratch inside a body is allowed (render-and-compare, v16)
    assert not M("python3 -c \"open('/tmp/ck.png','wb').write(b'x'); print('PASS')\"")
    # /tmp 草稿写合法。


def test_phase0_verifier_requires_content_probe():
    # Phase-0：Verifier 的"通过"必须建立在读交付物内容的探针上——存在性/
    # 元数据探针（ls/test）的通过降级为 unverified（docstring 里承诺已久的
    # 守卫真正落地）。
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.verifier_probes = 4

    class VM:
        # 伪 VM：内容探针给信息性输出，元数据探针给文件列表。
        def run_command(self, cmd, timeout=30, **k):
            if "grep" in cmd or "python" in cmd:
                return "PASS cell A1=42"                  # content probe: informative
                # 内容探针：信息性输出。
            if cmd.startswith("ls") or cmd.startswith("test"):
                return "form.xlsx\nref.pdf"               # metadata: informative, not content
                # 元数据探针：有信息但不构成内容。
            return ""

    def run(seq):
        # 工具：按脚本驱动 verify_independent。
        box = list(seq)
        def fake(model, system, user, **k):
            return box.pop(0) if box else '{"verdict":"unverified","findings":"x"}'
        old = V.chat
        V.chat = fake
        try:
            return V.verify_independent("t", VM(), cfg)[0]
        finally:
            V.chat = old

    # existence-only pass -> downgraded
    assert run(['{"probes": ["ls /home/user/Desktop"]}',
                '{"verdict":"pass","findings":"file exists"}']) == "unverified"
    # 只凭存在性的通过 → 降级为 unverified。
    # content-backed pass -> survives
    assert run(['{"probes": ["grep -q 42 /home/user/Desktop/form.xlsx && echo PASS"]}',
                '{"verdict":"pass","findings":"content correct"}']) == "pass"
    # 内容支撑的通过 → 存活。


def test_v21_dup_counting():
    # A：parse_turn 报告同类型重复数（dup），供主循环拒绝解码退化回合。
    # 两个程序 → dup 2（灾难：last-wins 会执行仪式性重复对象）；良性的跨
    # 类型对冲 → dup 1。
    from core.actor import Done as _D
    t = parse_turn('{"program":{"lang":"bash","code":"real work"}}\n'
                   '{"program":{"lang":"bash","code":"echo done"}}')     # 2 programs
    assert isinstance(t, Program) and t.dup == 2, t
    # 两个同类型程序：dup=2。
    t = parse_turn('{"program":{"lang":"bash","code":"x"}}\n{"done": null}')  # prog+done
    assert isinstance(t, Program) and t.dup == 1, t                      # precedence, benign
    # program+done 的良性对冲：dup=1（优先级正常）。
    t = parse_turn('{"look":{"path":"/a.png","question":"q"}}\n'
                   '{"look":{"path":"/a.png","question":"q"}}\n'
                   '{"look":{"path":"/a.png","question":"q"}}')          # 3 looks
    assert isinstance(t, Look) and t.dup == 3, t
    # 三个相同 look：dup=3。
    t = parse_turn('{"program":{"lang":"bash","code":"ls"}}')            # single clean
    assert isinstance(t, Program) and t.dup == 1, t
    # 单个干净程序：dup=1。


def _v21_drive(strict, replies, max_iters=8):
    # v21 A 组驱动器：严格单动作开关 + 脚本化回复 + 伪 VM。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = max_iters
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = strict
    cfg.max_consec_degen = 3
    cfg.wall_clock_secs = 3600
    # 严格单动作可配；连续退化上限 3。
    ran = {"n": 0}
    box = list(replies)
    def fake_chat(*a, **k):
        return box.pop(0) if box else replies[-1]
    # 按脚本回复（耗尽后重复最后一条）。
    class VM:
        def run_script(self, lang, code, timeout=600):
            ran["n"] += 1
            return Trace(stdout="hi\n[exit 0]", exit_code=0, secs=0.1)
            # 统计实际执行的脚本数。
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    old = L.chat
    L.chat = fake_chat
    try:
        res, hist = L.run_attempt("t", VM(), cfg,
                                  ArtifactSink(tempfile.mkdtemp(prefix="v21_")))
    finally:
        L.chat = old
    return res, hist, ran


def test_complete_role_opening_bypasses_actor_plan_wrapper():
    # 测试：完整的角色开场（instruction_is_complete_opening）不包 PLAN 计划
    # 包装——提示原样进对话首条，Agent 直接执行证据工作并收尾。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.max_iters = 2
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    opening = "VERIFIER AGENT — complete role-owned opening"
    # 完整角色开场文本。
    seen = {}

    def fake_chat(_model, _system, user, **_kwargs):
        seen["user"] = user
        return '{"program":{"lang":"bash","code":"echo evidence"}}'
        # 记录提示并返回证据程序。

    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="evidence\n[exit 0]", exit_code=0, secs=0.1)
        def run_command(self, command, timeout=30, **_kwargs):
            return ""

    old = L.chat
    L.chat = fake_chat
    try:
        result, history = L.run_attempt(
            opening, VM(), cfg,
            ArtifactSink(tempfile.mkdtemp(prefix="role_opening_")),
            instruction_is_complete_opening=True,
            terminal_handoff_ready=lambda: True)
        # 以完整开场 + 交接就绪启动尝试。
    finally:
        L.chat = old

    assert result.status == "done"
    # 正常收尾。
    assert seen["user"] == opening
    # 发给模型的第一条提示就是开场原文。
    assert history[0]["content"] == opening
    # 对话历史首条 = 开场原文。
    assert "PLAN:" not in seen["user"]
    # 提示里没有 PLAN 计划包装（开场不要求再报计划）。


def test_v21_degenerate_turn_runs_nothing_then_pivots():
    # v21 A 组：退化回合（同回合两个 program）在严格模式下什么都不执行，
    # 连续 3 次后升级为 stalled；且发出"多个行动对象"的重新询问。
    DUP = ('{"program":{"lang":"bash","code":"real"}}'
           '{"program":{"lang":"bash","code":"echo done"}}')   # 2 programs = degenerate
    # 两个 program = 退化回合。
    res, hist, ran = _v21_drive(True, [DUP])
    assert ran["n"] == 0, ran                                  # NOTHING executed
    # 什么都没执行（不执行重复对象）。
    assert res.status == "stalled", res.status                 # escalated after 3
    # 连续退化 3 次 → 停滞升级。
    assert any("MULTIPLE action objects" in str(m["content"])
               for m in hist if m["role"] == "user")           # re-ask was sent
    # 历史里有"多个行动对象"的重新询问。


def test_v21_control_runs_last_program():
    # v21 A 对照组：strict 关闭 = v21 前行为——多程序回复仍然执行（最后一个）。
    DUP = ('{"program":{"lang":"bash","code":"real"}}'
           '{"program":{"lang":"bash","code":"echo done"}}')
    res, hist, ran = _v21_drive(False, [DUP], max_iters=3)
    assert ran["n"] >= 1, ran                                  # old behavior executed
    # 旧行为：至少执行了一个程序。


def test_v21_clean_single_action_unaffected():
    # v21 A：正常单程序运行不受严格模式影响。
    res, hist, ran = _v21_drive(True,
        ['{"program":{"lang":"bash","code":"ls"}}',
         '{"done": {"checks": [{"desc":"d","probe":"ls / && echo PASS"}]}}'])
    assert ran["n"] >= 1, ran                                  # the clean program ran
    # 干净程序照常执行。


_B_PROG = '{"program":{"lang":"bash","code":"echo work"}}'
_B_DONE = '{"done":{"checks":[{"desc":"d","probe":"ls / && echo PASS"}]}}'
# B 组脚本常量：一个工作程序与一个收尾声明。


def _b_drive(budget_cond, replies, verdicts, max_iters=12, commit_frac=0.75):
    # v21 B 组驱动器：预算条件接受开关 + 脚本化回复与验证判定。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = max_iters
    cfg.independent_verify = True
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False          # isolate B from A
    # 关 A 开关：隔离 B 行为。
    cfg.budget_conditional_accept = budget_cond
    cfg.commit_frac = commit_frac
    cfg.unverified_accept = 2
    cfg.max_wrong_before_pivot = 3
    cfg.wall_clock_secs = 3600
    # B 参数：unverified 接受阈值 2、wrong 转向阈值 3、提交阶段占比可配。
    rbox, vbox = list(replies), list(verdicts)
    def fake_chat(*a, **k):
        return rbox.pop(0) if rbox else replies[-1]
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        return (vbox.pop(0) if vbox else verdicts[-1]), "finding text"
        # 验证判定按脚本吐。
    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        res, _ = L.run_attempt("t", VM(), cfg,
                               ArtifactSink(tempfile.mkdtemp(prefix="b_")))
    finally:
        L.chat, L.verify_independent = oc, ov
    return res


def test_v21_b_unverified_does_not_accept_before_commit():
    # B：提交阶段之前（预算未花）的 unverified 不得自动接受——弹回继续；
    # 持续 done 刷屏最终停滞（→ 上游换新上下文转向），绝不软接受。
    res = _b_drive(True, [_B_PROG] + [_B_DONE] * 6, ["unverified"] * 6, commit_frac=0.9)
    # 提交占比 0.9：几乎全程都不在提交阶段。
    assert res.status == "stalled", res.status          # refused to soft-accept early
    # 拒绝提前软接受：停滞。
    assert not any(v == "unverified" and False for _, v in res.inspections)  # sanity
    # 健全性检查（恒真占位，防御性断言）。


def test_v21_b_unverified_accepts_in_commit_phase():
    # B：进入提交阶段（预算已大部分花掉）且此前无 wrong 时，2 次 unverified
    # 的接受被允许——done 收尾成功。
    res = _b_drive(True, [_B_PROG, _B_PROG, _B_DONE, _B_DONE], ["unverified", "unverified"],
                   max_iters=10, commit_frac=0.2)   # commit at iter>=2
    # 提交占比 0.2：第 2 轮起即算提交阶段。
    assert res.status == "done", res.status
    # 提交阶段内接受：done。


def test_v21_b_repeated_wrong_keeps_same_actor_until_budget():
    # B：Verifier 的 wrong 是语义反馈——即使连续三个 findings 也不得丢弃
    # Actor 对话或触发换新模型转向；一直耗到预算结束。
    res = _b_drive(True, [_B_PROG, _B_DONE, _B_PROG, _B_DONE, _B_PROG, _B_DONE],
                   ["wrong", "wrong", "wrong"])
    assert res.status == "budget", res.status
    # 状态：预算耗尽（不转向）。
    assert sum(1 for _, v in res.inspections if v == "wrong") >= 3
    # 至少三次 wrong 检查：同一 Actor 持续被纠错。


def test_v21_b_control_accepts_early_2x_unverified():
    # B 对照组（开关关）：v21 前行为——第 2 次 unverified 无视预算直接接受。
    res = _b_drive(False, [_B_PROG, _B_DONE, _B_DONE], ["unverified", "unverified"],
                   commit_frac=0.9)
    assert res.status == "done", res.status
    # 对照：提前接受成功。


def test_v21_ctx_cap_worklog_line_boundary():
    # v21：工作日志按字符上限截断但绝不切断行（行边界对齐）；_fold_input
    # 同时保留头尾（错误在尾部，必须存活）。
    from core.loop import _cap_worklog, _fold_input
    assert _cap_worklog("short\nlog", 100) == "short\nlog"          # under cap unchanged
    # 上限内原样。
    text = "\n".join(f"line{i} value={i}" for i in range(400))
    out = _cap_worklog(text, 500)
    assert len(out) < len(text) and "truncated at the char cap" in out
    # 超限截断并带说明。
    kept = out.split("\n[... WORK LOG")[0]
    assert all(l.startswith("line") for l in kept.splitlines() if l)  # no mid-line chop
    # 保留部分全是完整行（无行中切断）。
    # _fold_input keeps head AND tail (errors land at the tail)
    s = "HEAD_START" + "x" * 9000 + "TRACEBACK_TAIL"
    fi = _fold_input(s, 1500, 2500)
    assert fi.startswith("HEAD_START") and fi.endswith("TRACEBACK_TAIL") and len(fi) < len(s)
    # 折叠输入保留头与尾、总长受控。


def test_v21_ctx_task_pinned_after_fold():
    # v21：折叠后的上下文必须钉住原始任务指令——压缩器产摘要时，任务原文
    # （逐字未变标记 + 唯一标记）仍在历史里。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.max_iters = 12
    cfg.independent_verify = False
    cfg.strict_one_action = False
    cfg.history_keep_pairs = 30
    cfg.keep_chars = 200          # tiny verbatim window
    # 极小的逐字窗口：迫使早期折叠。
    cfg.ctx_high_water = 300      # tiny -> fold early
    cfg.ctx_low_water = 150
    cfg.fold_batch = 2            # fold in small chunks so a fold happens fast
    # 小批量：折叠很快发生。
    cfg.wall_clock_secs = 3600
    TASK = "UNIQUE_TASK_MARKER_XYZ do the specific thing"
    # 带唯一标记的任务指令。
    seen = {"pinned": False}
    PROG = '{"program":{"lang":"bash","code":"echo a longish output line to grow context here"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if system == L.SUMMARIZER_SYSTEM:
            return "WORK LOG\n- CURRENT STATE: working"          # produce a summary
            # 压缩器调用：产摘要。
        if history and any("TASK (verbatim, unchanged" in str(m.get("content", ""))
                           and "UNIQUE_TASK_MARKER_XYZ" in str(m.get("content", ""))
                           for m in history):
            seen["pinned"] = True
            # 发现任务原文仍钉在历史里。
        return PROG
    class VM:
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="x" * 400 + "\n[exit 0]", exit_code=0, secs=0.1)
            # 每轮 400 字符输出：快速推高上下文。
        def run_command(self, cmd, timeout=30, **kw):
            return "PASS"
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc = L.chat
    L.chat = fake_chat
    try:
        L.run_attempt(TASK, VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="ctx_")))
    finally:
        L.chat = oc
    assert seen["pinned"], "task instruction not pinned into the compacted context"
    # 任务指令在折叠后的上下文中仍被钉住。


def test_v21_escalation_model_on_stuck_restart():
    # v21 模型升级：卡死后的全新转向/恢复尝试换用 escalation_model 运行；
    # 首次（主）尝试仍在主模型上。跨模型验证：升级尝试的 Actor 用升级模型，
    # 但 Verifier 钉在原模型上（不自验证）。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/m3"
    cfg.escalation_model = "escalation/qwen"
    cfg.max_iters = 40
    cfg.max_consec_repeat = 3        # identical program+output -> stall fast
    # 相同程序+输出 → 快速重复停滞。
    cfg.independent_verify = True
    cfg.max_resumes = 1
    cfg.history_keep_pairs = 0
    cfg.strict_one_action = False
    cfg.wall_clock_secs = 3600
    models_seen = []
    verify_pairs = []                                # (actor_model, resolved_verifier_model)
    # 记录（Actor 模型, 解析出的验证模型）对。
    PROG = '{"program":{"lang":"bash","code":"echo same"}}'
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        models_seen.append(model)
        return PROG                                  # identical forever -> repeat stall
        # 永远相同的程序 → 重复停滞。
    def fake_verify(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None):
        verify_pairs.append((cfg.model, cfg.verifier_model or cfg.model))  # what verifier.py resolves
        # 记录 verifier.py 实际解析出的模型组合。
        return "wrong", "not there"                  # never rescue -> stays stalled
        # 永不救场 → 保持停滞。
    class VM:
        def run_command(self, cmd, timeout=30, **kw):
            return "" if cmd.startswith("find /home/user") else "PASS"  # empty -> pivot
            # find 输出恒空 → 触发转向。
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0):
            return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fake_chat, fake_verify
    try:
        res, _ = L.run_with_resume("t", VM(), cfg,
                                   ArtifactSink(tempfile.mkdtemp(prefix="esc_")))
        # 跑带恢复的主循环。
    finally:
        L.chat, L.verify_independent = oc, ov
    assert "primary/m3" in models_seen, "primary attempt didn't run on M3"
    # 首次尝试在主模型上。
    assert "escalation/qwen" in models_seen, "stuck restart didn't escalate to the model"
    # 卡死重启升级到了升级模型。
    assert any(":esc" in str(v) for _, v in res.inspections), res.inspections
    # 检查标签带 :esc（升级标记）。
    # cross-model verification: on the escalated attempt the ACTOR is the escalation
    # model but the VERIFIER is pinned to the original model (not self-verify).
    esc_verify = [v for a, v in verify_pairs if a == "escalation/qwen"]
    assert esc_verify, "escalated attempt never reached the verifier"
    assert all(v == "primary/m3" for v in esc_verify), \
        f"escalated attempt self-verified instead of cross-checking: {verify_pairs}"
    # 升级尝试的验证者是原模型（不是自己验证自己）。
    # sanity: the primary (non-escalated) attempt still verifies on its own model
    assert ("primary/m3", "primary/m3") in verify_pairs, verify_pairs
    # 主尝试仍在自己的模型上验证。


def test_v21_no_escalation_when_unset():
    # v21：默认（escalation_model=""）→ 重启仍用主模型，无升级标记。
    import tempfile
    import core.loop as L
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None); cfg.model = "primary/m3"; cfg.escalation_model = ""
    cfg.max_iters = 40; cfg.max_consec_repeat = 3; cfg.independent_verify = True
    cfg.max_resumes = 1; cfg.history_keep_pairs = 0; cfg.strict_one_action = False
    cfg.wall_clock_secs = 3600
    seen = set()
    def fc(model, system, user, **k): seen.add(model); return '{"program":{"lang":"bash","code":"echo same"}}'
    def fv(instruction, vm, cfg, sink=None, turn_no=0, context="", session=None): return "wrong", "x"
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "" if cmd.startswith("find /home/user") else "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="same\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return None, "n"
    oc, ov = L.chat, L.verify_independent
    L.chat, L.verify_independent = fc, fv
    try:
        res, _ = L.run_with_resume("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="noesc_")))
    finally:
        L.chat, L.verify_independent = oc, ov
    assert seen == {"primary/m3"}, seen                  # only the primary model used
    # 只用了主模型。
    assert not any(":esc" in str(v) for _, v in res.inspections)
    # 无升级标记。


def _tiny_png():
    # 测试工具：生成 8×8 的最小 PNG。
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (8, 8), (10, 20, 30)).save(b, format="PNG")
    return b.getvalue()


def test_v22_vision_tool_look_delegates_to_vlm():
    # v22：配置 vision_model 后，look 把图像+问题路由给 VLM；主模型保持
    # "盲"状态（不带像素），下一轮只收到 VLM 的文字答案。
    import tempfile
    import core.loop as L
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/glm"; cfg.vision_model = "eyes/vlm"
    cfg.max_iters = 6; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    # 主模型 + 独立视觉模型。
    png = _tiny_png()
    calls = []
    script = ['{"look": {"path": "/home/user/vase.png", "question": "how many flowers?"}}',
              '{"done": null}']
    # Actor 脚本：先 look 后收尾。
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        kind = "VISION" if system == VISION_SYSTEM else "PRIMARY"
        calls.append({"model": model, "kind": kind, "img": image is not None, "user": user})
        if kind == "VISION":
            return "FAKE_VISION_ANSWER: I count 3 flowers. ALSO VISIBLE: a blue rim."
            # VLM 调用：返回视觉答案。
        return script.pop(0) if script else '{"done": null}'
        # 主模型调用：按脚本回复。
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return png, None
        # fetch_file 返回 PNG：模拟从 guest 抓图。
    import core.eyes as E                       # v30: the eyes call happens inside core.eyes
    # v30：eyes 的 chat 调用发生在 core.eyes 内。
    oc, oe = L.chat, E.chat; L.chat = E.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="v22_")))
        # 跑完整尝试。
    finally:
        L.chat, E.chat = oc, oe
        # 恢复两处 chat。
    vis = [c for c in calls if c["kind"] == "VISION"]
    assert vis and vis[0]["model"] == "eyes/vlm" and vis[0]["img"], "look didn't reach the VLM with an image"
    # look 确实带着图像到达 VLM。
    # the primary call AFTER the look must be BLIND and carry the VLM's text answer
    post = next(c for c in calls if c["kind"] == "PRIMARY" and "FAKE_VISION_ANSWER" in c["user"])
    assert not post["img"], "primary was handed pixels — should stay blind"
    # look 之后的主模型调用不带像素（保持盲）。
    assert "VISION TOOL REPORT" in post["user"]
    # 主模型收到的是 VISION TOOL REPORT 文字报告。
    assert not any(c["kind"] == "PRIMARY" and c["img"] for c in calls), "no primary call may carry an image"
    # 任何主模型调用都不得携带图像。
    print("v22 vision-tool look: PASS (VLM sees, primary blind, text delivered)")
    # 直跑模式的进度打印。


def test_v22_multi_path_look_preserves_every_requested_image():
    """No parser, fetch-loop, or imagery-stage ceiling may drop comparison paths."""
    # v22 多路径 look：解析器/抓取循环/图像阶段都不得丢弃任何对比路径——
    # 8 个请求路径全部被抓取、全部一次性送进 VLM。
    import json
    import tempfile
    import core.loop as L
    import core.eyes as E
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace

    cfg = load(None)
    cfg.model = "primary/glm"; cfg.vision_model = "eyes/vlm"; cfg.look_ensemble = 1
    cfg.max_iters = 3; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    # 单次 ensemble 路径。
    paths = [f"/home/user/frame_{i}.png" for i in range(8)]
    # 8 个请求路径。
    png = _tiny_png(); fetched = []; vision_counts = []
    script = [json.dumps({"look": {"paths": paths, "question": "compare all"}}),
              '{"done": null}']
    # Actor 脚本：8 路径 look → 收尾。

    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            vision_counts.append(len(image) if isinstance(image, list) else 1)
            return "all eight images compared"
            # 记录 VLM 每次收到的图像数。
        return script.pop(0) if script else '{"done": null}'

    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600):
            return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, path, max_bytes=None):
            fetched.append(path)
            return png, ""
            # 记录抓取顺序。

    old_loop_chat, old_eyes_chat = L.chat, E.chat
    L.chat = E.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg,
                      ArtifactSink(tempfile.mkdtemp(prefix="v22_multi_")))
    finally:
        L.chat, E.chat = old_loop_chat, old_eyes_chat

    assert fetched == paths, (fetched, paths)
    # 8 个路径按请求顺序全部被抓取（无丢弃）。
    assert vision_counts == [len(paths)], vision_counts
    # VLM 恰好一次收到全部 8 张图（无分批丢图）。


def test_v22_no_vision_model_legacy_attach():
    # v22 对照：未配置 vision_model 时走旧路径——图像直接挂在主模型调用上。
    import tempfile
    import core.loop as L
    from core.actor import VISION_SYSTEM
    from config.settings import load
    from core.trace import ArtifactSink
    from env.vm import Trace
    cfg = load(None)
    cfg.model = "primary/m3"; cfg.vision_model = ""       # <- legacy
    # 未配置视觉模型：旧路径。
    cfg.max_iters = 6; cfg.independent_verify = False
    cfg.history_keep_pairs = 0; cfg.strict_one_action = False; cfg.wall_clock_secs = 3600
    png = _tiny_png(); calls = []
    script = ['{"look": {"path": "/home/user/x.png", "question": "what is shown?"}}', '{"done": null}']
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        calls.append({"kind": "VISION" if system == VISION_SYSTEM else "PRIMARY", "img": image is not None})
        return script.pop(0) if (system != VISION_SYSTEM and script) else '{"done": null}'
        # 主模型按脚本回复；VLM 系统调用返回 null。
    class VM:
        def run_command(self, cmd, timeout=30, **kw): return "PASS"
        def run_script(self, lang, code, timeout=600): return Trace(stdout="ok\n[exit 0]", exit_code=0, secs=0.1)
        def fetch_file(self, p, max_bytes=0): return png, None
    oc = L.chat; L.chat = fake_chat
    try:
        L.run_attempt("t", VM(), cfg, ArtifactSink(tempfile.mkdtemp(prefix="v22b_")))
    finally:
        L.chat = oc
    assert not any(c["kind"] == "VISION" for c in calls), "no VLM should be called when vision_model unset"
    # 未配置视觉模型：不调用 VLM。
    assert any(c["kind"] == "PRIMARY" and c["img"] for c in calls), "legacy: image must attach to the primary"
    # 旧路径：图像挂在主模型调用上。
    print("v22 legacy attach: PASS (no VLM, image on primary)")


def test_v22_1_vision_agent_multiround():
    # v22.1：vision_rounds>1 时眼睛成为 Agent——多轮 inspect（grid/crop）
    # 后提交一个自信答案；格式化答案（文本+置信度+另见）返回给主模型。
    import core.loop as L
    from core.actor import VISION_AGENT_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = 5
    # 最多 5 轮视觉 Agent。
    png = _tiny_png(); calls = []
    script = ['{"inspect": {"op": "grid", "rows": 3, "cols": 1, "note": "split vase"}}',
              '{"inspect": {"op": "crop", "region": [0,0,4,4], "note": "zoom upper"}}',
              '{"answer": {"text": "27 flowers", "confidence": "high", "also_visible": "blue rim"}}']
    # 视觉 Agent 脚本：grid → crop → answer。
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        calls.append({"model": model, "agent_sys": system == VISION_AGENT_SYSTEM, "img": image is not None})
        return script.pop(0) if script else '{"answer": {"text": "x", "confidence": "low"}}'
        # 按脚本走多轮。
    oc = L.chat; L.chat = fake_chat
    try:
        ans = L._vision_agent("eyes/vlm", "count the flowers", [png], cfg)
        # 直接驱动视觉 Agent。
    finally:
        L.chat = oc
    assert len(calls) >= 3, calls                              # multiple inspect rounds
    # 多轮 inspect 调用。
    assert all(c["model"] == "eyes/vlm" and c["agent_sys"] and c["img"] for c in calls)
    # 每轮都在视觉 Agent 系统提示下带图像调用。
    assert "27 flowers" in ans and "confidence: high" in ans and "ALSO VISIBLE" in ans, ans
    # 格式化答案含文本/置信度/另见。
    print("v22.1 vision agent: PASS (multi-round inspect->answer, formatted)")


def test_v22_1_final_demand_forces_answer():
    # v22.1：Agent 迟迟不提交答案时，最后一轮"预算耗尽"强令提取答案。
    import core.loop as L
    from config.settings import load
    cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = 3
    png = _tiny_png(); n = {"i": 0}
    def fake_chat(model, system, user, max_tokens=0, temperature=0.0,
                  reasoning_effort="", history=None, image=None, **_kw):
        n["i"] += 1
        if "budget exhausted" in user:                         # final demand
            # 最终强令轮。
            return '{"answer": {"text": "final count 12", "confidence": "low"}}'
        return '{"inspect": {"op": "full"}}'                   # inspect forever otherwise
        # 否则永远只做 inspect。
    oc = L.chat; L.chat = fake_chat
    try:
        ans = L._vision_agent("eyes/vlm", "count", [png], cfg)
    finally:
        L.chat = oc
    assert "final count 12" in ans, ans
    # 最终答案被提取出来。
    assert n["i"] == cfg.vision_rounds + 1, n                  # 3 rounds + 1 final demand
    # 恰 3 轮 + 1 次强令。
    print("v22.1 final-demand: PASS")


def test_v22_1_dispatch_rounds_gate():
    # v22.1 分发门：vision_rounds>1 走视觉 Agent；rounds<=1 走 v30 look
    # ensemble（ensemble=1 即单发路径，eyes 的 chat 在 core.eyes 内）。
    import core.eyes as E
    import core.loop as L
    from core.actor import VISION_AGENT_SYSTEM
    from config.settings import load
    png = _tiny_png()
    for rounds, want_agent in [(1, False), (4, True)]:
        # 两组参数：1 轮走 ensemble、4 轮走 Agent。
        cfg = load(None); cfg.vision_model = "eyes/vlm"; cfg.vision_rounds = rounds
        seen = {"agent": False, "ensemble": False}
        def fake_agent_chat(model, system, user, max_tokens=0, temperature=0.0,
                            reasoning_effort="", history=None, image=None, **_kw):
            seen["agent"] = system == VISION_AGENT_SYSTEM
            return '{"answer": {"text": "ok", "confidence": "high"}}'
            # Agent 路径的记录。
        def fake_eyes_chat(model, system, user, max_tokens=0, temperature=0.0,
                           reasoning_effort="", history=None, image=None, **_kw):
            seen["ensemble"] = True
            return "ok"
            # ensemble 路径的记录。
        oc, oe = L.chat, E.chat; L.chat = fake_agent_chat; E.chat = fake_eyes_chat
        try:
            L._vlm_look("eyes/vlm", "q", [png], None, cfg)
            # 直接驱动 look 分发。
        finally:
            L.chat, E.chat = oc, oe
        assert seen["agent"] == want_agent, (rounds, seen)
        # 分发与轮数设置一致。
        assert seen["ensemble"] == (not want_agent), (rounds, seen)
        # 互补路径同样正确。
    print("v22.1 dispatch gate: PASS (rounds>1 -> agent, <=1 -> ensemble single-shot)")


def test_v22_1_grid_cells():
    # v22.1：grid_cells 把图像切分成网格单元并返回编码图像；指定 cell 只回一个。
    from core.imagery import grid_cells
    png = _tiny_png()
    allc, note = grid_cells([png], 2, 2)
    assert len(allc) == 4 and all(isinstance(b, (bytes, bytearray)) for b in allc), note
    # 2×2 网格产出 4 个字节单元。
    one, note2 = grid_cells([png], 3, 1, cell=[1, 0])
    assert len(one) == 1, note2
    # 3×1 网格 + 指定单元：只回 1 个。
    print("v22.1 grid_cells: PASS")


def test_v24_vision_verifier():
    # v24：独立检查员发出 look 探针 → 眼睛看到交付物 → 能对文本探针无法
    # 检查的视觉需求下 wrong 判定（t063 修复）。
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 4
    # 检查员 + 视觉验证开启。
    png = _tiny_png(); eyes = {"n": 0}
    insp = ['{"look": {"path": "/x/render.png", "question": "do the tails point at the mouth?"}}',
            '{"verdict": "wrong", "findings": "the tails point away from the mouth per the visual read"}']
    # 检查员脚本：先 look 探针，再基于视觉读数下 wrong。
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:                        # the EYES
            # 眼睛调用。
            eyes["n"] += 1
            assert image is not None, "vision probe must send an image"
            # 视觉探针必须带图像。
            return "The speech-bubble tails point AWAY from the mouth."
            # 眼睛的观察结果。
        return insp.pop(0) if insp else '{"verdict": "unverified", "findings": "x"}'  # inspector
        # 检查员按脚本回复。
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "PASS"
        def fetch_file(self, path, max_bytes=4_000_000): return png, None
        # fetch_file 交出 PNG。
    import core.eyes as E                       # v30: the probe's eyes call lives in core.eyes
    oc, oe = V.chat, E.chat; V.chat = E.chat = fc
    try:
        verdict, findings = V.verify_independent("point the tails at the mouth", VM(), cfg)
        # 直接驱动独立验证。
    finally:
        V.chat, E.chat = oc, oe
    assert eyes["n"] == 1, "the eyes were not called for the look-probe"
    # look 探针确实调用了眼睛。
    assert verdict == "wrong", verdict
    # 视觉证据支撑 wrong 判定。
    assert "away" in findings.lower(), findings
    # findings 含视觉读数结论。
    print("v24 vision-verifier: PASS (inspector looked, eyes saw mismatch, verdict=wrong)")


def test_v24_vision_verify_off():
    # v24 对照：vision_verify=False 时 look-only 回复被忽略（催促重答），
    # 眼睛绝不被调用。
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = False
    cfg.verifier_probes = 2
    # 视觉验证关闭。
    seq = ['{"look": {"path": "/x.png", "question": "?"}}',
           '{"verdict": "unverified", "findings": "could not confirm"}']
    eyes = {"n": 0}
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            eyes["n"] += 1; return "x"
        return seq.pop(0) if seq else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "PASS"
        def fetch_file(self, path, max_bytes=4_000_000): return _tiny_png(), None
    import core.eyes as E                       # v30: probe eyes live in core.eyes
    oc, oe = V.chat, E.chat; V.chat = E.chat = fc
    try:
        V.verify_independent("t", VM(), cfg)
    finally:
        V.chat, E.chat = oc, oe
    assert eyes["n"] == 0, "eyes must NOT be called when vision_verify=False"
    # 关闭时眼睛零调用。
    print("v24 vision-verify OFF: PASS (look-probe ignored, no eyes call)")


def test_v27_stall_steer():
    # v27：眼睛提醒在"停滞"（连续 2+ 轮探针读不到内容）时触发，而不是靠
    # 指令措辞分类——本指令无任何视觉词汇，探针无产出 → 仍注入 steer。
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 6
    seen = []
    replies = ['{"probes": ["unzip -o /x/f.pptx"]}',            # round 1: empty -> no content
               '{"probes": ["unzip -o /x/f.pptx -d /tmp/y"]}',  # round 2: empty -> STALL
               '{"verdict": "unverified", "findings": "x"}']
    # 两轮空探针 → 停滞。
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000):
            if "__rsiagent_canary__" in cmd:          # v32.2: channel alive, output real
                # v32.2：通道金丝雀探测回显真实输出。
                return "__rsiagent_canary__"
            return ""                              # never any content
            # 探针永远读不到内容。
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("finish the task on the file", VM(), cfg)   # NO visual words
        # 指令无视觉词汇。
    finally:
        V.chat = oc
    j = "\n".join(seen).lower()
    assert "[inspector note]" in j and "eyes" in j, "v27 stall-steer not injected"
    # 停滞触发了 [inspector note] 的眼睛提醒。
    print("v27 stall-steer: PASS (fires on stall, no instruction classification)")


def test_v27_no_overfire():
    # v27 回归修复：指令用了视觉词汇（align/center）但探针成功读到内容
    # （2+ 轮）时绝不能触发 steer——证明 steer 不再分类指令（v25 的
    # _VISUAL_REQ_RE 曾在这类词上对 23/69 个任务误触发）。
    import core.verifier as V
    from core.actor import VISION_SYSTEM
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = "eyes/v"; cfg.vision_verify = True
    cfg.verifier_probes = 4
    seen = []; eyes = {"n": 0}
    replies = ['{"probes": ["cat /x/report.csv"]}',            # content-read (round 1)
               '{"probes": ["grep total /x/report.csv"]}',     # content-read (round 2)
               '{"verdict": "pass", "findings": "aligned columns and centered title verified"}']
    # 两轮内容读成功 → 通过。
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        if system == VISION_SYSTEM:
            eyes["n"] += 1; return "x"
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "col1,col2\n1,2\n3,4"
        # 探针返回真实 CSV 内容。
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("align the columns and center the title", VM(), cfg)  # visual WORDS
        # 指令带视觉词汇。
    finally:
        V.chat = oc
    assert "[inspector note]" not in "\n".join(seen).lower(), "v27 over-fired on visual words"
    # 视觉词汇 + 探针成功：不注入 steer。
    assert eyes["n"] == 0, "eyes called on a text task that read content fine"
    # 内容读取顺利的文本任务不调用眼睛。
    print("v27 no-overfire: PASS (visual words + successful probes -> no steer)")


def test_v25_no_steer_when_blind():
    # v25：视觉关闭（无 vision_model）→ 没有眼睛 → steer 绝不触发
    # （没有可指向的工具）。
    import core.verifier as V
    from config.settings import load
    cfg = load(None); cfg.model = "insp/m"; cfg.vision_model = ""; cfg.vision_verify = True
    cfg.verifier_probes = 4
    seen = []
    replies = ['{"probes": ["ls /home/user"]}',
               '{"verdict": "unverified", "findings": "x"}']
    def fc(model, system, user, max_tokens=0, temperature=0.0, reasoning_effort=None,
           history=None, image=None, **_kw):
        seen.append(user)
        return replies.pop(0) if replies else '{"verdict": "unverified", "findings": "x"}'
    class VM:
        def run_command(self, cmd, timeout=30, cap=4000): return "out"
        def fetch_file(self, path, max_bytes=4_000_000): return b"", "n/a"
    oc = V.chat; V.chat = fc
    try:
        V.verify_independent("make the tail point to his mouth", VM(), cfg)
        # 指令带视觉措辞但视觉关闭。
    finally:
        V.chat = oc
    assert "[inspector note]" not in "\n".join(seen).lower(), "steer fired with no eyes"
    # 无眼睛时不注入 steer。
    print("v25 no-steer-when-blind: PASS (no vision_model -> no steer)")


def test_v26_autorender_image_passthrough():
    # v26 自动渲染：图像路径原样直传——绝不发渲染命令。
    from core.imagery import fetch_look_image
    calls = {"run": 0}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            calls["run"] += 1; return ""
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"PNGDATA", ""
    d, e = fetch_look_image(VM(), "/x/shot.PNG")          # case-insensitive
    # 后缀大小写不敏感。
    assert d == b"PNGDATA" and e == "", (d, e)
    assert calls["run"] == 0, "an image path must not trigger a render"
    # 零渲染命令。
    print("v26 autorender image-passthrough: PASS")


def test_v26_autorender_pptx():
    # v26：文档 → harness 经 soffice 渲染并抓取产出的 PNG。
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/rsiagent_render/SiriDemo.png\n"
            # 渲染命令回显产出路径。
        def fetch_file(self, path, max_bytes=4_000_000):
            seen["fetched"] = path; return b"RENDERED", ""
    d, e = fetch_look_image(VM(), "/home/user/Desktop/SiriDemo.pptx")
    assert d == b"RENDERED" and e == "", (d, e)
    # 渲染产物被抓回。
    assert "soffice" in seen["cmd"] and "convert-to png" in seen["cmd"], seen["cmd"]
    # 命令确实是 soffice 转 PNG。
    assert seen["fetched"] == "/tmp/rsiagent_render/SiriDemo.png", seen
    # 抓取的正是回显路径。
    print("v26 autorender document(pptx): PASS")


def test_v26_autorender_pdf():
    # v26：PDF 走 pdftoppm 分支。
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/rsiagent_render/page-1.png"
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"PDFPAGE", ""
    d, e = fetch_look_image(VM(), "/docs/report.pdf")
    assert d == b"PDFPAGE" and e == "", (d, e)
    assert "pdftoppm" in seen["cmd"], seen["cmd"]
    # 命令是 pdftoppm。
    print("v26 autorender document(pdf): PASS")


def test_v26_autorender_failure():
    # v26：渲染没有产出（soffice 缺失/失败）→ 优雅返回 (None, err)。
    from core.imagery import fetch_look_image
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            return ""                                     # no PNG path echoed
            # 没有回显任何 PNG 路径。
        def fetch_file(self, path, max_bytes=4_000_000):
            return b"x", ""
    d, e = fetch_look_image(VM(), "/x/thing.pptx")
    assert d is None and "render" in e.lower(), (d, e)
    # 数据为空、错误含 render。
    print("v26 autorender render-failure: PASS")


def test_v29_screen_capture():
    # v29：fetch_look_image("screen:") 用已知可靠的命令捕获实时屏幕并返回
    # PNG 字节——会话验证修复（t021 驱动了 Web 应用却得 0 分，因为验证者
    # 只会探文件系统）。
    from core.imagery import fetch_look_image
    seen = {}
    class VM:
        def run_command(self, command, timeout=30, cap=4000):
            seen["cmd"] = command; return "/tmp/rsiagent_render/screen.png"
        def fetch_file(self, path, max_bytes=4_000_000):
            seen["fetched"] = path; return b"SCREENPNG", ""
    d, e = fetch_look_image(VM(), "screen:")
    assert d == b"SCREENPNG" and e == "", (d, e)
    # 捕获到屏幕 PNG。
    assert "DISPLAY=:0" in seen["cmd"] and "screen.png" in seen["cmd"], seen["cmd"]
    # 命令带默认显示号与输出路径。
    assert seen["fetched"] == "/tmp/rsiagent_render/screen.png", seen
    # 抓取的正是产出路径。
    # explicit display
    d2, _ = fetch_look_image(VM(), "screen:1")
    assert "DISPLAY=:1" in seen["cmd"], seen["cmd"]
    # 显式显示号传入命令。
    print("v29 screen-capture: PASS (screen: -> live capture -> eyes)")


def test_v29_screen_capture_fail():
    # v29：无显示/无工具 → 优雅返回 (None, err)，绝不崩溃。
    from core.imagery import fetch_look_image
    class VM:
        def run_command(self, command, timeout=30, cap=4000): return ""   # nothing produced
        # 没有产出。
        def fetch_file(self, path, max_bytes=4_000_000): return b"x", ""
    d, e = fetch_look_image(VM(), "screen:")
    assert d is None and "screen" in e.lower(), (d, e)
    # 数据空、错误提及 screen。
    print("v29 screen-capture fail: PASS (no display -> graceful error)")


def test_v29_screen_capture_rejects_display_shell_injection():
    # v29：显示号参数注入 shell 命令（"screen:0; touch ..."）必须被拒绝
    # ——不执行任何命令、返回错误。
    from core.imagery import fetch_look_image
    class VM:
        def __init__(self): self.commands = []
        def run_command(self, command, timeout=30, cap=4000):
            self.commands.append(command); return "/tmp/rsiagent_render/screen.png"
        def fetch_file(self, path, max_bytes=4_000_000): return b"x", ""
    vm = VM()
    data, error = fetch_look_image(vm, "screen:0; touch /tmp/verifier-wrote")
    assert data is None and "display" in error.lower(), (data, error)
    # 注入被拒：数据为空、错误提及 display。
    assert vm.commands == []
    # 没有任何命令被执行。


if __name__ == "__main__":
    # 直跑模式：按名字序执行全部 test_ 函数（无 pytest 依赖的冒烟入口）。
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_parsing: OK")
