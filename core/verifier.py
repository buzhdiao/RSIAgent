# 【中文注释·模块定位】独立 Verifier——done 时刻的"零历史新上下文"检查（v8，系统级）。
# 论文中的 Verifier 隔离原则在此落地：语义纠缠失败（假完成第 3 种）表明 Actor 自己的
# 检查会继承 Actor 自己的错误假设，再多提示也查不出来；结构性修复是"独立性"——
# 一次零历史模型调用，只看任务指令与活机器（看不到 Actor 的推理、证据或检查），
# 自行重导出需求、只读探测机器、独立判定。验收规则：Actor 的检查必须通过 且 本次
# 独立检查未发现违规（"pass"才接受；反复 "unverified" 可在预算规则下迟接受；
# "wrong" 总是带着 FINDINGS（证据而非权威）弹回修复）。本文件同样是防泄漏审计表面
# （Constraint #0），一切内容必须通用。
"""独立 Verifier——done 时刻的"零历史新上下文"检查（v8，系统级）。

语义纠缠失败（假完成第 3 种）表明：Actor 自己的检查会继承 Actor 自己的错误
假设，再多提示也查不出来。结构性修复是"独立性"：一次零历史的模型调用，只看
任务指令与活机器——看不到 Actor 的推理、证据或检查——在自己的抽样上重新
导出需求（含任何约定），只读探测机器，然后判定。验收规则：Actor 的检查必须
通过 且 本次检查未发现违规——"pass"接受；反复 "unverified" 可在循环的预算
规则下迟接受（could-not-confirm 永不一票否决）；"wrong" 总是带着 FINDINGS
（证据而非权威）弹回修复。

本文件是被审计的表面（Constraint #0）：tests/test_no_leakage.py 在每次变更时
按任务黑名单扫描它。这里的一切内容都必须是通用的。
"""
# 导入 base64：归档 Look 像素附件的 base64 解码（重附时完整性校验）。
import base64
# 导入 hashlib：图像附件与无动作回复的 sha256 指纹。
import hashlib
# 导入 logging：模块级日志器。
import logging
# 导入 os：路径拼接与目录列举（崩溃恢复段扫描）。
import os
# 导入 re：agentic 令牌与 [APPEARANCE] 标记的正则。
import re
# 导入 tempfile：无 sink 时的临时产物目录。
import tempfile
# 导入 time：检查墙钟 deadline 计算。
import time

from core.actor import EPISTEMIC_CORE, Look, Program, parse_turn
# 导入 Actor 侧共享件：认识论内核、Look/Program 动作类型与回合解析器。
from core.checks import _category, _nonscratch_mutations
# 导入检查侧工具：探针分类与"非草稿区改动"只读守卫（与 Actor 检查同一 lint）。
from llm.client import (LLMTransportError, chat, durable_images, parse_object,
                        with_durable_images)
# 导入 LLM 客户端：传输错误、对话调用、持久图像附件的读取与重附、JSON 解析。

log = logging.getLogger("rsiagent.verifier")
# 模块级日志器（rsiagent.verifier 命名空间）。

# 【中文注释·机械活性上限】仅作机械活性检测：智能体可以跨多个有产出的段落持续调查；连续多段
# 无任何 Program/Look 属于运行时故障，需要恢复——绝不允许据此伪造候选判定或
# 宣布 Curriculum 收敛（与 phase1_wave 的同类上限语义一致）。
_MAX_ACTIONLESS_VERIFIER_SEGMENTS = 3
# 无动作段数上限：超过即视为基础设施故障（见上方中文注释）。


# Verifier 模板中的"视觉通道可选段"（模板字符串，内容一字不改）：
# 配置了视觉时教它用 look 动作（指向交付文件或 "screen:" 看活屏）；
_VERIFIER_LOOK_SECTION = """\
or, to check a VISUAL requirement no shell probe can settle (whether something is \
POSITIONED at, POINTS at, is SIZED/COLORED as, or LOOKS LIKE something), issue \
  {{"look": {{"path": "/abs/deliverable", "question": "<the visual thing to confirm>"}}}}
— ONE look per turn; point it STRAIGHT at the deliverable file (a document file is \
rendered to an image for you; you do NOT need to convert it yourself with a probe). \
When the deliverable is NOT a file but the state of a RUNNING app or browser session \
(a booking, a submitted form, an on-screen result), use path "screen:" to look at \
the LIVE screen instead — the work may be real yet leave no file the filesystem \
probes can find. The answer is an independent visual reading of the machine's \
current state; the exact delivered pixels remain in your persistent conversation \
history. Use them to confirm or refute visual requirements you cannot verify from \
text alone.
"""

_VERIFIER_NO_LOOK_SECTION = """\
(This configuration has NO visual channel: there is no "look" action — verify \
through text probes only, and report requirements that would need visual \
confirmation as could-not-confirm rather than guessing.)
"""
# 无视觉时的替代段：如实告知没有 look 动作，宁可报 could-not-confirm 也不许猜。

# 独立检查者的完整提示模板：任务 + 认识论内核 + 方法 + 三种判定语义 + 只读
# 探针纪律。内容为提示词，一字不改。
_VERIFIER_TEMPLATE = """\
You are an INDEPENDENT INSPECTOR. Someone else claims to have completed the task \
below on this machine. You have deliberately NOT been shown their work, their \
reasoning, or their assumptions — your value is an independent reading. Decide \
whether the machine's CURRENT state actually satisfies the task.

EPISTEMIC CORE — how truth is established here (applies to everything below):
{core}

METHOD:
1. Read the task and derive its FULL requirement inventory yourself — every property \
the task states or implies, including any naming or convention rules. If the task \
contains an already-solved instance, derive the convention FROM that instance and \
verify your reading against the real data on the machine before judging anything else.\
{dest_rule}
2. Probe the machine READ-ONLY to gather evidence for each requirement.
3. Judge each requirement on the evidence alone (core E6). Be suspicious: plausible \
is not verified, and a wrong interpretation that fails on the given instance is wrong.
4. Choose the verification channel by the claim type (core E2): structural evidence \
(coordinates, values, cell contents, attributes) proves STRUCTURAL facts; a \
requirement about a RENDERED result is settled by looking at the rendered \
deliverable, not by the numbers. WHEN A RENDER IS IMPOSSIBLE in this environment, \
MEASURE instead — derive the checkable quantities (dimensions, coordinates, counts, \
colors, text) from the source file with code (python standard library or the \
application's command-line/headless modules) and judge those against the requirement.
5. Before emitting `pass`, try to FALSIFY your own conclusion: identify the strongest \
plausible material way the COMPLETE result could still violate the task, including an \
interaction among parts that each look correct alone. If that possibility is unresolved, \
investigate it or return `wrong`/`unverified`; emit the one terminal `pass` only when \
your evidence settles it.

Each turn reply with ONLY ONE JSON object:
  {{"probes": ["<read-only shell command>", "..."]}}          // up to 4 per turn
{look_section}or your final verdict:
  {{"verdict": "pass" | "wrong" | "unverified",
{items_line}   "findings": "<per requirement: met / violated / could-not-confirm + the concrete \
evidence>",
   "doubts": "<ONLY unresolved uncertainty about a MATERIAL TASK requirement. Put \
incidental observations and non-blocking caveats in findings instead. This MUST be \
an empty string for pass; never omit a task-relevant doubt to make a pass smoother.>"}}

The three verdicts are NOT interchangeable:
- "pass": EVERY MATERIAL requirement is affirmatively evidenced on the machine.
- "wrong": you found CONCRETE evidence that some requirement is VIOLATED — cite that \
evidence in findings so it can be fixed. A required artifact shown ABSENT by a probe \
that ran successfully IS "wrong" (cite the listing that proves the absence).
- "unverified": you could not gather enough evidence to confirm or refute — that is \
NOT the same as wrong; say exactly what you could not confirm and why. Never report \
"wrong" on mere suspicion, and never report "pass" on hope.
Evidence rules are core E3/E4: errors are evidence (never silence the error stream \
of the command whose result you need); a timed-out probe, a "[channel error ...]" \
line, or empty output from an UNVERIFIED channel proves nothing — re-probe narrowly \
instead of concluding; empty output on a channel verified alive IS real evidence.

Rules: probes must be read-only (no writes, moves, deletions, installs). PROBE \
ECONOMY — timed-out probes are the main way inspections end unverified: start from \
the exact paths the task names; use cheap existence and attribute probes first (ls, \
test -e, head, wc -l) and read contents only where a requirement demands it; \
NEVER scan the whole filesystem or a whole home tree (find / grep -r over a large \
tree WILL time out and prove nothing) — target the narrowest directory that answers \
the requirement; batch several quick related probes into one round rather than \
spending a round on one big command; if a probe times out, split it into narrower \
probes, never repeat it. Probes are JSON strings — escape newlines as \\n, or write \
single-line commands joined by ';'."""


def _verifier_eyes_model(cfg) -> str:
    """v39.1：由哪个模型充当检查者 look 的"眼睛"。显式 verifier_vision_model
    优先（'self' = 主模型的"有视觉血统"）；否则用 Actor 委托的眼睛
    （vision_model）；否则没有（失明）。

    v40 眼睛修复（2026-07-22）：'self' 先解析 vision_model、再解析 model。
    在主段落里这不变化（原生主模型：vision_model='' -> model = 有视觉的主
    模型）。在升级段里 _replace 已把 model 改写成纯文本升级模型，但"降级主
    模型转任眼睛"的规则把原主模型留在了 vision_model——旧的 'return
    cfg.model' 分支无视它，把检查者的 look 路由到一个没有图像端点的模型
    （404 -> '(vision tool returned nothing)' -> 白烧一轮探针）。战役法证：
    主段落 108/108 的 look 都有应答；升级段约 0/63——37 个升级任务全部盲检。
    与每个"角色相对解析"bug 同根：'self' 必须跟随"有视觉血统"，
    而不是"职位头衔"。"""
    vvm = getattr(cfg, "verifier_vision_model", "") or ""
    # 取显式配置的 Verifier 视觉模型。
    if vvm == "self":
        return (getattr(cfg, "vision_model", "") or ""
                or getattr(cfg, "model", "") or "")
        # 'self'：先 vision_model（有视觉血统），再 model。
    return vvm or getattr(cfg, "vision_model", "") or ""
    # 否则：显式配置优先，回落 Actor 的委托眼睛，最后失明。


def build_verifier_system(cfg=None) -> str:
    """v35：检查者的提示按"每次检查"组装，使其机制描述对当前配置为真——
    look 动作曾被无条件宣传，却在没有视觉模型时被静默丢弃。"""
    vis = bool(_verifier_eyes_model(cfg)) and getattr(cfg, "vision_verify", True)
    # 是否有真眼睛：有眼睛模型且 vision_verify 开启。
    dest = (" When the task names a deliverable file, verify THAT file at THAT exact "
            "path — work found at other paths is staging, not delivery."
            if getattr(cfg, "verifier_destination_note", False) else "")
    # 可选的"交付地"条款：验证指令点名的那个确切路径；别处的工作只是暂存。
    # E4-A1c ()：逐需求的判定从埋在 `findings` 里的散文变成"数据"。它承载
    # harness 以前永远读不到的信息——哪条需求未决——从而弹回能点名该修的
    # 那一件事，检查之间的进展也可度量。标量判定仍是检查者自己的裁量：
    # items 只提供信息，不计算裁决。
    items = ("""   "items": [{"req": "<one requirement, in your own words>",
              "status": "met" | "violated" | "could-not-confirm",
              "evidence": "<the concrete evidence, or what is missing>"}, ...],
"""
             if getattr(cfg, "verifier_items", False) else "")
    # 可选的逐需求 items 段（模板字符串，内容一字不改）。
    return _VERIFIER_TEMPLATE.format(
        core=EPISTEMIC_CORE,
        dest_rule=dest,
        items_line=items,
        look_section=_VERIFIER_LOOK_SECTION if vis else _VERIFIER_NO_LOOK_SECTION)
    # 按配置拼装模板：内核 + 交付条款 + items 段 + 视觉段（有眼/无眼）。


# 向后兼容常量：视觉可用变体。
class _V:
    vision_model = "x"
    vision_verify = True


VERIFIER_SYSTEM = build_verifier_system(_V())
# 旧全局系统提示（假定有视觉）：供遗留引用；运行时建议用 build_verifier_system(cfg)。


# 【中文注释】VerifierSession：一个 Verifier 智能体的"会话 + 累计检查遥测"。
# 继承 list——会话本身就是消息列表（对话连续性保留 Verifier 自己此前的推理与
# 发现）。计数器只是遥测：证据新鲜度刻意对"每次候选检查"保持局部，因为更早的
# 内容探测可能描述的是 Actor 随后已修订过的工件。会话还承载崩溃恢复元数据
# （pending_observation/pending_images：最后一个已保留动作之后的环境结果，
# 尚不构成完整对话对）与基础设施暂停累计（嵌套 Verifier 停机不消耗 Actor 的
# 活动墙钟）。这是 Verifier"跨修订持久上下文"机制的实现载体。
class VerifierSession(list):
    """一个 Verifier Agent 的对话 + 累计检查遥测。

    会话连续性保留 Agent 自己此前的推理与发现。计数器只是遥测：证据新鲜度
    刻意对"每次候选检查"保持局部，因为更早的内容探测可能描述的是 Actor
    Agent 随后已修订过的工件。
    """

    def __init__(self, messages=(), *, on_event=None):
        super().__init__(messages)
        # 继承 list：会话本身就是消息列表。
        self.informative_probes = 0
        # 累计"有信息量"探针数（遥测）。
        self.content_probes = 0
        # 累计"读到候选内容"探针数（遥测）。
        self.inspections = 0
        # 累计检查次数（遥测）。
        # 崩溃安全的 agentic-verifier 续传元数据。原始轨迹是权威；
        # 这些字段只承载"最后一个被保留的 Program/Look 动作之后的环境结果"
        # ——它本身还不构成完整的对话对。
        self.pending_observation = ""
        # 待重附的环境观察文本。
        self.pending_images = []
        # 待重附的持久图像附件。
        self.pending_turn = None
        # 产生该观察的最后一个实质动作。
        self.transport_recurrence = False
        # 无动作传输是否已复发（语义重放激活标志）。
        # 本 Verifier Agent 内累计的供应商/API 停机。外层 Actor 运行时从这些
        # 计数器读增量，使嵌套 Verifier 的停机也不消耗 Actor 的活动工作墙钟。
        self.infra_pause_secs = 0.0
        self.infra_pauses = 0
        # 统一目标生命周期元数据。对话仍是语义事实源；这些字段只拥有机械的
        # 阶段/执行器状态。
        self.stage = ""
        self.candidate_generation = 0
        self.orientation_runs = 0
        # 阶段 / 候选代数 / 定向运行次数。
        self.on_event = on_event
        # 可选事件回调。
        self._program_executor = None
        # 当前绑定的私有执行器。
        self._scratch_archive = None
        # 卸载时保留的私有草稿区归档。

    def emit(self, event: str, **payload) -> None:
        """有回调则转发事件（生命周期台账）。"""
        callback = self.on_event
        if callable(callback):
            callback(event, payload)

    def acquire_executor(self, vm, *, hide_actor_memory=False,
                         private_paths=(), execution_mode="effect_isolated"):
        """为活动目标环境绑定一个私有草稿区生命周期。"""
        from core.verifier_runtime import (
            AgenticVerifierExecutor,
            _normalize_private_paths,
        )

        current = self._program_executor
        wanted_paths = _normalize_private_paths(
            hide_actor_memory, private_paths)
        # 规范化期望的私有路径。
        if (current is not None and current.vm is vm
                and current.private_paths == wanted_paths
                and current.execution_mode == execution_mode):
            return current
            # 已有完全匹配的执行器：复用（保持对话对应的边界）。
        if current is not None:
            self.detach_executor(preserve=True)
            # 不匹配：先卸载并保留草稿区。
        self._program_executor = AgenticVerifierExecutor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=wanted_paths,
            scratch_archive=self._scratch_archive,
            execution_mode=execution_mode)
        # 新建执行器（携带上次保留的草稿归档）。
        self._scratch_archive = None
        # 归档已移交执行器，本地清空。
        return self._program_executor
        # 返回执行器。

    def detach_executor(self, vm=None, *, preserve=True,
                        tolerate_archive_failure=False) -> None:
        """在目标 VM 被丢弃之前卸载执行器，可选保留草稿区。

        基础设施恢复期间客户机控制器可能已经不在，使最终的草稿导出不可能。
        ``close`` 执行的回滚仍是强制的；调用方只可在回滚成功之后选择放弃
        私有草稿归档。
        """
        from core.verifier_runtime import AgenticVerifierInfrastructureError

        executor = self._program_executor
        if executor is None or (vm is not None and executor.vm is not vm):
            return
            # 无执行器或 VM 不匹配：无事可做。
        archive = None
        archive_error = None
        try:
            if preserve:
                try:
                    archive = executor.export_scratch()
                    # 保留模式：先导出草稿。
                except AgenticVerifierInfrastructureError as exc:
                    if not tolerate_archive_failure:
                        raise
                    archive_error = exc
                    # 允许归档失败：记录错误但不阻断回滚。
        finally:
            try:
                executor.close()
                # 回滚候选并关闭执行器（强制）。
            finally:
                # 导出或回滚失败后绝不保留已关闭的执行器。后续恢复必须重建
                # 全新的受信边界，而不是翻出被污染的缓存对象。
                self._program_executor = None
        if preserve:
            self._scratch_archive = archive
            # 保留导出的草稿归档（供下次绑定续用）。
        if archive_error is not None:
            log.warning(
                "Verifier scratch could not be archived after a trusted-runtime "
                "failure; candidate rollback succeeded, so retrying with a fresh "
                "private scratch boundary: %s", archive_error)
            # 留痕：草稿归档失败但回滚成功，重试将用全新私有草稿边界。

    def close_executor(self) -> None:
        """彻底关闭执行器（不保留草稿区）。"""
        self.detach_executor(preserve=False)
        self._scratch_archive = None

_PASS_DOUBT_CLARIFICATION = """\
Your verdict says PASS, but `doubts` is non-empty. Resolve that contradiction \
YOURSELF inside this same inspection; do not hand an incidental note to the Actor \
Agent as repair work.

- If any stated doubt is unresolved and material to the TASK, continue probing or \
  return `wrong`/`unverified` as the evidence warrants.
- If the text is only a non-blocking caveat or unrelated observation, move it into \
  `findings` and return `pass` with `doubts` exactly `""`.

Reply with one normal probes/look object, or one revised verdict object."""
# PASS 与疑点并存的澄清提示：让同一个检查者自查矛盾（正文是提示词，一字不改）。


_CURRENT_CANDIDATE_EVIDENCE = """\
You proposed PASS without reading the CURRENT candidate during this inspection. \
Your earlier inspection remains useful context, but the Actor Agent may have revised \
the artifact since then. Probe or look at the current deliverable before ruling; then \
return the one normal terminal verdict when your evidence is sufficient."""
# "零当前候选内容"提示：先读当前交付物再裁决（正文一字不改）。


# v27：提醒检查者"你有眼睛"的 JIT 轻推。它由模型内在的停顿信号触发（探针
# 一直返回无可读内容）——与对指令的任何解读无关。v25 用指令正则检测"视觉
# 任务"，但那是 harness 侧的任务分类器（违背 rsiagent 的无路由论点），且在
# 约 1/3 仅仅使用了空间词汇（position/align/center/placed…）的非像素任务上
# 过度触发。已整体废弃："需求是否需要眼睛"由检查者依据 VERIFIER_SYSTEM 里的
# 通用原则（"结构数值证明不了渲染结果"）决定；这条提示只在文本验证明显失败
# 时、对任何任务揭示该能力。镜像 Actor 的 v23 look-redirect——静态提示词
# 采纳率约 0%，只有 JIT 轻推有效（v13/v23 教训）。
_VISUAL_STEER = (
    "[INSPECTOR NOTE] You have not used your EYES yet — and text probes alone cannot "
    "settle rendered/visual requirements. You also have EYES: issue {\"look\": {\"path\": "
    "\"/abs/deliverable\", \"question\": \"<what to confirm>\"}} pointed STRAIGHT at the "
    "deliverable file (a document is rendered to an image for you — you do not need to "
    "convert it yourself). If the requirement is about how something LOOKS or WHERE it "
    "visually lands, the render settles it where structural values cannot.")
# JIT 视觉提醒文本（条件式措辞："如果需求关乎外观……"）。


def _opening(instruction: str, rounds: int, context: str = "",
             tag_appearance: bool = False, elastic: bool = False) -> str:
    """构造检查开场信封：逐字任务 + 机械上下文 +（可选）标记要求 + 深度条款。"""
    ctx = ("\n\n" + context) if context else ""
    # 机械上下文（表面增量等）前置拼接。
    tag = ("\n\nAs you derive your requirement inventory in your FIRST reply, tag "
           "each requirement [MEASURABLE] (settled by commands, parsing, counts, "
           "numbers) or [APPEARANCE] (settled only by viewing a rendered result: "
           "colors, layout, position, visual style, on-screen content)."
           if tag_appearance else "")
    # 可选条款：首答里给每条需求打 [MEASURABLE]/[APPEARANCE] 标签。
    # E4-A1b：弹性深度下，检查以检查者的判断收束、绝不以计数器收束——所以
    # 开场不得宣传它会据此规划步调的预算（E3 时代每次检查都花光额度并在
    # 最后一轮裁决：预算形状的停止，而非判断形状的）。
    budget = ("You decide your own depth: there is NO round budget. Keep probing "
              "(and looking) until the evidence settles every requirement, then "
              "rule — rule because YOUR evidence is sufficient, never because you "
              "are running out of turns."
              if elastic else f"You have {rounds} probe rounds.")
    # 弹性模式："深度你定，没有轮次预算"；否则告知轮次数。
    return (f"TASK (as originally given):\n{instruction}{ctx}{tag}\n\n"
            f"{budget} The machine is live. Begin.")
    # 拼装开场：逐字任务 + 上下文 + 标记条款 + 深度条款。


class Findings(str):
    """发现文本本身，同时携带判定的结构化旁路数据：`doubts`——PASS 在没有
    肯定证据的情况下接受了什么（否则为空）；`items`（E4-A1c）——逐需求的
    判定数据。作为 str 的子类，所有既有调用点行为不变（先例：llm.client 的
    ChatText）。"""
    doubts: str = ""
    # 类级缺省：无未决疑点。
    items: tuple = ()
    # 类级缺省：无逐需求条目。


def _clean_items(raw) -> tuple:
    """把检查者的 items[] 归一化为 (req, status, evidence) 三元组。
    刻意宽容：畸形条目直接丢弃、绝不致命——标量判定永远是权威，
    items 只是旁路数据。"""
    ok = []
    for it in (raw or [])[:24]:
        # 最多取 24 条（防失控长度）。
        if not isinstance(it, dict):
            continue
            # 非字典条目：丢弃。
        req = str(it.get("req") or it.get("requirement") or "").strip()[:200]
        # 需求描述（兼容两种键名，截 200 字符）。
        st = str(it.get("status") or "").strip().lower().replace("_", "-")
        # 状态归一化（小写、下划线转连字符）。
        if st in ("could not confirm", "couldnot-confirm", "unconfirmed"):
            st = "could-not-confirm"
            # 各种"未能确认"拼法归一。
        if st in ("partially-met", "partial"):
            st = "could-not-confirm"       # "部分满足" ⇒ 未被肯定地解决
        if not req or st not in ("met", "violated", "could-not-confirm"):
            continue
            # 需求为空或状态不合法：丢弃。
        ok.append((req, st, str(it.get("evidence") or "").strip()[:300]))
        # 收录（证据截 300 字符）。
    return tuple(ok)


def _mk_findings(txt: str, doubts="", items=None) -> Findings:
    """从原始解析结果构造 Findings（doubts 截 800，items 清洗）。"""
    f = Findings(txt)
    f.doubts = str(doubts or "")[:800]
    f.items = _clean_items(items)
    return f


def carry_findings(text: str, src) -> Findings:
    """用新文本重建 Findings，同时携带 `src` 的旁路数据。

    E4-A1c bug（t034 实测抓到，丢了 14 份清单）：降级路径曾用字符串拼接
    重建 findings——那会返回纯 str 并悄悄丢掉 `.doubts`/`.items`——于是每一
    次带疑点的 pass 弹回（这里最常见的弹回）到达 Actor 时都没有清单。
    任何改写 findings 文本的调用点都必须走这里。"""
    f = Findings(text)
    f.doubts = str(getattr(src, "doubts", "") or "")
    f.items = tuple(getattr(src, "items", ()) or ())
    # 逐项搬运旁路数据。
    return f


def unsettled_items(findings) -> tuple:
    """弹回应当点名的条目：先 violated，后 could-not-confirm。"""
    its = tuple(getattr(findings, "items", ()) or ())
    return (tuple(i for i in its if i[1] == "violated")
            + tuple(i for i in its if i[1] == "could-not-confirm"))
    # 违规优先（必须修），未确认次之。


def items_punchlist(findings, cap: int = 6) -> str:
    """把未决条目渲染成给 Actor 的编号"整改清单"。"""
    rows = unsettled_items(findings)[:cap]
    # 最多列 6 条。
    if not rows:
        return ""
        # 无未决条目：空串。
    out = ["", "THE UNSETTLED ITEMS (address these specifically):"]
    for n, (req, st, ev) in enumerate(rows, 1):
        label = "VIOLATED" if st == "violated" else "COULD NOT CONFIRM"
        out.append(f"  {n}. [{label}] {req}" + (f" — {ev}" if ev else ""))
        # 编号行：状态标签 + 需求 +（证据）。
    settled = sum(1 for i in (getattr(findings, "items", ()) or ()) if i[1] == "met")
    out.append(f"  ({settled} other requirement(s) it judged met — leave those alone.)")
    # 尾注：已判定满足的数量——别动它们。
    return "\n".join(out)


def _probe_ok(p: str) -> bool:
    """检查者探针的只读守卫（与检查探针同一 v16 lint：带引号的解释器体被
    遮蔽，python 的 '>' 比较不会再被误读为 shell 重定向；允许 /tmp 草稿区
    写入）。"""
    if not isinstance(p, str) or not p.strip():
        return False
        # 非字符串或空白：直接不允许。
    return not _nonscratch_mutations(p)
    # 只要有任何"非草稿区改动"特征即拒绝（写/移/删/装）。


def _probe_informative(out: str) -> bool:
    """探针是否真的返回了可用证据？超时、空输出、命令不存在都是"无信息"
    ——完全建立在这些之上的检查什么也没验证，(v16) 不得给出 'pass'
    （对一个从未读过内容的任务给出仅含元数据的 'pass' 是虚假接受）。"""
    o = (out or "").strip()
    if not o or "timed out" in o[:60] or o.startswith("[channel error"):
        return False                     # v32.1: 死掉的传输什么也证明不了
    low = o.lower()
    return not (low.endswith("not found") or "command not found" in low
                or "no such file" in low and len(o) < 80)
    # "not found" 类输出（且极短）按无信息处理。


def _vision_probe(cfg, vm, path: str, question: str) -> str:
    """v24：给文本失明的检查者一次"眼睛"探测。从机器取图并问视觉模型，
    返回其文本观察——使视觉类需求（位于/指向/尺寸颜色/外观像）可被确认或
    证伪。失败返回 '(...)'，否则返回视觉读数。"""
    try:
        from core.imagery import fetch_look_image           # 延迟导入：避免循环依赖
        data, err = fetch_look_image(vm, path)              # v26: 文档自动渲染
    except Exception as e:                                  # noqa: BLE001
        return f"(could not read {path}: {e})"
        # 取图失败：返回括号错误说明（调用方据此识别非读数）。
    if not data:
        return f"(could not read {path}: {err})"
        # 空数据：同样视为取图失败。
    try:
        from core.eyes import ensemble_look                 # 延迟导入：避免循环依赖
        from dataclasses import replace as _dc_replace
        eyes = _verifier_eyes_model(cfg)
        if eyes and eyes != getattr(cfg, "vision_model", ""):
            cfg = _dc_replace(cfg, vision_model=eyes)        # v39.1: 检查者自己的路线
        q = question or ("Describe what is shown and whether it satisfies the stated "
                         "requirement; read any text verbatim.")
        # 问题缺省：描述所见 + 是否满足需求 + 逐字读文本。
        # v30：检查者的眼睛与 Actor 走同一个 look-ensemble——检查者一次读错
        # 就能把正确的 run 带偏（t063 seed990：一个读者把雨滴叠层叫成"三角
        # 形"，Agent 绕着它空转）。
        ans = ensemble_look(cfg, q, [data])
        return (ans or "").strip() or "(vision returned nothing)"
        # 返回视觉读数（空则如实说明）。
    except LLMTransportError:
        raise                         # 供应商状态不是视觉观察（交由暂停机制）
    except Exception as e:                                  # noqa: BLE001
        return f"(vision probe error: {e})"
        # 其他错误降级为错误说明。


# ---- R2a 外观门（verifier_appearance_attach） ------------------------------------
# 给检查者自己打了 [APPEARANCE] 标签却迟迟不解决的需求提供"机械视觉"：
# harness 渲染本次运行触碰过的交付物，并注入视觉读数。构造上通用：路径来自
# 溯源（``context`` 里的 surface_delta）、指令（任务输入）与检查者自己的探针
# 输出——绝不来自 harness 已知的名字。渲染机器（文件类型表、命令构造器）
# 在 core/render.py——纯基础设施，按契约不含任何提示词；每个面向模型的
# 字符串都留在这里、这个被审计的表面。

_APPEAR_TAG_RE = re.compile(r"^.*\[APPEARANCE\].*$", re.MULTILINE)
# [APPEARANCE] 标记行匹配器（多行模式逐行匹配）。


def _destination_notes(instruction: str, context: str) -> str:
    """P1：任务文本点名、但本次运行从未触碰的文件的"中性 FYI"行。
    纯字符串匹配（named_files 是词法的；台账是 mtime 事实）——未被触碰的
    点名文件是"致命"（本应交付）还是"预期"（只读输入），由检查者判断。
    最多 3 行。"""
    from core.render import named_files
    notes = []
    ctx = context or ""
    for base in named_files(instruction):
        if base not in ctx:
            notes.append(f"FYI: the task text mentions '{base}' — this file was NOT "
                         "created or modified by this run.")
            # 任务点名但上下文中未出现的文件：中性提示。
        if len(notes) >= 3:
            break
            # 封顶 3 条。
    return ("\n" + "\n".join(notes)) if notes else ""
    # 拼接为前置文本（无则空串）。


def _appearance_reqs(history) -> list:
    """检查者"第一答"中打了 [APPEARANCE] 标签的需求行。"""
    for m in history:
        if isinstance(m, dict) and m.get("role") == "assistant":
            return [l.strip()[:240] for l in
                    _APPEAR_TAG_RE.findall(str(m.get("content", "")))][:4]
            # 只看第一条助手消息，最多 4 行（每行截 240）。
    return []


def _appearance_attach(cfg, vm, instruction, context, history):
    """渲染至多 appearance_attach_max 个交付物，并经检查者的眼睛读取。
    返回 (证据块列表, 有信息量条数)。"""
    reqs = _appearance_reqs(history)
    if not reqs:
        return [], 0
        # 没有外观需求：无事可做。
    from core.render import harvest_paths, render_cmd
    vm.run_command("mkdir -p /tmp/rsiagent_insp", timeout=10)
    # 建客户机内暂存目录（草稿区，允许写入）。
    blocks, n = [], 0
    for i, p in enumerate(harvest_paths(instruction, context, history)):
        if n >= getattr(cfg, "appearance_attach_max", 2):
            break
            # 达到附着上限：停。
        out_png = f"/tmp/rsiagent_insp/attach_{i}.png"
        cmd = render_cmd(p, out_png)
        if not cmd:
            continue
            # 该类型无法渲染：跳过。
        vm.run_command(cmd, timeout=90)
        chk = vm.run_command(f"test -s {out_png!r} && echo RENDER_OK", timeout=10)
        if "RENDER_OK" not in (chk or ""):
            continue
            # 渲染失败（无非空输出）：跳过。
        q = ("APPEARANCE requirements under inspection:\n- " + "\n- ".join(reqs) +
             "\nDescribe exactly what is visually present that bears on these; "
             "read any visible text verbatim; note position/color/layout facts.")
        # 视觉提问：在检的外观需求 + 要求逐字读文本、记录位置/颜色/布局事实。
        va = _vision_probe(cfg, vm, out_png, q)
        if va and not va.startswith("("):
            n += 1
            blocks.append(f"[HARNESS-ATTACHED VISUAL EVIDENCE — mechanical render "
                          f"of run-touched deliverable {p}]\n{va}"[:1800])
            # 有效读数：作为机械渲染证据注入（截 1800 字符）。
    return blocks, n


_AGENTIC_VERDICT_TOKEN = re.compile(
    r"VERDICT:\s*(PASS|FAIL|UNVERIFIED)", re.IGNORECASE)
# 判定令牌：VERDICT: PASS|FAIL|UNVERIFIED（练习协议的词法约束）。
_AGENTIC_ROUTE_TOKEN = re.compile(
    r"ROUTE:\s*(HANDOFF|REVISE|EVOLVE)", re.IGNORECASE)
# 路由令牌：统一循环下 Verifier 只出路由词。
_AGENTIC_STAGE_TOKEN = re.compile(
    r"STAGE:\s*(ORIENTATION_READY)", re.IGNORECASE)
# 阶段令牌：候选盲定向完成的标记。


def _report_transport_lines(text: str):
    """产出一个"词法行视图"，不改写已归档的报告字节。

    某些聊天传输会把 JSON 转义的换行留成 Program ``code`` 字符串里两个字面
    字节 ``\\n``。报告通道是面向行的，所以只把转义的 CR/LF 写法当作令牌识别
    的分隔符。不要反转义其他内容，也不要改动作为证据保留的报告。
    """
    lexical = str(text or "")
    # 只在本地视图上做替换（原文本不动）。
    lexical = lexical.replace(r"\r\n", "\n")
    lexical = lexical.replace(r"\n", "\n").replace(r"\r", "\n")
    # 把字面 "\\n"/"\\r" 转义写法视为换行（仅为令牌识别）。
    return lexical.splitlines()
    # 返回词法行列表。


def _parse_agentic_verifier_report(
        text: str, allow_evolve: bool = False,
        allow_unverified: bool = False):
    """从无歧义的自由格式报告令牌解析出一个内部路由。

    报告属于 Verifier Agent。这个解析器刻意只是"传输解析器"：忽略 Markdown
    围栏/缩进代码里的示例，要求恰一个无歧义的独立决定。重复同一决定是传输
    幂等的；相互冲突的决定仍然无效。解析器不检查、不总结、不截断、不重释
    报告的任何其他字节。
    """
    token = _AGENTIC_ROUTE_TOKEN if allow_evolve else _AGENTIC_VERDICT_TOKEN
    # 按模式选令牌：路由词或判定词。
    matches = []
    fence = None
    for line in _report_transport_lines(text):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
            # 围栏行：切换状态、不参与匹配。
        if fence is not None or line.startswith(("\t", "    ")):
            continue
            # 围栏内或缩进代码块：忽略（那只是报告里的示例）。
        match = token.fullmatch(stripped)
        if match:
            matches.append(match.group(1).upper())
            # 收集整行匹配的令牌。
    if not matches or len(set(matches)) != 1:
        return None
        # 零个或多个不同令牌：无歧义性不成立，拒绝。
    if allow_evolve:
        return {
            "HANDOFF": "pass",
            "REVISE": "wrong",
            "EVOLVE": "evolve",
        }[matches[0]]
        # 路由词映射为内部判定。
    if matches[0] == "UNVERIFIED":
        return "unverified" if allow_unverified else None
        # UNVERIFIED 仅在显式允许时有效。
    return "pass" if matches[0] == "PASS" else "wrong"
    # PASS/FAIL 映射为 pass/wrong。


def _parse_agentic_orientation_report(text: str):
    """候选盲定向令牌的"仅传输"解析器。"""
    matches = []
    fence = None
    for line in _report_transport_lines(text):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
            # 围栏行不参与匹配。
        if fence is not None or line.startswith(("\t", "    ")):
            continue
            # 围栏内/缩进代码不参与。
        match = _AGENTIC_STAGE_TOKEN.fullmatch(stripped)
        if match:
            matches.append(match.group(1).upper())
            # 收集独立 STAGE 令牌。
    return ("orientation_ready"
            if matches and set(matches) == {"ORIENTATION_READY"}
            else None)
    # 恰好只有 ORIENTATION_READY 才算完成。


def _agentic_substantive_checkpoint(previous: list, raw: list):
    """把一个 Verifier Agent 的历史保留到它最后一次真实机器动作。

    ``run_attempt`` 追加完整的 user/assistant 对。Program 或 Look 是实质动作；
    仅仅许诺未来动作的叙述不是。最后一个动作产生的环境观察就是其后的 user
    消息，所以把它的完整文本与持久图像附件分开返回，供恢复提示无损投递。

    完整的 ``raw`` 历史仍由该段的 ArtifactSink 归档。这个 helper 只决定什么
    被允许回到活动语义上下文。
    """
    base = list(previous or [])
    full = list(raw or [])
    if len(base) % 2 or len(full) % 2:
        raise ValueError("Verifier Agent histories must contain complete pairs")
        # 两个历史都必须是完整的 user/assistant 对。
    if full[:len(base)] != base:
        raise ValueError("Verifier Agent segment did not preserve its input history")
        # 段必须原样保留输入历史前缀（否则上下文管理出错）。

    last_end = None
    last_turn = None
    for assistant_index in range(len(base) + 1, len(full), 2):
        message = full[assistant_index]
        if message.get("role") != "assistant":
            continue
        turn = parse_turn(str(message.get("content", "")))
        if isinstance(turn, (Program, Look)):
            last_end = assistant_index + 1
            last_turn = turn
            # 记录最后一个实质动作的位置（含其后的观察 user 消息）。

    if last_end is None:
        return base, "", [], None
        # 本段没有任何实质动作：保持原状。
    observation = ""
    observation_images = []
    if last_end < len(full) and full[last_end].get("role") == "user":
        observation = str(full[last_end].get("content", ""))
        observation_images = durable_images(full[last_end])
        # 提取最后动作后的环境观察（文本 + 像素附件）。
    # 恰好在有效动作之后截停。保留观察需要其后再有一对助手消息；若该回复
    # 无动作，保留这一对会教会恢复后的解码器重复畸形叙述。调用方改为把
    # 确切的观察重附到下一条恢复 user 消息上。
    return full[:last_end], observation, observation_images, last_turn
    # 返回（截停历史, 待重附观察, 待重附图像, 最后动作）。


def _agentic_semantic_replay(raw: list):
    """把所有真实动作及其结果重新编码，剔除无动作轮次。

    这是事件触发的解码器恢复，不是上下文长度压缩：任何 Program、Look、程序
    轨迹、文本 look 结果或有效的动作推理都不会被截断或总结。从未引起机器
    动作的重复叙述/轻推留在已归档的原始轨迹里，但从活动上下文中移除。
    """
    full = list(raw or [])
    if len(full) % 2:
        raise ValueError("Verifier Agent history must contain complete pairs")
        # 必须是完整对。

    records = []
    for assistant_index in range(1, len(full), 2):
        assistant = full[assistant_index]
        if assistant.get("role") != "assistant":
            continue
        turn = parse_turn(str(assistant.get("content", "")))
        user = full[assistant_index - 1]
        # 只有真实机器动作才是语义 Verifier 历史。user 轮可能携带宝贵的 Look
        # 像素，但绝不能为了凑 user/assistant 配对而保留无动作的助手回复；
        # 像素改为作为前一 Look 之后的"待重附观察"随行。
        if not isinstance(turn, (Program, Look)):
            continue
            # 无动作回复：跳过（像素随后走 pending 通道）。
        observation = None
        if (assistant_index + 1 < len(full)
                and full[assistant_index + 1].get("role") == "user"):
            observation = dict(full[assistant_index + 1])
            # 该动作之后的环境观察。
        records.append((dict(user), dict(assistant), observation,
                        turn if isinstance(turn, (Program, Look)) else None))
        # 记录（原 user, 助手, 观察, 动作）。

    if not records:
        return [], "", [], None
        # 全程无实质动作：重放为空。

    replay = []
    previous_observation = None
    last_turn = None
    for index, (original_user, assistant, observation, turn) in enumerate(records):
        original_text = str(original_user.get("content", ""))
        previous_text = (str(previous_observation.get("content", ""))
                         if previous_observation else "")
        if index == 0:
            transport_text = original_text
            # 第一条：原样保留（开场信封）。
        else:
            parts = []
            if previous_text:
                parts.append(
                    "LOSSLESS RESULT OF YOUR PREVIOUS SUBSTANTIVE ACTION:\n---\n"
                    + previous_text + "\n---")
                # 前一动作的无损结果。
            if original_text and original_text != previous_text:
                parts.append(
                    "ORIGINAL CONTEXT THAT PRECEDED YOUR NEXT RECORDED ACTION:\n---\n"
                    + original_text + "\n---")
                # 下一个被记录动作之前的原始上下文（与结果不同才附）。
            transport_text = "\n\n".join(parts) or original_text
            # 拼装传输 user 文本。
        transport_user = {"role": "user", "content": transport_text}
        attachments = []
        if previous_observation:
            attachments.extend(durable_images(previous_observation))
            # 前一观察的像素随行。
        attachments.extend(durable_images(original_user))
        # 原 user 的像素（若有）也随行。
        transport_user = with_durable_images(transport_user, attachments)
        # 把像素附件挂回传输 user。
        replay += [transport_user, assistant]
        # 重放对：传输 user + 原助手（动作原样）。
        previous_observation = observation
        # 滚动更新"上一观察"。
        if turn is not None:
            last_turn = turn
            # 记录最后一个动作。

    pending = (str(previous_observation.get("content", ""))
               if previous_observation else "")
    # 最后动作之后的观察（待重附）。
    pending_images = (durable_images(previous_observation)
                      if previous_observation else [])
    # 其像素附件。
    return replay, pending, pending_images, last_turn
    # 返回（重放历史, 待重附文本, 待重附像素, 最后动作）。


def _agentic_image_bytes(attachments: list) -> list[bytes]:
    """解码并完整性校验已归档的 Look 像素，供重附。"""
    out = []
    for item in list(attachments or []):
        try:
            data = base64.b64decode(str(item["data"]), validate=True)
            # 严格 base64 解码（validate=True 拒绝畸形字符）。
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed durable Verifier image attachment") from exc
            # 结构畸形：fail closed。
        expected = str(item.get("sha256", ""))
        actual = hashlib.sha256(data).hexdigest()
        if expected and expected != actual:
            raise ValueError("durable Verifier image attachment failed integrity check")
            # sha256 不匹配：拒绝重附被篡改的像素。
        out.append(data)
    return out


def _agentic_actionless_fingerprints(history: list, start: int) -> set[str]:
    """畸形/无动作助手回复的"归一化身份"。

    这些只是传输诊断。它们既不给候选打分，也不施加重试次数；复发只是用来
    选择更强的格式恢复消息。
    """
    fingerprints = set()
    for assistant_index in range(start + 1, len(history or []), 2):
        message = history[assistant_index]
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content", ""))
        if parse_turn(content) is not None:
            continue
            # 能解析出动作的回复不算无动作。
        normalized = " ".join(content.lower().split()) or "(empty reply)"
        # 归一化：小写 + 空白折叠（空回复给哨兵名）。
        fingerprints.add(hashlib.sha256(normalized.encode(
            "utf-8", "replace")).hexdigest())
        # 指纹化，便于识别"同一畸形反复出现"。
    return fingerprints


def _agentic_recovery_prompt(report_channel: str, reason: str,
                              observation: str = "", last_turn=None,
                              observation_images=(),
                              recurrent: bool = False,
                              allow_evolve: bool = False,
                              allow_unverified: bool = False,
                              stage: str = "candidate_verification") -> str:
    """某段未发布报告之后的"仅传输"续传提示。"""
    recurrence = (
        "A normalized actionless response has recurred. Those raw responses remain "
        "archived for audit but were not fed back into your working context. "
        if recurrent else
        "The actionless tail remains archived for audit but was not fed back into "
        "your working context. ")
    # 复发与否选不同文案（都强调：原文已归档但未回灌上下文）。
    evidence = ""
    if observation:
        evidence = (
            "\n\nLOSSLESS RESULT OF YOUR LAST SUBSTANTIVE ACTION:\n---\n"
            + observation + "\n---")
        # 上次实质动作的无损结果。
    if isinstance(last_turn, Look):
        if observation_images:
            evidence += (
                "\n\nThe exact pixels from that successfully delivered Look are "
                "reattached to this recovery turn. If you need a newly captured "
                "state, reissue the Look before deciding.")
            # 像素已重附：需要新状态就重发 Look。
        else:
            evidence += (
                "\n\nNo pixel attachment accompanied that Look result. If pixels "
                "are needed, reissue the Look before deciding.")
            # 没有像素：需要就重发 Look。
    if stage == "orientation":
        judgment = "candidate or routing"
        publication = (
            "your private orientation handoff with STAGE: ORIENTATION_READY")
        # 定向阶段：发布阶段令牌。
    else:
        judgment = ("HANDOFF, REVISE, EVOLVE, or routing" if allow_evolve
                    else ("PASS, FAIL, UNVERIFIED, or correctness"
                          if allow_unverified else
                          "PASS, FAIL, or correctness"))
        # 判定词表按模式变化。
        publication = ("one HANDOFF / REVISE / EVOLVE route" if allow_evolve
                       else ("one PASS / FAIL / UNVERIFIED report"
                             if allow_unverified else "the complete report"))
        # 发布物按模式变化。
    return (
        "VERIFIER AGENT — SAME-CONTEXT TRANSPORT RECOVERY.\n\n"
        + recurrence
        + f"No {judgment} judgment was inferred from the transport "
          "failure, and your authority over the investigation is unchanged. "
          f"The prior segment did not publish a usable report through "
          f"{report_channel}: "
          f"{reason}. Continue from your last substantive checkpoint. Take your "
          "next real step with one ordinary Program or Look JSON action. When your "
          "decision is ready, use a verifier-report Program action to publish the "
          f"complete report with {publication} to the host harness; do not merely "
          "announce that you "
          "will do so."
        + evidence)
    # 恢复提示：没有从传输失败推断任何判定、调查权不变；继续干，决策就绪时
    # 用 verifier-report 程序动作发布（不要只是宣布）。


# 【中文注释】verify_agentic：运行一次"持久、效果隔离"的 Verifier 智能体检查
# （基准默认路径）。Verifier 保留普通持久对话、任意 code-as-policy 与 Look 通道；
# 其 Program 执行器把候选文件系统变为只读，隔离进程/网络/GUI 副作用，同时保留
# 一块私有可写草稿区（scratch）——这是 Verifier 隔离原则的机械实现：它调查的
# 是"受回滚保护的候选"，Actor 私有工件被隐藏，其客户机内改动在检查后被回滚，
# Actor 的工作恢复时不残留 Verifier 的痕迹。配置中的数值上限与 wall_budget 只是
# 紧急看门狗，不是停止指令。session 传入 VerifierSession 即可跨修订延续同一上下文。
def verify_agentic(instruction: str, vm, cfg, sink=None, turn_no: int = 0,
                    context: str = "", session: list = None,
                    wall_budget: float = None,
                    stage: str = "candidate_verification",
                    continue_candidate: bool = False,
                    curriculum_review: str = "",
                    actor_evidence_leads: str = "",
                    iters_budget: int | None = None):
    """运行一次"持久、效果隔离"的 Verifier Agent 检查。

    Verifier 保留普通的持久 Agent 对话、任意 code-as-policy 与 Look 通道。
    它的 Program 执行器把候选文件系统变为只读，并隔离进程/网络/GUI 效果，
    同时保留一块私有可写草稿区（scratch）。所选配置中的数值上限与
    ``wall_budget`` 只是紧急看门狗，不是停止指令。
    """
    from config.settings import load
    from core.loop import run_attempt
    from core.trace import ArtifactSink
    from core.verifier_runtime import (
        AGENTIC_VERIFIER_NUDGE, AGENTIC_VERIFIER_PREMATURE_DONE,
        AGENTIC_VERIFIER_ROUTE_NUDGE,
        AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE,
        AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE,
        AGENTIC_VERIFIER_ORIENTATION_NUDGE,
        AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE,
        AGENTIC_VERIFIER_STRICT_NUDGE, AgenticVerifierExecutor, AgenticVerifierInfrastructureError,
        AgenticVerifierNoProgressError,
        agentic_verifier_system_for, agentic_verifier_user_message,
        validate_verifier_look_path)
    from explore.charter import (
        agent_harness_verifier_charter,
        agent_harness_verifier_orientation_charter,
    )
    # 延迟导入：运行时组件、角色 nudges、执行器、系统提示构造器与章程。

    if stage not in {"orientation", "candidate_verification"}:
        raise ValueError(f"unsupported Verifier Agent stage: {stage!r}")
        # 阶段必须二选一。
    if stage == "orientation" and (continue_candidate or curriculum_review):
        raise ValueError("orientation cannot continue a candidate review")
        # 定向阶段与候选续查互斥（定向必须候选盲）。
    if curriculum_review and not continue_candidate:
        raise ValueError(
            "a Curriculum review may only continue the unchanged candidate")
        # 课程复审只能针对"未改变的候选"。

    verifier_cfg = load(getattr(cfg, "agentic_verifier_config", ""))
    # 加载 Verifier 控制面配置。
    # 通用交接传输，与自进化的 Agent 阶段一致。这些开关不决定如何调查、
    # 也不决定证据何时算充分。
    verifier_cfg.practice_mode = True
    verifier_cfg.independent_verify = False
    verifier_cfg.agent_decided_stop = True
    verifier_cfg.max_resumes = 0
    # 练习模式/关内置验证/Agent 自定停止/禁续跑——传输由本函数管理。
    # ``verifier_evolve_route`` 保持统一 harness 的 fail-closed 终局传输可用。
    # 但在分权设计中，本地正确性与高层搜索是不同的权威：本 Agent 只说
    # PASS/FAIL，每个 FAIL 之后由持久 Curriculum Agent 路由。
    allow_evolve = (
        bool(getattr(cfg, "verifier_evolve_route", False))
        and not bool(getattr(cfg, "verifier_local_verdict_only", False)))
    # 是否启用路由令牌（EVOLVE 语义）。
    orienting = stage == "orientation"
    # 是否为候选盲定向阶段。
    allow_unverified = (
        bool(getattr(cfg, "verifier_unverified_evidence", False))
        and not allow_evolve and not orienting)
    # 是否允许 UNVERIFIED 令牌（与路由/定向互斥）。
    hide_actor_memory = bool(getattr(
        cfg, "verifier_hide_actor_memory", False))
    private_paths = tuple(getattr(cfg, "verifier_private_paths", ()) or ())
    execution_mode = str(getattr(
        cfg, "verifier_execution_mode", "effect_isolated")
        or "effect_isolated")
    # Actor 私有面隐藏、私有路径集合与执行隔离模式。
    terminal_token = ("stage token" if orienting else
                      ("route" if allow_evolve else "verdict"))
    # 终局令牌的种类名（错误消息用）。

    persistent = session if session is not None else VerifierSession()
    # 持久会话：传入或新建。
    stage_lifecycle = bool(getattr(cfg, "verifier_stage_lifecycle", False))
    if stage_lifecycle and not isinstance(persistent, VerifierSession):
        raise TypeError(
            "the staged Verifier lifecycle requires one VerifierSession")
        # 阶段生命周期必须配 VerifierSession（要阶段/代数字段）。
    if isinstance(persistent, VerifierSession):
        persistent.inspections += 1
        inspection_no = persistent.inspections
        # 检查编号（跨修订累计）。
        persistent.stage = stage.upper()
        if orienting:
            persistent.orientation_runs += 1
            # 定向运行计数。
        else:
            if continue_candidate:
                if persistent.candidate_generation < 1:
                    raise ValueError(
                        "cannot continue a candidate before its first inspection")
                    # 首查之前不能"续查"。
            else:
                persistent.candidate_generation += 1
                # 新候选：代数 +1。
        candidate_generation = max(1, persistent.candidate_generation)
        persistent.emit(
            "VERIFIER_STAGE_STARTED", stage=persistent.stage,
            inspection=inspection_no,
            candidate_generation=(None if orienting
                                  else candidate_generation))
        # 发阶段开始事件。
    else:
        inspection_no = 1
        candidate_generation = inspection_no
        # 普通列表会话：退化为单次编号。
    current_history = list(persistent)
    # 本检查的起点历史（跨修订延续）。
    report_channel = (f"verifier-report channel for turn {turn_no}, "
                      f"inspection {inspection_no}")
    # 报告通道描述（章程与恢复提示里引用）。
    verifier_cfg.practice_done_requires = ""
    # done 不要求任何发布物：发布校验由执行器的 report validator 承担。
    if orienting:
        inspection_prompt = agent_harness_verifier_orientation_charter(
            instruction, report_channel, mechanical_context=context,
            execution_mode=execution_mode)
        # 定向章程（候选盲：不给候选上下文）。
    else:
        inspection_prompt = agent_harness_verifier_charter(
            instruction, report_channel, mechanical_context=context,
            allow_evolve=allow_evolve,
            candidate_generation=candidate_generation,
            curriculum_review=curriculum_review,
            allow_unverified=allow_unverified,
            execution_mode=execution_mode,
            actor_evidence_leads=actor_evidence_leads)
        # 候选验证章程（可附课程复审意见与 Actor 证据线索）。
    prompt = inspection_prompt
    persist_scratch = bool(getattr(cfg, "verifier_persist_scratch", False))
    if persist_scratch and isinstance(persistent, VerifierSession):
        program_executor = persistent.acquire_executor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=private_paths,
            execution_mode=execution_mode)
        owns_executor = False
        # 持久模式：跨检查复用执行器（草稿区续用）。
    else:
        program_executor = AgenticVerifierExecutor(
            vm, hide_actor_memory=hide_actor_memory,
            private_paths=private_paths,
            execution_mode=execution_mode)
        owns_executor = True
        # 临时模式：本检查独占执行器，finally 里关闭。
    program_executor.clear_published_report()
    # 清掉上一次检查遗留的已发布报告。
    if orienting:
        program_executor.set_report_validator(
            _parse_agentic_orientation_report,
            "exactly one unambiguous standalone STAGE: ORIENTATION_READY line",
        )
        # 定向：发布校验器查 STAGE 令牌。
    elif allow_evolve:
        program_executor.set_report_validator(
            lambda report: _parse_agentic_verifier_report(
                report, allow_evolve=True),
            "one unambiguous standalone ROUTE: HANDOFF, ROUTE: REVISE, or "
            "ROUTE: EVOLVE line",
        )
        # 路由模式：查 ROUTE 令牌。
    else:
        program_executor.set_report_validator(
            lambda report: _parse_agentic_verifier_report(
                report, allow_unverified=allow_unverified),
            ("one unambiguous standalone VERDICT: PASS, VERDICT: FAIL, or "
             "VERDICT: UNVERIFIED line" if allow_unverified else
             "one unambiguous standalone VERDICT: PASS or VERDICT: FAIL line"),
        )
        # 判定模式：查 VERDICT 令牌（按需允许 UNVERIFIED）。
    verifier_system = agentic_verifier_system_for(
        execution_mode, orienting=orienting, allow_evolve=allow_evolve,
        allow_unverified=allow_unverified)
    # 按阶段/模式组装系统提示。
    if hide_actor_memory or private_paths:
        verifier_system += (
            "\nHarness-owned Actor-private paths are mechanically hidden from your "
            "Programs and file Looks. Verify authoritative inputs and the candidate "
            "surface; private policy/scratch contents are not correctness evidence.\n")
        # 追加"私有面已被机械隐藏"的说明（防它把私有内容当证据）。
    if orienting:
        action_nudge = AGENTIC_VERIFIER_ORIENTATION_NUDGE
        strict_action_nudge = AGENTIC_VERIFIER_ORIENTATION_NUDGE
        premature_done_nudge = \
            AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE
        # 定向阶段的 nudges。
    else:
        action_nudge = (AGENTIC_VERIFIER_ROUTE_NUDGE if allow_evolve
                        else AGENTIC_VERIFIER_NUDGE)
        strict_action_nudge = (
            AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE if allow_evolve
            else AGENTIC_VERIFIER_STRICT_NUDGE)
        premature_done_nudge = (
            AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE if allow_evolve
            else AGENTIC_VERIFIER_PREMATURE_DONE)
        # 候选验证阶段的 nudges（路由/判定两套）。

    configured_wall = float(getattr(verifier_cfg, "wall_clock_secs", 86400) or 86400)
    available_wall = configured_wall if wall_budget is None else min(
        configured_wall, max(0.0, float(wall_budget)))
    deadline = time.time() + available_wall
    # 检查墙钟 deadline：配置值与传入预算取小。
    continuing = bool(current_history)
    # 是否为同上下文续传。
    temp_dir = None
    if sink is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="rsiagent-agentic-verifier-")
        verifier_root = temp_dir.name
        # 无 sink：用临时目录承载段归档。
    else:
        verifier_root = os.path.join(
            sink.root, "verifier_agent", f"inspection_{inspection_no:03d}")
        # 有 sink：按检查编号建归档目录。

    # 崩溃的进程可能带着同一个 sink 被续跑。绝不覆盖其原始段归档；
    # 从已持久化的最大段号之后追加。
    segment = 0
    if os.path.isdir(verifier_root):
        persisted = []
        for name in os.listdir(verifier_root):
            match = re.fullmatch(r"segment_(\d+)", name)
            if match:
                persisted.append(int(match.group(1)))
        if persisted:
            segment = max(persisted) + 1
            # 扫描已有段号，续跑从最大号 +1 开始。

    last_reason = "Verifier Agent did not publish a valid report"
    # 最后的失败原因（未发布有效报告为缺省）。
    infrastructure_failure = False
    # 是否发生了基础设施失败。
    progress_blocked = False
    # 是否被"无进展守卫"阻断。
    pending_observation = str(getattr(
        persistent, "pending_observation", "") or "")
    pending_images = list(getattr(persistent, "pending_images", []) or [])
    pending_turn = getattr(persistent, "pending_turn", None)
    # 取崩溃恢复元数据（待重附的观察/像素/最后动作）。
    semantic_replay_active = bool(getattr(
        persistent, "transport_recurrence", False))
    # 语义重放是否处于激活态。
    if semantic_replay_active and current_history:
        replayed, replay_observation, replay_images, replay_turn = \
            _agentic_semantic_replay(current_history)
        if replayed:
            current_history = replayed
            pending_observation = replay_observation or pending_observation
            pending_images = replay_images or pending_images
            pending_turn = replay_turn or pending_turn
            persistent[:] = current_history
            # 激活态续跑：先把历史做一次语义重放（剔除无动作尾巴）。
    # ``inspection_prompt`` 是本候选不可变的权威信封：逐字任务、机械候选
    # 上下文与路由章程。它不进入 ``current_history``，直到本次检查产出第一个
    # 实质 Program/Look。因此首个段落若是干回合，必须完整重述它；否则传输
    # 恢复只以"继续"开场，逼 Verifier 从文件系统猜任务。
    inspection_anchored = False
    if current_history and (pending_observation or pending_images
                            or pending_turn is not None):
        recovery_prompt = _agentic_recovery_prompt(
            report_channel,
            "the prior process stopped after your last substantive action",
            observation=pending_observation, last_turn=pending_turn,
            observation_images=pending_images,
            allow_evolve=allow_evolve,
            allow_unverified=allow_unverified, stage=stage)
        prompt = inspection_prompt + "\n\n" + recovery_prompt
        # 崩溃恢复开场：权威信封 + 恢复提示（含待重附观察）。
    seen_actionless = set()
    # 已见过的无动作指纹集合。
    actionless_segments = 0
    # 连续无动作段数。
    remaining_iters = iters_budget
    # 剩余迭代预算（可空）。
    try:
        while True:
            # 段循环：跑一段 -> 检查点 -> 判定或恢复。
            if remaining_iters is not None and remaining_iters <= 0:
                last_reason = "Verifier Agent reached its remaining iteration ceiling"
                break
                # 迭代预算耗尽。
            remaining = deadline - time.time()
            if remaining <= 0:
                last_reason = "Verifier Agent reached the run's emergency wall ceiling"
                break
                # 墙钟耗尽。
            segment_root = os.path.join(verifier_root, f"segment_{segment:03d}")
            segment_sink = ArtifactSink(segment_root)
            # 本段轨迹目录与接收器。
            published = {}
            segment_base = list(current_history)
            # 本段起点历史（用于检查点截停）。

            def terminal_report_ready():
                # 终局探针：报告一经发布且可解析为合法令牌，立即停（发布即提交）。
                report = program_executor.published_report
                verdict = (_parse_agentic_orientation_report(report)
                           if orienting else
                           _parse_agentic_verifier_report(
                               report, allow_evolve=allow_evolve,
                               allow_unverified=allow_unverified))
                if verdict is None:
                    return False
                published.update(report=report, verdict=verdict)
                return True

            result, raw_history = run_attempt(
                prompt, vm, verifier_cfg, segment_sink,
                wall_budget=remaining,
                **({"iters_budget": remaining_iters} if remaining_iters is not None else {}),
                initial_history=current_history,
                continue_context=continuing,
                terminal_handoff_ready=terminal_report_ready,
                program_executor=program_executor,
                look_path_validator=lambda path: validate_verifier_look_path(
                    path, hide_actor_memory=hide_actor_memory,
                    private_paths=private_paths),
                look_fetcher=program_executor.fetch_look_image,
                system_prompt=verifier_system,
                action_nudge=action_nudge,
                strict_action_nudge=strict_action_nudge,
                premature_done_nudge=premature_done_nudge,
                instruction_is_complete_opening=True,
                user_message_transform=lambda message: agentic_verifier_user_message(
                    message, allow_evolve=allow_evolve,
                    stage=("orientation" if orienting else
                           "candidate_verification"),
                    execution_mode=execution_mode,
                    allow_unverified=allow_unverified),
                opening_image=(_agentic_image_bytes(pending_images)
                               if pending_images else None))
            # 跑一段 Verifier：全部以注入件替换——受信执行器、Look 校验器/取图器、
            # 系统/轻推提示、完整开场信封与待重附像素。
            if remaining_iters is not None:
                remaining_iters -= max(1, int(getattr(result, "turns", 0)
                                              or getattr(result, "iters", 0)))
                # 扣减本段消耗（取 turns 或 iters，至少 1）。
            # run_attempt 把可恢复的供应商/API 停机排除在活动工作时钟之外。
            # 让外层检查 deadline 与同一时钟对齐，402/429 停机就不能悄悄耗尽
            # Verifier 的权威。
            pause_secs = float(
                getattr(result, "infra_pause_secs", 0.0) or 0.0)
            pause_events = int(getattr(result, "infra_pauses", 0) or 0)
            deadline += pause_secs
            # deadline 后移 = 本段停机不计入检查预算。
            if isinstance(persistent, VerifierSession):
                persistent.infra_pause_secs += pause_secs
                persistent.infra_pauses += pause_events
                # 累计到持久会话（外层 Actor 时钟据此扣减）。
            current_history, new_observation, new_images, new_turn = \
                _agentic_substantive_checkpoint(segment_base, raw_history)
            # 实质检查点：历史截停到最后真实动作，观察/像素拆出待重附。
            advanced = new_turn is not None
            actionless_segments = 0 if advanced else actionless_segments + 1
            # 有实质动作则连续无动作计数清零。
            if advanced:
                # 检查点现已包含紧邻本动作之前、承载完整检查信封的 user 轮。
                # 后续恢复可以从历史继续，无需重复信封。
                inspection_anchored = True
                pending_observation = new_observation
                pending_images = new_images
                pending_turn = new_turn
                # 更新待重附状态。
            actionless = _agentic_actionless_fingerprints(
                raw_history, len(segment_base))
            recurrent = bool(actionless & seen_actionless)
            seen_actionless.update(actionless)
            # 收集本段无动作指纹并检测复发。
            semantic_replay_activated = False
            if recurrent and not advanced and not semantic_replay_active:
                replayed, replay_observation, replay_images, replay_turn = \
                    _agentic_semantic_replay(current_history)
                if replayed and len(replayed) < len(current_history):
                    current_history = replayed
                    pending_observation = (
                        replay_observation or pending_observation)
                    pending_images = replay_images or pending_images
                    pending_turn = replay_turn or pending_turn
                    semantic_replay_active = True
                    semantic_replay_activated = True
                    # 第一个真实动作可能紧跟在对开场的干回复之后。重放会丢掉
                    # 那一对承载权威的信封。在真实动作重新把它锚进恢复历史
                    # 之前，完整重述不变的检查信封。
                    inspection_anchored = False
                    log.warning(
                        "recurring actionless verifier transport -> semantic replay "
                        "(%d -> %d pairs; all actions/results preserved)",
                        len(segment_base) // 2, len(current_history) // 2)
                    # 留痕：语义重放激活（所有动作/结果均保留）。
            if isinstance(persistent, VerifierSession):
                persistent.pending_observation = pending_observation
                persistent.pending_images = list(pending_images)
                persistent.pending_turn = pending_turn
                persistent.transport_recurrence = semantic_replay_active
                # 崩溃恢复元数据回写会话。
            persistent[:] = current_history
            # 会话历史同步为检查点后的历史。

            # 原始轨迹已包含每个被丢弃的字节。这个紧凑边车让恢复边界直接可
            # 审计，且不复制、不截断任何对话内容。
            recovery = {
                "status": str(getattr(result, "status", "infra")),
                "history_pairs_before": len(segment_base) // 2,
                "raw_history_pairs": len(raw_history) // 2,
                "checkpoint_history_pairs": len(current_history) // 2,
                "checkpoint_advanced": advanced,
                "last_substantive_action": (
                    type(new_turn).__name__ if new_turn is not None else None),
                "pending_observation_chars": len(pending_observation),
                "pending_observation_images": len(pending_images),
                "discarded_tail_pairs": (
                    len(raw_history) - len(current_history)) // 2,
                "normalized_actionless_recurrence": recurrent,
                "semantic_replay_activated": semantic_replay_activated,
                "semantic_replay_active": semantic_replay_active,
                "actionless_segments": actionless_segments,
                "max_actionless_segments": _MAX_ACTIONLESS_VERIFIER_SEGMENTS,
                "raw_transcript": "transcript.json",
            }
            # 恢复边车：纯计数/布尔/指针，零内容复制。
            segment_sink.save_recovery(recovery)

            report = published.get("report", "")
            verdict = published.get("verdict")
            if verdict is None:
                # 探针未捕获：段结束后再读一次执行器上的已发布报告。
                report = program_executor.published_report
                read_reason = ("report was not published"
                               if not report else
                               f"report lacks one unambiguous standalone "
                               f"{terminal_token} token")
                # 记录"为什么没读到判定"。
                verdict = (_parse_agentic_orientation_report(report)
                           if orienting else
                           _parse_agentic_verifier_report(
                               report, allow_evolve=allow_evolve,
                               allow_unverified=allow_unverified))
            else:
                read_reason = ""
                # 探针已捕获：无需解释。
            if verdict is not None:
                # 拿到合法终局令牌：本检查结束。
                if orienting:
                    persistent.pending_observation = ""
                    persistent.pending_images = []
                    persistent.pending_turn = None
                    persistent.stage = "ORIENTATION_READY"
                    persistent.emit(
                        "VERIFIER_STAGE_COMPLETED",
                        stage="ORIENTATION",
                        inspection=inspection_no,
                        candidate_generation=None)
                    # 定向完成：清待重附状态、更新阶段、发事件。
                    findings = Findings(report)
                    findings.doubts = ""
                    findings.items = ()
                    if sink is not None:
                        sink.save_verify2(
                            turn_no, "orientation_ready", findings,
                            list(persistent))
                    return "orientation_ready", findings
                    # 返回定向完成。

                findings = Findings(report)
                findings.doubts = ""
                findings.items = ()
                # 判定报告本身即发现文本（无旁路数据）。
                if isinstance(persistent, VerifierSession):
                    persistent.pending_observation = ""
                    persistent.pending_images = []
                    persistent.pending_turn = None
                    persistent.stage = (
                        "ROUTE_COMMITTED" if allow_evolve
                        else ("EVIDENCE_REQUESTED"
                              if verdict == "unverified"
                              else "VERDICT_COMMITTED"))
                    # 阶段推进：路由已提交/证据已请求/判定已提交。
                    completion = {
                        "stage": "CANDIDATE_VERIFICATION",
                        "inspection": inspection_no,
                        "candidate_generation": candidate_generation,
                    }
                    if allow_evolve:
                        completion["route"] = {
                            "pass": "HANDOFF", "wrong": "REVISE",
                            "evolve": "EVOLVE"}.get(verdict, verdict)
                    else:
                        completion["verdict"] = {
                            "pass": "PASS", "wrong": "FAIL",
                            "unverified": "UNVERIFIED",
                        }.get(verdict, verdict)
                    persistent.emit("VERIFIER_STAGE_COMPLETED", **completion)
                    # 发完成事件（路由词或判定词）。
                if sink is not None:
                    sink.save_verify2(turn_no, verdict, findings,
                                      list(persistent))
                    # 判定与会话落盘。
                return verdict, findings
                # 返回（判定, 发现）。

            status = str(getattr(result, "status", "infra"))
            # 本段终态。
            if status == "done":
                last_reason = (read_reason or
                               f"report lacks one unambiguous standalone "
                               f"{terminal_token} token")
                # done 但报告不可解析：以读取原因作为恢复理由。
            elif status in {"stalled", "stalled_quiescent", "budget"}:
                last_reason = "Verifier Agent ended a segment before publishing its decision"
                # 停滞/预算：段在发布决定前结束。
            else:
                last_reason = (f"Verifier Agent ended at emergency/infrastructure "
                               f"status {status}")
                if status == "infra":
                    infrastructure_failure = True
                break
                # 紧急状态：中断循环（infra 时标记）。
            if actionless_segments >= _MAX_ACTIONLESS_VERIFIER_SEGMENTS:
                last_reason = (
                    "Verifier Agent produced no Program or Look actions in "
                    f"{actionless_segments} consecutive segments; runtime recovery "
                    "is required before verification can continue")
                infrastructure_failure = True
                progress_blocked = True
                recovery.update(
                    status="infra", segment_status=status,
                    stop_reason="no_substantive_progress", reason=last_reason)
                segment_sink.save_recovery(recovery)
                log.error(last_reason)
                break
                # 连续多段无动作：机械活性守卫触发（绝不伪造判定）。
            recovery_prompt = _agentic_recovery_prompt(
                report_channel, last_reason,
                observation=pending_observation,
                last_turn=pending_turn,
                observation_images=pending_images, recurrent=recurrent,
                allow_evolve=allow_evolve,
                allow_unverified=allow_unverified,
                stage=stage)
            # 构造恢复提示（含待重附观察与复发文案）。
            prompt = (recovery_prompt if inspection_anchored else
                      inspection_prompt + "\n\n" + recovery_prompt)
            # 信封已锚进历史时只发恢复提示；否则重述完整权威信封。
            continuing = True
            segment += 1
            # 同上下文续传，段号推进。
    finally:
        if owns_executor:
            program_executor.close()
            # 临时执行器：无论成败都关闭（回滚候选）。
        if temp_dir is not None:
            temp_dir.cleanup()
            # 临时归档目录清理。

    if progress_blocked:
        raise AgenticVerifierNoProgressError(last_reason)
        # 无进展守卫触发：抛专用异常（调用方做运行时恢复）。
    if infrastructure_failure:
        raise AgenticVerifierInfrastructureError(last_reason)
        # 基础设施失败：抛专用异常（可回滚重试）。

    findings = Findings(last_reason)
    findings.doubts = last_reason
    findings.items = ()
    # 兜底：以 last_reason 构造"未验证"发现。
    if sink is not None:
        sink.save_verify2(turn_no, "unverified", findings, list(persistent))
    return "unverified", findings
    # 预算/其他原因未出判定：返回 unverified（绝不猜 pass/wrong）。


# 【中文注释】verify_independent：独立检查（v8 经典路径，非 agentic 路由时使用）。
# 返回 (verdict, findings)；verdict 为 "pass" | "wrong" | "unverified"。
# 刻意三态：判 "wrong" 必须有具体违规证据（must-fix）；无法证实也不谎报通过。
def verify_independent(instruction: str, vm, cfg, sink=None, turn_no: int = 0,
                       context: str = "", session: list = None,
                       wall_budget: float = None,
                       continue_candidate: bool = False,
                       curriculum_review: str = "",
                       actor_evidence_leads: str = ""):
    """运行独立检查。返回 (verdict, findings)，verdict 为 "pass" | "wrong" |
    "unverified"。可选的统一 Agent 路由也可返回 "evolve"；普通基准配置产生
    不了它。

    刻意三态："wrong" 需要具体的违规证据（必须修）；"unverified" 是
    未能确认——与错误不是一回事（浅检查不得否决它无法重新推导的深工作）。
    格式轻推不消耗探针轮次。``context``（v17）是附在任务后的机械文件变更
    证据——它绝不泄露 Actor 的推理，独立性因此保持。完整的检查者对话经
    ``sink`` 持久化以供事后剖析。

    ``session``（E4-A1， 的设计）：开启 cfg.verifier_continuity 时，调用方
    每次运行传入同一个 :class:`VerifierSession`。Verifier 的交互与其先前的
    探针/判定作为它自己的历史累积（与 Actor 一样），因此第 N 次检查发出的
    WRONG 在第 N+1 次仍被记得。机械证据新鲜度不累积：每次候选检查在 PASS
    之前必须读过当前工件，累计计数器只是遥测。对 Actor 对话的失明不动摇：
    会话只装 Verifier 自己的工作。遗留 list 仍只保留对话历史。
    开关关闭或 session 为 None -> 全新上下文，与 E3 逐字节一致。"""
    if getattr(cfg, "agentic_verifier_config", ""):
        return verify_agentic(
            instruction, vm, cfg, sink=sink, turn_no=turn_no,
            context=context, session=session, wall_budget=wall_budget,
            continue_candidate=continue_candidate,
            curriculum_review=curriculum_review,
            actor_evidence_leads=actor_evidence_leads)
        # 配置了 Agentic Verifier：整个转交 agentic 路径。

    model = cfg.verifier_model or cfg.model
    # 检查模型：verifier_model 优先，回落主模型。
    vsys = build_verifier_system(cfg)                    # v35: 与配置一致的机制描述
    continuity = session is not None and getattr(cfg, "verifier_continuity", False)
    # 是否启用跨检查会话连续性。
    history: list = session if continuity else []
    # 历史：连续性时复用会话，否则全新。
    evidence_session = (session
                        if continuity and isinstance(session, VerifierSession)
                        else None)
    # 仅当是 VerifierSession 才记遥测。
    if evidence_session is not None:
        evidence_session.inspections += 1
        # 检查计数 +1。
    if getattr(cfg, "verifier_destination_note", False):
        context = (context or "") + _destination_notes(instruction, context)
        # 可选：附加"任务点名但未触碰"的中性提示。
    elastic = getattr(cfg, "verifier_elastic_depth", False)
    # 是否弹性深度（无轮次预算，判断收束）。
    # E4-A1b：计数器退化为"失控兜底"（从不给判定的模型不能永远转下去）；
    # 200 约为旧上限的 33 倍，且从未有检查超过 7 轮。墙钟仍然约束运行本身。
    budget_rounds = 200 if elastic else cfg.verifier_probes
    nudge_cap = 20 if elastic else 6
    # 轮次预算与格式轻推上限（弹性模式放宽）。
    user = _opening(instruction, cfg.verifier_probes, context,
                    tag_appearance=getattr(cfg, "verifier_appearance_attach", False),
                    elastic=elastic)
    # 构造开场信封。
    if continuity and history:
        # E4-A1 桥：这是对同一 run 的"再检查"——检查者自己先前的工作就在历史
        # 里。先前的 WRONG 站得住，直到新证据解决它；已确立的不要重新推导。
        user += ("\n\nCONTINUING INSPECTION: you have inspected this run before — "
                 "your prior probes, findings and verdicts are in your conversation "
                 "above. Weigh them: a WRONG you issued stands until NEW evidence "
                 "resolves it; re-check the previously-failing points first. Prior "
                 "evidence guides you but may describe an earlier candidate: read "
                 "the current deliverable during this inspection before PASS.")
        # 续查提示（正文是提示词内容）。
    verdict = "unverified"
    findings = ("independent inspection reached no verdict within its probe budget — "
                "completion could not be confirmed or refuted")
    # 缺省结果：预算内未出判定。
    concluded = False
    # 是否已得出判定。
    informative = 0
    content_seen = 0
    # v16/v20：新鲜度对本候选检查局部。持久 Verifier 在语义上记得先前证据，
    # 但旧读数不能为 Actor 可能已修订的候选清除内容守卫。
    rounds_used, nudges = 0, 0        # v11: 格式轻推不再消耗调用
    canary_refunds = 0                # v32.2: 因停机退还的轮次（上限 2）
    #                                   ——限定探针轮次的预算里，被格式税
    #                                   拖累的模型不能反而拿到更少的证据
    #                                   （两者分别设上限防循环）
    vis_on = getattr(cfg, "vision_verify", True) and bool(_verifier_eyes_model(cfg))  # v24+v39.1
    # 视觉通道是否可用。
    looked = steered = False          # v27: 检查者是否用过眼睛/是否被提醒过
    attached = False                  # R2a: harness 附着的视觉证据是否已投递
    while rounds_used < budget_rounds and nudges < nudge_cap:
        # 主循环：轮次与轻推预算内反复采样。
        out = chat(model, vsys, user, max_tokens=cfg.verifier_max_tokens,
                   temperature=cfg.temperature, top_p=getattr(cfg, "top_p", -1.0),
                   reasoning_effort=cfg.reasoning_effort,
                   history=history,
                   reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0))
        # 调用检查模型。
        amsg = {"role": "assistant", "content": out or "(empty reply)"}
        if continuity and getattr(cfg, "reasoning_in_history", False):
            from llm.client import pop_last_reasoning   # E4-A1 ()：verifier 的
            _rsn = pop_last_reasoning()                  # 会话与 Actor 一样
            if _rsn:                                     # 在 RIH 下运行
                amsg["reasoning"] = _rsn
            # 连续性会话：完整的助手消息（含 reasoning）回历史。
        history += [{"role": "user", "content": user}, amsg]
        # 对话对入历史。
        obj = parse_object(out) or {}
        # 解析 JSON 对象。
        v = obj.get("verdict")
        if v == "fail":                                   # 兼容旧措辞
            v = "wrong"
        if v in ("pass", "wrong", "unverified"):
            # 得到判定。
            candidate_findings = _mk_findings(
                str(obj.get("findings", ""))[:2000], obj.get("doubts", ""),
                obj.get("items"))
            # 构造发现（含疑点与逐需求条目）。
            if (v == "pass" and candidate_findings.doubts.strip()
                    and getattr(cfg, "doubts_block_pass", False)):
                # 自由文本字段不是可靠的任务分类器。让同一个 Verifier Agent
                # 自行澄清其疑点是否真的阻塞任务，而不是机械地把正确的候选
                # 弹回给 Actor。
                findings = candidate_findings
                nudges += 1
                user = _PASS_DOUBT_CLARIFICATION
                log.info("verifier pass carried doubts -> same-agent clarification")
                continue
                # 带疑点的 pass：同 Agent 澄清。
            _blind = (content_seen == 0 if getattr(cfg, "verifier_content_guard", True)
                      else informative == 0)   # A/B: Phase-0 守卫（内容） vs 更早版本
            if v == "pass" and _blind:
                # 候选局部的 Phase 0：保留同一个 Verifier Agent 让它收集新鲜
                # 证据，而不是仅仅因为第一次回答倚仗了更早的检查就把好候选弹回。
                findings = candidate_findings
                nudges += 1
                user = _CURRENT_CANDIDATE_EVIDENCE
                log.info("verifier pass with 0 current-candidate content probes -> "
                         "same-agent reinspection")
                continue
                # 零当前内容探针的 pass：同 Agent 复查。
            verdict = v
            findings = candidate_findings
            concluded = True
            break
            # 接受判定。
        probes = [p for p in (obj.get("probes") or []) if _probe_ok(p)][:4]
        # 只读守卫过滤探针（每轮最多 4 条）。
        look = obj.get("look") if vis_on else None        # v24（vis_on 已提升到上方）
        look = look if (isinstance(look, dict) and look.get("path")) else None
        # look 请求（有眼睛且路径合法才有效）。
        if not probes and not look:                       # 格式轻推——不耗轮次
            nudges += 1
            user = ('Reply with ONE JSON object: {"probes": [...]} (read-only; escape '
                    'newlines as \\n inside the JSON strings, or join commands with '
                    "';')"
                    + (' or {"look": {"path": "...", "question": "..."}}' if vis_on else '')
                    + ' or {"verdict": "pass"|"wrong"|"unverified", '
                    '"findings": "...", "doubts": "..."}.')
            continue
            # 输出格式不对：提示重发。
        rounds_used += 1
        # 消耗一个探针轮次。
        outs = []
        round_informative = 0
        for p in probes:
            po = vm.run_command(p, timeout=cfg.verifier_probe_timeout)
            if _probe_informative(po):
                informative += 1
                round_informative += 1
                if _category(p) == "content":     # v20 Phase-0: 是否读到了内容？
                    content_seen += 1
            outs.append(f"$ {p}\n{po}"[:1200])
            # 逐条执行探针并收集输出（截 1200）。
        if probes and round_informative == 0 and canary_refunds < 2:
            # v32.2 CANARY: 一整轮"什么都没有"有两种截然不同的原因——"机器上
            # 本来就没有"（证据）与"通道聋了"（停机）。客户机服务可能"活着但
            # 坏了"（HTTP 200，对一切都返回空体——t003 s994 的最终检查：列目、
            # 元数据、PIL 与 look 探针全部"returned no results"；没有异常，
            # 所以 v32.1 标记从未触发）。让机器说一个词；沉默 = 停机，本轮
            # 什么都不算证据。退还该轮（有上限，永久死通道不能无限循环）。
            cv = vm.run_command("echo __rsiagent_canary__", timeout=10)
            if "__rsiagent_canary__" not in (cv or ""):
                canary_refunds += 1
                rounds_used -= 1
                outs.append("[CHANNEL OUTAGE detected: even a trivial `echo` "
                            "returned nothing — the machine's command channel is "
                            "not answering. NOTHING in this round is evidence "
                            "about the machine (round not counted). Re-run these "
                            "probes.]")
                log.warning("verifier canary silent — round refunded (%d/2)",
                            canary_refunds)
                # 金丝雀沉默：整轮退还。
            else:
                outs.append("[channel verified alive by canary: the empty outputs "
                            "above are REAL — those commands genuinely printed "
                            "nothing]")
                # 金丝雀发声：空输出是真实的。
        if look:                                          # v24: 视觉探针（眼睛看交付物）
            looked = True                                 # v25: 检查者用过眼睛了
            va = _vision_probe(cfg, vm, look["path"], look.get("question", ""))
            if not va.startswith("("):                    # 真读数而非错误
                informative += 1
                content_seen += 1                         # 对交付物内容的视觉"读"
            outs.append(f"[VISION look {look['path']}]\n{va}"[:1500])
            # 视觉读数并入轮输出。
        if (getattr(cfg, "verifier_appearance_attach", False) and vis_on
                and not looked and not attached and rounds_used >= 2):
            # R2a 外观门：模型自己打了 [APPEARANCE] 标签、两轮过去仍没 look
            # ——不再"邀请"，直接送证据。计数口径与 look 分支一致（视觉读内容）。
            blocks, n_att = _appearance_attach(cfg, vm, instruction, context, history)
            if blocks:
                attached = steered = True                  # 提醒被取代
                informative += n_att
                content_seen += n_att
                outs.extend(blocks)
                log.info("verifier round %d: appearance-gate attached %d render(s)",
                         rounds_used, n_att)
                # 注入渲染证据。
        steer = ""                                        # v27: 停顿时注入 JIT 眼睛提醒
        if vis_on and not looked and not steered and (
                (rounds_used >= 2 and content_seen == 0)   # 两轮后仍然"文本失明"
                or rounds_used >= 3):                      # v32.1: 或干脆从未看过
            # v27 只在文本探测失败时触发——文本探针成功的检查从不被提醒"你有
            # 眼睛"，于是在决定性需求是视觉的时候凭着结构代理值通过（t003：
            # 滤水器被从文件名/元数据认定是"雨"，从未看过一眼；评分 0）。两个
            # 触发条件都是模型内在行为（探针结果 + look 使用），绝不是指令
            # 分类；提醒本身保持条件式（"如果需求关乎外观……"）。
            steer, steered = "\n\n" + _VISUAL_STEER, True
            log.info("verifier round %d: JIT eyes-reminder injected (%s)", rounds_used,
                     "text-blind" if content_seen == 0 else "never-looked")
            # 注入一次视觉提醒。
        tail = ("\n\nYou decide when the evidence is enough: more probes, a look, "
                "or the verdict object — there is no round budget and no time "
                "pressure." if elastic else
                f"\n\n{cfg.verifier_probes - rounds_used} round(s) left. More probes, "
                "or the verdict object now.")
        # 尾注：弹性模式"证据够不够你定"；否则报剩余轮次。
        user = "PROBE OUTPUT:\n" + "\n".join(outs) + tail + steer
        # 下一轮输入：探针输出 + 尾注 + 提醒。
    if not concluded:
        # v11 FINAL DEMAND —— v34 修复：最后一轮的 PROBE OUTPUT 曾被构造却从未
        # 发送（循环在投递之前就退出了），于是模型基于第 1..N-1 轮判定，而
        # 第 N 轮的证据——通常正是它最有针对性的探针——被执行后即被丢弃。
        # 审计：最近 400 次检查中 280 次丢了一个已执行轮；92 个判定抱怨了
        # 其实存在的证据缺失。现在把待发输出与索答一起投递，判定建立在
        # 真正跑过的一切之上。
        pending = user if user.startswith("PROBE OUTPUT:") else ""
        # 待投递的最后一轮探针输出。
        demand = ((pending + "\n\n") if pending else "") + (
            "Probe budget exhausted — no more probes will be run. Based ONLY on "
            "the evidence already gathered (including the probe output above, if "
            "any), output the verdict object "
            'NOW: {"verdict": "pass"|"wrong"|"unverified", "findings": "<per '
            'requirement: met / violated / could-not-confirm + evidence>", '
            '"doubts": "<unresolved material TASK uncertainty; MUST be empty for '
            'pass>"}.')
        # 索答：预算耗尽，基于已收集证据立即给判定（正文是提示词）。
        out = chat(model, vsys, demand,
                   max_tokens=cfg.verifier_max_tokens, temperature=cfg.temperature,
                   reasoning_effort=cfg.reasoning_effort, history=history,
                   reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0))
        # 强制索答调用。
        history += [{"role": "user", "content": demand},
                    {"role": "assistant", "content": out or "(empty reply)"}]
        obj = parse_object(out) or {}
        v = obj.get("verdict")
        if v == "fail":
            v = "wrong"
        if v in ("pass", "wrong", "unverified"):
            candidate_findings = (_mk_findings(
                str(obj.get("findings", ""))[:2000], obj.get("doubts", ""),
                obj.get("items")) if obj.get("findings") else findings)
            # 构造最终发现。
            if (v == "pass"
                    and str(getattr(candidate_findings, "doubts", "") or "").strip()
                    and getattr(cfg, "doubts_block_pass", False)):
                # 探针预算已尽，但判定语义仍是 Verifier Agent 的裁量。给它最后
                # 一次同上下文机会自己分类其疑点，而不是由 harness 代劳。
                clarification = (_PASS_DOUBT_CLARIFICATION
                                 + "\n\nNo more probes can be run now; return only a "
                                   "revised verdict object.")
                out2 = chat(model, vsys, clarification,
                            max_tokens=cfg.verifier_max_tokens,
                            temperature=cfg.temperature,
                            reasoning_effort=cfg.reasoning_effort, history=history,
                            reasoning_max_tokens=getattr(
                                cfg, "reasoning_max_tokens", 0))
                # 同 Agent 澄清调用。
                history += [{"role": "user", "content": clarification},
                            {"role": "assistant", "content": out2 or "(empty reply)"}]
                obj2 = parse_object(out2) or {}
                v2 = "wrong" if obj2.get("verdict") == "fail" \
                    else obj2.get("verdict")
                revised = (_mk_findings(
                    str(obj2.get("findings", ""))[:2000], obj2.get("doubts", ""),
                    obj2.get("items")) if obj2.get("findings") else candidate_findings)
                if (v2 in ("wrong", "unverified")
                        or (v2 == "pass" and not str(
                            getattr(revised, "doubts", "") or "").strip())):
                    v, candidate_findings = v2, revised
                    # 澄清有效：采纳修订后的判定。
                else:
                    v = "unverified"
                    candidate_findings = carry_findings(
                        "Verifier Agent did not resolve its contradictory PASS and "
                        "task doubts before the inspection safety ceiling. "
                        + str(candidate_findings), candidate_findings)
                    # 矛盾未解决：降级为 unverified（旁路数据随行）。
            verdict = v
            findings = candidate_findings
            _blind = (content_seen == 0
                      if getattr(cfg, "verifier_content_guard", True)
                      else informative == 0)
            if verdict == "pass" and _blind:
                verdict = "unverified"
                findings = carry_findings(
                    "[downgraded pass->unverified: no probe READ the current "
                    "candidate — prior-candidate evidence alone cannot confirm] "
                    + str(findings), findings)
                log.info("final verifier pass with 0 current-candidate content probes -> "
                         "unverified")
                # 最终 pass 仍然零内容读数：降级为 unverified（内容守卫）。
    if evidence_session is not None:
        evidence_session.informative_probes += informative
        evidence_session.content_probes += content_seen
        # 累计遥测回写会话。
    if sink is not None:
        sink.save_verify2(turn_no, verdict, findings, history)
        # 判定与完整对话落盘（事后剖析）。
    return verdict, findings
    # 返回（判定, 发现）。


def second_opinion(instruction: str, vm, cfg, sink, turn_no: int, reason: str,
                   first_report: str, context: str = "", session=None):
    """R2/v31：升级模型上的"疑点门控"第二次检查——在第一次检查无法扎实支撑
    的接受决定之前的最后一道复审。

    动机（tested-47 扫描 + t032 冒烟）：run 以"自信但错误"收场，而唯一的模型
    升级触发器是停滞——一个错误却自信的 run 顺着未验证接受规则一路滑到
    done-at-0（t063、t032 两次实测；8/46 任务 = 1.49 分）。本函数把"接受决定"
    本身交给另一个更强模型的抽样。

    独立性保持：复审者看到任务、机器与第一位检查者的报告（检查者对检查者
    ——绝不是 Actor 的推理）。它经 verify_independent 在 verifier_model 为
    升级模型的配置上跑同一套探针/look 机器；其判定由它自己的证据决定。
    返回 (verdict, findings, review_model)。"""
    from dataclasses import replace as _replace
    review_model = (getattr(cfg, "escalation_model", "") or cfg.verifier_model
                    or cfg.model)
    # 复审模型：升级模型优先，回落 verifier/主模型。
    rcfg = _replace(cfg, verifier_model=review_model)
    # 派生配置：只换 Verifier 席位。
    ctx = (
        (context + "\n\n" if context else "")
        + "A FIRST independent inspection " + reason + ". Its report:\n"
        + str(first_report)[:1500]
        + "\n\nYou are the SECOND and DECIDING reviewer, on a different model. Do not "
          "inherit the first report's conclusions — re-derive the requirements and "
          "adversarially probe the machine yourself, with special attention to "
          "everything the first inspection could not affirmatively confirm. Where a "
          "requirement is about a rendered or on-screen result, LOOK at it (render or "
          "screen:) rather than inferring from structural values."
    )
    # 复审上下文：第一次检查的原因与报告（截 1500）+ "你是第二个且一锤定音的
    # 复审者，别继承其结论"的指令（正文是提示词）。
    class _Capture:
        """垫片 sink：截获复审者的 verify2 载荷，使它的探针轨迹能以 review.json
        持久化（v32.1——它曾被静默丢弃，导致 AB-12 法证里复审者的探针失败
        无法剖析）。"""
        transcript: list = []

        def save_verify2(self, n, v, f, transcript=None):
            self.transcript = transcript or []

    cap = _Capture()
    verdict, findings = verify_independent(
        instruction, vm, rcfg, sink=cap, turn_no=turn_no, context=ctx,
        session=session)
    # 用升级配置跑独立检查（独立会话语义保持）。
    if sink is not None:
        try:
            sink.save_review(turn_no, verdict, str(findings), reason, review_model,
                             transcript=cap.transcript)
            # 复审结果与轨迹落盘。
        except Exception:                                   # noqa: BLE001 — 绝不致命
            log.warning("could not persist review artifact for turn %d", turn_no)
            # 落盘失败只警告（复审结论不受影响）。
    return verdict, findings, review_model
    # 返回（判定, 发现, 复审模型）。


def disagreement_message(findings: str) -> str:
    """检查发现"具体违规证据"时回传给 Actor 的消息。

    E4-A1c：当检查者报告了结构化条目时，把未决条目作为编号整改清单附加
    ——Actor 不必从一整段文字里猜哪条需求实际未决。"""
    return ("An INDEPENDENT INSPECTION of the machine state (performed without "
            "seeing your work or assumptions) found concrete evidence that the task "
            "is NOT complete. Its findings:\n" + findings
            + items_punchlist(findings) + "\n\nReconcile these "
            "findings with your own evidence — where you and the inspection "
            "interpret the task differently, hunt for the evidence that "
            "discriminates the two readings (especially any already-solved instance "
            "the task provides) — fix what is actually wrong, then declare done "
            "again.")
    # 消息内容：独立检查发现未完成 + 整改清单 + "调和分歧、修真正错处"的指令。


def unverified_message(findings: str, evidence_request: bool = False) -> str:
    """检查"未能确认"（≠ 发现错误）时回传给 Actor 的消息。

    ``evidence_request``（E4-A1）：连续性时代的措辞——要求经"独立通道"给出
    证明。旧措辞建议写一个证据文件，这正是 E2 门拒绝的"自证仪式"，t039 就
    照做然后自信地拿了 0.0；旧措辞逐字保留在下方只为开关关闭时的逐字节一致。"""
    if evidence_request:
        return ("An INDEPENDENT INSPECTION could not CONFIRM the task is complete — "
                "it did not find it wrong; it could not gather enough evidence "
                "either way. What it could not confirm:\n" + findings +
                items_punchlist(findings) +
                "\n\nDEMONSTRATE the unconfirmed parts through an INDEPENDENT "
                "CHANNEL the inspection can re-run from the machine itself: render "
                "the deliverable and check the result, query the live service's OWN "
                "state, re-open the artifact with a different tool, show the "
                "process/server actually holding the work. Evidence the WORLD "
                "produces — a file you author about your own work proves nothing "
                "and will not count. Then declare done again with those "
                "independent-channel probes among your checks; do NOT change "
                "correct work just because it was hard to confirm. If your "
                "evidence is actually thin there, strengthen the work first.")
        # 连续性措辞：世界产生的证据才算数（正文是提示词）。
    return ("An INDEPENDENT INSPECTION could not CONFIRM the task is complete — it "
            "did not find it wrong; it could not gather enough evidence either way. "
            "What it could not confirm:\n" + findings + "\n\nIf your own evidence "
            "for the unconfirmed parts is solid, make it easy to check FROM THE "
            "MACHINE: write it to a file next to the deliverable (an evidence file "
            "with the exact values/paths to re-check) — a fresh inspection sees "
            "files, never your printed output — and declare done again; do "
            "NOT change correct work just because it was hard to confirm. If your "
            "evidence is actually thin there, strengthen it first.")
    # 旧措辞（逐字保留）：把证据写成机器旁的文件供新检查复看。
