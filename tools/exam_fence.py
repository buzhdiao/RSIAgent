#!/usr/bin/env python3
"""Host-only rejection checks for benchmark leakage.

Guest isolation, injected-text checks, transcript audits, and distinctive grader
constant tuples keep grading material outside practice. Practice mode rejects
task identifiers and instructions; exam mode permits the authorized instruction
while continuing to reject grader and repository signals.
"""
# 仅限宿主侧的"基准泄漏"拒绝检查（围栏/fence）。
# guest 隔离、注入文本检查、转录审计与"评分器独特常量元组"共同保证评分材料
# 不进入练习。practice 模式拒绝任务标识与指令；exam 模式允许"被授权的指令"，
# 但继续拒绝评分器与仓库信号。
import json
# json：读写常量黑名单与审计结果。
import os
# os：路径拼接与文件遍历。
import re
# re：各类泄漏信号的正则。
import sys
# sys：调整模块搜索路径。
import shlex
# shlex：把宿主路径安全引用进 guest 探针命令。
import posixpath
# posixpath：以 POSIX 语义归一化观测到的路径。

from pathlib import Path
# Path：路径对象。

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 把仓库根加入 sys.path 以导入 config。
from config.runtime_paths import resolve_root, resolve_osworld_root
# 运行时路径解析：仓库根与 OSWorld-V2 根。

V2_DIR = str(resolve_osworld_root() / "evaluation_examples/task_class")
# 评分器源码目录（task_XXX.py 所在）。
FENCE_DIR = str(resolve_root() / "results/audit/fence")
# 围栏工件目录（常量黑名单输出处）。
EXAM_SEATS = ["042", "044", "056", "011", "036", "062",           # benchmark-derived fixtures
              "001", "005", "006", "085"]                          # guards
# 受监视的考位（seat）列表（保留原行内英文注释：前 6 个为基准派生夹具，后 4 个为守卫）。

# --- signal patterns -------------------------------------------------------
# --- 信号模式（保留原分隔注释） ---
PATH_PAT = re.compile(r"OSWorld-V2|evaluation_examples|task_class", re.I)
# 路径信号：出现基准仓库路径名即命中。
# Do not mask these components inside an otherwise public application path.
# （中文）不要在"本属公开应用路径"里掩盖这些成分。
_PROTECTED_PATH = re.compile(
    r"\b(?:evaluation_examples|task_class|grader_constants\.json)\b"
    r"|\bdesktop_env/+evaluators\b|\btask_\d{3}\.py\b", re.I)
# 受保护路径成分：即使出现在应用路径里也保持可见（不做观测脱敏）。
_BENCHMARK_PATH = re.compile(
    r"(?<![\w.-])(?:[\w~./-]+/)?OSWorld-V2"
    r"(?:/[^\s\"'`<>;,()\[\]{}\\]*)?(?![\w-])", re.I)
# 匹配完整的 OSWorld-V2 路径引用（用于"观测路径复制"的定位）。
REPO_PAT = re.compile(r"github\.com/+[^ '\"]*OSWorld|xlang-ai", re.I)
# 仓库信号：GitHub 上 OSWorld 仓库链接或 xlang-ai 组织名。
TASKID_PAT = re.compile(r"\btask[_ ]?0*(%s)\b" % "|".join(EXAM_SEATS), re.I)
# 任务 id 信号：task_042 之类的受监视考位编号。

_COMMON = {"0", "1", "2", "3", "10", "100", "1.0", "0.0", "0.5", "1080",
           "1920", "60", "30", "24", "50", "0.1", "0.25", "0.75"}
# 常见数字常量集合：不构成考位独特性，提取常量时剔除。

# v1.2 (2026-08-15, EXP-2026-002 deviation #4): APP-VOCABULARY ALLOWLIST.
# A grader that parses an app's files necessarily quotes the app's format
# vocabulary — and every legitimate practice session writes that same
# vocabulary, because it is what the app's files ARE. Such tokens can never
# identify a seat. Frozen from the eps1-8 intersection sweep, run while the
# regime was PROVEN clean (L1 probe passed, all cards gated) — the only
# non-circular corpus. Reviewed per-token; every entry is app-schema/tool
# vocabulary, none is task-author content:
#   MLT/Shotcut XML schema + services: frame_rate_num/den, lift_gamma_gain,
#     warp_speed;  Zotero API/DB fields + item types: entry_count, item_ids,
#     item_type, parent_id, journal article, Collection ;  shell idiom /
#     generic tool names: sleep 2, list_directory;  bare decimals already
#     colliding in clean practice: 1.35, 1034, 3.00, 7.00.
# （中文）v1.2 应用词汇白名单：解析应用文件的评分器必然引用该应用的格式词汇，
# 任何合法练习也会写出同样的词汇（那正是应用文件的构成），这类 token 永远无法
# 唯一标识考位。清单冻结自"ep1-8 交集扫描"（在已证明干净、L1 探针通过、全部
# 卡片受门禁的时期运行）——唯一非循环语料。逐 token 人工复核：每条都是
# 应用 schema/工具词汇，无一是任务作者内容。
_APP_VOCAB = {"frame_rate_num", "frame_rate_den", "lift_gamma_gain",
              "warp_speed", "entry_count", "item_ids", "item_type",
              "parent_id", "journal article", "Collection ", "sleep 2",
              "list_directory", "1.35", "1034", "3.00", "7.00",
              # MLT filter/property schema uses the same vocabulary
              # as lift_gamma_gain/warp_speed above; ordinary editing
              # writes these in any legitimate fade/color/mask/speed work.
              # Task-author content (deliverable filenames, instruction
              # fragments) deliberately NOT allowlisted.
              # （中文）MLT 滤镜/属性 schema 与上面 lift_gamma_gain/warp_speed 同源；
              # 正常淡出/调色/遮罩/变速编辑都会写出这些值。任务作者内容
              # （交付文件名、指令片段）被刻意排除在白名单之外。
              "fade_out", "gain_r", "gain_g", "gain_b", "mask_start",
              "warp_resource", "0.96",
              # Ordinary document-record columns, independent of task content.
              # （中文）普通文档记录列名，与任务内容无关。
              "case_name", "pdf_url"}
# 应用词汇白名单（详见上方中英文说明）。
# Clock fragments ("02.000" from HH:MM:SS.mmm) are format artifacts, never
# distinctive — excluded as a CLASS, not just the two observed instances.
# （中文）时钟片段（HH:MM:SS.mmm 中的"02.000"）是格式产物、从不具备独特性——
# 按"类"排除，而不只是排除观察到的两个实例。
_CLOCK_FRAG = re.compile(r"^\d{2}\.\d{3}$")
# 时钟片段模式。

# Numeric harness-contract lines ("BUDGET: 40",
# "REP: 2") are bare integers by construction and can collide with a seat's
# numeric constants; such lines are stripped from the copy of the text used
# for constant-tuple counting ONLY — the path/URL/task-id/8-gram layers
# always audit the full text.
# （中文）数值型 harness 契约行（"BUDGET: 40"、"REP: 2"）按构造就是裸整数，
# 可能与考位的数字常量冲突；这类行只从"常量元组计数用的文本副本"中剔除——
# 路径/URL/任务 id/8-gram 各层始终审计全文。
_BUDGET_LINE = re.compile(r"^(?:BUDGET|REP):\s*\d+\s*$", re.M)
# 数值契约行模式（多行模式）。
_NUMERIC_LITERAL = re.compile(r"\d+(?:\.\d+)?\Z")
# 数字字面量判定（用于选择词边界策略）。


def _contains_exact_literal(text: str, literal: str) -> bool:
    """Return whether ``literal`` occurs as a complete token in ``text``.

    The denylist contains both numeric values and identifier-like strings.
    Raw substring matching makes an SVG coordinate such as ``131.45508``
    look like the constant ``1.45``, and makes ``tile_size_bytes_minus1``
    look like ``size_bytes``.  Keep the literal match case-sensitive, as it
    has always been, but require token boundaries at word-like edges.  A dot
    is additionally part of a numeric token so a decimal cannot match inside
    a longer decimal or version number.
    """
    # 判断 literal 是否以"完整 token"形式出现在 text 中。
    # 黑名单既有数值也有标识符样式字符串：裸子串匹配会让 SVG 坐标 131.45508
    # 看起来像常量 1.45、让 tile_size_bytes_minus1 看起来像 size_bytes。
    # 保持字面匹配一直以来的大小写敏感，但要求词状边界；对小数而言点号也算
    # 数字 token 的一部分，因此小数不能匹配进更长的小数或版本号里。
    if not literal or literal not in text:
        # 快速路径：子串都不存在则必无完整 token。
        return False
        # 返回未命中。
    numeric = bool(_NUMERIC_LITERAL.fullmatch(literal))
    # 判断是否纯数字字面量。
    boundary = r"[\w.]" if numeric else r"\w"
    # 数字字面量用"词字符或点"作边界；其他用词字符。
    left = rf"(?<!{boundary})" if re.match(r"\w", literal[0]) else ""
    # 首字符是词字符时要求左边界。
    right = rf"(?!{boundary})" if re.match(r"\w", literal[-1]) else ""
    # 尾字符是词字符时要求右边界。
    return re.search(left + re.escape(literal) + right, text) is not None
    # 带边界地搜索转义后的字面量。


def _observed_path_copy(text: str) -> str:
    """Classify quoted README paths only in a recorded program observation.

    Executed code is audited separately. Keep URLs, unknown repository files,
    protected paths and all non-path text visible; this is not an output bypass.
    """
    # 只对"程序观测记录"中的 README 路径做分类脱敏。
    # 被执行的代码另行审计。URL、未知仓库文件、受保护路径与一切非路径文本保持
    # 原样可见；这不是输出旁路。
    def replace(match):
        # 单个路径匹配的替换决策。
        value = match.group(0)
        # 原始匹配文本。
        prefix, suffix = re.split(r"OSWorld-V2", value, maxsplit=1, flags=re.I)
        # 以 OSWorld-V2 为界拆出前缀与后缀。
        if (prefix.startswith("//")
                or text[max(0, match.start() - 1):match.start()] == ":"
                or _PROTECTED_PATH.search(value)):
            # URL（//开头）、前一字符是冒号（协议/伪协议）或包含受保护成分：保留原样。
            return value
            # 不脱敏。
        relative = posixpath.normpath(suffix.lstrip("/") or ".")
        # 归一化相对路径。
        if (relative == "." or relative == "self_hosted_websites"
                or relative.startswith("self_hosted_websites/")):
            # 指向自托管网站的观测路径：属于应用本身的公开路径。
            return "[observed application path]"
            # 以占位符替换（脱敏但不隐藏其余内容）。
        return value
        # 其余路径保留原样。
    return _BENCHMARK_PATH.sub(replace, text)
    # 对全部基准路径匹配应用替换规则。


def _text_fields(value):
    """Decode JSON fields before matching: a literal newline is not '\\n'."""
    # 先解码 JSON 字段再做匹配：真实换行符不等于字符串"\\n"。
    if isinstance(value, dict):
        # 字典：产出键并递归产出值。
        for key, item in value.items():
            # 遍历键值。
            yield str(key)
            # 键也参与匹配。
            yield from _text_fields(item)
            # 递归值。
    elif isinstance(value, list):
        # 列表：递归产出元素。
        for item in value:
            # 遍历元素。
            yield from _text_fields(item)
            # 递归。
    else:
        # 标量：字符串原样，其余序列化为 JSON。
        yield value if isinstance(value, str) else json.dumps(value)
        # 产出文本字段。


def _transcript_audit_copy(text: str) -> tuple[str, str]:
    """Return decoded text plus its path-check copy for recorded observations."""
    # 返回（解码后的全文，用于路径检查的观测脱敏副本）。
    try:
        doc = json.loads(text)
        # 尝试按 JSON 解析转录。
    except (ValueError, TypeError):
        # 不是 JSON：两个副本都为原文。
        return text, text
        # 原样返回。
    if (not isinstance(doc, dict) or not isinstance(doc.get("system"), str)
            or not isinstance(doc.get("messages"), list)):
        # 结构不符合转录格式：原样返回。
        return text, text
        # 原样返回。
    from core.actor import Program, parse_turn
    # 延迟导入：Program 动作类型与回合解析器。

    original = "\n".join(_text_fields(doc))
    # 解码后的全文（所有文本字段拼接）。
    previous = None
    # 上一条消息（用于定位"PROGRAM OUTPUT"观测对）。
    for message in doc["messages"]:
        # 遍历消息。
        if not isinstance(message, dict):
            # 非字典消息：重置游标。
            previous = None
            # 置空。
            continue
            # 下一条。
        content = message.get("content")
        # 取消息内容。
        if (message.get("role") == "user" and isinstance(content, str)
                and content.startswith("PROGRAM OUTPUT (")
                and isinstance(previous, dict)
                and previous.get("role") == "assistant"
                and isinstance(previous.get("content"), str)):
            # 形如"user 收到 PROGRAM OUTPUT 且前一条是 assistant"的观测记录。
            try:
                action = parse_turn(previous["content"])
                # 解析 assistant 是否发布了 Program。
            except (ValueError, TypeError):
                # 解析失败按无 Program 处理。
                action = None
                # 置空。
            if isinstance(action, Program):
                # 确认是程序观测：对输出副本做路径脱敏。
                message["content"] = _observed_path_copy(content)
                # 仅替换该消息的副本内容（original 保留原文）。
        previous = message
        # 游标推进。
    return original, "\n".join(_text_fields(doc))
    # 返回原文全文与（可能脱敏的）检查副本。


def _trace_audit_copy(path: Path, text: str) -> str:
    """Apply the same observation rule to an archived Program's raw stdout."""
    # 对已归档 Program 的原始 stdout 施用同样的观测规则。
    if not any((path.parent / name).is_file()
               for name in ("program.py", "program.sh")):
        # 目录中没有程序源文件：不是程序输出，保持原样。
        return text
        # 原文返回。
    try:
        metadata = json.loads((path.parent / "trace_meta.json").read_text())
        # 读取轨迹元数据。
    except (OSError, ValueError):
        # 读取失败：保持原样。
        return text
        # 原文返回。
    if (isinstance(metadata, dict)
            and type(metadata.get("exit_code")) is int
            and type(metadata.get("secs")) in (int, float)
            and isinstance(metadata.get("timed_out"), bool)):
        # 元数据齐备：确属一次已记录的程序观测。
        return _observed_path_copy(text)
        # 返回路径脱敏副本。
    return text
    # 否则原文返回。


def extract_constants(seat: str) -> list:
    """Distinctive literals from one grader: floats with >=2 decimals, ints
    >=4 digits, and quoted strings 6..60 chars that look like content (not
    code). Host-side only — the output file must NEVER be pushed to a guest
    or referenced in any prompt."""
    # 从单个评分器提取独特字面量：小数点后至少 2 位的浮点、至少 4 位的整数、
    # 以及 6-60 字符且"像内容而非代码"的带引号字符串。仅限宿主侧——输出文件
    # 绝不能被推进 guest 或在任何提示中被引用。
    src = open(os.path.join(V2_DIR, f"task_{seat}.py"),
               encoding="utf-8", errors="ignore").read()
    # 读取该考位的评分器源码。
    out = set()
    # 常量集合。
    for m in re.findall(r"(?<![\w.])(\d+\.\d{2,}|\d{4,})(?![\w.])", src):
        # 提取"带边界"的长数字字面量（小数点后>=2 位或整数>=4 位）。
        if m not in _COMMON:
            # 剔除常见数字。
            out.add(m)
            # 加入集合。
    for m in re.findall(r"['\"]([A-Za-z][A-Za-z0-9 _\-\.]{5,60})['\"]", src):
        # 提取 6-60 字符、以字母开头的带引号字符串。
        low = m.lower()
        # 小写副本用于关键词排除。
        if (" " in m or "_" in m) and not any(
                k in low for k in ("http", "self.", "utf", "ignore", "task_",
                                   ".py", "def ", "return", "error")):
            # 保留"像自然语言内容"的串（含空格或下划线），排除代码样式串。
            out.add(m)
            # 加入集合。
    return sorted(out)
    # 返回排序后的常量列表。


def build_denylist(path: str = "") -> dict:
    """Write the per-seat constant lists (L4). Idempotent; run at build.

    v1.1 (2026-08-14, EXP-2026-002 recorded deviation): drop benchmark-common
    constants — document frequency measured across ALL task graders in
    V2_DIR, not just our 10 seats: a token appearing in >=3 different tasks'
    graders ('codec_type', 'mkdir -p ', '1337') is tool vocabulary, not a
    seat identifier; ep002's legitimate card was false-positived on exactly
    such tokens."""
    # 生成各考位的常量黑名单（L4 层）。幂等；构建期运行。
    # v1.1 记录在案的偏差：剔除"基准通用"常量——文档频率在 V2_DIR 全部评分器
    # （而非仅 10 个考位）上统计：出现在 >=3 个不同任务评分器里的 token
    # （'codec_type'、'mkdir -p '、'1337'）是工具词汇，不是考位标识；
    # ep002 的合法卡片正是被这类 token 误伤的。
    path = path or os.path.join(FENCE_DIR, "grader_constants.json")
    # 输出路径缺省为围栏目录下的 grader_constants.json。
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 确保输出目录存在。
    raw = {s: extract_constants(s) for s in EXAM_SEATS}
    # 先提取各考位的原始常量。
    df = {}
    # 文档频率表：token -> 出现的任务评分器数。
    for fn in os.listdir(V2_DIR):
        # 遍历评分器目录。
        m = re.match(r"task_(\d+)\.py$", fn)
        # 匹配评分器文件名。
        if not m:
            # 非评分器文件跳过。
            continue
            # 下一个。
        for v in set(extract_constants(m.group(1))):
            # 对每个评分器统计其常量的出现（按任务去重计数）。
            df[v] = df.get(v, 0) + 1
            # 频率累加。
    data = {s: [v for v in vals if df.get(v, 0) < 3
                and v not in _APP_VOCAB and not _CLOCK_FRAG.match(v)]
            for s, vals in raw.items()}
    # 过滤：文档频率 <3、不在应用词汇白名单、不是时钟片段。
    json.dump(data, open(path, "w"), indent=1)
    # 写出黑名单 JSON。
    return data
    # 返回黑名单。


def _load_constants() -> dict:
    # 加载常量黑名单；不存在时现场构建。
    p = os.path.join(FENCE_DIR, "grader_constants.json")
    # 黑名单路径。
    if os.path.exists(p):
        # 已存在则直接加载。
        return json.load(open(p))
        # 返回黑名单。
    return build_denylist()
    # 缺失时构建并返回。


def instruction_text(seat: str) -> str:
    """The task's instruction string (handles triple- and single-quoted)."""
    # 提取任务的 instruction 字符串（兼容三引号与单引号写法）。
    src = open(os.path.join(V2_DIR, f"task_{seat}.py"),
               encoding="utf-8", errors="ignore").read()
    # 读取评分器源码。
    m = re.search(r'instruction\s*=\s*f?(?:"""(.+?)"""|"(.+?)")', src, re.S)
    # 匹配 instruction 赋值（f-string 前缀也可）。
    return (m.group(1) or m.group(2)) if m else ""
    # 返回指令文本；未匹配返回空串。


def _instruction_8grams() -> set:
    """8-gram shingles of the exam-seat instructions (practice-mode L2/L3)."""
    # 生成全部考位指令的 8-gram shingle 集合（practice 模式的 L2/L3 层）。
    grams = set()
    # shingle 集合。
    for s in EXAM_SEATS[:6]:
        # 只对 6 个基准派生夹具考位取指令（守卫考位不产生泄漏源）。
        words = re.findall(r"[a-z0-9']+", instruction_text(s).lower())
        # 分词（小写化）。
        for i in range(len(words) - 7):
            # 滑窗生成 8-gram。
            grams.add(" ".join(words[i:i + 8]))
            # 加入集合。
    return grams
    # 返回 shingle 集合。


_GRAMS_CACHE = None
# shingle 集合的进程内缓存。


def _authorized_near_shingle(gram: str, authorized_words: list[str]) -> bool:
    """Accept a target shingle after at most four in-order word omissions.

    Agent-authored text naturally drops courtesy words and determiners from the
    operator-authorized instruction, including a repeated role noun phrase.
    That is still the authorized input, not a grader leak. Keep the exception
    deliberately narrow: all eight words must occur in order inside a
    twelve-word window of the exact authorized instruction. Reordering,
    substitution, and wider paraphrase remain fenced.
    """
    # 允许"至多省略四个词且保序"的目标 shingle 视为授权内容。
    # Agent 写的文本自然会省略授权指令中的客套词/限定词（包括重复的角色名词短语），
    # 那仍是授权输入而非评分器泄漏。例外刻意收窄：8 个词必须按序出现在
    # 精确授权指令的 12 词窗口内。换序、替换与更宽的改写仍被围栏拒绝。
    wanted = gram.split()
    # 拆出 shingle 的 8 个词。
    if len(wanted) != 8:
        # 非 8-gram 一律不接受。
        return False
        # 拒绝。
    max_span = len(wanted) + 4
    # 最大窗口：8 词 + 至多 4 个省略。
    for start, word in enumerate(authorized_words):
        # 在授权指令词序列中寻找与 shingle 首词一致的位置。
        if word != wanted[0]:
            # 首词不匹配则跳过。
            continue
            # 下一个位置。
        end = min(len(authorized_words), start + max_span)
        # 本候选窗口的右界。
        cursor = start + 1
        # 匹配游标。
        for expected in wanted[1:]:
            # 依次匹配其余 7 个词。
            while cursor < end and authorized_words[cursor] != expected:
                # 跳过不匹配的词（即允许的"省略"）。
                cursor += 1
                # 游标前进。
            if cursor >= end:
                # 窗口耗尽仍未匹配：本位置失败。
                break
                # 跳出内层。
            cursor += 1
            # 匹配一个词，游标越过它。
        else:
            # 全部 8 词按序匹配成功。
            return True
            # 接受。
    return False
    # 所有起点都失败：不接受。


def audit_text(text: str, mode: str = "practice",
               authorized_instruction: str = "", *,
               path_text: str | None = None) -> list:
    """Return violation records for one text (empty means clean).

    ``authorized_instruction`` is the narrow target-aware exception: shingles
    and literal constants already present in that operator-authorized natural-
    language instruction are training input, not leakage.  Structural signals
    (benchmark paths/repositories/task ids) and every grader-only value remain
    forbidden.  The empty default preserves the blind fence exactly.

    ``path_text`` is an optional observation-aware copy for PATH_PAT only.
    All other signals are checked against the unchanged text, including
    task identifiers or constants inside an otherwise ordinary application path.
    """
    # 对一段文本返回违规记录（空即干净）。
    # ``authorized_instruction`` 是收窄的"目标感知"例外：已存在于操作者授权的
    # 自然语言指令中的 shingle 与字面常量属于训练输入，不是泄漏。结构性信号
    # （基准路径/仓库/任务 id）与一切"评分器独有值"仍被禁止。空缺省保持盲围栏不变。
    # ``path_text`` 仅用于 PATH_PAT 的"观测感知"副本；其余信号一律审计原文，
    # 包括普通应用路径中的任务 id 或常量。
    global _GRAMS_CACHE
    # 使用模块级缓存。
    hits = []
    # 命中列表。
    path_match = PATH_PAT.search(text if path_text is None else path_text)
    # 在（可能的观测副本上）查找路径信号。
    if path_match:
        # 命中路径信号。
        hits.append({"kind": "exam-path", "match": path_match.group(0)})
        # 记录 exam-path。
    if REPO_PAT.search(text):
        # 原文上查找仓库信号。
        hits.append({"kind": "repo-url", "match": REPO_PAT.search(text).group(0)})
        # 记录 repo-url。
    if mode == "practice":
        # practice 模式额外的任务 id 与指令 shingle 检查。
        m = TASKID_PAT.search(text)
        # 查找任务 id。
        if m:
            # 命中任务 id。
            hits.append({"kind": "task-id", "match": m.group(0)})
            # 记录 task-id。
        if _GRAMS_CACHE is None:
            # 缓存未建立则构建。
            _GRAMS_CACHE = _instruction_8grams()
            # 生成并缓存 shingle 集合。
        allowed_grams = set()
        # 授权指令自身的 shingle（豁免集）。
        awords = []
        # 授权指令的词序列。
        if authorized_instruction:
            # 有授权指令才计算豁免集。
            awords = re.findall(
                r"[a-z0-9']+", authorized_instruction.lower())
            # 分词。
            allowed_grams = {" ".join(awords[i:i + 8])
                             for i in range(len(awords) - 7)}
            # 生成授权指令的 8-gram。
        words = re.findall(r"[a-z0-9']+", text.lower())
        # 对待审文本分词。
        for i in range(len(words) - 7):
            # 滑窗生成 8-gram。
            g = " ".join(words[i:i + 8])
            # 当前 shingle。
            if (g in _GRAMS_CACHE and g not in allowed_grams
                    and not _authorized_near_shingle(g, awords)):
                # 命中考位指令 shingle，且不在授权豁免集、也不是保序省略的授权近似。
                hits.append({"kind": "instruction-8gram", "match": g})
                # 记录 instruction-8gram。
                break
                # 一条即足够（避免重复记录）。
    consts = _load_constants()
    # 加载常量黑名单。
    tuple_text = _BUDGET_LINE.sub("", text)    # constant-tuple copy only
    # 生成"常量元组计数专用"文本副本：剔除数值契约行（保留原行内英文注释）。
    for seat, vals in consts.items():
        # 逐考位统计命中。
        # Runs may retain a denylist built before a schema exclusion was added.
        # （中文）运行期可能还持有"schema 排除加入之前"构建的黑名单，这里再次过滤。
        found = [v for v in vals if v not in _APP_VOCAB
                 and _contains_exact_literal(tuple_text, v)]
        # 找出以完整 token 形式出现、且不在应用词汇白名单中的常量。
        if authorized_instruction:
            # 有授权指令时豁免其中已有的常量。
            found = [v for v in found if not _contains_exact_literal(
                authorized_instruction, v)]
            # 剔除出现在授权指令中的常量。
        strings = [v for v in found
                   if len(v) >= 6 and re.search(r"[A-Za-z]", v)]
        # 含字母的长常量（字符串类）。
        nums = [v for v in found if v not in strings]
        # 其余为数字类。
        # tuple rule v1.1 (red-team #7 + ep002 false positive): a pair of
        # generic decimals ("0.20"+"0.35") is not a leak. A hit now needs a
        # letter-bearing distinctive constant (>=2 matches total) or >=3
        # co-occurring numeric constants of one seat.
        # （中文）元组规则 v1.1（红队 #7 + ep002 误报）：一对普通小数
        # （"0.20"+"0.35"）不构成泄漏。现在命中需要：存在含字母的独特常量
        # （且总命中数 >=2），或同一考位 >=3 个数字常量共存。
        if (strings and len(found) >= 2) or len(nums) >= 3:
            # 满足更严格的元组规则才判命中。
            hits.append({"kind": "constant-tuple", "seat": seat,
                         "match": (strings + nums)[:4]})
            # 记录 constant-tuple（最多列出 4 个命中值）。
    return hits
    # 返回全部命中。


def audit_transcripts(root: str, mode: str = "practice",
                      authorized_instruction: str = "") -> list:
    """Walk transcripts under root; return per-file violations (L3)."""
    # 遍历 root 下的转录工件；返回逐文件的违规列表（L3 层）。
    out = []
    # 结果列表。
    for dirpath, _, files in os.walk(root):
        # 递归遍历目录。
        for fn in files:
            # 逐文件。
            if (not fn.endswith((".txt", ".json", ".jsonl", ".md"))
                    and fn not in {"program.py", "program.sh"}):
                # 只审计文本工件与程序源文件。
                continue
                # 跳过其他文件。
            p = os.path.join(dirpath, fn)
            # 文件完整路径。
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
                # 容错读取全文。
            except OSError:
                # 读取失败则跳过。
                continue
                # 下一个文件。
            path_text = None
            # 观测感知副本（默认无）。
            if fn == "transcript.json":
                # 转录文件：取解码全文与观测脱敏副本。
                txt, path_text = _transcript_audit_copy(txt)
                # 双副本准备。
            elif fn == "trace.txt":
                # 轨迹文件：取观测脱敏副本（原文仍为审计文本）。
                path_text = _trace_audit_copy(Path(p), txt)
                # 仅路径层用副本。
            hits = audit_text(
                txt, mode=mode,
                authorized_instruction=authorized_instruction,
                path_text=path_text)
            # 对该文件执行全部围栏检查。
            if hits:
                # 有命中则记录。
                out.append({"file": p, "hits": hits})
                # 记录文件与命中。
    return out
    # 返回全部违规。


GUEST_PROBE_CMDS = [
    # L1 live verification, run INSIDE the guest via vm.run_command at P0.
    # Every probe must come back DENIED/absent or the fence FAILS.
    # （中文）L1 实地验证：在 P0 时经 vm.run_command 在 guest 内运行。
    # 每个探针都必须返回 DENIED/不存在，否则围栏判定失败。
    # （以下保留原始探测命令。）
    ("host-fs", "ls " + shlex.quote(str(resolve_osworld_root())) + " 2>&1 | head -2"),
    # 探针 1：guest 内列宿主 OSWorld 根目录（应被拒绝/不存在）。
    ("host-fs-2", "cat " + shlex.quote(str(Path(FENCE_DIR) / "grader_constants.json"))
                  + " 2>&1 | head -1"),
    # 探针 2：guest 内读宿主评分器常量黑名单（应被拒绝/不存在）。
    ("qcow-mount", "ls /System.qcow2 2>&1 | head -1"),
    # 探针 3：guest 内查宿主磁盘镜像挂载点（应不存在）。
    ("host-http", "for p in 8000 8080 5003 3000 80; do "
                  "curl -sm2 http://10.0.2.2:$p/OSWorld-V2/ -o /dev/null "
                  "-w \"$p:%{http_code} \"; done; echo"),
    # 探针 4：guest 内依次探测宿主网关常见端口上的基准 HTTP 服务（不应可达）。
]


def judge_guest_probe(results: dict) -> list:
    """results = {name: output}. Return failures (empty = L1 PASS)."""
    # 判定 guest 探针结果：results = {名称: 输出}；返回失败列表（空即 L1 通过）。
    bad = []
    # 失败列表。
    for name, out in results.items():
        # 逐探针判定。
        low = (out or "").lower()
        # 小写副本便于关键词判断。
        if name.startswith("host-fs") and not any(
                k in low for k in ("no such file", "not found", "denied",
                                   "cannot access")):
            # 文件系统探针：必须表现为"不存在/被拒绝"。
            bad.append({"probe": name, "out": out[:120]})
            # 记录失败与截断输出。
        if name == "qcow-mount" and "no such file" not in low:
            # 镜像探针：必须"不存在"。
            bad.append({"probe": name, "out": out[:120]})
            # 记录失败。
        if name == "host-http" and re.search(r":(200|301|302)", out or ""):
            # HTTP 探针：任何端口出现 200/301/302 即宿主服务可达，失败。
            bad.append({"probe": name, "out": out[:120]})
            # 记录失败。
    return bad
    # 返回失败列表。


if __name__ == "__main__":
    # 命令行入口。
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        # 子命令 build：构建常量黑名单。
        d = build_denylist()
        # 构建并取结果。
        print(f"constants: {sum(len(v) for v in d.values())} across "
              f"{len(d)} seats -> {FENCE_DIR}/grader_constants.json")
        # 输出构建摘要。
    elif len(sys.argv) > 2 and sys.argv[1] == "audit":
        # 子命令 audit：审计指定目录的转录。
        mode = sys.argv[3] if len(sys.argv) > 3 else "practice"
        # 可选第三参指定模式（缺省 practice）。
        v = audit_transcripts(sys.argv[2], mode=mode)
        # 执行审计。
        print(json.dumps(v, indent=1) if v else "CLEAN")
        # 有违规打印明细，否则打印 CLEAN。
        sys.exit(1 if v else 0)
        # 有违规以 1 退出（供 CI 拦截）。
    else:
        # 无子命令：打印模块说明。
        print(__doc__)
        # 输出 docstring。
