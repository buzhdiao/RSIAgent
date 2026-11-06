"""rsiagent eyes — R3 "two witnesses + judge" (v32, 2026-07-15; supersedes the v30 vote).

FORENSIC BASIS (results/r1r2_effectiveness.md, 12-run deep-read): the v30 ensemble's
VOTING never beat a single read on a decisive perception (0/12) and its merged
consensus twice manufactured a wrong answer (t101 averaged a correct-low count into a
wrong compromise; t049's consensus was confidently wrong). The only component with a
demonstrated score win was the split-triggered NUDGE that routed the actor to a
ground-truth channel (t032: enumerate catalog -> side-by-side verify; t049: the
actor's own pixel-count code). Conclusion: models out-voting models does not create
truth — a checkable channel does.

v32 design:
- 2 independent witnesses (1 canonical + 1 hot skeptic). Enough to DETECT
  disagreement; the 3rd/5th votes bought nothing and multiplied cost.
- A JUDGE (small text call) classifies agreement only: full | partial | split.
  Comparing two texts is radically easier than reading pixels — the judge is never
  asked to decide what is true, only whether two answers are the same.
- On full/partial: the judge's consensus text is delivered (as before).
- On SPLIT: NO merged answer, ever. The harness delivers BOTH readings VERBATIM plus
  a directive resolution protocol (enumerate / compute / read-the-source). A
  disagreement is evidence about uncertainty; merging destroys it, averaging launders
  it into fake confidence.
- The report carries a machine-readable .split flag (str subclass) so the loop can
  ledger unresolved splits and gate done-declarations on them (R3 gate, loop.py).

Design rules preserved from v30: uniform for every look (no importance classifier);
no task/grader token anywhere (audited surface, tests/test_no_leakage.py); eyes
failing must never crash the run; infra failure must never leave the actor worse off
than a single read.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from core.imagery import prepare_look_images
from llm.client import LLMTransportError, chat

log = logging.getLogger("rsiagent.eyes")

# 看图缺省问题：描述图中一切与计算机任务相关的可见内容，文字逐字读出
DEFAULT_Q = ("Describe everything visible in this image that could matter to a "
             "computer task, reading any text verbatim.")

# Witness 2..N system: same perception contract as VISION_SYSTEM (imported lazily to
# avoid an import cycle) plus an explicit independent-skeptic stance.
# （中文）第 2..N 位见证人的附加系统段：感知契约与 VISION_SYSTEM 相同（为避免
# import 环延迟导入），再加一段显式的“独立怀疑者”立场。
SKEPTIC_SUFFIX = (
    "\n4. You are ONE OF SEVERAL INDEPENDENT readers of this image; others answer the "
    "same question separately. Read with fresh, skeptical eyes: before committing, ask "
    "what a hasty reader could mistake this for (similar-looking names, styles, shapes; "
    "miscounts; misread digits), check those confusions against the pixels, then commit "
    "to YOUR OWN best answer with confidence. Do not hedge into a non-answer."
)

# 判定员（judge）系统提示词：只判“两份读数是否一致”，绝不判“哪份为真”
JUDGE_SYSTEM = (
    "You compare INDEPENDENT visual readings of the SAME image, given the question "
    "they answered. Decide ONLY whether they agree on the CORE answer — the "
    "identification, count, name, value, or judgment the question actually asks for. "
    "Wording differences are agreement; different names, counts, values, or opposite "
    "judgments are disagreement. You are NOT asked what is true — only whether the "
    "readings say the same thing.\n"
    "Reply in EXACTLY this shape (no preamble):\n"
    "AGREEMENT: full | partial | split\n"
    "ANSWER: <only for full/partial: the agreed core answer — direct, concrete, "
    "verbatim text/numbers preserved>\n"
    "DETAILS-DIFFER: <only for partial: the secondary details the readings disagree "
    "on, one line>\n"
    "Rules: 'split' means the CORE answer itself differs — and when the question "
    "asks for SEVERAL values (multiple images or sub-questions), a disagreement on "
    "ANY asked-for value IS a split, not 'partial'. 'partial' is reserved for "
    "agreement on every asked-for value with only incidental details differing. "
    "Output NO answer line for a split and do NOT attempt to reconcile, average, or "
    "pick a winner. Never invent content found in no reading."
)

# The resolution protocol delivered with every split — directive, not advisory.
# Each branch generalizes a demonstrated win: (a) t032 catalog-enumerate,
# (b) t049 pixel-count / t101-style compute, (c) file/DOM/app-state reads.
# （中文）每次 SPLIT 一并下达的“消解协议”：指令式而非建议式；各分支分别对应
# 已被验证有效的胜法（编目枚举 / 代码计数度量 / 直读源头）。
RESOLUTION_PROTOCOL = (
    "READINGS CONFLICT — do not average them, do not pick one. Resolve the answer "
    "through a channel where it is CHECKABLE, then record the winning evidence as "
    "data (program output or an evidence file):\n"
    "(a) WHICH-ONE / naming dispute -> ENUMERATE the full candidate set from the "
    "environment itself (catalog, registry, directory, dataset) and verify candidates "
    "by direct side-by-side comparison — the true answer may appear in NEITHER "
    "reading.\n"
    "(b) COUNT / position / size dispute -> COMPUTE it: crop the region and "
    "count/measure with code (image processing, pixel measurement), or read the "
    "coordinates/values from the source file.\n"
    "(c) file / app / page content dispute -> read the SOURCE directly (file bytes, "
    "extracted text, DOM, application state).\n"
    "(d) only if no such channel exists -> take ONE more close-up look at the "
    "contested region with a narrower question, then commit and state the residual "
    "uncertainty.\n"
    "Repetition of the same reading by the same witness across multiple views is NOT "
    "independent confirmation — a biased reader repeats its bias with perfect "
    "consistency. Only cross-witness agreement or a code-channel result confirms."
)


# 看图报告：str 子类，附带机器可读的 split 标志（既有调用点零改动）
class LookReport(str):
    """The text delivered to the caller, carrying a machine-readable split flag so
    the loop can ledger unresolved disagreements (str subclass — every existing
    call site keeps working; ChatText/Findings precedent)."""
    # 是否存在未消解的读数分歧（SPLIT）
    split: bool = False


def _mk_report(txt: str, split: bool = False) -> LookReport:
    # 构造报告对象并打上 split 标志
    r = LookReport(txt)
    r.split = bool(split)
    return r


def unresolved_splits_message(entries) -> str:
    """R3 done-gate bounce (one-shot): the actor declared done while visual readings
    it acted on were never resolved to agreement. General text; entries are
    mechanical {turn, path, q} facts from the loop's ledger."""
    # 把台账条目渲染成逐行列出（turn/path/问题）
    lines = "\n".join(f"- turn {e['turn']}: {e['path']} — {e['q']}" for e in entries)
    # 一次性驳回文案：列出未消解冲突 + 消解协议，要求解决后重新声明 done
    return (
        "Before this declaration can be accepted: independent visual readings "
        "DISAGREED at the following look(s) and were never resolved (no later "
        "reading of the same target reached agreement, and no recorded evidence "
        "settled the conflict):\n" + lines + "\n\n" + RESOLUTION_PROTOCOL + "\n\n"
        "For each conflict above, either resolve it through a checkable channel and "
        "correct the deliverable if needed, or — if it genuinely does not affect the "
        "task's end state — state why in your evidence. Then declare done again."
    )


def _vision_system() -> str:
    # 延迟导入基础感知系统提示词，避免与 core.actor 形成 import 环
    from core.actor import VISION_SYSTEM          # lazy: avoid import cycle
    return VISION_SYSTEM


def _one_read(model: str, system: str, question: str, image, temperature: float,
              max_tokens: int = 2000, reasoning_max_tokens: int = 0) -> str:
    """One witness; provider failures remain transport, never visual evidence."""
    try:
        # 单次见证读取：带图调用对话模型
        ans = chat(model, system, question, max_tokens=max_tokens, temperature=temperature,
                   reasoning_effort="", history=[], image=image,
                   reasoning_max_tokens=reasoning_max_tokens)
    except LLMTransportError:
        # 传输层故障原样上抛（由上层基础设施处理）
        raise
    except Exception as e:                        # noqa: BLE001 — eyes must not crash the run
        # 其他异常转为错误文本：看图失败绝不弄崩整场运行
        return f"(vision tool error: {e})"
    return (ans or "").strip()


def _parallel_reads(jobs, question: str, image, max_tokens: int = 2000,
                    reasoning_max_tokens: int = 0) -> list:
    """jobs = [(model, system, temperature), ...] -> answers in job order."""
    if len(jobs) == 1:
        # 单任务直接同步执行
        m, s, t = jobs[0]
        return [_one_read(m, s, question, image, t, max_tokens,
                          reasoning_max_tokens)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        # 多见证并发读取（每任务一个线程），结果保持任务顺序
        futs = [ex.submit(_one_read, m, s, question, image, t, max_tokens,
                          reasoning_max_tokens)
                for (m, s, t) in jobs]
        return [f.result() for f in futs]


# 判定员输出的 AGREEMENT 行解析
_AGREE_RE = re.compile(r"AGREEMENT:\s*(full|partial|split)", re.IGNORECASE)

_QUANT_RE = re.compile(  # pre-108 item 4: questions where witness-2 EARNS its cost —
    r"how many|count|number of|closer|closest|farther|larger|smaller|bigger|"  # counting,
    r"position|located|aligned|angle|degrees|coordinates|which of|left or right|"  # comparison,
    r"above or below|distance|spacing|how far",  # spatial relations
    re.IGNORECASE)


def _wants_ensemble(question: str) -> bool:
    """Route by the question's linguistic shape (general — no task knowledge):
    quantitative/spatial questions keep the full witness ensemble; identify/describe
    questions run single-witness (campaign split-rates: 30-33% vs ~1-7%)."""
    # 只按问题的语言形态路由（数量/空间类保留完整见证组），不含任务知识
    return bool(_QUANT_RE.search(question or ""))


def _judge(cfg, question: str, reads: list):
    """Classify agreement across 2+ reads -> (verdict, judge_text). verdict in
    {'full','partial','split',None}; None = judge unavailable (infra)."""
    # 判定员模型：可用独立小模型，缺省用主模型
    model = getattr(cfg, "look_merge_model", "") or cfg.model
    # 拼装判定输入：问题 + 各读数逐份列出
    body = f"QUESTION: {question}\n\n" + "\n\n".join(
        f"READING {i + 1}:\n{r}" for i, r in enumerate(reads))
    try:
        # 判定员调用：小预算、零温度
        out = (chat(model, JUDGE_SYSTEM, body, max_tokens=1200, temperature=0.0,
                    reasoning_effort="", history=[]) or "").strip()
    except LLMTransportError:
        # 传输故障上抛
        raise
    except Exception as e:                        # noqa: BLE001
        # 判定员不可用：记日志并返回 None（上层有降级路径）
        log.warning("look-judge failed: %s", e)
        return None, ""
    m = _AGREE_RE.search(out)
    if not m:
        # 输出不含可解析的裁定行：视为不可用但保留原文
        return None, out
    # 返回（裁定, 去掉裁定行后的正文）
    return m.group(1).lower(), _AGREE_RE.sub("", out, count=1).strip()


def _split_report(reads: list) -> LookReport:
    # 分歧报告的读数标签
    labels = ["READING A (canonical)", "READING B (independent skeptic)"]
    parts = []
    for i, r in enumerate(reads):
        lab = labels[i] if i < len(labels) else f"READING {i + 1} (independent)"
        parts.append(f"{lab}:\n{r}")
    # 两份读数逐字并列 + 消解协议，并打上 split 标志（绝不合并平均）
    return _mk_report("\n\n".join(parts) + "\n\n" + RESOLUTION_PROTOCOL, split=True)


def ensemble_look(cfg, question: str, datas: list, region=None) -> LookReport:
    """R3 entry point. look_ensemble=1 reproduces the v22 single-shot path exactly.
    N>=2: N independent witnesses; a judge classifies agreement; consensus is
    delivered on full/partial; a SPLIT delivers both readings verbatim + the
    resolution protocol and is flagged (.split) for the loop's ledger/done-gate.
    Judge unavailable (infra) -> both readings delivered, NOT flagged as split
    (never leave the actor worse off than a single read on an outage)."""
    # 见证人数（look_ensemble），至少 1
    n = max(1, int(getattr(cfg, "look_ensemble", 1)))
    if n >= 2 and getattr(cfg, "look_witness_router", False) and not _wants_ensemble(question):
        # 路由开关打开且问题属“描述/识别”类：降为单见证（省成本）
        n = 1                                     # item 4: describe/identify -> one witness
    # 服务端准备图像（概览/切片/区域裁剪）
    image, _ = prepare_look_images(datas, region=region,
                                   max_side=getattr(cfg, "look_max_side", 0) or None)
    # 问题缺省化
    q = question or DEFAULT_Q
    vs = _vision_system()
    # 见证人回复预算（0 则取 2000 的时代默认值）
    mt = int(getattr(cfg, "vision_max_tokens", 0) or 2000)
    rmt = int(getattr(cfg, "reasoning_max_tokens", 0))   # v36: witness budget —
    #      reasoning-first eyes (kimi-k3) need ~16k to answer; default = m3-era 2000
    if n == 1:
        # 单见证路径（v22 行为不变）
        ans = _one_read(cfg.vision_model, vs, q, image, 0.0, mt, rmt)
        return _mk_report(ans or "(vision tool returned nothing)")

    temp = float(getattr(cfg, "look_ensemble_temp", 0.8))
    # v33 (): HETEROGENEOUS witnesses. Two same-model reads only decorrelate
    # sampling noise — a systematic (architecture-shaped) misread is repeated twice
    # and its agreement launders wrongness into confidence (t003: unanimous-wrong).
    # A second MODEL has different blind spots: agreement means far more, and splits
    # can catch errors the same-model pair is structurally blind to. Same call
    # count, zero added cost. "" = incumbent single-model behavior.
    # （中文）v33 异构见证：同模型双读只能去相关采样噪声；第二个“不同模型”有不同
    # 盲区，一致更有意义，调用数不变、零额外成本。
    second = getattr(cfg, "vision_model_2", "") or cfg.vision_model
    # 见证人 1：规范读数（零温度）
    jobs = [(cfg.vision_model, vs, 0.0)]
    # 见证人 2..N：怀疑者立场 + 更高温度
    jobs += [(second, vs + SKEPTIC_SUFFIX, temp)] * (n - 1)
    # 并发读取并过滤掉报错的读数
    reads = [r for r in _parallel_reads(jobs, q, image, mt, rmt)
             if r and not r.startswith("(vision tool error")]
    if not reads:
        # 全部失败：返回空结果提示
        return _mk_report("(vision tool returned nothing)")
    if len(reads) == 1:                           # the other witness errored out
        # 只剩一份有效读数：直接交付（降级为单读）
        return _mk_report(reads[0])

    # 判定员裁定一致程度
    verdict, judge_text = _judge(cfg, q, reads)
    if verdict == "split":
        # 核心答案分歧：记录日志并交付分歧报告（不打不平均）
        log.info("look-judge: SPLIT on core answer (%d reads)", len(reads))
        return _split_report(reads)
    if verdict in ("full", "partial"):
        # 一致/部分一致：交付判定员共识文本 + 机器可读标签
        tag = (f"[{len(reads)} independent visual reads agree]" if verdict == "full"
               else f"[core answer agreed by {len(reads)} independent reads; "
                    "secondary details differ — noted above]")
        return _mk_report(f"{judge_text}\n{tag}")
    # judge unavailable/unparseable: deliver the information, do not claim a conflict
    # 判定员不可用/输出不可解析：并列交付各读数，但不宣称存在冲突（故障不劣于单读）
    joined = "\n\n".join(f"[independent read {i + 1}] {r}" for i, r in enumerate(reads))
    return _mk_report("[agreement judge unavailable — independent readings follow]\n"
                      + joined)
