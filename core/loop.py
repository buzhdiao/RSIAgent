# 【中文注释·模块定位】RSIAgent 的尝试循环（attempt loop）——code as policy，
# 以执行轨迹闭环。一次尝试 = 一段有界对话：模型反复提交"完整程序"（在 VM 中执行、
# 完整轨迹回传），直到它以"自著、由任务指令导出的检查"声明 done，且这些检查必须在
# 活环境中真正通过。密封评分器（sealed grader，Constraint #0）：本循环只接收一个
# 指令字符串和一个 VM 句柄（run_script/run_command）；基准评分器在结构上不可达——
# runner 只在本函数返回之后调用它一次。官方分数因此天然处于学习环之外。
"""RSIAgent 的尝试循环（attempt loop）——code as policy，以执行轨迹闭环。

一次尝试 = 一段有界对话：模型反复提交"完整 PROGRAM"（在 VM 中执行、完整轨迹
回传），直到它以自著的、由任务指令导出的检查声明 done，且这些检查必须在活
环境中真正通过。

密封评分器（Constraint #0）：本循环只接收一个指令字符串和一个 VM 句柄
（run_script / run_command）。基准评分器在结构上从这里不可达——runner 只在本
函数返回之后调用它一次。
"""
# 导入 hashlib：程序/输出指纹（md5）与循环检测。
import hashlib
# 导入 logging：模块级日志器。
import logging
# 导入 os：环境变量开关（JSON 重试）与路径归一化。
import os
# 导入 time：墙钟预算与暂停计时。
import time
# 导入 dataclass：结果遥测容器 LoopResult。
from dataclasses import dataclass


def _normalize_program_transport_failure(trace) -> bool:
    """旧式/重复 VM 适配层漏报暂存失败时 fail closed 地补判。

    ``VM.run_script`` 是这一位（infra_fail）的主权威。这个窄兜底只识别
    "缺失的执行尾标 + 受信包装器确切的 mktemp/运行日志失败形状"。一个仅仅
    打印这些字样的模型程序仍会收到真实的 ``[exit N]`` 尾标，不可能伪装成
    rsiagent 基础设施。
    """

    if bool(getattr(trace, "infra_fail", False)):
        # 权威位已置起：直接确认为基础设施失败。
        return True
    stdout = str(getattr(trace, "stdout", "") or "")
    # 取轨迹 stdout（None 安全）。
    wrapper_failed = (
        getattr(trace, "exit_code", None) is None
        and "mktemp: failed to create file via template" in stdout
        and "/tmp/rsiagent_" in stdout
        and "Read-only file system" in stdout)
    # 窄判据：没有退出码（尾标缺失）且 stdout 恰好是受信包装器的三段失败特征
    # （mktemp 模板失败 + /tmp/rsiagent_ 路径 + 只读文件系统）。
    if wrapper_failed:
        trace.infra_fail = True
        # 补判为基础设施失败：模型代码根本没执行。
        log.warning(
            "normalized an unclassified Actor Program staging failure — "
            "model-authored code did not execute")
        # 留痕：这是一次被归一化的未分类暂存失败。
    return wrapper_failed
    # 返回是否触发了兜底判定。


def _md5(s: str) -> str:
    """字符串的 md5 指纹（仅用于重复检测，非安全用途）。"""

    return hashlib.md5((s or "").encode("utf-8", "replace")).hexdigest()
    # None 安全（视作空串），不可编码字符用 replace 容错。


def _digest_lines(msgs: list, digest_chars: int = 220) -> str:
    """把消息列表渲染成确定性的摘要行（每条截到 digest_chars）。"""

    # 仅作为摘要模式的"饥荒兜底"保留（digest 压缩模式已于 2026-07-03 移除，
    # 用户决定：被模型自写的 WORK LOG 取代——t030 A/B 中它以 0.9-1.0 对 0.2-0.8 胜出）。
    return "\n".join(
        f"[{'you' if m['role'] == 'assistant' else 'env'}] "
        + " ".join(str(m["content"]).split())[:digest_chars] for m in msgs)
    # 每条一行：角色前缀 + 空白归一后的内容前缀截断。


def _cap_worklog(text: str, limit: int) -> str:
    """v21（上下文）：把 WORK LOG 截在"行边界"上（绝不在句子或数值中间截断——
    与旧的盲目 [:14000] 不同，那会无声地切掉整段内容）。保留顶部——摘要器被
    要求把当前状态/关键值/下一步放在最前、可重新推导的参考细节放在最后——
    并明确标注截断发生了。"""
    if len(text) <= limit:
        # 未超限：原样返回。
        return text
    cut = text.rfind("\n", 0, limit)
    # 在上限内找最后一个换行（截在那里保持行完整）。
    if cut < limit // 2:                                  # 上限附近没有可用的换行
        cut = limit                                       # -> 最后手段：硬切
    return (text[:cut].rstrip()
            + "\n[... WORK LOG truncated at the char cap — least-important (re-derivable) "
              "detail dropped; the machine still holds ground truth, re-probe if needed]")
    # 截断文本 + 追加如实的丢弃说明（被丢的是最不重要、可重推导的细节；
    # 真值仍在机器里，需要时可重新探测）。


def _fold_input(content, head: int = 1500, tail: int = 2500) -> str:
    """v21（上下文）：把被折叠的消息渲染成"头部+尾部"（旧版是 [:4000] 前缀，
    会藏住肥大轨迹的最后约 4k——而 traceback 和最终错误行恰恰落在那里）。"""
    s = str(content)
    # 统一转成字符串。
    if len(s) <= head + tail:
        # 不超限：原样返回。
        return s
    return s[:head] + "\n…[trace middle elided]…\n" + s[-tail:]
    # 折叠：保留头（开头上下文）和尾（traceback/错误行），中段如实标注省略。


def _fold_once(history: list, covered: int, n_pairs: int, summary: str, cfg, sink,
               turn_no: int):
    """把最旧的 ``n_pairs`` 个未折叠对话对折叠进 WORK LOG（一次摘要器调用）。
    摘要器返回空时退回"追加确定性摘要行"（两种方式都保证有进展）。
    返回 (summary, covered)。"""
    batch = history[covered * 2:(covered + n_pairs) * 2]
    # 取出待折叠的批次（covered 之后 n_pairs 个 user/assistant 对）。
    rendered = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n"
        + _fold_input(m["content"]) for m in batch)
    # 渲染批次：每条标注 YOU/ENV 并做头尾折叠。
    out = chat(cfg.model, SUMMARIZER_SYSTEM,
               f"CURRENT LOG:\n{summary or '(empty)'}\n\nOLDER TURNS TO FOLD IN:\n"
               f"{rendered}\n\nOutput the updated log only.",
               max_tokens=cfg.max_tokens, temperature=0.0,
               reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    # 一次摘要调用：现行日志 + 待折叠旧轮次 -> 输出更新后的日志（温度 0，确定性）。
    cap = cfg.worklog_max_chars
    # 工作日志字符上限。
    if out.strip():
        summary = _cap_worklog(out.strip(), cap)          # v21: 行边界截断、保留顶部
    else:                                   # 摘要器"饥饿"（返回空）-> 确定性兜底
        combined = summary + "\n" + _digest_lines(batch)
        # 兜底：把批次渲染成确定性摘要行接在现行日志后。
        if len(combined) > cap:                           # 这里保留尾部（最新），
            b = combined.find("\n", len(combined) - cap)  # 且截在行边界上
            combined = combined[b + 1:] if b >= 0 else combined[-cap:]
        summary = combined
        log.warning("summarizer returned empty — folded batch as digest lines")
        # 留痕：发生了兜底折叠。
    covered += n_pairs
    # 推进已折叠对数。
    if sink is not None:
        sink.save_summary(turn_no, summary)
        # 落盘最新工作日志（供审计/恢复）。
    log.info("work log updated: %d pairs folded (total %d), %d chars",
             n_pairs, covered, len(summary))
    # 记录折叠遥测。
    return summary, covered
    # 返回更新后的（日志, 已折叠对数）。


def _effective_keep(history: list, cfg) -> int:
    """v14：逐字窗口的大小按"字符预算"（keep_chars）而非固定对数计算。
    从最新到最旧累计字符，预算花完即停。下限 6 对（进行中的工作必须逐字保留），
    上限 history_keep_pairs。keep_chars=0 -> 退回旧的固定对数行为。"""
    if cfg.keep_chars <= 0:
        # 未启用字符预算：用固定的 history_keep_pairs。
        return cfg.history_keep_pairs
    pairs = len(history) // 2
    # 总对话对数。
    tot, k = 0, 0
    # 累计字符数、累计保留对数。
    for i in range(pairs - 1, -1, -1):
        # 从最新一对向前遍历。
        c = (len(str(history[2 * i]["content"]))
             + len(str(history[2 * i + 1]["content"])))
        # 该对话对的字符量（user + assistant）。
        if k >= 6 and tot + c > cfg.keep_chars:
            break
            # 已达下限且再放一对就超预算：停。
        tot += c
        k += 1
        # 计入这一对。
        if k >= cfg.history_keep_pairs:
            break
            # 达到上限：停。
    return max(6, min(k, cfg.history_keep_pairs)) if pairs >= 6 else pairs
    # 夹在下限 6 与配置上限之间（历史不足 6 对时全保留）。


def _fold_summary(history: list, covered: int, summary: str, cfg, sink, turn_no: int):
    """摘要模式压缩。触发条件是"被评估的"，不是"排定的"（v9.2）：设置了
    ctx_high_water 时，只有当渲染后的上下文（WORK LOG + 逐字尾部）真的超过
    高水位才折叠——按 fold_batch 分块折叠最旧的对、绝不折到最后
    history_keep_pairs 之内，直到降回低水位以下。高/低水位的迟滞防止在边界
    上每轮都折叠；产出小轨迹的轮次可能几十轮不折叠，吞下巨大轨迹的一轮立即
    折叠。水位是"每个配置的值"（与模型相关：按模型可用窗口定大小）；机制
    本身与模型无关。ctx_high_water=0 保留旧的计数触发（只要累积满 fold_batch
    个未折叠对就折叠）。返回 (summary, covered)。"""
    pairs = len(history) // 2
    # 总对话对数。
    keep = _effective_keep(history, cfg)    # v14: 按字符预算定的逐字窗口
    # 逐字保留的对数。

    def size() -> int:                      # 模型这一轮会看到的上下文大小
        return len(summary) + sum(len(str(m["content"])) + len(m.get("reasoning", ""))
                                  for m in history[covered * 2:])   # v38: reasoning 计入
    # 大小 = 工作日志 + 未折叠部分（内容 + reasoning 字段）的总字符数。

    if cfg.ctx_high_water > 0:
        # 尺寸触发模式（v9.2）。
        if size() <= cfg.ctx_high_water:
            return summary, covered
            # 未超高水位：本轮不折叠。
        low = cfg.ctx_low_water or cfg.ctx_high_water // 2
        # 低水位（未配置则取高水位一半）。
        s0 = size()
        # 折叠前尺寸（日志用）。
        folded = False
        # 本轮是否折叠过。
        # FLOOR GUARD (v11): 只按整份 fold_batch 折叠。当仅逐字窗口一项就压过
        # 高水位（肥轨迹 + 低水位）时，">=1 对就折"的条件会每轮都折叠——
        # 观测到 70 次折叠/76 轮：每轮重写日志、烧摘要器，正是迟滞要防止的。
        # 要求整批可折，把最坏折叠频率限制到每 fold_batch 轮一次；
        # 水位合理时（keep 窗口 << 高水位）行为不变。
        while size() > low and pairs - keep - covered >= cfg.fold_batch:
            # 仍高于低水位且还能凑出整批：继续折叠。
            summary, covered = _fold_once(history, covered, cfg.fold_batch, summary,
                                          cfg, sink, turn_no)
            # 折一批。
            folded = True
            # 标记已折叠。
        if folded:
            log.info("size-triggered fold: %d -> %d chars (high %d / low %d, "
                     "keep %d pairs)", s0, size(), cfg.ctx_high_water, low, keep)
            # 记录尺寸触发折叠的遥测。
        return summary, covered
        # 返回（日志, 已折叠对数）。

    unfolded = pairs - keep - covered
    # 旧计数触发：未折叠对数 = 总数 - 逐字窗口 - 已折叠。
    if unfolded < cfg.fold_batch:
        return summary, covered
        # 不满一批：不折叠。
    return _fold_once(history, covered, unfolded, summary, cfg, sink, turn_no)
    # 一次性把全部未折叠对折掉。


import re as _re
# 导入 re 并别名为 _re：JIT 检测正则（延迟命名避免与形参冲突观感）。

from core.checks import run_checks, validate
# 导入检查工具：done 声明的自检校验（validate）与活环境执行（run_checks）。
from core.eyes import ensemble_look, unresolved_splits_message
# 导入视觉集成：Look 的多证人共识（ensemble_look）与视觉分歧提示。
from core.imagery import fetch_look_image, grid_cells, prepare_look_images
# 导入图像原语：取图/自动渲染、网格切分、按区域裁剪准备。
from llm.client import LLMTransportError, chat, durable_user_message
# 导入 LLM 客户端：传输错误、对话调用、带原生视觉附件的持久 user 消息。
from llm.client import pop_last_reasoning as _pop_last_reasoning, parse_object
# 导入 reasoning 弹出（v38 K3 协议）与 JSON 对象解析。
from core.actor import (FIDELITY_NOTE, NUDGE, ONE_ACTION_NUDGE, PREMATURE_DONE, REPEAT_WARNING,
                         STRICT_NUDGE, SUMMARIZER_SYSTEM, VISION_AGENT_SYSTEM, Ask, Look, Program, build_system,
                         continuation_message, extract_plan, look_answer_message, look_message, multi_program_note,
                         opening_message, parse_turn, repair_message, trace_message)
# 导入 Actor 侧全部提示词/动作类型/解析器：系统提示、 nudges、Program/Look/Ask
# 动作对象、开场与续传消息、回合解析、轨迹渲染等。

# JIT 保真触发（v13）：经富文档库保存的程序会在其轨迹中收到"往返有损"
# 提醒——静态提示词版本的采纳率为 0%，在决策时刻送达才是解法。模式是"库级"
# 而非"任务级"：靠近富文档库或格式 token 的 .save( 调用。
_RICHSAVE_RE = _re.compile(
    r"\.save\s*\(|save_workbook|SaveAs", _re.IGNORECASE)
# 保存动作模式：.save( / save_workbook / SaveAs。
_RICHLIB_RE = _re.compile(
    r"openpyxl|python-docx|python-pptx|\bdocx\b|\bpptx\b|\bxlsx\b|\bods\b|\bodt\b"
    r"|Workbook\s*\(|Presentation\s*\(|Document\s*\(", _re.IGNORECASE)
# 富文档库模式：openpyxl/docx/pptx/ods/odt 及其类构造调用。
# v28：v23 的 `_look_redirect` JIT（按扩展名+疑问词的关键词分类器）已被移除
# ——它只是在强化系统原则 10（"精确的事用代码；look 只用于识别；读文件用代码"），
# A/B 证明它非承重（t063 上触发 56 次但 ON 不比 OFF 好），也是 harness 里最后一个
# 任务/行为正则分类器。原则自身承载它；决定权在模型。
from core.verifier import (VerifierSession, disagreement_message,
                            second_opinion, unverified_message,
                            verify_independent)
# 导入独立验证：会话容器、WRONG 分歧消息、第二意见、未确认消息、独立验证入口。
from core.verifier_runtime import (
    AgenticVerifierInfrastructureError, AgenticVerifierNoProgressError,
)
# 导入 Agentic Verifier 的两类守卫异常：基础设施失败与无进展。

log = logging.getLogger("rsiagent.loop")
# 模块级日志器（rsiagent.loop 命名空间）。


def _provider_request(cfg) -> dict:
    """转发显式冻结的 OpenRouter 路由；默认配置保持惰性（不发任何路由参数）。"""
    order = getattr(cfg, "provider_order", ())
    # 取配置的路由顺序（缺省为空）。
    if not order:
        return {}
        # 未配置：返回空字典（不向请求注入任何 provider 选项）。
    return {
        "provider_order": order,
        "provider_allow_fallbacks": getattr(
            cfg, "provider_allow_fallbacks", True),
        "provider_require_parameters": getattr(
            cfg, "provider_require_parameters", False),
    }
    # 展开为请求选项：冻结路由、是否允许回退、是否要求参数透传。


_MEMORY_REVISIT = {
    "verifier": "The Verifier Agent has provided new evidence about the current candidate.",
    "fold": "Some earlier conversation context was compressed into the work log.",
    "recovery": "The conversation or machine channel has just recovered from an interruption.",
    "stagnation": "Recent actions show a recurring or low-information pattern.",
}
# 记忆回访提示文案：按生命周期事件（验证新证据/上下文折叠/通道恢复/行动停滞）分类。


def _memory_revisit(cfg, event: str) -> str:
    """在生命周期边界提供检索机会，但不替 Actor 做选择。

    未挂接冻结记忆时完全惰性。它绝不点名、排序、读取或总结任何记忆文件；
    Actor Agent 可以完全忽略它。
    """
    if not getattr(cfg, "env_memory_dir", ""):
        return ""
        # 未挂接记忆目录：返回空（不提示）。
    return ("\n\n[MEMORY RETRIEVAL OPPORTUNITY] " + _MEMORY_REVISIT[event]
            + " Durable memory remains available at ~/.memory. Consider re-reading "
              "previously useful files or inspecting unread files that may now be "
              "relevant; decide yourself what, if anything, to retrieve.")
    # 追加提示：只告知事件与 ~/.memory 可用，检索与否由 Actor 自己决定。


def _action_signature(turn):
    """一个 Program/Look 请求的"身份"，刻意不含其结果。"""
    if isinstance(turn, Program):
        # 程序动作：语言 + 代码 sha256。
        code_sha256 = hashlib.sha256(
            (turn.code or "").encode("utf-8", "replace")).hexdigest()
        # 代码内容指纹（None 视为空串）。
        return ("program", str(turn.lang).strip().lower(), code_sha256)
        # 返回 (kind, 语言, 代码指纹)。
    if isinstance(turn, Look):
        # 观察动作：路径列表 + 问题 + 区域。
        return ("look", tuple(turn.paths), turn.question,
                tuple(turn.region) if turn.region is not None else None)
        # 返回可哈希的元组形式。
    return None
    # 其他动作（Ask/done）不参与循环观察。


class _ActionRecurrence:
    """只观察"精确重复的连续动作块"，不控制它。

    两动作块是最小单位：单动作重复已有独立的、感知输出的检测器。对索引使
    常规历史下的观察为 O(1)；当某对再次出现时，把报告的块向后延伸——只要仍
    保持精确、不重叠的匹配。更早的指纹在无关绕路之后依然可用。
    """

    def __init__(self):
        # 初始化指纹序列与"该对首次出现起点"的索引。
        self.signatures = []
        # 历史动作指纹（按出现顺序）。
        self.first_pair = {}
        # 相邻对指纹 -> 首次出现的起始下标。

    def add(self, signature):
        """返回 (更早块起点, 当前块起点, 宽度)，无重复则 None。"""
        self.signatures.append(signature)
        # 登记新指纹。
        if len(self.signatures) < 2:
            return None
            # 不足两个动作：构不成"对"。
        current_start = len(self.signatures) - 2
        # 当前相邻对的起始下标（最后两个动作）。
        pair = tuple(self.signatures[-2:])
        # 当前相邻对的指纹。
        earlier_start = self.first_pair.setdefault(pair, current_start)
        # 查该对首次出现的起点（没有则登记当前位置）。
        # 相邻块可以相接，但绝不能重叠。
        if earlier_start == current_start or earlier_start + 2 > current_start:
            return None
            # 与首次出现相同位置或重叠：不算重复块。
        earlier_end = earlier_start + 1
        # 更早块的结束下标。
        width = 2
        # 初始宽度 = 一个两动作块。
        while (earlier_start > 0 and current_start - 1 > earlier_end
               and self.signatures[earlier_start - 1]
               == self.signatures[current_start - 1]):
            # 两侧各向前扩一位，只要指纹继续逐位相同。
            earlier_start -= 1
            current_start -= 1
            width += 1
            # 扩展匹配窗口。
        return earlier_start, current_start, width
        # 返回重复块的精确位置与宽度。


def _cycle_observation(action_iters: list, match) -> str:
    """只给纯证据：不要求回应形态，也不规定决定。"""
    earlier_start, current_start, width = match
    # 解包重复块的位置与宽度。
    earlier = action_iters[earlier_start:earlier_start + width]
    current = action_iters[current_start:current_start + width]
    # 取两段块的迭代号。
    return (
        "\n\nLIVENESS OBSERVATION: parsed actions "
        f"{earlier[0]}-{earlier[-1]} and {current[0]}-{current[-1]} are the same "
        f"{width}-action block (matching action kind and program-code/look-request "
        "fingerprints). This is execution-history evidence only; the harness has not "
        "inferred whether the recurrence is useful. Account for it when choosing "
        "your next action."
    )
    # 活性观察文本：两段动作指纹相同——这只是执行历史证据，harness 没有推断
    # 重复是否有用；如何考虑由模型自行决定。


def _vlm_look(vision_model: str, question: str, datas: list, region, cfg) -> str:
    """v22：把 look 的图像与问题路由给视觉模型，返回其"文本"答案。失明的主
    模型绝不接收像素——它只对这段文本观察做推理。Constraint #0 仍然成立：
    VLM 只看到环境内容 + Actor 的问题。v22.1：vision_rounds>1 时这是一个多轮
    AGENT（先 zoom/crop/grid 再下结论）；<=1 是单发工具——自 v30 起经由
    look-ensemble（core/eyes.py）路由：look_ensemble=1 时精确复现单发路径，
    否则在其上运行 N 个独立证人 + 共识/分歧合并。"""
    if getattr(cfg, "vision_rounds", 1) > 1:
        # 多轮模式：交给多轮视觉 Agent。
        return _vision_agent(vision_model, question, datas, cfg, region=region)
    return ensemble_look(cfg, question, datas, region=region)
    # 单发模式：交给 look-ensemble（含证人与共识）。


def _fmt_vision_answer(a) -> str:
    """把 Agent 的答案对象（文本 + 置信度 + 显著上下文）渲染为文本。"""
    if isinstance(a, dict):
        # 结构化答案：拼接各字段。
        txt = str(a.get("text", "")).strip()
        # 主文本。
        if a.get("confidence"):
            txt += f"\n[confidence: {a['confidence']}]"
            # 有置信度则附上。
        if a.get("also_visible"):
            txt += f"\nALSO VISIBLE: {a['also_visible']}"
            # 有"其他可见内容"则附上。
        return txt
        # 返回拼接结果。
    return str(a).strip()
    # 非字典（纯文本）：原样去空白。


def _vision_agent(vision_model: str, question: str, datas: list, cfg, region=None) -> str:
    """v22.1：多轮感知 Agent（镜像 verify_independent 的模式）。VLM 可在至多
    cfg.vision_rounds 轮内 zoom/crop/grid 检视图像，然后给出自信的答案。一眼
    之劳会数错/读错小字，系统性的检视不会。轮次耗尽时由一次"强制索答"调用
    逼出答案。"""
    rounds = max(2, int(getattr(cfg, "vision_rounds", 5)))
    # 检视轮数（至少 2）。
    q = question or "Describe everything relevant to a computer task; read text verbatim."
    # 问题缺省：描述一切相关内容并逐字读出文本。
    history: list = []
    # 视觉 Agent 自己的会话历史。
    view, note = prepare_look_images(datas, region=region)        # v35: 尊重 Actor 请求
    #                                                             的裁剪区域（此前
    #                                                             被静默忽略）
    user = (f"QUESTION: {q}\n\n{note}\nInspect as needed, then answer. You have up to "
            f"{rounds} inspection rounds.")
    # 开场：问题 + 图像说明 + 轮次预算。
    answer, used, nudges = None, 0, 0
    # 最终答案、已用轮次、格式纠偏次数。
    try:
        while used < rounds and nudges < 3:
            # 主循环：轮次与纠偏预算内反复。
            out = chat(vision_model, VISION_AGENT_SYSTEM, user, max_tokens=1500,
                       temperature=0.0, reasoning_effort="", history=history, image=view)
            # 调视觉模型（温度 0，附当前视图）。
            history += [{"role": "user", "content": user},
                        {"role": "assistant", "content": out or "(empty reply)"}]
            # 把本轮对话记入视觉 Agent 历史。
            obj = parse_object(out) or {}
            # 解析 JSON 对象。
            if "answer" in obj:
                answer = _fmt_vision_answer(obj["answer"]); break
                # 模型给出答案：渲染并结束。
            insp = obj.get("inspect")
            # 否则应是一个 inspect 指令。
            if not isinstance(insp, dict):                       # 纠偏——本轮不消耗
                nudges += 1
                user = ('Reply with ONE JSON object: {"inspect": {...}} or '
                        '{"answer": {"text": "...", "confidence": "...", "also_visible": "..."}}.')
                continue
                # 输出格式不对：提示重发（不消耗检视轮次）。
            used += 1
            # 消耗一个检视轮次。
            op = str(insp.get("op", "full"))
            # 取操作类型（full/grid/crop/zoom）。
            if op == "grid":
                view, note = grid_cells(datas, insp.get("rows", 2), insp.get("cols", 2),
                                        insp.get("cell"))
                # 网格切分检视。
            elif op in ("crop", "zoom") and insp.get("region"):
                view, note = prepare_look_images(datas, region=insp["region"])
                # 按模型指定区域裁剪/放大。
            else:
                view, note = prepare_look_images(datas, region=None)
                # 回到整图。
            user = (f"{note}\n\n{rounds - used} inspection round(s) left. Inspect again, "
                    "or give your answer object now.")
            # 下一轮提示：剩余轮次 + 再次检视或作答。
        if answer is None:                                       # 强制索答（verifier 模式）
            out = chat(vision_model, VISION_AGENT_SYSTEM,
                       "Inspection budget exhausted. Based ONLY on what you have already "
                       'seen, give your final answer NOW: {"answer": {"text": "...", '
                       '"confidence": "...", "also_visible": "..."}}.',
                       max_tokens=1500, temperature=0.0, reasoning_effort="",
                       history=history, image=view)
            # 轮次耗尽：逼它基于已看内容立即给出最终答案。
            answer = _fmt_vision_answer((parse_object(out) or {}).get("answer", out))
            # 渲染最终答案（解析失败则用原始输出）。
    except LLMTransportError:
        raise                              # 调用方暂停处理；传输问题不是感知问题
    except Exception as e:                                       # "眼睛"绝不能拖垮整个 run
        return f"(vision agent error: {e})"
        # 任何感知内部错误降级为错误说明文本，不让 run 崩溃。
    return (answer or "").strip() or "(vision agent returned nothing)"
    # 返回答案（空则如实说明）。


_FIND = (r"find /home/user/Desktop /home/user/Documents /home/user -maxdepth 2 "
         r"-type f -not -path '*/.*' -printf '%T@ %p\n' 2>/dev/null | sort -rn")
# 客户机内"可交付表面"清点命令：浅层普通文件 + 修改时间戳，按时间倒序。


def _path_within(path: str, roots=()) -> bool:
    """判断 path 是否落在 roots 中的某个根之下（用于排除私有表面）。"""

    normalized = os.path.normpath(path)
    # 归一化目标路径。
    return any(
        normalized == os.path.normpath(root)
        or normalized.startswith(os.path.normpath(root).rstrip("/") + "/")
        for root in (roots or ()))
    # 等于某根，或以"根/"为前缀（防前缀误配，如 /home/user2 不会匹配 /home/user）。


def _snapshot(vm, excluded_paths=()) -> dict:
    """v17：某一时刻候选可交付表面的 {路径 -> mtime} 映射。done 时做差分，告诉
    检查者本次运行真正"创建/修改"了哪些与指令相关的文件——伪造/循环类家族
    （改名副本、从零新建文档、栽种证据）会让指令点名的表面保持原封不动，
    检查者因此能看见并推理这一点（只是建议性上下文，不是硬门：提升检查者的
    能力，而不是它的权限）。"""
    # 这是机械上下文，不是模型选择的检索。静默丢弃旧路径可能抹掉权威
    # 原件或真正的交付物，所以传输完整清单，相关性由 Verifier 裁决。
    out = vm.run_command(_FIND, timeout=30, cap=0) or ""
    # 在客户机内执行清点（不截断输出）。
    m = {}
    # 结果映射。
    for line in out.splitlines():
        # 逐行解析。
        parts = line.strip().split(" ", 1)
        # 第一段是 mtime，其余是路径（路径可能含空格）。
        if len(parts) == 2 and not _path_within(parts[1], excluded_paths):
            m[parts[1]] = parts[0]
            # 排除私有路径后登记。
    return m
    # 返回表面快照。


def _surface_delta(vm, baseline: dict, excluded_paths=()) -> str:
    """给检查者的上下文字符串：自 ``baseline`` 以来创建/修改的文件。"""
    if not baseline:
        return ""
        # 没有基线：无法差分，返回空。
    now = _snapshot(vm, excluded_paths=excluded_paths)
    # 取当前快照。
    created = [p for p in now if p not in baseline]
    # 本次新建的路径。
    modified = [p for p in now if p in baseline and now[p] != baseline[p]]
    # mtime 变化的旧路径。
    if not created and not modified:
        return ""
        # 无变化：空串（这是一个"可证明未动文件"的信号）。
    parts = ["FILE-CHANGE EVIDENCE (mechanical, since this attempt began — use it to "
             "judge whether the surface the task NAMES was actually acted on; a task "
             "that says to edit/fill an existing file is NOT satisfied by a brand-new "
             "differently-named file left beside an untouched original):"]
    # 头部说明：这是机械证据；用于判断"任务点名的表面是否真的被动过"。
    if created:
        parts.append("CREATED this run: " + ", ".join(sorted(created)))
        # 新建清单（排序保证稳定）。
    if modified:
        parts.append("MODIFIED this run: " + ", ".join(sorted(modified)))
        # 修改清单。
    return "\n".join(parts)
    # 拼接为多行证据文本。


def _actor_evidence_leads(accepted, results) -> str:
    """UNVERIFIED 之后，无损传输且仅传输"可复现的 Done 探针"。

    Verifier 保持独立：Actor 的推理、结论与私有轨迹保持隐藏。这些确切的
    命令只是线索——Verifier 可以重跑它们，或从另一条通道证伪。
    """
    blocks = []
    # 线索块列表。
    for index, check in enumerate(accepted, start=1):
        # 逐条已接受的检查。
        result = results[index - 1] if index <= len(results) else None
        # 对应的执行结果（缺失则 None）。
        observed = ("PASS" if result is not None and result.passed else
                    "FAIL" if result is not None else "NOT_RUN")
        # harness 侧的执行分类（仅事实，非判定）。
        blocks.append(
            f"LEAD {index}\n"
            f"Actor-stated claim: {check.desc}\n"
            f"Exact reproducible probe proposed by Actor:\n"
            f"--- BEGIN PROBE ---\n{check.probe}\n--- END PROBE ---\n"
            f"Harness execution classification: {observed}")
        # 线索块：Actor 声称的主张 + 确切可复现探针 + harness 执行分类。
    return "\n\n".join(blocks)
    # 拼接全部线索。


# 【中文注释】LoopResult：一次尝试的完整结果遥测（@dataclass 生成 __init__ 等）。
# status：done/evolve/budget/safety_ceiling/stalled/infra；iters 是"被解析的动作
# 回合"（程序+look+done），turns 是全部模型调用（含空转/被轻推的回合），两者之差
# 即格式损耗。infra_pause_secs 把供应商停机从工作时钟剔除。surface_delta 在停滞
# 退出时记录文件增量（""=可证明未改任何文件；None=未采样）——pivot 决策依据。
@dataclass
class LoopResult:
    """一次尝试的完整结果遥测（@dataclass 生成 __init__ 等）。"""
    status: str = "budget"      # done | evolve | budget | safety_ceiling | stalled | infra
    iters: int = 0              # 消耗的"被解析动作回合"数（程序 + look + done）
    turns: int = 0              # 全部模型调用数（含空转/被轻推的回合）
    programs_run: int = 0
    # 执行过的程序数。
    looks: int = 0              # look 动作数（按需视觉）
    asks: int = 0               # 经配置的用户通道发出的提问数
    checks_passed: int = 0      # 被接受的 done 中通过的检查数
    wall_secs: float = 0.0
    # 墙钟耗时（含暂停；纯工作时间 = 它减去 infra_pause_secs）。
    infra_pause_secs: float = 0.0  # 从工作时钟剔除的供应商/API 等待
    infra_pauses: int = 0          # 可恢复传输暂停的批次数（可审计）
    inspections: list = None    # [(轮次, 判定)] —— 每次独立检查事件
    resumes: int = 0            # v15: 已消耗的全新上下文续跑次数
    worklog: str = ""           # 最终 WORK LOG（续跑交接 / 事后剖析用）
    verifier_route: str = ""    # 统一循环的传输路由；未启用时为空
    verifier_report: str = ""   # 本地 Verifier Agent 的完整无损报告
    curriculum_route: str = ""  # PASS 后为 HANDOFF/VERIFY_MORE；FAIL 后为 REVISE/EVOLVE
    curriculum_report: str = "" # Curriculum Agent 的完整无损路由报告
    surface_delta: str = None   # v18: 停滞退出时的文件增量（"" = 可证明未创建/
    #                             修改任何文件；None = 未知/未采样）


# 【中文注释】用户通道基础设施错误：ask_user 通道一旦接入，其失败就是环境/传输
# 失败，不是"Actor 可以用猜测替代"的证据。抛出异常保持官方评估器密封，
# 防止被基础设施损伤的候选拿到分数。
class UserChannelInfrastructureError(RuntimeError):
    """任务提供的用户应答通道未能产生应答。

    一旦接入了 ``ask_user``，其失败就是环境/传输失败，不是 Actor 可以用
    猜测来替代的证据。抛出异常保持官方评估器密封，防止被基础设施损伤的
    候选拿到分数。
    """


# 【中文注释】_call_with_transport_pause：语义级模型操作的"可恢复传输暂停"包装。
# 供应商/API 异常不是助手消息——绝不伪造空回复。可恢复失败按机械延迟原样重试；
# 调用方经 on_pause 把暂停时长从活动工作时钟扣除（嵌套调用也公平）；致命的
# 请求/配置错误立即抛出，不让坏配置无限等待。
def _call_with_transport_pause(call, cfg, *, label: str = "LLM",
                               on_pause=None):
    """运行一次语义级模型操作，遇到可恢复传输失败就"暂停"而非伪造结果。

    供应商/API 异常不是一条助手消息。原样操作在机械延迟后重试；调用方可经
    ``on_pause`` 把暂停时长移出自己的活动工作时钟。致命的请求/配置错误仍然
    立即抛出。
    """
    delay = max(0.0, float(getattr(cfg, "llm_infra_retry_secs", 30.0) or 0.0))
    # 重试间隔（配置可调，缺省 30 秒）。
    while True:
        # 重试循环：可恢复错误一直等到成功或致命错误。
        started = time.monotonic()
        # 记录本次尝试开始时刻（单单调时钟，不受系统改时影响）。
        try:
            return call()
            # 正常路径：执行并返回。
        except LLMTransportError as exc:
            # 捕获传输层错误。
            if not exc.recoverable:
                log.error("%s transport failure is not recoverable: %s", label, exc)
                raise
                # 不可恢复（请求/配置错误）：立即抛出，不做无限等待。
            status = (f"HTTP {exc.status_code}"
                      if exc.status_code is not None else type(exc.cause).__name__)
            # 状态描述：HTTP 状态码或底层异常类型名。
            log.warning("%s infrastructure pause (%s); preserving the same Agent "
                        "context and retrying unchanged in %.0fs", label, status, delay)
            # 留痕：基础设施暂停，Agent 上下文保持不变。
            time.sleep(delay)
            # 机械延迟后重试同一操作。
            paused = max(0.0, time.monotonic() - started)
            # 计算本次暂停实际时长。
            if on_pause is not None:
                on_pause(paused)
                # 通知调用方：从其工作时钟中扣除这段暂停（嵌套也公平）。


def uv_accept_ok(cfg, unverified: int, commit: bool, wrongs: int) -> bool:
    # 【中文注释】E4-A1"未验证即接受"决策（抽成纯函数便于测试）。
    # 遗留策略：第 N 次 could-not-confirm 就接受（E3 逃生门，多个负分案例的根源）；
    # budget_conditional_accept 只在收官阶段且记录干净时接受；verifier_continuity
    # 让一次 WRONG 撤销本运行剩余的"疑点利益"——用证据而非耗尽的耐心结案。
    """E4-A1：未验证即接受的决策（抽成纯函数便于测试）。

    遗留策略（两个开关都关）：第 N 次 could-not-confirm 就接受，不管本次
    运行的记录——E3 逃生门（t005 -1.0 / t039 -.55 / t035 -.50 都栽在这里）。
    budget_conditional_accept（v21，造好但从未开启）：只在收官阶段且记录干净
    时接受。verifier_continuity（）：一次 WRONG 撤销本次运行剩余的
    "疑点利益"——用证据而非耗尽的耐心结案。
    """
    # 全 Agent 验证臂有二元终局契约。缺失或无效的报告是基础设施/传输缺位，
    # 绝不是"候选正确"的证据——无论发生多少次。
    if getattr(cfg, "agentic_verifier_config", ""):
        return False
        # 启用 Agentic Verifier 时永远不接受"未验证"：交由二元契约裁决。
    return (unverified >= cfg.unverified_accept
            and (not cfg.budget_conditional_accept or (commit and wrongs == 0))
            and not (getattr(cfg, "verifier_continuity", False) and wrongs > 0))
    # 三条件：达到未确认次数阈值；（若开启预算条件）处于收官期且无 WRONG；
    # （若开启连续性）本运行没有出过 WRONG。


def run_attempt(instruction: str, vm, cfg, sink, iters_budget: int = None,
                wall_budget: float = None, opening_extra: str = "",
                iter_hook=None, initial_history=None,
                continue_context: bool = False,
                allow_noop_done: bool = False,
                terminal_handoff_ready=None, verifier_session=None,
                program_executor=None, look_path_validator=None,
                look_fetcher=None,
                system_prompt=None, action_nudge=None,
                strict_action_nudge=None, premature_done_nudge=None,
                user_message_transform=None, surface_baseline=None,
                instruction_is_complete_opening: bool = False,
                verifier_failure_router=None, verifier_pass_router=None,
                ask_user=None, opening_image=None):
    """运行一次尝试；返回 (LoopResult, history)。``sink`` 是一个 ArtifactSink。
    ``iters_budget``/``wall_budget`` 覆盖配置预算（v15 续跑传入剩余量）；
    ``opening_extra`` 前置拼接到开场消息（即续跑交接）。``initial_history`` +
    ``continue_context`` 选择在同一 Actor Agent 对话中开启后续阶段。
    ``allow_noop_done`` 允许这类阶段在不强制执行无意义程序的情况下保持现状。
    ``terminal_handoff_ready`` 是可选的"仅传输"回调：在程序动作完成后返回真
    值表示 Agent 已发布其规范终局交接，尝试在再一次随机模型调用之前结束。
    它不检查任务正确性。``verifier_session`` 在该 Verifier 身份活跃期间承载
    同一个 Verifier Agent；续跑控制器在"同角色续跑"时复用它，在 Verifier
    模型/配置切换后供给一个新会话。``program_executor`` 可选地只替换模型
    自著 Program 的执行；默认 ``vm.run_script``。``look_path_validator`` 可在
    渲染前拒绝模型选择的 Look 目标，``look_fetcher`` 可经角色专属隔离边界执行
    受信渲染；默认走普通 VM 渲染器。全部缺省时保持每个 Actor 调用点不变。
    ``system_prompt`` 与三个 nudge 覆盖让受限角色描述自己真实的动作契约，
    而不继承 Actor 专属的 Python/Bash 指令。``user_message_transform`` 可无损
    地为每条外发观察包上该角色的权威动作提醒。
    ``instruction_is_complete_opening`` 让受限角色供给自己的完整首段/恢复信封，
    而不继承 Actor 专属的 PLAN/done 语言；对所有 Actor 调用点默认关闭。
    ``opening_image`` 可选地把此前已交付的确切视觉观察重新附到恢复段的首个
    user 轮次上。它是传输状态，不是新观察；对每个普通 Actor 调用默认关闭。
    以上全部默认关闭，保持既有调用点行为。
    ``verifier_failure_router`` 是可选的"分权"回调，只在具体的 Verifier Agent
    FAIL 之后使用。它接收完整报告并返回 ``(REVISE|EVOLVE, 完整 Curriculum
    Agent 报告)``。没有回调时，与历史上"同一 Actor 修订"的行为逐字节一致。
    ``verifier_pass_router`` 是统一循环的互补回调。它接收完整的 Verifier PASS
    并返回 ``(HANDOFF|VERIFY_MORE, 完整 Curriculum Agent 报告)``。VERIFY_MORE
    在未改变的候选上续用同一个 Verifier Agent；它绝不把控制权交回 Actor，
    也不改变机器状态。
    ``iter_hook``（PREREG E7
    B5，默认 None 时完全惰性）：
    每次循环迭代以 res.iters 调用一次；返回真值会以 "stalled_quiescent" 结束
    尝试——这是练习侧停滞检测器的钩子。评测配置绝不传入它；为 None 时行为
    逐字节一致。"""
    t0 = time.time()
    # 工作时钟起点（供应商停机会把它向后平移）。
    budget = iters_budget or cfg.max_iters
    # 迭代预算：显式传入优先，否则用配置值。
    wall_cap = wall_budget or cfg.wall_clock_secs
    # 墙钟上限：显式传入优先，否则用配置值。
    agent_decided_stop = bool(getattr(cfg, "agent_decided_stop", False))
    # 是否"由 Agent 自己决定停止"（预算只作紧急看门狗，不进开场提示）。
    res = LoopResult()
    # 结果遥测容器。
    res.inspections = []
    # 初始化检查事件列表（dataclass 缺省 None）。

    def _record_transport_pause(seconds: float, events: int = 1) -> None:
        # 把活动工作起点向后平移：供应商停机既不消耗 Actor 也不消耗
        # Verifier 的紧急墙钟预算。
        nonlocal t0
        # 修改外层 t0。
        t0 += seconds
        # 时钟起点后移 = 工作时钟"扣除"这段停机。
        res.infra_pause_secs += seconds
        # 累计暂停秒数（遥测）。
        res.infra_pauses += max(0, int(events))
        # 累计暂停批次（遥测）。

    def _llm_operation(label: str, call):
        # 统一的语义模型操作入口：自带传输暂停与记账。
        return _call_with_transport_pause(
            call, cfg, label=label, on_pause=_record_transport_pause)

    history: list = list(initial_history or [])
    # 会话历史（拷贝入参，本函数内继续追加）。
    if len(history) % 2:
        raise ValueError("initial_history must contain complete user/assistant pairs")
        # 历史必须由完整的 user/assistant 对构成。
    if instruction_is_complete_opening:
        user = instruction
        # 受限角色自带完整开场信封：指令文本原样作为首条 user 消息。
    else:
        _opening = continuation_message if continue_context else opening_message
        # 按是否续传选择开场消息构造器。
        user = _opening(
            instruction, None if agent_decided_stop else budget,
            wall_mins=None if agent_decided_stop else int(wall_cap // 60))
        # 构造开场：指令 + （非 Agent 决定停止时）预算与剩余分钟数。
    if opening_extra:
        user = opening_extra + "\n\n" + user
        # 前置附加 opening_extra（如续跑交接文本）。
    dry = 0          # 连续不可解析回合数
    degen = 0        # v21 (A): 连续"退化多动作"回合数（解码器循环）
    nudges = 0       # 仅累计遥测；绝不作为完成/停滞上限
    dones = 0        # 连续失败的 done 声明数
    plan = ""        # 模型自己最新的 'PLAN:' 行——钉进每条轨迹消息
    image = opening_image  # 附到下一次 chat 调用的图像字节（Look 之后）；
    #                  恢复段可用归档的 Look 像素精确初始化；
    #                  成功送达的字节随后会留在 history 里
    prev_sig = None  # (程序 md5, stdout md5) —— 重复检测器
    repeats = 0      # 连续 REPEAT 检出次数：仅警告打不破温度 0 的吸引子
    #                  （run 自己的日志能诊断出循环，Actor 却在继续它），
    #                  所以第二次再犯按 dry 重试同等待遇升温
    cycle_tracker = (_ActionRecurrence()
                     if getattr(cfg, "cycle_evidence", False) else None)
    # 循环块观察器（配置开启才建）。
    cycle_iters = []
    # 参与观察的动作迭代号。
    cycle_active = False  # 一次重复块持续期间抑制重复提示
    extra = 0        # 已授予的"检查-修复"扩展回合数（受配置上限约束）
    unverified = 0   # 无法确认的检查次数；达到 cfg.unverified_accept 时
    #                  接受声明（浅检查 ≠ 一票否决权）
    wrongs = 0       # 有生以来"被确认错误"的次数；它们阻止把后来的
    #                  unverified 洗成接受，但绝不替换 Actor Agent，
    #                  也不丢弃其语义修复上下文。
    forced = False   # 预算末尾那唯一一次强制检查是否已用过
    summary = ""     # 摘要模式压缩：模型自写的运行日志（WORK LOG）
    covered = 0      # 已折叠进摘要的对话对数
    fidelity_sent = False   # JIT 富保存提醒每次尝试只发一次
    infra_streak = 0        # v17: 连续 run 包装器失败数（死 VM 检测器）
    doubt_reviews = 0       # R2/v31: 已消耗的疑点升级二次检查次数（有上限）
    had_done_bounce = False # R2/v31: 是否有过 done 声明被弹回……
    progs_since_bounce = 0  # ……弹回后跑过的程序数——下次接受时若为 0
    #                         即"空心重声明"（机器上什么都不可能变过；
    #                         Actor 重掷或削弱了检查）-> 触发复审
    splits = []             # R3/v32: 读数出现"分歧"的 look 台账——
    #                         {'turn','path','q','open'}。同一 path 之后的
    #                         look 若达成一致则关闭对应条目；未关闭条目
    #                         会门控第一次 done（一次性），并作为机械
    #                         上下文交给检查者。
    split_gate_spent = False  # R3: 一次性 done 门是否已触发
    witness_gate_spent = False  # Fix D-2（E2 剖检）：一次性见证弹回
    actor_evidence_requested = False  # Agentic UNVERIFIED 只索要可复现线索，
    #                                   在 Actor 的下一次 Done 声明里给出。
    vsession = (verifier_session if isinstance(verifier_session, VerifierSession)
                else VerifierSession())  # E4-A1: 每个项目只有一个 Verifier Agent。
    # 它的对话与累计遥测共享项目生命周期；证据新鲜度在每次 verify_independent
    # 调用内部重置。
    if getattr(cfg, "archive_eyes", False) and sink is not None:
        # v32.5: 把交给"眼睛"的每张图都归档到 <run>/eyes/ —— 人类裁判必须能
        # 打开模型确切看到的东西（alpha 拉平 bug 曾因无人能复看而隐藏了 3 轮）。
        import os as _os
        from core import imagery as _imagery
        _imagery.ARCHIVE_DIR = _os.path.join(getattr(sink, "root", "."), "eyes")
        # 设置图像归档目录。
    system = system_prompt or build_system(
        cfg, ask_enabled=ask_user is not None)  # 机制匹配当前角色
    private_surface_paths = ()
    # 表面快照的排除路径（Verifier 私有面）。
    if getattr(cfg, "verifier_evolve_route", False):
        private_surface_paths = tuple(dict.fromkeys(
            tuple(getattr(cfg, "verifier_private_paths", ()) or ())
            + (("/home/user/.memory",)
               if getattr(cfg, "verifier_hide_actor_memory", False) else ())))
        # 统一循环路由启用时：私有路径 = 配置私有路径 +（按需）Actor 记忆目录，
        # 去重保序。
    baseline = (_snapshot(vm, excluded_paths=private_surface_paths)
                if surface_baseline is None
                else dict(surface_baseline))
    # v17: 侦察（setup 后）时刻的可交付表面清单。统一目标运行在 Actor 存在
    # 之前捕获它，并在全新上下文的 Actor 续跑之间复用同一份精确 S0 投影。

    def _split_context() -> str:
        """R3：未决视觉分歧的机械说明，附加给检查者（v17 风格——只说循环自身
        观察到的事实，绝不说 Actor 的推理）。"""
        op = [s for s in splits if s['open']]
        # 取仍未关闭的分歧条目。
        if not op:
            return ""
            # 没有未决分歧：空串。
        lines = "; ".join(f"{s['path']} ({s['q']})" for s in op[:6])
        # 列出前 6 条：路径 + 问题。
        return ("Mechanical note: independent visual readings of the machine "
                "DISAGREED and were never re-read to agreement on: " + lines +
                ". If any of these bears on a requirement, verify that point "
                "directly (render/look/measure) rather than trusting either reading.")
        # 机械说明：独立视觉读数曾不一致且从未重读到一致；若相关请直接验证。

    def _with_splits(base: str) -> str:
        # 把分歧说明拼在给定上下文之后（有才拼）。
        sctx = _split_context()
        return (base + "\n\n" + sctx) if sctx else base

    def _verifier_wall_budget() -> float:
        """Verifier 共享、且不能悄悄延长本次运行的墙钟。"""
        return max(0.0, wall_cap - (time.time() - t0))
        # 剩余墙钟（不为负）。

    def _route_verified_pass(f) -> tuple[str, str]:
        """让高层权威审计一个 PASS 是否可以终结。"""
        if verifier_pass_router is None:
            return "HANDOFF", ""
            # 无回调：历史行为，直接交接。
        routed = verifier_pass_router(str(f))
        # 把完整 PASS 报告交给高层路由者。
        if (not isinstance(routed, tuple) or len(routed) != 2
                or not isinstance(routed[0], str)
                or not isinstance(routed[1], str)):
            raise RuntimeError(
                "verifier_pass_router must return (HANDOFF|VERIFY_MORE, report)")
            # 返回形状不合法：立即报错（绝不猜）。
        route, report = routed[0].strip().upper(), routed[1]
        # 归一路由词与报告。
        if route not in {"HANDOFF", "VERIFY_MORE"} or not report.strip():
            raise RuntimeError(
                "Curriculum Agent PASS routing must be HANDOFF/VERIFY_MORE "
                "with a report")
            # 路由词必须合法且报告非空。
        res.curriculum_route = route
        # 记录路由到遥测。
        res.curriculum_report = report
        # 记录完整路由报告。
        res.inspections.append((res.turns, f"curriculum:{route.lower()}"))
        # 检查事件台账补一条。
        log.info("Verifier PASS -> persistent Curriculum Agent -> %s", route)
        # 留痕。
        return route, report
        # 返回路由与报告。

    def _inspect(context: str, actor_evidence_leads: str = ""):
        """先跑本地验证，再取得独立的终局钥匙。

        每份已提交的 Verifier 报告都会在 Curriculum 或 Actor 可能花费大量时间
        之前做检查点并卸下私有暂存。这移除了长期闲置的命名空间保活者，
        同时保住其确切字节。
        """
        curriculum_review = ""
        # 课程复审文本（VERIFY_MORE 循环时非空）。
        same_candidate = False
        # 是否为"同一候选上的追加检查"。
        infra_retries = 0
        # 已用基础设施重试次数。
        max_infra_retries = max(
            0, int(getattr(cfg, "verifier_infra_retries", 0) or 0))
        # 允许的基础设施重试上限。
        while True:
            # 循环：基础设施重试或 VERIFY_MORE 复检。
            kwargs = {
                "context": context,
                "session": vsession,
            }
            # 基础调用参数：上下文 + 持久会话。
            # 保持既有 verifier 调用面逐字节不变，除非 Curriculum Agent
            # 确实要求对同一未变候选再查一次。历史/自定义 verifier 可调用物
            # 因此无需感知这个可选的双钥匙生命周期。
            if same_candidate:
                kwargs.update(
                    continue_candidate=True,
                    curriculum_review=curriculum_review)
                # 追加检查：告知"候选未变"并附课程复审意见。
            if actor_evidence_leads:
                kwargs["actor_evidence_leads"] = actor_evidence_leads
                # 有 Actor 证据线索则传入。
            if getattr(cfg, "agentic_verifier_config", ""):
                kwargs.update(wall_budget=_verifier_wall_budget())
                # Agentic 模式：传剩余墙钟预算。
            nested_secs_before = vsession.infra_pause_secs
            nested_events_before = vsession.infra_pauses
            # 记录嵌套会话的暂停计数基线（用于取增量）。
            try:
                inspected = _llm_operation(
                    "Verifier Agent",
                    lambda: verify_independent(
                        instruction, vm, cfg, sink, res.turns, **kwargs))
                # 执行独立验证（外层带传输暂停保护）。
            except AgenticVerifierNoProgressError:
                # 这需要检查/上下文恢复，而不是用同一个无动作检查点
                # 再重建一次 VM 边界。
                try:
                    sink.save_transcript(system, history)
                except Exception:
                    log.exception("Could not flush Actor context at Verifier guard")
                    # 尽力把 Actor 上下文落盘供恢复；失败仅记日志。
                raise
                # 原样上抛：由上层按基础设施事件处理。
            except AgenticVerifierInfrastructureError:
                if (not getattr(cfg, "agentic_verifier_config", "")
                        or not getattr(cfg, "verifier_persist_scratch", False)
                        or not isinstance(vsession, VerifierSession)
                        or infra_retries >= max_infra_retries):
                    raise
                    # 不满足重试条件（非 agentic/不持久化/非会话/超次数）：直接抛。
                # 执行器持有 QEMU 检查点。卸载会关闭它并在任何重试之前恢复
                # 确切候选。死掉的控制可能阻止可选的私有暂存导出；该归档
                # 可以在基础设施重试中放弃，但回滚失败仍然抛错、保持 fail-closed。
                recovery_started = time.time()
                vsession.detach_executor(
                    vm, preserve=True, tolerate_archive_failure=True)
                # 卸下执行器：回滚候选到检查点，允许归档失败。
                recovery_secs = time.time() - recovery_started
                # 恢复耗时。
                if recovery_secs > 0:
                    _record_transport_pause(recovery_secs, 1)
                    # 从工作时钟扣除（基础设施恢复不计工作）。
                infra_retries += 1
                same_candidate = True
                # 计数并保持"同一候选"。
                log.warning(
                    "Verifier trusted runtime failed; candidate rolled back, "
                    "rebuilding boundary for retry %d/%d",
                    infra_retries, max_infra_retries)
                # 留痕。
                continue
                # 重试本轮。
            # Agentic Verifier 是一个嵌套 run_attempt。它已经排除自己的供应商
            # 暂停并记录在持久会话上；只把本次检查的增量传播给外层 Actor 时钟。
            nested_secs = max(
                0.0, vsession.infra_pause_secs - nested_secs_before)
            nested_events = max(
                0, vsession.infra_pauses - nested_events_before)
            # 计算本次检查期间的暂停增量。
            if nested_secs or nested_events:
                _record_transport_pause(nested_secs, nested_events)
                # 增量记账。
            if (getattr(cfg, "agentic_verifier_config", "")
                    and getattr(cfg, "verifier_persist_scratch", False)
                    and isinstance(vsession, VerifierSession)):
                vsession.detach_executor(vm, preserve=True)
                # 报告已提交：做检查点并卸下执行器（保留候选现场）。

            verdict, findings = inspected
            # 解包判定与发现。
            if verdict != "pass" or verifier_pass_router is None:
                return inspected
                # 非 PASS 或无 PASS 路由：原样返回。
            route, curriculum_report = _route_verified_pass(findings)
            # PASS：交给高层路由审计。
            if route == "HANDOFF":
                return inspected
                # HANDOFF：接受，返回结果。
            # 候选没有变。同一个 Verifier Agent 只收到高层对"报告是否充分"的
            # 质疑，并继续自行决定如何调查、其本地判定是否仍然成立。
            curriculum_review = curriculum_report
            same_candidate = True
            # 进入 VERIFY_MORE 复检循环。

    # ---- R2/v31 辅助：在"接受决定"处的疑点门控二次检查 ----
    def _review_ok() -> bool:
        # 复审可用性：开关开、有升级模型、且复审次数未到上限。
        return (getattr(cfg, "doubt_escalation", False) and bool(cfg.escalation_model)
                and doubt_reviews < getattr(cfg, "doubt_reviews_max", 2))

    def _route_verified_failure(f) -> str:
        """让高层权威为一次本地正确性失败选择路由。"""
        if verifier_failure_router is None:
            # 无回调：走历史协议。
            if getattr(cfg, "verifier_failure_starts_evolution", False):
                res.inspections.append((res.turns, "protocol:evolve_after_fail"))
                log.info(
                    "Verifier FAIL -> self-evolving benchmark protocol -> EVOLVE")
                return "EVOLVE"
                # 自进化基准协议：FAIL 触发 EVOLVE。
            return "REVISE"
            # 默认：同一 Actor 修订。
        routed = verifier_failure_router(str(f))
        # 把完整 FAIL 报告交给高层路由者。
        if (not isinstance(routed, tuple) or len(routed) != 2
                or not isinstance(routed[0], str)
                or not isinstance(routed[1], str)):
            raise RuntimeError(
                "verifier_failure_router must return (REVISE|EVOLVE, report)")
            # 返回形状不合法：报错。
        route, report = routed[0].strip().upper(), routed[1]
        # 归一化路由词与报告。
        if route not in {"REVISE", "EVOLVE"} or not report.strip():
            raise RuntimeError(
                "Curriculum Agent routing must be REVISE/EVOLVE with a report")
            # 路由词合法且报告非空。
        res.curriculum_route = route
        res.curriculum_report = report
        res.inspections.append((res.turns, f"curriculum:{route.lower()}"))
        # 全部记入遥测。
        log.info("Verifier FAIL -> persistent Curriculum Agent -> %s", route)
        # 留痕。
        return route

    def _wrong_bounce(f) -> str:
        """对复审 'wrong' 判定复用检查者-wrong 分支的镜像。语义反馈总是回到
        同一个 Actor Agent/上下文。"""
        nonlocal wrongs, extra, user, had_done_bounce, progs_since_bounce
        # 修改外层状态。
        wrongs += 1
        # 记一次被确认的错误。
        if _route_verified_failure(f) == "EVOLVE":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(f)
            return "evolve"
            # 高层要求进化：终态 evolve。
        had_done_bounce, progs_since_bounce = True, 0
        # 记录弹回事实并清零弹回后程序数（空心重声明检测）。
        if extra < cfg.inspect_extension * cfg.max_inspect_extensions:
            extra += cfg.inspect_extension
            log.info("review bounce -> +%d turns (extension %d)",
                     cfg.inspect_extension, extra)
            # 预算内授予扩展回合供修复。
        user = disagreement_message(f) + _memory_revisit(cfg, "verifier")
        # 下一条消息 = 分歧反馈 + 记忆回访提示。
        return "bounced"
        # 返回"已弹回"。

    def _review(reason: str, first_report: str) -> str:
        """跑第二意见；返回 accept / bounced / evolve / stall。"""
        nonlocal doubt_reviews
        doubt_reviews += 1
        # 复审次数 +1（受上限约束）。
        review_session = VerifierSession()
        # 第二意见用独立会话（与主 Verifier 分开）。
        v2, f2, rmodel = _llm_operation(
            "Verifier second opinion",
            lambda: second_opinion(
                instruction, vm, cfg, sink, res.turns, reason, first_report,
                context=_surface_delta(
                    vm, baseline, excluded_paths=private_surface_paths),
                session=review_session))
        # 调第二意见（附机械表面增量上下文与复审理由）。
        # 全 Agent 第二意见是另一个嵌套 run_attempt。它内部处理的供应商暂停
        # 要与主持久 Verifier Agent 一样记账。
        if review_session.infra_pause_secs or review_session.infra_pauses:
            _record_transport_pause(
                review_session.infra_pause_secs, review_session.infra_pauses)
            # 把嵌套会话的暂停增量记入外层时钟。
        res.inspections.append((res.turns, f"review:{v2}"))
        # 检查事件台账。
        log.info("iter %d: doubt-review #%d (%s) -> %s",
                 res.iters, doubt_reviews, rmodel, v2)
        # 留痕。
        if v2 == "wrong":
            return _wrong_bounce(f2)
            # 第二意见确认错误：弹回修复。
        if v2 == "evolve":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(f2)
            return "evolve"
            # 第二意见要求进化：终态 evolve。
        return "accept"          # pass，或第二次 could-not-confirm：只有具体的
    #                              违规证据才能在这里阻止接受

    while True:
        # 主循环：预算/时钟守卫 -> 压缩 -> 采样 -> 解析 -> 分派动作。
        if iter_hook is not None and iter_hook(res.iters):
            res.status = "stalled_quiescent"     # E7 B5: harness 因静默而终止；
            log.info("stall detector ended the session at iter %d",  # 由调用方
                     res.iters)                  # 原样评分
            break
            # 停滞检测器钩子触发：会话在这里结束。
        if res.iters >= budget + extra:          # 预算耗尽 -> 一次"强制检查"
            if agent_decided_stop:
                res.status = "safety_ceiling"
                log.warning("emergency iteration ceiling reached before agent done")
                break
                # Agent 决定停止模式：上限只是紧急天花板。
            if (cfg.independent_verify and not forced   # 大多数弱模型运行从不
                    and time.time() - t0 <= wall_cap):   # 声明完成，检查者从没
                forced = True                           # 有机会帮忙——预算耗尽
                verdict, findings = _inspect(  # 时强制检查一次
                    _with_splits(_surface_delta(
                        vm, baseline, excluded_paths=private_surface_paths)))
                res.inspections.append((res.turns, f"forced:{verdict}"))
                log.info("forced end-of-budget inspection -> %s", verdict)
                if verdict == "pass":
                    res.status = "done"                 # 检查确认完成（虽未声明）
                    if getattr(cfg, "verifier_evolve_route", False):
                        res.verifier_route = "HANDOFF"
                        res.verifier_report = str(findings)
                    break
                    # PASS：以 done 收尾并按需交接。
                if verdict == "evolve":
                    res.status = "evolve"
                    res.verifier_route = "EVOLVE"
                    res.verifier_report = str(findings)
                    break
                    # 进化路由：以 evolve 收尾。
                if (verdict == "wrong" and
                        extra < cfg.inspect_extension * cfg.max_inspect_extensions):
                    if _wrong_bounce(findings) == "evolve":
                        break
                    continue
                    # 确认错误且还有扩展额度：弹回修复。
            break
            # 预算耗尽：以当前状态（budget）结束。
        if time.time() - t0 > wall_cap:
            if agent_decided_stop:
                res.status = "safety_ceiling"
                log.warning("emergency wall ceiling reached before agent done")
            else:
                log.info("wall clock exhausted")
            break
            # 墙钟耗尽：结束（Agent 决定停止模式记为 safety_ceiling）。
        if cfg.history_keep_pairs > 0:      # 压缩 = 模型自写 WORK LOG（唯一模式）
            before_fold = covered
            summary, covered = _llm_operation(
                "Actor work-log compaction",
                lambda: _fold_summary(                          # （唯一模式）
                    history, covered, summary, cfg, sink, res.turns))
            if covered > before_fold:
                user += _memory_revisit(cfg, "fold")
                # 本轮发生了折叠：给 Actor 一次记忆回访机会。
            ctx = ([{"role": "user", "content":
                     # v21 (context): 把任务逐字钉在日志上方——开场消息是第 0 对、
                     # 最先被折叠的东西，目标曾因此只剩摘要器的转述（有些 run 干脆丢了）。
                     "TASK (verbatim, unchanged — this is the actual goal):\n"
                     + instruction + "\n\nWORK LOG — your running summary of earlier "
                     "turns (full details live in the machine; re-probe if you need "
                     "specifics):\n" + summary}]
                   + history[covered * 2:]) if summary else history
            # 上下文 = [逐字任务 + WORK LOG] + 未折叠的逐字历史。
        else:
            ctx = history                   # 0 = 完整历史（短程默认）
        if getattr(cfg, "primary_temperature", -1.0) >= 0:
            temp = cfg.primary_temperature   # 官方 K3 采样点；恒温常开 ->
            #                                  吸收掉 dry 重试的升温
        else:
            temp = cfg.retry_temperature if (dry or degen or repeats >= 2) else cfg.temperature
            # 未设恒温：dry/退化/重复时升温重试，否则用常温。
        sent_user = (user_message_transform(user)
                     if user_message_transform is not None else user)
        # 角色消息变换（受限角色包上权威动作提醒）。
        sent_image = image
        # 本次要附的图像字节。
        transport_options = {}
        if dry and os.environ.get("RSIAGENT_JSON_ACTION_RETRY") == "1":
            # 格式重试：模型、采样、上下文与解析器全部保持不变。
            # 只有确认路由支持 response_format 后才启用。
            transport_options["json_object"] = True
            # 启用 JSON 对象响应格式。
            log.info("retrying unparseable action with JSON object response format")
        out = _llm_operation(
            "Actor Agent",
            lambda: chat(
                cfg.model, system, sent_user, max_tokens=cfg.max_tokens,
                temperature=temp, top_p=getattr(cfg, "top_p", -1.0),
                reasoning_effort=cfg.reasoning_effort,
                history=ctx,
                image=sent_image,
                reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
                **transport_options,
                **_provider_request(cfg)))                      # dry 重试升温：温度 0 时
        image = None                               # 退化的解码器会永远重采样"同一份"
        #                                            干输出（约 25% 的团灭类）；
        #                                            升温打破循环
        amsg = {"role": "assistant", "content": out or "(empty reply)"}
        # 构造助手消息（空回复也如实记录）。
        if getattr(cfg, "reasoning_in_history", False):
            _rsn = _pop_last_reasoning()           # v38 官方 K3 协议：完整的
            if _rsn:                               # 助手消息（含 reasoning）回到
                amsg["reasoning"] = _rsn           # 上下文，而不只是内容
        # 保留确切的原生视觉观察，而不是只留"附了图"这句话。委托眼睛的报告
        # 已是持久文本，到达这里时 ``sent_image`` 为 None。
        history += [durable_user_message(sent_user, sent_image), amsg]
        # user（带原生图像附件）+ assistant 追加进历史。
        res.turns += 1
        # 模型调用数 +1。
        sink.save_turn(res.turns, out)
        # 原始输出落盘。
        plan = extract_plan(out) or plan          # 最新修订优先；跨回合存活
        turn = parse_turn(out)
        # 解析动作回合。

        if turn is None:                                   # 模型真正的"干"回合
            dry += 1
            nudges += 1
            if dry >= cfg.max_consec_dry:
                res.status = "stalled"
                log.info("stalled: %d consecutive dry turns / %d nudges", dry, nudges)
                break
                # 连续干回合达到上限：停滞。
            user = ((strict_action_nudge or STRICT_NUDGE)
                    if getattr(cfg, "action_discipline", False)
                    else (action_nudge or NUDGE))     # v36 k3fit: 感知叙述的轻推
            continue
            # 轻推后重试。
        dry = 0
        if cfg.strict_one_action and getattr(turn, "dup", 1) >= 2:
            # v21 (A): 解码器退化——回复里含 >=2 个同类动作对象，last-wins 会
            # 执行仪式性/重复对象（连续 17 个 `echo done` 空转、看不见的 34 轮
            # 构建循环）。什么都不跑，重新要一个动作；连续卡死升级到全新上下文
            # pivot——它能可靠打破解码器循环。
            degen += 1
            nudges += 1
            if degen >= cfg.max_consec_degen:
                res.status = "stalled"
                log.info("stalled: %d consecutive degenerate multi-action turns "
                         "(%dx one kind) -> stall/pivot", degen, getattr(turn, "dup", 1))
                break
                # 连续退化达到上限：停滞。
            user = ONE_ACTION_NUDGE
            log.info("iter %d: degenerate turn (%dx one kind) -> re-ask, ran nothing",
                     res.iters, getattr(turn, "dup", 1))
            continue
            # 重新索要单动作。
        degen = 0
        res.iters += 1
        # 有效动作回合 +1。
        cycle_note = ""
        action_sig = _action_signature(turn) if cycle_tracker is not None else None
        # 取动作指纹（观察器存在时）。
        if action_sig is not None:
            cycle_iters.append(res.iters)
            # 登记迭代号。
            cycle_match = cycle_tracker.add(action_sig)
            # 查重复块。
            if cycle_match is not None and not cycle_active:
                cycle_note = _cycle_observation(cycle_iters, cycle_match)
                a, b, width = cycle_match
                log.info("iter %d: recurring %d-action block observed (%d-%d repeats "
                         "%d-%d); evidence delivered without gating",
                         res.iters, width,
                         cycle_iters[a], cycle_iters[a + width - 1],
                         cycle_iters[b], cycle_iters[b + width - 1])
                # 发现新的重复块：生成观察文本并留痕。
            cycle_active = cycle_match is not None
            # 更新"重复进行中"状态。
        elif cycle_tracker is not None:
            # Done/check 边界开启新的动作序列。后续生命周期阶段不应与声明前的
            # 动作算作连续。
            cycle_tracker = _ActionRecurrence()
            cycle_iters = []
            cycle_active = False
            # 重置观察器。

        if isinstance(turn, Program):
            # 程序动作分支：执行 -> 轨迹回传 -> 重复检测。
            dones = 0
            # 有实质动作：失败 done 计数清零。
            res.programs_run += 1
            # 程序计数 +1。
            progs_since_bounce += 1                        # R2: 机器状态从现在起可能变了
            sink.save_program(res.turns, turn.lang, turn.code)
            # 程序原文落盘。
            log.info("iter %d: program (%s, %d chars)", res.iters, turn.lang, len(turn.code))
            execute_program = program_executor or vm.run_script
            # 执行器：注入的 program_executor 或默认 vm.run_script。
            trace = execute_program(
                turn.lang, turn.code, timeout=cfg.script_timeout)
            # 执行模型自著程序，拿到完整轨迹。
            _normalize_program_transport_failure(trace)
            # 归一化传输层失败（补判漏报的暂存失败）。
            recovered_channel = False
            # 通道是否已恢复。
            channel_failure = (trace.infra_fail
                               and trace.stdout.startswith("[channel error:"))
            # 通道失败特征：infra 失败且 stdout 以 "[channel error:" 开头。
            recovery_wait = getattr(vm, "wait_for_controller", None)
            recovery_secs = getattr(cfg, "controller_recovery_secs", 0)
            # 恢复能力与恢复窗口配置。
            if channel_failure and callable(recovery_wait) and recovery_secs > 0:
                # 资源重的 Actor 动作可能重启了客户机控制器。不要消耗随机的
                # Actor 轮次去探测暂时死掉的通道，也绝不重放失败的动作。
                # 机械等待，然后让同一段对话基于明确的失败自行推理。
                recovered_channel, recovery_report = recovery_wait(
                    timeout=recovery_secs,
                    probe_interval=getattr(
                        cfg, "controller_recovery_probe_secs", 5.0),
                    stable_probes=getattr(
                        cfg, "controller_recovery_stable_probes", 2))
                # 机械等待恢复窗口（按间隔探测、要求连续稳定）。
                disposition = (
                    "The failed program was NOT replayed. Its result is unknown; "
                    "inspect live state before relying on any partial effects."
                    if recovered_channel else
                    "The failed program was NOT replayed and the environment remains "
                    "unavailable.")
                # 恢复/未恢复两种处置说明（都强调没有重放）。
                trace.stdout += (
                    "\n[HARNESS CHANNEL RECOVERY: " + recovery_report + ". "
                    + disposition + "]")
                # 把恢复报告与处置追加到轨迹（对话据此推理）。
            sink.save_trace(res.turns, trace)
            # 轨迹落盘。
            log.info("iter %d: exit=%s secs=%.0f out=%d chars", res.iters,
                     trace.exit_code, trace.secs, len(trace.stdout))
            if trace.infra_fail:                           # v17: 死 VM 检测器 ——
                if program_executor is not None:
                    # 受信 Verifier 沙箱失败意味着它自著的动作没有执行。
                    # 不要再消耗 Agent 轮次把基础设施解读成候选证据。
                    res.status = "infra"
                    log.warning(
                        "aborting Verifier phase on trusted sandbox failure — "
                        "infra, not task")
                    break
                    # 受信沙箱失败：按 infra 中止，绝不是任务结论。
                if recovered_channel:
                    infra_streak = 0
                    log.info("iter %d: controller stable again; same Actor resumes "
                             "without action replay", res.iters)
                    # 控制器恢复稳定：同一 Actor 继续，不重放动作。
                elif channel_failure and callable(recovery_wait) and recovery_secs > 0:
                    # 一个完整的恢复窗口比三次即时的模型生成探测更有力地
                    # 证明通道已死。
                    res.status = "infra"
                    log.warning("aborting: controller failed its full %.0fs recovery "
                                "window — infra, not task", recovery_secs)
                    break
                    # 恢复窗口失败：按 infra 中止。
                else:
                    infra_streak += 1                      # run 包装器无法暂存程序
                if infra_streak >= 3:                      # （客户机文件系统只读？）。
                    res.status = "infra"                   # 自连击开始以来没有程序
                    log.warning("aborting: %d consecutive run-wrapper failures "  # 跑过
                                "(guest fs read-only?) — infra, not task", infra_streak)
                    break                                  # ——连续 3 次即判 infra。
            else:
                infra_streak = 0
                # 程序正常执行：连击清零。
            # 某些 agentic 阶段通过程序动作发布自由格式终局交接。发布必须与停止
            # 原子化：再要一次礼仪性的 ``done`` 采样，会让 Agent 已写下的决定在
            # 随机间隙里可被改写。调用方拥有纯词法的就绪谓词；core 既不知道令牌
            # 词表也不知道其含义。
            if (terminal_handoff_ready is not None
                    and not trace.infra_fail
                    and terminal_handoff_ready()):
                res.status = "done"
                log.info("iter %d: canonical terminal handoff published",
                         res.iters)
                break
                # 规范终局交接已发布：立即结束（不再做随机模型调用）。
            spent = time.time() - t0                       # 收官阶段按以下任一触发：
            commit = (False if agent_decided_stop else
                      (res.iters >= max(1, int(budget * cfg.commit_frac))
                       or spent >= wall_cap * cfg.commit_frac))   # 预算或墙钟
            wall_mins = (None if agent_decided_stop else
                         max(0, int((wall_cap - spent) // 60)))
            turns_left = None if agent_decided_stop else budget + extra - res.iters
            # 收官标志、剩余分钟数、剩余回合数（Agent 决定停止时不告知）。
            user = trace_message(trace, turns_left, plan, commit,
                                 cfg.trace_head, cfg.trace_tail, wall_mins=wall_mins,
                                 context_max_chars=getattr(
                                     cfg, "trace_context_max_chars", 0))
            # 构造轨迹回传消息（头/尾保留、剩余预算、PLAN 钉入）。
            if recovered_channel:
                user += _memory_revisit(cfg, "recovery")
                # 通道刚恢复：给一次记忆回访机会。
            if turn.extras:                                # 多程序回复：只有最后
                user += multi_program_note(turn.extras)    # 一个跑了——要说破，否则
                log.info("iter %d: %d extra program object(s) discarded",  # 模型会建立
                         res.iters, turn.extras)           # 错误的环境模型
            if (not fidelity_sent and _RICHSAVE_RE.search(turn.code)
                    and _RICHLIB_RE.search(turn.code)):
                user += FIDELITY_NOTE                      # JIT 送达（v13）：在保存
                fidelity_sent = True                       # 时刻触发，仅一次
                log.info("iter %d: rich-save detected -> fidelity note", res.iters)
                # 富文档库保存 + 保真提醒：在决策时刻送达。
            sig = (_md5(turn.code), _md5(trace.stdout))    # 重复检测器：
            if sig == prev_sig:                            # 代码与输出全同
                repeats += 1
                user += REPEAT_WARNING                     # 第一次：警告；
                if repeats == 1:
                    user += _memory_revisit(cfg, "stagnation")
                log.info("iter %d: REPEAT detected (x%d)%s", res.iters, repeats,
                         " -> temp escalation" if repeats >= 2 else "")
                if repeats >= cfg.max_consec_repeat:       # 第二次起给解码器升温；
                    res.status = "stalled"                 # 到上限：快速失败——
                    log.info("stalled: %d consecutive identical program+output",
                             repeats)                      # 警告与升温都被证明
                    break                                  # 无效（497 次）；
            else:                                          # 合法轮询的输出是"变化的"，
                repeats = 0                                # 永远走不到这里
            prev_sig = sig
            user += cycle_note
            if cycle_note:
                user += _memory_revisit(cfg, "stagnation")
                # 有循环观察时：附加观察 + 停滞记忆回访。
            continue

        if isinstance(turn, Look):
            # 观察动作分支：取图/委托视觉 -> 文本答案或像素附件。
            dones = 0
            res.looks += 1
            # look 计数 +1。
            datas, errs = [], []
            # 成功取回的图像与错误列表。
            for p in turn.paths:
                # 逐个路径取图。
                if look_path_validator is not None:
                    allowed, reason = look_path_validator(p)
                    if not allowed:
                        errs.append(f"{p}: {reason}")
                        continue
                        # 路径校验器拒绝：记错误，跳过。
                fetch_image = look_fetcher or (
                    lambda target: fetch_look_image(vm, target))
                # 取图器：注入的 look_fetcher 或默认 VM 渲染。
                d, e = fetch_image(p)                      # v26: 文档自动渲染为图像
                if d is None:
                    errs.append(f"{p}: {e}")
                else:
                    datas.append(d)
                    # 成功：登记图像数据。
            sink.save_look(res.turns, turn.path, turn.question, ok=bool(datas))
            # look 事件落盘。
            log.info("iter %d: look %s (%d attached%s)", res.iters, turn.path,
                     len(datas), f", errors: {'; '.join(errs)}" if errs else "")
            spent = time.time() - t0
            commit = (False if agent_decided_stop else
                      (res.iters >= max(1, int(budget * cfg.commit_frac))
                       or spent >= wall_cap * cfg.commit_frac))
            wall_mins = (None if agent_decided_stop else
                         max(0, int((wall_cap - spent) // 60)))
            turns_left = None if agent_decided_stop else budget + extra - res.iters
            # 与 Program 分支相同的收官/预算参数。
            if not datas:
                user = (f"Could not read {turn.path}: {'; '.join(errs)}. Document files "
                        "are rendered to an image automatically — check the path names a "
                        "real, non-empty file, then continue."
                        + ("" if agent_decided_stop else
                           f" {budget + extra - res.iters} action turn(s) remain."))
                # 一张图都没取到：回传错误 + 修正建议（文档会自动渲染，检查路径）。
            elif cfg.vision_model:                             # v22: 把"看"委托给视觉
                answer = _llm_operation(
                    "visual reader",
                    lambda: _vlm_look(                              # 模型；主模型保持失明
                        cfg.vision_model, turn.question,             #
                        datas, turn.region, cfg))
                log.info("iter %d: vision tool (%s) -> %d chars",
                         res.iters, cfg.vision_model, len(answer))
                if getattr(answer, "split", None) is True:     # R3: 把冲突记入台账
                    # (v34: `is True` —— 纯 str 的 .split 是绑定"方法"，恒为真值；
                    # 视觉 Agent 臂返回纯 str，曾给每次 look 都记了一笔幻影分歧)
                    splits.append({'turn': res.turns, 'path': turn.path,
                                   'q': (turn.question or "")[:80], 'open': True})
                    log.info("iter %d: split LEDGERED (%d open)", res.iters,
                             sum(1 for s in splits if s['open']))
                    # 读数分歧：记入台账（开放状态）。
                else:                                          # 该路径达成一致
                    closed = 0                                 # -> 关闭其开放分歧
                    for s in splits:
                        if s['open'] and s['path'] == turn.path:
                            s['open'] = False
                            closed += 1
                    if closed:
                        log.info("iter %d: %d split(s) on %s closed by agreeing re-read",
                                 res.iters, closed, turn.path)
                        # 一致的重读关闭了旧的分歧条目。
                image = None                                   # 不附给主模型
                user = look_answer_message(turn.path, turn.question, answer,
                                           turns_left, plan, commit,
                                           wall_mins=wall_mins)
                # 主模型只收文本答案。
                if errs:
                    user = f"(Some paths not attached — {'; '.join(errs)}.)\n\n" + user
                    # 部分路径失败：如实说明。
            else:
                image, att_note = prepare_look_images(datas, region=turn.region,
                                                     max_side=getattr(cfg, "look_max_side", 0) or None)  # pre-108 第 5 项：与 eyes 路径的原生对齐
                user = look_message(turn.path, turn.question,   # 附到下一次调用；此后保留
                                                               #
                                    turns_left, plan, commit,
                                    wall_mins=wall_mins)
                # 原生视觉路径：像素挂到下一轮 chat 调用上。
                if att_note:
                    user = att_note + "\n\n" + user
                    # 图像准备说明前置。
                if errs:
                    user = f"(Not attached — {'; '.join(errs)}.)\n\n" + user
                    # 失败路径如实说明。
            user += cycle_note
            if cycle_note:
                user += _memory_revisit(cfg, "stagnation")
                # 循环观察与记忆回访（同 Program 分支）。
            continue

        if isinstance(turn, Ask):
            # 提问分支：经配置的用户通道获取外部应答。
            res.asks += 1
            # 提问计数 +1。
            question = turn.question.strip()
            # 取问题文本。
            if ask_user is None:
                answer = ("No user-response channel is configured for this task. "
                          "Continue from evidence available in the environment.")
                ok = False
                # 未配置通道：如实告知并让 Agent 从环境证据继续。
            else:
                try:
                    answer = str(ask_user(question))
                    ok = bool(answer.strip())
                    # 调用通道；空应答视为不可用。
                except Exception as exc:  # 通道失败显式化，绝不静默吞掉
                    answer = ("[USER CHANNEL ERROR: " + type(exc).__name__ +
                              ": " + str(exc) + "]")
                    ok = False
            sink.save_ask(res.turns, question, answer, ok)
            # 问答落盘。
            log.info("iter %d: ask user -> %s", res.iters,
                     "answered" if ok else "unavailable")
            if ask_user is not None and not ok:
                # 配置的任务通道出错或返回空应答就是基础设施损坏。不要把这种
                # 缺位变成合成的用户答案、不要让 Actor 去猜，也不要在由此产生
                # 的候选上调用密封评分器。
                raise UserChannelInfrastructureError(
                    "configured user-response channel failed; task attempt is "
                    "infra-invalid and must be retried from a clean environment")
                # 通道损坏：抛错，本次尝试 infra 作废，须在干净环境中重试。
            user = ("USER RESPONSE to your question:\n" + answer +
                    "\n\nContinue the same task. Reply with one program, look, ask, "
                    "or done action.")
            # 回传用户应答，要求继续同一任务。
            continue

        # --- Done 声明 ---
        if res.programs_run == 0 and not allow_noop_done:  # 什么都没做就想 done
            dones += 1
            if dones >= cfg.max_consec_done:
                res.status = "stalled"
                break
                # 连续空手 done 到上限：停滞。
            user = premature_done_nudge or PREMATURE_DONE
            continue
            # 轻推：先做事再声明。
        if getattr(cfg, "practice_mode", False):
            # P2 练习档：done 直接结束会话——没有任务检查、没有检查者
            # （本就没有任务）。claim->verify->curate 在循环之外
            # （run_explore.py）基于内存差异、回滚前运行。
            _req = getattr(cfg, "practice_done_requires", "")
            if _req:
                # BRIEF Phase-0 门（07-31，t012 "prep 里写任务"绕过之后）：
                # 会话声明的交付物必须先"存在"，done 才会被接受。只探测存在性
                # ——绝不读内容。反复拒绝会撞 max_consec_done -> stalled ->
                # fail-open。
                _probe = vm.run_command(
                    f"test -f {_req} && echo GATE_YES || echo GATE_NO")
                # 机械存在性探测。
                if "GATE_YES" not in _probe:
                    dones += 1
                    if dones >= cfg.max_consec_done:
                        res.status = "stalled"
                        break
                        # 反复拒绝：停滞。
                    user = (f"Not finished yet: {_req} does not exist. This "
                            "session's one required product is that file — "
                            "write it first (even a minimal one stating your "
                            "honest judgment, e.g. that your memory does not "
                            "apply, is valid), then declare done again.")
                    continue
                    # 提示：先写出那个必需文件（哪怕是最小的诚实判断）。
            res.status = "done"
            log.info("iter %d: practice session declared complete", res.iters)
            break
            # 练习会话完成。
        accepted, rejections, dropped = validate(
            turn.checks, cfg.max_checks, instruction=instruction,
            witness_gates=getattr(cfg, "done_witness_gates", False))
        # 校验自检：接受合法检查、拒绝/丢弃非法项（上限、可证伪性等）。
        results = run_checks(accepted, vm, timeout=cfg.check_timeout)
        # 在活环境里真正执行全部已接受检查。
        sink.save_checks(res.turns, accepted, rejections + dropped, results)
        # 检查与结果落盘。
        n_pass = sum(1 for r in results if r.passed)
        # 统计通过数。
        log.info("iter %d: done declared — %d/%d checks pass, %d rejected, %d dropped",
                 res.iters, n_pass, len(results), len(rejections), len(dropped))
        all_pass = bool(accepted) and all(r.passed for r in results)
        # 是否全部通过（且至少有一条检查）。
        arbiter = all_pass and bool(rejections) and cfg.independent_verify
        # "检查者仲裁"场景：全过但有被拒项。
        # R3/v32 一次性分歧门：在台账中的视觉分歧仍未解决时声明 done，会带着
        # 台账 + 解决协议弹回一次——检测终于"门控"，而且是在还有回合余量时
        # 触发（法证发现：在终局接受时才浮出的疑点改变不了任何事）。
        # 第二次声明无论如何放行：代价封顶为一次弹回。
        open_splits = [s for s in splits if s['open']]
        # 取仍开放的分歧条目。
        if (all_pass and (not rejections or arbiter) and open_splits
                and getattr(cfg, 'split_done_gate', False) and not split_gate_spent):
            # v34 修复：门的弹回是 HARNESS 发起的引导，不是一次失败的声明
            # ——它绝不能计入 max_consec_done。旧的 `dones += 1` + 停滞检查
            # 让这扇门成了第三击，杀死了健康的 run（t063 s995：两次检查失败
            # 的 done，然后一次 9/9 全过的 done -> 门触发 -> stalled；Actor
            # 根本没看到"先解决分歧"的消息）。构造上一次性
            # （split_gate_spent），无循环风险。
            split_gate_spent = True
            had_done_bounce, progs_since_bounce = True, 0
            res.inspections.append((res.turns, f"splitgate:{len(open_splits)}"))
            log.info("iter %d: done gated — %d unresolved visual split(s)",
                     res.iters, len(open_splits))
            user = unresolved_splits_message(open_splits)
            continue
            # 弹回一次：要求先解决视觉分歧。
        # 门摩擦修复：可证伪性正则会把无害的只读探针误判为"会改动"；若要求
        # 零拒绝，done 就会被永远挡住（run 会卡在反复"全过但 1 条被拒"上）。
        # 当全部已接受检查都通过、只有门在反对时，改由独立检查者"仲裁"——
        # 正则保留它对"实际执行探针"的否决权，而不是对 done 的否决权。
        # Fix D-2（E2 剖检，一次性）：已接受探针里不含独立通道见证
        # （render/convert/screenshot/活进程/http）的 done 只验证了记录，
        # 没验证工作——正是输掉的那些 run 的签名。弹回一次索要一个见证探针；
        # 第二次声明无论如何放行（v34：harness 引导，不是失败的 done——
        # 不计停滞次数）。
        _WITNESS = ("soffice", "convert", "pdftoppm", "screenshot", "import -",
                    "xwd", "xdotool", "curl", "wget", "http", "ps ", "pgrep",
                    "qpdf", "pdftotext", "identify", "compare ")
        # 独立通道见证工具特征串。
        if (all_pass and (not rejections or arbiter) and accepted
                and getattr(cfg, "done_witness_gates", False)
                and not witness_gate_spent
                and not any(w in c.probe.lower() for c in accepted
                            for w in _WITNESS)):
            # 全过但没有任何独立通道见证：门控一次。
            witness_gate_spent = True
            had_done_bounce, progs_since_bounce = True, 0
            res.inspections.append((res.turns, "witnessgate"))
            log.info("iter %d: done gated once — no independent-channel witness "
                     "among %d accepted checks", res.iters, len(accepted))
            user = ("Your checks all pass, but every one of them re-reads files "
                    "or state this run produced — none witnesses the work "
                    "through an independent channel. Add AT LEAST ONE check "
                    "that verifies the deliverable from outside your own "
                    "records: render/convert it and inspect the result, query "
                    "the live system, or measure the artifact with a tool that "
                    "did not build it. Then declare done again (this request "
                    "fires only once).")
            continue
            # 弹回一次：要求加一条从"自己记录之外"验证交付物的检查。
        if all_pass and (not rejections or arbiter):
            # 声明成立的候选路径：全部通过（或由检查者仲裁）。
            if cfg.independent_verify:                 # v8: 全新上下文检查——
                if arbiter:
                    log.info("iter %d: %d rejected check(s), all accepted pass -> "
                             "inspector as arbiter", res.iters, len(rejections))
                    # 仲裁场景留痕。
                evidence_leads = (
                    _actor_evidence_leads(accepted, results)
                    if (actor_evidence_requested
                        and getattr(cfg, "verifier_unverified_evidence", False))
                    else "")
                # 按需附带 Actor 证据线索（上次 UNVERIFIED 时请求过才带）。
                actor_evidence_requested = False
                # 线索请求消费即清除。
                verdict, findings = _inspect(
                    _with_splits(_surface_delta(
                        vm, baseline, excluded_paths=private_surface_paths)),
                    actor_evidence_leads=evidence_leads)
                # 独立检查：机械表面增量 + 分歧说明作为上下文。
                res.inspections.append(
                    (res.turns, f"arbiter:{verdict}" if arbiter else verdict))
                log.info("iter %d: independent inspection -> %s", res.iters, verdict)
                if verdict == "evolve":
                    res.status = "evolve"
                    res.verifier_route = "EVOLVE"
                    res.verifier_report = str(findings)
                    break
                    # 检查者要求进化：终态 evolve。
                if verdict == "wrong":                 # 已确认违规：必须修。
                    if _wrong_bounce(findings) == "evolve":
                        break
                    continue
                    # 弹回同一 Actor 修复。
                if verdict == "unverified":            # 无法确认 ≠ 错误：浅检查
                    unverified += 1                    # 不该有一票否决权
                    # v21 (B)："could not confirm"只在晚期（收官阶段）自动接受，
                    # 且此前没有已确认错误；否则弹回修复。旧规则不管预算/错误
                    # 记录就接受第 2 次 unverified——那些 run 中 47% 得分 <0.1
                    # （预算没用完就自截；错误连击被洗白成接受）。
                    spent = time.time() - t0
                    commit = (res.iters >= max(1, int(budget * cfg.commit_frac))
                              or spent >= wall_cap * cfg.commit_frac)
                    # 重新计算收官标志。
                    # 统一循环要求显式的 HANDOFF 路由。没有产生路由的传输/
                    # 基础设施失败，绝不允许被旧的"晚期预算 could-not-confirm"
                    # 规则洗白成完成。
                    accept_uv = (not getattr(cfg, "verifier_evolve_route", False)
                                 and uv_accept_ok(
                                     cfg, unverified, commit, wrongs))
                    # 接受决定：统一循环下绝不接受；否则按 E4-A1 规则。
                    if not accept_uv:
                        if getattr(cfg, "verifier_unverified_evidence", False):
                            actor_evidence_requested = True
                            # 下次声明要求带可复现证据线索。
                        dones += 1
                        had_done_bounce, progs_since_bounce = True, 0   # R2
                        if dones >= cfg.max_consec_done:
                            res.status = "stalled"
                            break
                            # 反复无法确认：停滞。
                        user = unverified_message(
                            findings,
                            evidence_request=getattr(cfg, "verifier_continuity",
                                                     False))
                        continue
                        # 弹回修复/补充证据。
                    if _review_ok():
                        # R2/v31 触发 A：未验证即接受规则正要把一次
                        # could-not-confirm 洗成 done——这正是把"自信但错误"
                        # 的 run 以 0 分交付的那条路径（t063；t032 两次实测）。
                        # 把接受决定交给升级模型复核。
                        st = _review(
                            "could not CONFIRM completion (could-not-confirm is not "
                            "wrong), and the budget-phase acceptance rule was about "
                            "to accept the declaration anyway", str(findings))
                        if st == "bounced":
                            continue
                        if st == "evolve":
                            break
                        if st == "stall":
                            res.status = "stalled"
                            break
                            # 按第二意见结论行动。
                    else:
                        # R3: 复审关闭/到顶——保留诊断标记（法证审计靠这些工件）
                        res.inspections.append((res.turns, "doubt:uv_accept"))
                    log.info("unverified x%d (commit=%s wrongs=%d) -> accepting",
                             unverified, commit, wrongs)
                    # 接受（含理由遥测）。
                if verdict == "pass":
                    # R2/v31 触发 B/C：一个建立在"自述疑点"上的 pass（对实质需求
                    # 没有肯定证据就下了判断——t021 类），或一次"空心重声明"
                    # （上次弹回后没有跑过程序：机器上什么都不可能变过——要么
                    # 检查被削弱了，要么第一次弹回是噪声——让第二个模型来定）。
                    doubts = str(getattr(findings, "doubts", "") or "").strip()
                    hollow = had_done_bounce and progs_since_bounce == 0
                    if (doubts or hollow) and _review_ok():
                        why = []
                        if doubts:
                            why.append("PASSED while listing requirements it could "
                                       "not affirmatively confirm")
                        if hollow:
                            why.append("accepted a declaration re-issued with NO "
                                       "program executed since the last bounced "
                                       "declaration (machine state cannot have "
                                       "changed in between)")
                        first = str(findings) + (f"\nITS STATED DOUBTS: {doubts}"
                                                 if doubts else "")
                        st = _review(" and ".join(why), first)
                        if st == "bounced":
                            continue
                        if st == "evolve":
                            break
                        if st == "stall":
                            res.status = "stalled"
                            break
                            # 带疑点的 pass 交给第二意见裁决。
                    elif doubts or hollow:
                        # R3: 复审关闭/到顶——只留诊断标记
                        res.inspections.append(
                            (res.turns,
                             "doubt:" + ("pass_with_doubts" if doubts else "hollow")))
            if (cfg.independent_verify
                    and getattr(cfg, "verifier_evolve_route", False)):
                res.verifier_route = "HANDOFF"
                res.verifier_report = str(findings)
                # 统一循环：记录 PASS 交接路由与完整报告。
            res.checks_passed = len(results)
            res.status = "done"
            break
            # 接受声明：以 done 收尾。
        dones += 1
        had_done_bounce, progs_since_bounce = True, 0      # R2: 检查失败弹回
        if dones >= cfg.max_consec_done:
            res.status = "stalled"
            log.info("stalled: %d consecutive failed done declarations", dones)
            break
            # 连续失败声明到上限：停滞。
        user = repair_message(results, rejections)
        # 回传修复消息（检查结果 + 拒绝原因）。

    if res.status == "stalled":
        # v18: 停滞退出时做一次机械文件增量。"" 是"没有创建/修改任何候选交付物"
        # 的证明——上游靠它区分"已有存量工作、继续干"与"策略抽签失败、该转向"。
        res.surface_delta = (_surface_delta(
            vm, baseline, excluded_paths=private_surface_paths)
            if baseline else None)
        # 无基线时记 None（未采样）。

    if (res.status == "stalled" and cfg.independent_verify and not forced
            and time.time() - t0 <= wall_cap):
        # v11: 停滞恰恰是强制检查存在的意义——工作可能已完成（或进行过半）却
        # 从未声明/被接受；曾有 run 带着已落袋的真实状态卡死，却无人看过一眼。
        # 判定被记录；"pass" 会升级状态。（不扩展/不再进入：一个重新进入自身
        # 吸引子的卡死 run 没有期望价值——刻意保守。）
        forced = True
        verdict, findings = _inspect(
            _with_splits(res.surface_delta or ""))
        res.inspections.append((res.turns, f"stall:{verdict}"))
        log.info("stall-exit inspection -> %s", verdict)
        if verdict == "pass":
            res.status = "done"
            if getattr(cfg, "verifier_evolve_route", False):
                res.verifier_route = "HANDOFF"
                res.verifier_report = str(findings)
            # 停滞检查通过：升级为 done。
        elif verdict == "evolve":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(findings)
            # 检查者要求进化。
        elif verdict == "wrong" and _route_verified_failure(findings) == "EVOLVE":
            res.status = "evolve"
            res.verifier_route = "EVOLVE"
            res.verifier_report = str(findings)
            # 确认错误且高层路由为 EVOLVE。

    res.wall_secs = time.time() - t0
    # 记录总耗时。
    res.worklog = summary
    # 记录最终工作日志。
    if agent_decided_stop and sink is not None:
        sink.save_transcript(system, history)
        # Agent 决定停止模式：完整轨迹落盘（供生命周期上层使用）。
    return res, history
    # 返回（结果遥测, 历史）。


def _synthesize_worklog(history: list, cfg) -> str:
    """v36 (k3fit)：带着"空工作日志"的停滞续跑，等于给全新尝试什么都没交
    ——短段落从不触发上下文折叠，学到的一切被丢弃、重新侦察（t032 s900）。
    对被丢弃历史的尾部做一次摘要器调用，补上折叠本应生成的交接。"""
    tail = history[-60:]
    # 取尾部 60 条消息。
    rendered = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n" + _fold_input(m["content"])
        for m in tail)
    # 渲染为 YOU/ENV 折叠文本。
    out = chat(cfg.model, SUMMARIZER_SYSTEM,
               "CURRENT LOG:\n(empty)\n\nOLDER TURNS TO FOLD IN:\n" + rendered
               + "\n\nOutput the updated log only.",
               max_tokens=cfg.max_tokens, temperature=0.0,
               reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    # 一次摘要调用：从空日志合成工作日志。
    return _cap_worklog((out or "").strip(), cfg.worklog_max_chars)
    # 按上限裁剪后返回。


def _strategy_review(history: list, worklog: str, cfg) -> str:
    """v18：一次有界的自我事后剖析调用（同一 Actor 模型——单模型规则），
    输入是停滞尝试自己的日志与最后几轮。返回纯文本（调用"饥饿"时返回 ""；
    pivot 消息没有它也独立成立）。"""
    from core.actor import STRATEGY_REVIEW_SYSTEM, strategy_review_ask
    # 延迟导入复盘提示词。
    tail = "\n\n".join(
        f"[{'YOU' if m['role'] == 'assistant' else 'ENV'}]\n"
        + str(m["content"])[:1500] for m in history[-6:])
    # 最后 6 轮的短渲染（每条截 1500 字符）。
    out = chat(cfg.model, STRATEGY_REVIEW_SYSTEM,
               strategy_review_ask(worklog, tail), max_tokens=cfg.max_tokens,
               temperature=cfg.temperature, reasoning_effort=cfg.reasoning_effort,
               reasoning_max_tokens=getattr(cfg, "reasoning_max_tokens", 0),
               **_provider_request(cfg))
    # 调用复盘。
    return (out or "").strip()[:2500]
    # 截断到 2500 字符返回（饥饿时为空串）。


def escalation_cfg(cfg):
    """卡住的 run 升级到"另一个模型"时，构造新段落的配置（v21 模型升级）。
    纯函数——无副作用，因此可单元测试（这套替换曾经内联在绑定 VM 的循环里
    多年无法测试，悄悄剥掉了每个升级 GLM 段的 RIH）。

    角色相对的替换都在这里发生，这个区分就是全部要点：
      * vision_model -> 角色相对的"眼睛"（v39 原生一致性）：原生主模型升级为
        文本模型时，被降级的主模型成为它的眼睛。反方向：当被委托的眼睛自己
        成为 Actor 时，它用原生视觉，而不是冗余地委托给自己。
      * verifier_model -> 恢复跨模型（xverify）：若配置的 Verifier 就是升级后
        的 Actor，则把原 Actor 模型挪进 Verifier 席位，不允许自我审查。
      * agentic_verifier_config -> 升级角色专属配置（若设置）。调用方必须把
        其对话保持在 verifier 模型专属的会话里。
      * 采样/供应商路由 -> 升级专属值。采样点与供应商约束是模型相关的；
        Z.AI 路由绝不能跟着 GLM Actor 走进一个 K3 请求。

    不重置的项：reasoning_in_history。RIH 与模型无关——对两边都起决定作用
    （v38 K3 + GLM 阶梯 07-28；DECISIONS_LEDGER 规则 "rih: always"）。旧代码
    在阶梯之前误以为 RIH 是"k3 的协议"而把它重置为 False，悄悄从每个升级
    GLM 段里剥掉了它（08-12："这是我们的 bug"）。现在它继承基础配置的值。"""
    from dataclasses import replace as _replace
    if (getattr(cfg, "vision_model", "")
            and cfg.escalation_model == cfg.vision_model):
        esc_vision = ""  # 前任被委托的眼睛现在是原生有视觉的 Actor
    else:
        esc_vision = cfg.vision_model or cfg.model
        # 否则沿用角色相对的视觉模型（缺省回落到主模型）。
    esc_verifier = cfg.verifier_model or cfg.model
    # Verifier 席位：配置值或回落主模型。
    if (getattr(cfg, "escalation_verifier_cross_model", False)
            and esc_verifier == cfg.escalation_model):
        esc_verifier = cfg.model
        # 开启跨模型验证且 Verifier 撞上升级模型：换回原 Actor 模型（防自审）。
    esc_agentic_verifier = (
        getattr(cfg, "escalation_agentic_verifier_config", "")
        or getattr(cfg, "agentic_verifier_config", ""))
    # Agentic Verifier 配置：升级专属优先，否则沿用。
    return _replace(
        cfg, model=cfg.escalation_model,
        vision_model=esc_vision,
        primary_temperature=getattr(
            cfg, "escalation_primary_temperature", -1.0),
        provider_order=getattr(cfg, "escalation_provider_order", ()),
        provider_allow_fallbacks=getattr(
            cfg, "escalation_provider_allow_fallbacks", True),
        provider_require_parameters=getattr(
            cfg, "escalation_provider_require_parameters", False),
        verifier_model=esc_verifier,
        agentic_verifier_config=esc_agentic_verifier)
    # 生成升级配置副本：主模型、视觉、采样点、路由约束、验证席位全部就位。


def _verifier_session_identity(cfg) -> tuple[str, str]:
    """持久 Verifier Agent 的会话身份。

    角色/模型切换不得把一个模型的私有推理历史喂给另一个模型。同身份的
    Actor 续跑保留完全相同的既有会话。
    """
    agentic = str(getattr(cfg, "agentic_verifier_config", "") or "")
    if agentic:
        return "agentic", agentic
        # Agentic 模式：以专属配置路径为身份。
    return "legacy", str(getattr(cfg, "verifier_model", "") or cfg.model)
    # 旧式模式：以 verifier 模型名为身份。


def run_with_resume(instruction: str, vm, cfg, sink, opening_extra: str = "",
                    verifier_sessions: dict | None = None,
                    runtime_state: dict | None = None,
                    surface_baseline: dict | None = None,
                    verifier_failure_router=None, verifier_pass_router=None,
                    verifier_session_prepare=None, ask_user=None):
    """v15：停滞是"对话的病"，从来不是"机器的病"——剖检过的每次停滞
    （重复吸引子、多程序卡死、干死）都活在卡死的解码器上下文里，而机器状态
    始终健全。"全新上下文"是检查者的立身原则；这里把它应用到 Actor：当一次
    尝试停滞（且停滞检查未能挽救）而预算尚余时，丢弃对话、保留机器，用一份
    事实性交接在剩余预算上开一次全新尝试。受 cfg.max_resumes 约束。

    ``verifier_session_prepare`` 是一个可选的生命周期边界，在每个 Actor 段
    之前以活动配置及其模型私有的 :class:`VerifierSession` 调用。它让统一
    循环的调用方能为新激活的跨模型 Verifier 建立"候选盲"定向，而不把该策略
    暴露给普通基准运行。

    v18（策略多样的重启）：交接现在按停滞出口处的机械文件增量分流。增量为
    空 = 该尝试可证明什么都没落袋——抽签本身就是失败（策略彩票），所以新
    尝试得到一份自我事后剖析 + 一条"采取实质不同路线"的明确指令。增量非空
    （或未知）= 有工作落袋——保留已被验证的 v15 续跑交接不变。只使用自
    信号：循环状态、文件 mtime、模型自己的话——没有任何评分器派生信息
    （Constraint #0）。"""
    import os as _os

    from core.actor import handoff_message, pivot_handoff_message
    from core.trace import ArtifactSink

    # 统一循环的调用方在 EVOLVE 循环之后传入同一个注册表，让目标 Verifier
    # Agent 在 Actor Agent 与环境刷新的同时保留自己的调查上下文。普通基准
    # 运行省略它，得到历史上"单任务局部"的生命周期。
    if verifier_sessions is None:
        verifier_sessions = {}
        # 未提供注册表：本地新建（历史行为）。
    verifier_sessions.setdefault(
        _verifier_session_identity(cfg), VerifierSession())
    verifier_session = verifier_sessions[_verifier_session_identity(cfg)]
    # 按身份取/建持久 Verifier 会话。
    if verifier_session_prepare is not None:
        # 统一循环调用方用这个可选边界确保每个独立 Verifier 模型身份在检查
        # "Actor 形状的环境"之前，先完成自己的"候选盲"S0 定向。普通 harness
        # 调用方省略它，生命周期与历史完全一致。
        verifier_session_prepare(cfg, verifier_session)
        # 执行候选盲定向。
    res, history = run_attempt(instruction, vm, cfg, sink,
                               opening_extra=opening_extra,
                               verifier_session=verifier_session,
                               surface_baseline=surface_baseline,
                               verifier_failure_router=verifier_failure_router,
                               verifier_pass_router=verifier_pass_router,
                               ask_user=ask_user)
    # 第一次尝试。
    active_history = history
    active_cfg = cfg
    # 记录"活动"段的历史与配置（统一循环续接用）。

    def _record_resume_transport_pause(seconds: float) -> None:
        # 续跑间隙的传输暂停记账（累加到结果遥测）。
        res.infra_pause_secs += seconds
        res.infra_pauses += 1

    while (res.status == "stalled" and res.resumes < cfg.max_resumes):
        # 续跑循环：只有"停滞且还有续跑额度"才继续。
        iters_left = cfg.max_iters - res.iters
        wall_left = cfg.wall_clock_secs - res.wall_secs
        # 计算剩余预算。
        if iters_left < 10 or wall_left < 300:
            break
            # 余量太小（<10 回合或 <5 分钟）：不值得再开一段。
        n = res.resumes + 1
        # 续跑编号。
        wl = res.worklog if cfg.resume_with_worklog else ""
        # 按配置决定是否携带工作日志。
        if not wl and getattr(cfg, "resume_synthesize_worklog", False) and history:
            wl = _call_with_transport_pause(
                lambda: _synthesize_worklog(history, cfg), cfg,
                label="resume work-log synthesis",
                on_pause=_record_resume_transport_pause)  # 绝不空手交接
            # 空日志时合成一份（k3fit 修复）。
        if cfg.strategy_pivot and res.surface_delta == "":   # v18: 什么都没落袋 -> 转向
            review = _call_with_transport_pause(
                lambda: _strategy_review(history, res.worklog, cfg), cfg,
                label="resume strategy review",
                on_pause=_record_resume_transport_pause)
            extra = pivot_handoff_message(review, wl)
            tag = f"pivot{n}"
            # 策略转向：自我复盘 + 明确指令换路线。
        else:                                  # 有落袋或未知 -> v15 续跑
            extra = handoff_message(wl)
            tag = f"resume{n}"
            # 常规续跑交接。
        extra += _memory_revisit(cfg, "recovery")
        # 附加恢复事件的记忆回访提示。
        log.info("stalled with budget left -> fresh-context %s "
                 "(%d iters, %.0fs wall remain)", tag, iters_left, wall_left)
        # v21 (model-escalation): 卡住 run 的全新尝试可配置为跑在"另一个模型"
        # 上。升级只在这个罕见的卡死重启时触发；Constraint #0 依然成立，因为
        # 两段收到的是同样的密封输入。Actor 换成升级模型，而"完全不对称"时
        # 原 Actor 模型被挪进 Verifier 席位。专属的 Agentic Verifier 配置也可
        # 在此切换；其对话刻意与前一模型的私有 Verifier 历史分开。两个不同的
        # 模型必须仍然一致认为候选完成了。
        rcfg = cfg
        if cfg.escalation_model and cfg.escalation_model != cfg.model:
            rcfg = escalation_cfg(cfg)     # 纯函数 + 已单测（见 escalation_cfg）
            tag += ":esc"
            log.info("escalating stuck run -> %s (eyes=%s, verifier pinned to %s)",
                     cfg.escalation_model, rcfg.vision_model, rcfg.verifier_model)
            # 触发模型升级。
        active_verifier_session = verifier_sessions.setdefault(
            _verifier_session_identity(rcfg), VerifierSession())
        # 按新身份取/建 Verifier 会话（不同模型不同会话）。
        if verifier_session_prepare is not None:
            verifier_session_prepare(rcfg, active_verifier_session)
            # 新身份也要候选盲定向。
        sink2 = ArtifactSink(_os.path.join(sink.root, f"resume{n}"))
        # 新段落的轨迹目录。
        if tag.startswith("pivot"):
            sink2.save_summary(0, "PIVOT SELF-REVIEW:\n" + extra)
            # 转向段留档自复盘。
        r2, h2 = run_attempt(instruction, vm, rcfg, sink2, iters_budget=iters_left,
                             wall_budget=wall_left, opening_extra=extra,
                             verifier_session=active_verifier_session,
                             surface_baseline=surface_baseline,
                             verifier_failure_router=verifier_failure_router,
                             verifier_pass_router=verifier_pass_router,
                             ask_user=ask_user)
        # 全新上下文续跑：剩余预算 + 交接开场。
        active_history = h2
        active_cfg = rcfg
        r2.inspections = (res.inspections + [(res.turns, tag)]
                          + [(t + res.turns, v) for t, v in r2.inspections])
        # 合并检查事件台账（新段轮次加上偏移）。
        r2.iters += res.iters
        r2.turns += res.turns
        r2.programs_run += res.programs_run
        r2.looks += res.looks
        r2.asks += res.asks
        r2.wall_secs += res.wall_secs
        r2.infra_pause_secs += res.infra_pause_secs
        r2.infra_pauses += res.infra_pauses
        r2.resumes = n
        # 累加全部遥测（跨段连续）。
        res, history = r2, history + h2
        # 结果与历史滚动合并。
    if runtime_state is not None:
        runtime_state.clear()
        runtime_state.update(
            active_history=active_history,
            active_cfg=active_cfg,
            active_verifier_identity=_verifier_session_identity(active_cfg),
        )
        # 统一循环调用方可读取"活动段"状态（历史/配置/Verifier 身份）。
    return res, history
    # 返回最终结果与合并历史。
