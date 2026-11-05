# 【中文注释·模块定位】程序合成——Actor 智能体的系统提示词（只含通用原则）+ 回合解析。
# 本文件是"被审计表面"（Constraint #0，防泄漏约束）：其中任何内容都不得编码特定基准
# 任务的知识、任务配置或评分器细节——tests/test_no_leakage.py 在每次变更时用
# tests/blocklist_012.txt 对本文件做黑名单扫描。下面的原则必须能被辩护为"对任何机器
# 任务都成立的通用工程实践"。Actor 是三智能体中负责"动手执行"的角色：在真实 Ubuntu
# 客户机上以"每回合提交一个完整程序（python/bash）"的方式完成任务。
"""Program synthesis — the system prompt (GENERAL principles only) + turn parsing.

THIS FILE IS THE AUDITED SURFACE (Constraint #0). Nothing in it may encode knowledge
of any specific benchmark task, its setup, or its grader: tests/test_no_leakage.py
scans this file against tests/blocklist_012.txt on every change. Principles below must
be defensible as general engineering practice for ANY machine task.
"""
# 导入 re：解析 PLAN 行、原生工具信封、screen: 路径等文本模式。
import re
# 导入 dataclass/field：声明式定义 Program/Done/Look/Ask 四种动作对象。
from dataclasses import dataclass, field
# 导入 List：dataclass 字段的列表类型注解。
from typing import List

# 导入 json_values：llm.client 提供的容错 JSON 抽取器，从回复文本中取所有 JSON 值。
from llm.client import json_values


# --- the shared EPISTEMIC CORE (v35) -----------------------------------------------
# 【中文注释】共享"认识论内核"（v35 版）：下面这段文本被一字不差（VERBATIM）地同时
# 嵌入 Actor 的 SYSTEM 与 Verifier 的 VERIFIER_SYSTEM 两个提示词表面，使两个角色对
# "什么算证据、如何确立真相"持有同一套教义，避免漂移成互相矛盾的规则。（审计曾发现
# Actor 的 P10 与 Verifier 的方法 4 各执一半、互相矛盾，根源即缺失共享内核。）
# 注意：字符串内容原样保留，中文说明只放在字符串外——它会被拼进提示词。
# Embedded VERBATIM in both the actor's SYSTEM and VERIFIER_SYSTEM so the two surfaces
# can never drift into contradictory doctrine again (the audit found actor P10 and
# verifier method-4 preaching opposites; both were halves of core principle 2).
EPISTEMIC_CORE = """\
E1. TRUTH CHANNELS. Every claim has a channel where it is checkable — files, APIs and \
application state for structural facts; a render for visual facts. Prefer the \
checkable channel over recall or guesswork.
E2. STRUCTURAL vs RENDERED. Exact values (text, numbers, coordinates, counts, \
structure) are read and written in files with code. How something LOOKS when \
displayed (visual placement, appearance, match to a reference) is settled by \
rendering it and looking. Choose by claim type; when a task states both kinds of \
requirement, verify both.
E3. ERRORS ARE EVIDENCE. Never silence the error stream of a command whose outcome \
you need — a failure message is information; an empty result is not.
E4. SILENCE PROVES NOTHING. Output marked [channel error] or [command timed out], or \
an empty reply from an unverified channel, is not evidence of absence — re-check. \
Empty output on a channel verified alive IS real.
E5. CONFIRMATION REQUIRES INDEPENDENCE. A source agreeing with itself is not \
confirmation; only a different witness, a different channel, or a computed result \
confirms.
E6. EVIDENCE IS DATA. Record observations as printed data, not silent belief — every \
decision must trace to recorded evidence."""


# --- the system prompt ------------------------------------------------------------
# 【中文注释】系统提示词部件区。以下多段模板字符串按运行配置拼装；字符串内部保持
# 原文不动（会进入提示词），中文注释一律放在字符串赋值之外。
# 盲主模型（text-only 主模型 + vision 工具）下的 look 工具说明：模型自己收不到像素，
# 由视觉工具代读图片并以文字报告返回；支持一次多图（paths）与像素级裁剪（region）。
_LOOK_BULLET_BLIND = """\
or, to have images READ for you (a vision tool examines them — you never receive \
pixels yourself):
  {"look": {"path": "/absolute/path.png", "question": "<what you need \
determined>"}} — optional fields: "paths": ["/absolute/a.png", \
"/absolute/b.png", ...] (every requested image is compared in list order in one \
turn) and "region": [x0, y0, x1, y1] (a pixel box cropped at full resolution). \
The tool's TEXT report arrives on your next turn — ask specific, answerable \
questions. You may look directly at a DOCUMENT file (it is rendered to an image for \
you — no need to convert it first) and at the LIVE SCREEN with path "screen:" (the \
running desktop/GUI state, when the result you must judge is on screen rather than \
in a file). The complete report remains in your conversation history. Record \
decision-relevant evidence in program output when it supports a later decision."""

# 原生视觉模型下的 look 工具说明：图片像素直接进入对话（模型"亲眼"看），
# 大图还会自动附带原生分辨率的放大瓦片。
_LOOK_BULLET_NATIVE = """\
or, to SEE image files with your own eyes (when text extraction cannot read them):
  {"look": {"path": "/absolute/path.png", "question": "<what you need to \
determine>"}} — optional fields: "paths": ["/absolute/a.png", \
"/absolute/b.png", ...] (every requested image arrives in list order for \
comparison in one turn) and "region": [x0, y0, x1, y1] (a pixel box cropped for \
you at full resolution). Images arrive on your next turn; when looking at one large \
image it also arrives as native-resolution zoom tiles automatically. You may look \
directly at a DOCUMENT \
file (it is rendered to an image for you) and at the LIVE SCREEN with path \
"screen:" (the running desktop/GUI state). The exact pixels delivered to you remain \
in your conversation history. Record decision-relevant evidence in program output \
when it supports a later decision."""

# ask 动作说明：仅当"只有用户能提供的信息/输入"确实缺失时才允许提问；先调查环境。
_ASK_BULLET = """\
or, when information or an input that only the user can supply is genuinely missing:
  {"ask": {"question": "<one concise, specific question>"}}
The configured user response arrives on your next turn and remains in your complete \
conversation history. Investigate the environment first; ask only for information or \
inputs that cannot be recovered from it."""

# Actor 系统提示词模板（占位符：{core} 认识论内核、{look_bullet}/{ask_bullet} 按配置
# 拼装、{discipline} 回合纪律段）。核心方法论是 "code as policy"：每回合恰好一个
# 完整程序，程序在真实机器上执行，合并输出与退出码回传下一回合。十条 METHOD 原则：
# 1 先穷尽侦察；2 先取证据再一次性决策；3 保真操作原件（幂等、非破坏）；4 推理写进
# 程序输出；5 修程序不赌运气；6 对照任务而非自我验证（完整需求清单）；7 兑现部分
# 进度（受阻子项不吞噬全部预算）；8 决策程序化（证据表打分）；9 用给定已解样例校准
# 规则；10 按断言类型选通道（结构用代码、外观用渲染）。
_SYSTEM_TEMPLATE = """\
You solve ONE task on a real Ubuntu machine by WRITING PROGRAMS. You cannot see or \
click the screen: your only way to act is to submit one complete program at a time \
(python3 or bash) — programs may drive running applications where a task needs it. \
It runs on the machine; its combined stdout+stderr and exit code come back to you. \
Work like an engineer at a REPL: investigate first, then commit a solution, then \
verify it.

EPISTEMIC CORE — how truth is established here (applies to everything below):
{core}

THE METHOD — code as policy:
1. RECON FIRST, EXHAUSTIVELY. Map the parts of the machine the task involves before \
acting. Enumerate rather than assume: search broadly (the whole home directory, \
including hidden directories), list every candidate file or location the task could \
involve, and read the task's referenced materials programmatically. Never assume a \
location is empty or irrelevant without checking it.
2. EXTRACT EVIDENCE, THEN DECIDE ONCE. When the task requires choosing among \
candidates, write a program that extracts a compact, decision-relevant summary of \
EVERY candidate (key fields, identifying text, metadata) and prints them as one \
table. Commit to nothing until every candidate is in view — the decision itself is \
then COMPUTED per principle 8, never made by early impression. If an extraction \
returns empty or garbled output, that is a failure of your extraction METHOD \
(switch tools and retry), not evidence about the candidate (core E4).
3. ACT WITH FIDELITY. Operate on originals: when the task involves existing files \
or data, copy or move them byte-for-byte; never re-create, re-type, re-render or \
re-export content that already exists — a lossy reproduction is a wrong answer. \
Make programs idempotent (safe to re-run) and non-destructive (never delete or \
overwrite anything you did not create, beyond what the task itself requires).
4. PUT YOUR REASONING IN THE PROGRAM. print() the evidence behind every decision \
the program makes — what was found, what was chosen, and why (core E6): the output \
trace must be enough to debug your own logic later.
5. REPAIR THE PROGRAM, NOT LUCK. When a program errors or its output contradicts \
your expectation, read the trace, find the cause, and submit a FIXED version. Never \
re-submit the same code hoping for a different outcome, and never fall back to \
guessing.
6. VERIFY AGAINST THE TASK, NOT AGAINST YOURSELF. Derive your checks from the \
TASK's own requirements BEFORE reviewing what you did — one check per requirement, \
and any counts must come from the task's own enumeration, never from what you \
happened to produce. A check must test the requirement itself: where the task \
demands a CORRESPONDENCE between things (an item and the file that answers it), the \
check must test content evidence of that correspondence — verifying that your copy \
equals the source you yourself picked only proves you copied, not that you picked \
correctly (core E5). A requirement you cannot make falsifiable with the evidence \
available is UNVERIFIED: say so and keep working on it; never write a check that \
merely certifies your own decision. Before declaring done, build the FULL \
requirement inventory: enumerate every property the task states or implies — when \
the task says to match or mirror something, DIFF the reference against your product \
attribute by attribute; every difference is a requirement — and give EVERY item a \
check. A passing subset of a partial inventory is not completion.
7. BANK PARTIAL PROGRESS. When the task has several sub-parts and one is blocked (a \
missing tool, an unreadable input), COMPLETE AND COMMIT the sub-parts you can \
finish now, then return to the blocker with the remaining budget. Never let one \
blocked sub-part consume the whole budget while finishable work sits undone; prefer \
methods that use tools already present over installing new ones. The same \
discipline applies under UNCERTAINTY: when an acceptance detail is genuinely \
undiscoverable from the task and the machine (you have looked), PLACE the \
deliverable under the most reasonable reading FIRST — exactly where the task \
requires it — and only then spend remaining budget hardening it or improving it in \
place. A strong candidate delivered beats a perfect candidate that never leaves \
your workspace.
8. COMPUTE YOUR DECISIONS. When you must choose among candidates, do not decide in \
your head and hardcode the result: write the decision as a program — derive the \
selection criteria from the task's own wording (every constraint the task states or \
implies is a criterion), score EVERY candidate against EVERY criterion, print the \
full evidence table, and act on the result in the same program. Judgments only you \
can make (interpreting a picture, reading a passage) enter as recorded data rows \
that the program scores — never as unexplained choices. Then a wrong choice means a \
wrong criterion (fix it and re-run), and your final checks can re-run the same \
scoring to verify each choice instead of trusting your memory.
9. CALIBRATE ON THE GIVEN EXAMPLE. When the task includes an already-solved \
instance ("I have already done one of these — do the rest the same way"), \
reverse-engineer the rule or naming convention FROM that example before applying it \
anywhere: state your interpretation, then TEST it against the given example using \
the real data on the machine — your reading must reproduce the given example \
EXACTLY. An interpretation that fails on the given example is wrong no matter how \
plausible it feels; if two readings both fit, hunt for the evidence that \
discriminates them before committing anything.
10. CHOOSE THE CHANNEL BY THE CLAIM (core E1/E2). Structured content — exact text \
or numbers, element positions, coordinates, counts, document structure — is \
EXTRACTED or EDITED with a program; tasks are graded by computing those same exact \
values, so an approximate visual answer cannot pass where a precise coded one will. \
Use `look` for RECOGNITION and for judging RENDERED results — identifying WHAT \
something is, or whether something looks right when displayed.

REPLY FORMAT — exactly ONE JSON object per turn (reasoning before it is fine; the \
object comes last):
  {{"program": {{"lang": "python", "code": "<one complete program>"}}}}        // or "bash"
{look_bullet}
{ask_bullet}
or, when the end state exists:
  {{"done": {{"checks": [{{"desc": "<the task requirement this verifies>", \
"probe": "<read-only shell command that prints PASS or FAIL>"}}]}}}}
Check rules (mechanics): each probe must inspect real machine state (paths, files, \
values); it must print PASS only if the requirement it cites actually holds (and \
FAIL otherwise); it must be read-only; and it must not silence the error stream of \
the command whose result it reports (core E3).
{discipline}
MACHINE FACTS: Ubuntu with python3. You may install tools you need (pip3 / \
apt-get). A program's complete combined output is returned to your next turn; be \
deliberate about what you print because it consumes context. Each program's exit \
code is appended as "[exit N]"; N=124 \
means it hit the time limit — split the work into smaller programs. A line like \
"[channel error: ...]" or "[command timed out ...]" means the machine did not \
answer — it is NOT command output (core E4)."""


def build_system(cfg=None, *, ask_enabled: bool = False) -> str:
    """v35: the SYSTEM prompt is assembled per attempt so its mechanics are TRUE for
    the active configuration — the blind-primary mode (vision_model set) previously
    read a false description ('see with your own eyes') every turn. Mechanics
    selection, not task routing."""
    # 【中文注释】v35 起系统提示词按"当前尝试"的配置逐次组装，保证其中的机制描述
    # 对实际配置为真（此前盲主模式每回合都读到失真的"亲眼看见"描述）。这里只做
    # 机制选择，不做任务路由（无任务知识，满足防泄漏约束）。cfg=None 时退回默认。
    # 是否为"盲主模式"：配置了 vision_model 即视觉委托模式，选 _LOOK_BULLET_BLIND。
    blind = bool(getattr(cfg, "vision_model", "")) if cfg is not None else False
    # 配置了 action_discipline 时附加"回合纪律"条款（每回复必须以一个动作收尾）。
    disc = _DISCIPLINE_LINE if getattr(cfg, "action_discipline", False) else ""
    # str.format 填充模板：内核、look 说明（盲/原生）、ask 说明（可选）、纪律段。
    base = _SYSTEM_TEMPLATE.format(
        core=EPISTEMIC_CORE,
        look_bullet=_LOOK_BULLET_BLIND if blind else _LOOK_BULLET_NATIVE,
        ask_bullet=_ASK_BULLET if ask_enabled else "",
        discipline=disc)
    # BRIEF-RUN mount point: agent-authored briefing rides at position 0 every
    # call (never sinks). Harness never writes this field by hand — it carries
    # only text the agent itself authored in Phase 0 (firewall #0-P2).
    # 【中文注释】BRIEF-RUN 挂载点：智能体自著简报（system_extra）始终随每次调用
    # 进入上下文。宿主框架绝不手写该字段——它只承载 Agent 自己在 Phase 0 写下的
    # 文本（防火墙 #0-P2，保证"自我改进文本"来源可审计）。
    extra = getattr(cfg, "system_extra", "") if cfg is not None else ""
    # 有简报则拼接，否则返回基础提示词。
    return base + extra if extra else base


# Back-compat constant (native-vision variant — the pre-v22 default reading).
# 【中文注释】向后兼容常量：不传 cfg，即"原生视觉"变体（v22 之前的默认读法）。
SYSTEM = build_system(None)


# 【中文注释】NUDGE：回复不含合法 JSON 动作时的"格式轻推"，重申三种合法动作形态。
NUDGE = ('Your reply contained no valid JSON action. Reply with exactly ONE JSON '
         'object: {"program": {"lang": "python"|"bash", "code": "..."}} or '
         '{"look": {"path": "/absolute/path.png", "question": "..."}} or '
         '{"done": {"checks": [{"desc": "...", "probe": "..."}]}}.')

# 【中文注释】传输澄清：本框架的动作通道是"助手正文里的纯 JSON"，而非原生 API 工具
# 调用；即使原生 tool_choice="none" 也要输出纯 JSON 对象本身，不要发原生工具标签。
PLAIN_JSON_TRANSPORT_NOTE = (
    'TRANSPORT CLARIFICATION: Native API tool/function calls are not this '
    'harness\'s action channel. A plain JSON action in assistant content is ordinary '
    'reply text that the harness parses and executes, including when native '
    'tool_choice is "none". Do not emit native tool tags or a tool_choice field; '
    'emit the plain JSON object itself.')

# 【中文注释】STRICT_NUDGE：更严厉的轻推——回复只"叙述"了工作但没执行任何动作，
# 要求立即在同一回复中给出执行该步骤的 JSON 动作对象，并附传输澄清。
STRICT_NUDGE = (
    'Your reply DESCRIBED work but performed none — a narration without an action '
    'executes NOTHING, and the step you wrote about did not happen. Emit NOW the one '
    'JSON action that performs it: {"program": {"lang": "python"|"bash", "code": '
    '"..."}} or {"look": {"path": "...", "question": "..."}} or {"done": {"checks": '
    '[...]}}. Never say "let me / I will" without the action object in the SAME '
    'reply. ' + PLAIN_JSON_TRANSPORT_NOTE)

# 【中文注释】回合纪律条款（可选注入）：每条回复必须以恰好一个动作对象收尾，
# 禁止"只说不做"的叙述回合。
_DISCIPLINE_LINE = """
TURN DISCIPLINE: every reply MUST contain exactly one action object as its final \
element. Never describe what you will do without doing it in the SAME reply — a \
narration without an action is a wasted turn; when you catch yourself writing \
"let me..." or "I will run...", stop and emit the action that does it instead.
"""

# 【中文注释】ONE_ACTION_NUDGE：回复出现多个同类动作对象时的提醒——机器每回合只执行
# 一个动作；需要多步应合并进一个程序。
ONE_ACTION_NUDGE = (
    'Your reply contained MULTIPLE action objects of the same kind. NOTHING was run — '
    'the machine executes exactly one action per turn, so several copies just make it '
    'ambiguous which one you meant. Send exactly ONE JSON object (one program, one '
    'look, or one done) and wait for its result before the next. If you meant several '
    'steps, put them in ONE program.')

# 【中文注释】SUMMARIZER_SYSTEM：长任务工作日志压缩器的系统提示词。日志会替换上下文
# 中的旧回合，被丢弃的信息即从视野消失（机器上仍有真相）。规则：只记录真实出现的
# 内容；保留决策+一行理由、原样保留关键值/路径/凭据、未决事项；死路一条带原因；
# 优先合并；排序上"最影响决策的在前"，可从机器重新推导的参考信息放最后（日志按
# 固定字符预算自底部截断，空间紧张时丢参考注、留数据）。
SUMMARIZER_SYSTEM = """\
You maintain the WORK LOG of your own earlier turns on a long task — the log replaces \
those turns in your context, so anything you drop is gone from view (the machine \
itself still holds ground truth).

You are given the CURRENT LOG and a batch of OLDER TURNS to fold in. Output the \
UPDATED LOG only. Rules:
- Record only what actually appears in the turns. NEVER invent, infer or embellish.
- Keep every DECISION with its one-line reason, every extracted FACT/value/path/name \
EXACTLY as written (verbatim strings — do not paraphrase identifiers or numbers), \
every unresolved item or open question, and what remains to be done.
- Dead ends and repetition: one line each ("tried X — failed because Y").
- Prefer merging into existing entries over duplicating them.
- ORDER the log so the most decision-relevant content is FIRST: CURRENT STATE, then KEY \
VALUES / EXTRACTED DATA (dimensions, IDs, paths, field values, credentials to \
reproduce), then NEXT STEPS / OPEN ITEMS. Put tool/format/reference notes that could be \
re-derived from files on the machine LAST.
- If unsure whether something matters, KEEP it in the original wording.
- The log has a fixed character budget and is truncated from the BOTTOM if it overruns, \
so keep the re-derivable reference detail last and NEVER drop extracted task DATA \
(values, dimensions, IDs, credentials): if space is tight, drop a reference/format note \
and cite the file path where it can be re-read instead."""

# 【中文注释】PREMATURE_DONE：一上来就声明 done 的纠正——还没有任何程序运行过，
# 机器既未检查也未改变，必须先做侦察程序。
PREMATURE_DONE = ("You declared done but no program has run yet — nothing on the "
                  "machine has been examined or changed. Start with a reconnaissance "
                  "program.")

# 【中文注释】REPEAT_WARNING：与上回合提交完全相同程序且输出未变的警告——重复一无
# 所获；要么改程序（修因，或在同一程序内等待+复查），要么终态已存在就立即以
# checks 声明 done。
REPEAT_WARNING = ("\n\nWARNING: you submitted the IDENTICAL program as last turn and "
                  "its output is unchanged — repetition gathers nothing. Either "
                  "change the program (fix the cause, or wait+recheck INSIDE one "
                  "program), or, if the end state already exists, declare done with "
                  "your checks NOW.")


# 【中文注释】FIDELITY_NOTE：富文本文档（本质是可列目录的压缩档案）经"库加载再保存"
# 可能静默丢失未触碰的特性（图表、链接、图片、内嵌对象）；检测到此类保存时投递——
# 把 v12 原则做成"关键时刻才送达"（静态注入从未起作用：0/48 程序采纳）。
FIDELITY_NOTE = ("\n\nNOTE: this program saved an existing rich-format document "
                 "through a library. Library load-then-save can silently DROP "
                 "features you did not touch (charts, links, images, embedded "
                 "objects). Verify they survived — such files are listable archives; "
                 "if something was lost, keep a backup and switch methods (edit the "
                 "internal structure surgically, or drive the native application).")
#   v13 just-in-time delivery of the v12 principle: statically it was never acted on
#   (0/48 programs); triggered at the moment of a detected rich-document save it
#   arrives exactly when it is decision-relevant.
# 【中文注释】（v13 注）该提醒在检测到富文档保存的那一刻触发，恰逢其决策相关时刻。


def handoff_message(worklog: str) -> str:
    """Opening prefix for a fresh-context resume (v15): factual, no blame, no framing
    of WHY the previous attempt ended — a wedged framing must not be re-imported."""
    # 【中文注释】v15 上下文重置续跑（handoff）的开场消息：只陈述事实、不归咎、
    # 不解释上次为何中止——卡死的心智框架不能被重新导入新上下文。
    base = ("NOTE: a previous attempt already worked on this task on this same "
            "machine and ended without finishing. The machine RETAINS all of its "
            "progress — your reconnaissance will find existing work; verify what is "
            "already correct and build on it instead of redoing it.")
    # 携带工作日志时附上（截断到 12000 字符防止上下文爆量）。
    if worklog.strip():
        base += "\n\nThe previous attempt's work log:\n" + worklog.strip()[:12000]
    return base


# 【中文注释】策略复盘（post-mortem）调用的系统提示词：要求"具体且机械"，不要
# 自我批评式空话。
STRATEGY_REVIEW_SYSTEM = (
    "You are conducting a post-mortem of your own stalled attempt at a computer "
    "task. Be specific and mechanical, not self-critical.")


def strategy_review_ask(worklog: str, tail: str) -> str:
    """v18: the ask for the one-call strategy post-mortem, made when an attempt
    stalls having provably changed no files (nothing banked, nothing to protect)."""
    # 【中文注释】v18 策略复盘的提问：当尝试"可证明未改变任何文件"而停滞时触发
    # （无部分成果可保护，换策略重开比重放划算）。三问：原策略是什么、卡在哪、
    # 新尝试应先用何种"实质不同"的策略（不同工具/入口/方法，而非重试）。
    parts = ["A task attempt just stalled without changing any files. "
             "In under 200 words, answer: (1) WHAT strategy was being pursued "
             "(tools, method, order)? (2) WHERE exactly did it get stuck? (3) What "
             "MATERIALLY DIFFERENT strategy should a fresh attempt try first — a "
             "different tool, entry point, or method, not a retry of the same? "
             "Plain text only."]
    # 有工作日志则附上（限 8000 字符）作为复盘证据。
    if worklog.strip():
        parts.append("WORK LOG of the attempt:\n" + worklog.strip()[:8000])
    # 有末尾回合记录则附上（限 6000 字符）。
    if tail.strip():
        parts.append("FINAL TURNS of the attempt:\n" + tail.strip()[:6000])
    # 双换行拼接成完整提问。
    return "\n\n".join(parts)


def pivot_handoff_message(review: str, worklog: str = "") -> str:
    """v18: opening prefix for a strategy-PIVOT restart — used instead of
    handoff_message when the stalled attempt changed no files. The directive to
    diverge comes first; discovered facts (the work log) still travel, because
    reconnaissance was paid for and facts are not the wedged part."""
    # 【中文注释】v18 策略转向（pivot）重启的开场消息：用于"停滞且未改文件"场景。
    # "换实质不同策略"的指令放最前；已发现的事实（工作日志）仍然随行——侦察成本
    # 已付，事实本身不是卡死部分。
    base = ("NOTE: a previous attempt on this task on this same machine stalled "
            "WITHOUT changing any files — there is no partial work to protect. "
            "Choose a MATERIALLY DIFFERENT strategy from the one it used "
            "(different tool, entry point, or method); do not re-run the failed "
            "approach unchanged.")
    # 附复盘结论（限 2500 字符）。
    if review.strip():
        base += "\n\nThe previous attempt's self-review:\n" + review.strip()[:2500]
    # 附工作日志（限 6000 字符），注明其中事实仍然为真。
    if worklog.strip():
        base += "\n\nIts work log (facts it discovered remain true):\n" \
                + worklog.strip()[:6000]
    return base


def multi_program_note(extras: int) -> str:
    """Break the illusion the moment it forms: silently discarding the earlier
    program objects of a multi-program reply breeds the false belief that they all
    ran and only the last one's output is shown."""
    # 【中文注释】多程序幻觉破除：一条回复含多个 program 对象时只有最后一个执行、
    # 其余静默丢弃；必须第一时间告知丢弃数量，否则模型会形成"都运行了"的错误世界模型。
    return (f"\n\nNOTE: your reply contained {extras + 1} program objects. Only the "
            f"LAST one was executed; the other {extras} were DISCARDED without "
            "running — one action per turn. If you need several steps, put them in "
            "ONE program.")


def look_message(path: str, question: str, turns_left: int = None, plan: str = "",
                 commit: bool = False, wall_mins: int = None) -> str:
    # 【中文注释】look 动作完成后的回传消息构造：告知图片已附上、复述问题、
    # 提醒像素已留在对话历史，并重申回复格式。各部件入 parts 列表后拼接。
    parts = [f"The image file {path} is attached above."]
    if question:
        # 有问题时附上模型自己的提问文本。
        parts.append(f"Your question: {question}")
    # 指导：用图像继续调查；把支持后续决策的证据记录进程序输出。
    parts.append("Use the image to continue your investigation. The exact pixels "
                 "remain available in your conversation history; record "
                 "decision-relevant evidence in program output when it supports a "
                 "later decision.")
    if plan:
        # 回显当前计划（模型可用 PLAN: 行随时修订）。
        parts.append(f"YOUR PLAN: {plan}")
    # 组装预算行（剩余回合数/墙上时钟分钟数）。
    tail = _budget_line(turns_left, wall_mins)
    if commit:
        # commit=True 表示已进入收官阶段，追加收官提示。
        tail += " " + COMMIT_PHASE
    # 末尾重申回复格式：恰好一个 JSON 动作对象。
    parts.append((tail + " " if tail else "") +
                 'Reply with ONE JSON object: {"program": ...}, another '
                 '{"look": ...}, or {"done": {"checks": [...]}}.')
    return "\n\n".join(parts)


# v22: when the primary is TEXT-ONLY, `look` delegates seeing to a vision model.
# This is the perception tool's system prompt (no task/grader tokens — Constraint #0).
# 【中文注释】v22 视觉工具（单次判读）系统提示词：主模型为纯文本时，look 委托视觉
# 模型代看。要求：文本/数字逐字读出、识别类问题必须给出唯一最佳判断+置信度、
# 附 "ALSO VISIBLE" 补充。不含任何任务/评分器标记（满足防泄漏 Constraint #0）。
VISION_SYSTEM = (
    "You are a precise visual-perception tool. A blind agent working on a computer task "
    "sends you an image (or images) plus a question. The agent cannot see and must ACT "
    "on your answer, so be both accurate AND decisive.\n"
    "1. Answer directly and concretely: read any text/numbers VERBATIM; give exact "
    "counts, positions, colors, sizes, spatial relationships. Never invent TEXT or "
    "numbers that are not actually there.\n"
    "2. When the question asks you to IDENTIFY or RECOGNIZE something (a theme, style, "
    "framework, product, layout, object), COMMIT to your single most-likely answer from "
    "the visual design plus your own knowledge — EVEN IF no explicit name/label is "
    "written on screen. 'No name is visible' is useless to a blind agent; give your "
    "best-guess identification and state confidence (high/medium/low).\n"
    "3. Add a short 'ALSO VISIBLE:' line with other salient details that could matter.\n"
    "Keep the two rules distinct: do NOT fabricate text that isn't there, but DO commit "
    "to recognitions. If a detail is genuinely illegible, say so — yet still give your "
    "best inference for any identification question."
)


# v22.1: the MULTI-ROUND vision AGENT (mirrors the verifier). Instead of one glance, it
# can zoom/crop/grid-inspect over several rounds before committing — single glances
# mis-count and mis-read small text; systematic inspection does not. No task/grader
# tokens (Constraint #0).
# 【中文注释】v22.1 多轮视觉智能体系统提示词（对标 Verifier 的多轮检查）：作答前可
# 多轮 inspect——crop 放大局部读小字、grid 网格分块（至多 6 格）整图清点、full 重看
# 全图；最后给出"整图"答案。计数规则：答案是整图总数；网格必须逐格计数后求和；
# 禁止逐格缩放。
VISION_AGENT_SYSTEM = (
    "You are a careful visual-perception AGENT working for a blind agent on a computer "
    "task. You are shown an image and a question, and you may INSPECT it over several "
    "rounds before answering.\n"
    "Your answer is ALWAYS about the WHOLE image. First form a whole-image answer from "
    "the full view — for many questions (including counting clearly-separated items) one "
    "careful look at the whole is best. Use inspection only when detail is genuinely too "
    "small or dense to resolve at once.\n"
    "Each round, reply with ONE JSON object:\n"
    '  {"inspect": {"op": "crop", "region": [x0,y0,x1,y1], "note": "why"}}  '
    "— zoom to a pixel box to READ small text / verify fine detail;\n"
    '  {"inspect": {"op": "grid", "rows": R, "cols": C, "note": "why"}}  '
    "— split into an RxC grid of AT MOST 6 cells (e.g. 2x3) and see EVERY cell at native "
    "resolution at once; to count many items, count each cell and SUM across all cells;\n"
    '  {"inspect": {"op": "full", "note": "re-examine the whole"}};\n'
    '  or {"answer": {"text": "<whole-image answer; numbers/text verbatim>", '
    '"confidence": "high|medium|low", "also_visible": "<salient details>"}}.\n'
    "COUNTING RULES: (1) the answer is the total for the WHOLE image; (2) if you grid, "
    "you must SUM every cell's count — NEVER report one cell's or one region's count as "
    "the total; (3) do not zoom cells one at a time (you will lose track); grid ONCE into "
    "<=6 cells, count them all, and sum. Do NOT fabricate text/numbers that aren't there, "
    "but for IDENTIFY/RECOGNIZE questions (theme, style, framework, object) COMMIT to your "
    "best-guess answer from visual style + knowledge even if unlabeled — 'not visible' is "
    "useless to a blind agent — and state confidence."
)


def look_answer_message(path: str, question: str, answer: str, turns_left: int = None,
                        plan: str = "", commit: bool = False,
                        wall_mins: int = None) -> str:
    """Deliver the vision tool's TEXT answer to a blind primary (v22 vision_model path)."""
    # 【中文注释】把视觉工具的文字报告回传给盲主模型（v22 vision_model 路径）：
    # 明确告知模型自己看不到图片，只能依据报告行动。
    parts = [f"Your look at {path} was answered by the vision tool "
             "(you cannot see the image yourself)."]
    if question:
        # 复述模型当时的提问，便于对齐上下文。
        parts.append(f"You asked: {question}")
    # 附上视觉工具报告全文。
    parts.append(f"VISION TOOL REPORT:\n{answer}")
    # 指导：把报告视为对该文件的观察；需要更多细节可再发更具体的 look。
    parts.append("Treat this report as your observation of that file. The complete "
                 "report remains in your conversation history; continue freely, and "
                 "issue another look with a more specific question if you need more.")
    if plan:
        # 回显计划。
        parts.append(f"YOUR PLAN: {plan}")
    # 预算行 + 可选收官提示。
    tail = _budget_line(turns_left, wall_mins)
    if commit:
        tail += " " + COMMIT_PHASE
    # 末尾重申回复格式。
    parts.append((tail + " " if tail else "") +
                 'Reply with ONE JSON object: {"program": ...}, another '
                 '{"look": ...}, or {"done": {"checks": [...]}}.')
    return "\n\n".join(parts)


# 【中文注释】收官阶段提示：预算大半已花——停止扩大调查面，先把能支撑的子部分完成
# 落位再验证；无法解决的如实报"未验证"，不许猜测。
COMMIT_PHASE = ("COMMIT PHASE — most of your budget is spent: stop broadening the "
                "investigation. Finish and place the sub-parts you can support NOW, "
                "then verify; state anything unresolved as unverified rather than "
                "guessing.")


def opening_message(instruction: str, budget: int = None,
                    wall_mins: int = None) -> str:
    # 【中文注释】任务开场消息。budget=None：无回合上限版本——给出任务与"全新 shell"
    # 事实，要求回复第一行写 PLAN: 计划并给出首个侦察程序。
    if budget is None:
        return (f"TASK:\n{instruction}\n\n"
                "You are at a fresh shell on the machine where this task must be "
                "done. Nothing has been examined yet. Decide how much work the task "
                "requires and declare done only when you judge its end state ready. "
                "Begin your reply with one line 'PLAN: <your phases>' — revise it in "
                "later replies whenever reality disagrees with it — then give your "
                "first program (reconnaissance).")
    # 带预算版本：显式声明回合总数（每个 program/look/done 都消耗一回合）与可选
    # 墙上时钟限制，并要求计划中为每个阶段分配回合预算。
    wall = ("" if wall_mins is None else
            f" and a wall-clock limit of ~{wall_mins} minutes — time your programs "
            "spend running counts against it")
    return (f"TASK:\n{instruction}\n\n"
            "You are at a fresh shell on the machine where this task must be done. "
            f"Nothing has been examined yet. You have {budget} action turns total "
            f"(each submitted program, look, or done-declaration costs one){wall}. Begin "
            f"your reply with one line 'PLAN: <your phases, each with a turn budget, "
            f"within {budget}>' — revise it in later replies the same way whenever "
            "reality disagrees with it — then give your first program "
            "(reconnaissance).")


def continuation_message(instruction: str, budget: int = None,
                         wall_mins: int = None) -> str:
    """Open a new phase inside an existing Actor Agent conversation.

    Unlike :func:`opening_message`, this must not claim that the shell or
    conversation is fresh: callers use it precisely when the prior task
    trajectory is being retained as evidence for a follow-up phase.
    """
    # 【中文注释】在既有 Actor 对话中开启新阶段（与 opening_message 的关键区别）：
    # 不得声称 shell/对话是全新的——调用方恰恰要保留先前任务轨迹作为后续阶段的证据。
    if budget is None:
        # 无回合预算版本：延续现有对话与机器状态继续工作。
        return (f"NEXT PHASE:\n{instruction}\n\n"
                "Continue with the conversation and machine state you already have. "
                "Decide how much work this phase requires and declare done only when "
                "you judge its end state ready. Reply with one program, look, or done "
                "declaration at a time.")
    # 带预算版本：声明本阶段回合数与可选墙上时钟限制。
    wall = ("" if wall_mins is None else
            f" and a wall-clock limit of ~{wall_mins} minutes — time your programs "
            "spend running counts against it")
    return (f"NEXT PHASE:\n{instruction}\n\n"
            "Continue with the conversation and machine state you already have. "
            f"You have {budget} action turns total for this phase "
            f"(each submitted program, look, or done-declaration costs one){wall}. "
            "Reply with one program, look, or done declaration at a time.")


def _budget_line(turns_left: int = None, wall_mins: int = None) -> str:
    """The binding constraint must be VISIBLE: a run can die of wall clock with
    plenty of turns left (time spent inside programs consumes no turns) — without
    this line the model paces itself against the wrong budget."""
    # 【中文注释】预算行：约束必须"可见"。一次运行可能死于墙上时钟而回合数还剩很多
    # （程序内部运行时间不消耗回合数）；缺了这行模型会按错误的预算分配节奏。
    if turns_left is None:
        # 未提供剩余回合数时不输出预算行。
        return ""
    if wall_mins is None:
        # 只有回合预算：报告剩余回合数。
        return f"{turns_left} action turn(s) remain."
    # 双预算：同时报告剩余回合与剩余墙钟分钟，先耗尽者终止尝试。
    return (f"{turns_left} action turn(s) and ~{wall_mins} minute(s) of wall clock "
            "remain (time your programs spend running counts; whichever runs out "
            "first ends the attempt).")


def trace_message(trace, turns_left: int = None, plan: str = "",
                  commit: bool = False,
                  head: int = 0, tail: int = 0, wall_mins: int = None,
                  context_max_chars: int = 0) -> str:
    # 【中文注释】程序执行结果回传消息的构造。先拼状态行：退出码（未知则 exit unknown）。
    status = f"exit {trace.exit_code}" if trace.exit_code is not None else "exit unknown"
    if trace.timed_out:
        # 超时标记：对模型是重要信号（该把工作拆成更小的程序了）。
        status += ", TIMED OUT"
    # 优先使用 context_stdout（上下文版输出），否则退回原始 stdout；两者皆无则注明。
    context_stdout = getattr(trace, "context_stdout", None)
    raw_body = context_stdout or trace.stdout or "(no output)"
    if (head <= 0 or tail <= 0) and context_max_chars > 0 \
            and len(raw_body) > context_max_chars:
        # The ArtifactSink has already persisted trace.stdout losslessly. Bound only
        # the observation placed in the next provider request, keeping more of the
        # tail because diagnostics and command summaries conventionally land there.
        # 【中文注释】未显式指定 head/tail 且正文超出上下文预算：按 2:5 / 3:5 比例
        # 截断。ArtifactSink 已把 trace.stdout 无损持久化，这里只裁剪"下一次发给
        # 模型供应商的观察"；保留更多尾部，因为诊断信息与命令摘要通常落在输出末尾。
        head = max(1, context_max_chars * 2 // 5)
        tail = max(1, context_max_chars - head)
    # 应用头尾截断（中段省略并标注省略字符数与归档说明）。
    body = _head_tail(raw_body, head, tail)
    # 第一段：程序输出 + 状态 + 耗时秒数（.0f 取整秒）。
    parts = [f"PROGRAM OUTPUT ({status}, {trace.secs:.0f}s):\n{body}"]
    if plan:
        # 回显计划。
        parts.append(f"YOUR PLAN: {plan}")
    # 组装预算行。
    tail_line = _budget_line(turns_left, wall_mins)
    if commit:
        # 收官阶段追加提示。
        tail_line += " " + COMMIT_PHASE
    # 末尾重申：回复恰好一个 JSON 动作（下一发/修正后的程序、look、或终态存在时的 done）。
    parts.append((tail_line + " " if tail_line else "") +
                 'Reply with ONE JSON object: your next or fixed '
                 '{"program": ...}, a {"look": ...}, or {"done": {"checks": [...]}} once the task\'s '
                 "end state exists on the machine.")
    return "\n\n".join(parts)


# 【中文注释】PLAN 行正则：MULTILINE 使 ^ 匹配行首，可从多行文本中找出所有 "PLAN: ..." 行。
_PLAN_RE = re.compile(r"^\s*PLAN:\s*(.+)$", re.MULTILINE)


def extract_plan(text: str) -> str:
    """The LAST 'PLAN: ...' line of a reply (the model may revise its plan), bounded."""
    # 【中文注释】提取回复中最后一条 PLAN 行（模型可能随进度修订计划），截断到 300 字符。
    # 技法：循环迭代 finditer 至结束，m 停在最后一个匹配——比 findall 更省内存。
    m = None
    for m in _PLAN_RE.finditer(text or ""):
        pass
    return m.group(1).strip()[:300] if m else ""


def repair_message(results, rejections) -> str:
    # 【中文注释】检查未通过时的修复反馈构造：逐条列出 PASS/FAIL、探针命令与输出尾部。
    lines = ["NOT DONE — your verification did not pass:"]
    for r in results:
        # 每条检查一行：通过与否、描述、探针、输出末 200 字符（空则注明 none）。
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"[{mark}] {r.desc}\n    probe: {r.probe}\n    output: "
                     f"{(r.output or '(none)')[-200:]}")
    for rej in rejections:
        # 被宿主拒绝的检查（如探针非只读、静音错误流）单独列出。
        lines.append(f"[REJECTED CHECK] {rej}")
    # 指导：FAIL 意味着终态并不真正存在（用程序修复）或检查本身错了（修检查）；
    # 处理根因后重新声明 done。
    lines.append("\nA FAIL means the end state is not actually there (fix it with a "
                 "program) or the check itself is wrong (fix the check). Address the "
                 "cause, then declare done again.")
    return "\n".join(lines)


def _head_tail(s: str, head: int, tail: int) -> str:
    # 【中文注释】头尾保留截断工具：head/tail 非正则不截断；文本不超长（<=head+tail+60）
    # 不截断；否则保留头尾并在中段插入省略标记（含被省略字符数与"完整输出已归档"）。
    if head <= 0 or tail <= 0:
        return s
    if len(s) <= head + tail + 60:
        return s
    return (s[:head] + f"\n...[{len(s) - head - tail} chars omitted; full output "
            "archived]...\n" + s[-tail:])


def _as_look(d: dict):
    """Build a Look from any dict carrying look fields ("path" or "paths", optional
    "region"); None when no usable path is present."""
    # 【中文注释】从任意携带 look 字段的字典构造 Look；无可用路径时返回 None。
    def normalize_path(value) -> str:
        # 内嵌函数：路径归一化——先去首尾空白。
        path = str(value).strip()
        # Some OpenAI-compatible Kimi routes conflate the advertised live-screen
        # sentinel with a local image argument and emit ``screen: /abs/file.png``.
        # The intent is unambiguous when the suffix is an absolute path.  Preserve
        # ordinary ``screen:`` / ``screen:1`` targets and leave final verifier path
        # authorization to ``validate_verifier_look_path``.
        # 【中文注释】某些 OpenAI 兼容的 Kimi 路由会把"实时屏幕哨兵"与本地图片参数
        # 混为一谈，产出 "screen: /abs/file.png"。后缀为绝对路径时意图明确——取真实
        # 路径；普通 "screen:" / "screen:1" 目标保持原样，最终 Verifier 路径授权交给
        # validate_verifier_look_path 处理。fullmatch 保证整串匹配（IGNORECASE）。
        match = re.fullmatch(r"screen:\s*(/.*)", path, flags=re.IGNORECASE)
        return match.group(1).strip() if match else path

    # paths 列表：支持一次 look 携带多张图（列表顺序即比对顺序）；先处理复数 paths。
    paths = []
    if isinstance(d.get("paths"), list):
        paths = [normalize_path(p) for p in d["paths"] if str(p).strip()]
    elif str(d.get("path", "")).strip():
        # 单数 path 字段退化为单元素列表。
        paths = [normalize_path(d["path"])]
    # 没有任何可用路径则不是合法 look。
    if not paths:
        return None
    # region 必须是四元素列表（x0,y0,x1,y1 像素框），否则视为未提供。
    region = d.get("region")
    if not (isinstance(region, list) and len(region) == 4):
        region = None
    # 构造 Look：多路径 + 问题 + 可选裁剪框。
    return Look(paths=paths, question=str(d.get("question", "")).strip(), region=region)


# 【中文注释】原生工具信封正则（Kimi 无损格式）：匹配 "<|open|>call tool=..." 到
# "<|close|>call" 的完整调用体；DOTALL 让 .*? 跨行匹配；命名分组提取工具名与调用体。
_NATIVE_TOOL_CALL_RE = re.compile(
    r'<\|open\|>call\s+tool="(?P<tool>[^"]+)"[^\r\n]*?<\|sep\|>'
    r'(?P<body>.*?)<\|close\|>call', re.DOTALL)
# 【中文注释】参数级正则：在调用体内逐个抽取 key/value 参数对。
_NATIVE_TOOL_ARG_RE = re.compile(
    r'<\|open\|>argument\s+key="(?P<key>[^"]+)"[^\r\n]*?<\|sep\|>'
    r'(?P<value>.*?)<\|close\|>argument', re.DOTALL)


def _native_tool_objects(text: str) -> list:
    """Normalize provider-native textual tool envelopes into ordinary actions.

    Some OpenAI-compatible routes serialize a valid tool call into Kimi's lossless
    ``<|open|>call ...`` text instead of returning it through the API tool field.
    Treating that transport artifact as narration causes dry-turn recovery even
    though the Agent selected a concrete action.  This parser is deliberately
    conservative: it recognizes only complete calls with named arguments and never
    executes prose outside the envelope.
    """
    # 【中文注释】把供应商原生文本工具信封归一化为普通动作对象。若把这种传输伪影当
    # 叙述处理，会触发"空转回合恢复"——尽管 Agent 明明选了具体动作。解析器刻意保守：
    # 只识别带命名参数的完整调用，绝不执行信封之外的散文。
    objects = []
    # 遍历文本中所有完整工具调用信封。
    for match in _NATIVE_TOOL_CALL_RE.finditer(text or ""):
        # 工具名小写归一化便于比较。
        tool = match.group("tool").strip().lower()
        # 字典推导式：从调用体抽取所有 {参数名: 值}。
        args = {
            arg.group("key").strip(): arg.group("value")
            for arg in _NATIVE_TOOL_ARG_RE.finditer(match.group("body"))
        }
        # bash/python/python3/verifier-report → program 动作（有代码才有效）。
        if tool in {"bash", "python", "python3", "verifier-report"}:
            code = str(args.get("code", ""))
            if code.strip():
                # python3 归一化为 lang="python"，其余保留原名。
                lang = "python" if tool == "python3" else tool
                objects.append({"program": {"lang": lang, "code": code}})
            continue
        # program 工具：允许显式 lang（缺省 python）。
        if tool == "program":
            code = str(args.get("code", ""))
            if code.strip():
                objects.append({"program": {
                    "lang": str(args.get("lang", "python")), "code": code}})
            continue
        # look 工具：只保留 path/question 两个已知字段；path 非空才有效。
        if tool == "look":
            look = {key: value for key, value in args.items()
                    if key in {"path", "question"}}
            if str(look.get("path", "")).strip():
                objects.append({"look": look})
            continue
        # ask/ask_user 工具：问题非空才构成 ask 动作。
        if tool in {"ask", "ask_user"}:
            question = str(args.get("question", "")).strip()
            if question:
                objects.append({"ask": {"question": question}})
    # 返回全部归一化出的动作字典列表。
    return objects


# --- turn parsing -------------------------------------------------------------------
# 【中文注释】回合解析区：把模型的一条回复文本解析为结构化动作对象。
@dataclass
class Program:
    # 【中文注释】Program 动作：一次提交的完整程序（lang: python/bash；code: 全文）。
    lang: str
    code: str
    extras: int = 0     # additional program objects in the SAME reply that were
    #                     discarded (only the last runs) — the loop tells the model,
    #                     because silent discarding breeds a false world-model ("the
    #                     env ran them all and only shows the last output")
    # 【中文注释】extras：同一回复中被丢弃的额外 program 对象数（只有最后一个运行）。
    # 主循环会把这一点告诉模型——静默丢弃会滋生错误世界模型。
    dup: int = 1        # v21 (A): max count of a SINGLE action kind in the reply
    #                     (programs/looks/dones). >=2 means a decoder-degeneration turn
    #                     where last-wins would run a wrong/ritual object.
    # 【中文注释】dup（v21 A）：回复中"单一动作种类"的最大出现数。>=2 说明是解码器
    # 退化回合，此时朴素"后者优先"可能跑错/跑出仪式性对象，主循环据此特殊处理。


@dataclass
class Done:
    # 【中文注释】Done 动作：声明任务终态已达成，携带检查清单（每项 {desc, probe}）。
    checks: List[dict] = field(default_factory=list)
    dup: int = 1        # v21 (A): see Program.dup
    # 【中文注释】dup：含义同 Program.dup（解码器退化检测计数）。


@dataclass
class Look:
    # 【中文注释】Look 动作：视觉查看请求；一次调用可携带全部待比对图片路径。
    paths: List[str] = field(default_factory=list)   # all requested images, in ONE call
    question: str = ""
    # 【中文注释】question：向视觉工具提出的问题（下一回合收到文字报告或像素）。
    region: List[int] = None      # optional [x0, y0, x1, y1] server-side crop (px)
    # 【中文注释】region：可选的 [x0,y0,x1,y1] 服务端像素裁剪框。
    dup: int = 1                  # v21 (A): see Program.dup
    # 【中文注释】dup：解码器退化检测计数。

    @property
    def path(self) -> str:        # display / logging convenience
        # 【中文注释】便捷属性：多路径拼成逗号串，仅供展示/日志使用。
        return ", ".join(self.paths)


@dataclass
class Ask:
    # 【中文注释】Ask 动作：向用户提问（仅限环境无法恢复的信息/输入）。
    question: str
    dup: int = 1


def parse_turn(text: str):
    """The model's action for this turn: Program, Look, Ask, Done, or None.

    Tolerant of prose preambles, markdown fences and draft-then-correct replies.
    PRECEDENCE across ALL top-level objects: Program > Look > Ask > Done — the conservative
    "keep working" rule applied turn-wide (models sometimes emit a good {"program"}
    followed by a hedging {"done"} object; naive last-object-wins discards the
    program). Within a kind, the LAST wins (draft-then-correct). A bare {"lang","code"}
    object counts as a program; a bare {"checks": [...]} or an explicit {"done": null}
    counts as a done declaration (the latter with zero checks → check-feedback)."""
    # 【中文注释】回合解析入口：返回本回合动作（Program/Look/Ask/Done）或 None
    # （解析失败，上层触发格式重试）。对散文前导、markdown 围栏、"草稿后修正"式回复
    # 保持容错。跨所有顶层对象的优先级 Program > Look > Ask > Done——保守的"继续
    # 工作"规则（模型有时先发正经 program 再补含糊 done；朴素"最后对象优先"会把
    # program 丢掉）。同种类内"最后者优先"。裸 {"lang","code"} 算程序；裸
    # {"checks":[...]} 或 {"done":null} 算 done 声明（后者零检查→检查反馈流程）。
    text = re.sub(r"\]<\][a-z]+\[>\[", " ", text or "")   # strip template-delimiter
    #                                                       leak artifacts (decoder
    #                                                       degeneration residue)
    # 【中文注释】第一步：清除解码器退化残留的模板定界符泄漏片段（如 "]<]x[>["）。
    objs = [v for v in json_values(text) if isinstance(v, dict)]
    # 【中文注释】第二步：容错解析器抽取文本中所有 JSON 值，只保留字典对象。
    # These API-style envelopes are not the plain JSON action channel. Silently
    # ignoring them can select a trailing Done from a simulated tool transcript,
    # even though its purported programs/results never reached the executor.
    # Keep the reply unparsed so the existing formatting retry can ask for one
    # real action. Recognize the envelope independently of its invented tool
    # name (for example python, terminal, or a provider-specific namespace).
    # Do not execute or reinterpret the claimed tool results.
    # 【中文注释】API 风格信封（name+arguments）不是本框架的纯 JSON 动作通道：静默
    # 忽略可能选中"模拟工具对话记录"尾部的 Done，而其声称的程序/结果从未到达执行器。
    # 因此保持整个回复"未解析"，让既有格式重试索要真实动作。识别信封不依赖其杜撰的
    # 工具名（python、terminal 或供应商命名空间均可）；绝不执行/重新解释声称的工具结果。
    if any(isinstance(obj.get("name"), str)
           and obj["name"].strip()
           and isinstance(obj.get("arguments"), (dict, str)) for obj in objs):
        return None
    # 【中文注释】第三步：追加原生工具信封解析出的动作（Kimi 文本信封归一化）。
    objs.extend(_native_tool_objects(text))
    # 【中文注释】四种动作槽位与计数器初始化。
    program, look, ask, done = None, None, None, None
    n_programs = n_looks = n_asks = n_dones = 0
    # 【中文注释】第四步：遍历所有对象，登记最后出现的各类动作（同种类最后者优先）。
    for obj in objs:
        prog = obj.get("program")
        # 标准形态：{"program": {"lang","code"}}，code 非空才有效。
        if isinstance(prog, dict) and str(prog.get("code", "")).strip():
            n_programs += 1
            program = Program(lang=str(prog.get("lang", "python")), code=str(prog["code"]))
            continue
        # 嵌套形态：{"program": {"look": {...}}}——program 键里套的是 look。
        if isinstance(prog, dict) and isinstance(prog.get("look"), dict):
            lk = _as_look(prog["look"])                              # nested-look shape
            if lk:
                n_looks += 1; look = lk
                continue
        # program 信封缺 code 但携带 look 字段（常见 lang:"look" 的退化写法）→ 按 look。
        if isinstance(prog, dict) and not str(prog.get("code", "")).strip():
            lk = _as_look(prog)                          # look fields in a program
            if lk:                                       # envelope, often lang:"look"
                n_looks += 1; look = lk
                continue
        # 裸程序对象：顶层直接 {"lang","code"}（没有 program 包裹）也算程序。
        if str(obj.get("code", "")).strip():                  # bare program object
            n_programs += 1
            program = Program(lang=str(obj.get("lang", "python")), code=str(obj["code"]))
            continue
        # 标准 look：{"look": {...}}。
        lk = obj.get("look")
        if isinstance(lk, dict):
            l2 = _as_look(lk)
            if l2:
                n_looks += 1; look = l2
                continue
        # 标准 ask：{"ask": {"question"}}。
        aq = obj.get("ask")
        if isinstance(aq, dict) and str(aq.get("question", "")).strip():
            n_asks += 1
            ask = Ask(question=str(aq["question"]).strip())
            continue
        # 标准 done：{"done": {...}}；done:null 或非字典 → 零检查的 Done。
        if "done" in obj:
            d = obj["done"]
            n_dones += 1
            done = Done(checks=list(d.get("checks") or []) if isinstance(d, dict) else [])
            continue
        # 裸 done：顶层直接 {"checks": [...]}。
        if isinstance(obj.get("checks"), list):               # bare done object
            n_dones += 1
            done = Done(checks=list(obj["checks"]))
            continue
        # 裸 look 字段：顶层直接 {"path"/"question"} 且无 code → 视为 look。
        if "question" in obj and not str(obj.get("code", "")).strip():
            l3 = _as_look(obj)                                # bare look fields
            if l3:
                n_looks += 1; look = l3
    # 【中文注释】第五步：按 Program > Look > Ask > Done 优先级选出本回合动作。
    result = program or look or ask or done
    if result is not None:
        # 记录"单一动作种类最大重复数"，供主循环识别解码器退化回合（v21 A）。
        result.dup = max(n_programs, n_looks, n_asks, n_dones)  # v21 (A)
        if isinstance(result, Program) and n_programs > 1:
            # 选中 Program 且回复中有多个程序时，记录被丢弃的额外程序数。
            result.extras = n_programs - 1
    return result
