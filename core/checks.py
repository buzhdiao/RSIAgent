"""Verification checks — model-authored, machine-executed, instruction-derived.

The model declares done with a list of {desc, probe} checks; the harness's only job is
(a) a falsifiability gate that rejects lazy or mutating probes, and (b) running the
accepted probes read-only in the VM. PASS/FAIL semantics are conservative: a probe
passes iff its output contains PASS and no FAIL.
"""
import re
from dataclasses import dataclass


# 单条检查：自然语言描述 + 可执行探针命令
@dataclass
class Check:
    desc: str
    probe: str


# 单条检查的执行结果：描述、探针、是否通过、截断后的输出
@dataclass
class CheckResult:
    desc: str
    probe: str
    passed: bool
    output: str


# Persistent-state mutations that are destructive/installing regardless of target.
# v34: matched as WHOLE COMMAND WORDS (start-of-string or after a shell separator),
# not substrings — "confirm file" used to trip 'rm ', "grep -w add" tripped 'dd '.
# （中文）硬性改动命令黑名单：删除/移动/改权限/下载/安装等破坏性或安装性命令，
# 与任务无关一律禁止；v34 起按完整命令词匹配（行首或 shell 分隔符之后），
# 避免 “confirm file” 误触 'rm '、“grep -w add” 误触 'dd ' 这类子串误报。
_HARD_MUTATING_RE = re.compile(
    r"(?:^|[;&|`$(\s])"
    r"(rm|mv|shred|dd|chmod|chown|wget|rmdir|unlink)\s"
    r"|sed\s+-i|pip3?\s+install|apt(?:-get)?\s|curl\s+(?:-\S*\s+)*-[oO]\b")
# File-CREATING commands whose target is checked: writing under /tmp scratch is
# read-only w.r.t. graded state (deliverables never live in /tmp), so a check may
# stage a render/copy there and inspect it (v16: enables render-and-compare checks).
# （中文）会建文件的命令，其写入目标要被检查：写进 /tmp 临时区对被评状态而言仍是
# 只读（交付物绝不放 /tmp），因此允许先在 /tmp 渲染/复制再检查（v16 起支持
# “渲染-对比”型检查）。
_TARGETED_MUTATING = ("cp", "tee", "mkdir", "touch", "truncate")
# /tmp 临时区前缀
_SCRATCH = "/tmp/"
# redirections that are harmless in a read-only probe. v34: added the plain
# stdout silencers — `cmd >/dev/null && echo PASS` is the standard read-only idiom
# and was the single largest false-positive class in the gate's history (174 of 337
# archived rejections; agents were being taught to write weaker checks).
# （中文）对只读探针无害的重定向白名单：v34 补上纯 stdout 静音写法 ——
# `cmd >/dev/null && echo PASS` 是标准只读惯用法，曾是该闸门史上最大误报来源
# （337 条存档拒绝中占 174 条，等于在教 Agent 写更弱的检查）。
_HARMLESS_REDIR = ("2>&1", "2>/dev/null", "&>/dev/null", "1>&2", "2> /dev/null",
                   ">/dev/null", "> /dev/null", "1>/dev/null", "1> /dev/null",
                   ">> /dev/null", ">>/dev/null")

# 引号包裹片段（含解释器体的单双引号串）
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# shell 输出重定向目标
_REDIR = re.compile(r">>?\s*([^\s|;&()]+)")
# 建文件类命令：命令词 + 可选选项 + 目标路径
_MKFILE = re.compile(r"\b(cp|tee|mkdir|touch|truncate)\b\s+((?:-\S+\s+)*)([^\s|;&()]+)")

# Interpreter one-liner bodies — python -c "...", perl -e '...', etc. The v16 quote-
# masking deliberately hides these from the shell-token scan (so a python '>' inside a
# string is not misread as a redirect), but that same masking blinds the gate to real
# mutations INSIDE the body: a check `python3 -c "shutil.rmtree('X'); print('PASS')"`
# would pass the read-only gate and destroy graded state. These run against the live
# scored machine, so the body is scanned separately for destructive calls / non-scratch
# writes (a hard prerequisite once the inspector authors verification programs).
# （中文）解释器一行体（python -c "..."、perl -e '...' 等）：v16 的引号遮蔽让它们
# 躲过了 shell 词法扫描（字符串里的 '>' 不再被误读为重定向），但也让闸门看不见
# 体内的真实改动——这类探针直接跑在计分机上，必须单独扫描体内的删除调用与
# 非临时区写入（验证者自己编写验证程序后的硬性前提）。
_INTERP_BODY = re.compile(
    r"(?:python3?|perl|ruby|node|php|bash|sh)\s+-[ce]\s+(['\"])(.*?)\1", re.DOTALL)
_BODY_DELETE = re.compile(                          # deletes/moves/renames: never
    r"shutil\.(?:rmtree|move)\b"                    # read-only, flag regardless of path
    r"|os\.(?:remove|unlink|rmdir|removedirs|rename|renames|replace|truncate)\b"
    r"|\.unlink\s*\(|\brmtree\s*\(|\bunlink\b"
    r"|\.write_(?:text|bytes)\s*\(")
_BODY_WRITE_OPEN = re.compile(                       # open(path, 'w'|'a'|'x'|...)
    r"open\s*\(\s*[^,)]+,\s*(['\"])[^'\"]*[wax][^'\"]*\1")


def _nonscratch_mutations(probe: str):
    """Shell-level mutations of NON-scratch (non-/tmp) state in ``probe``. Empty list
    means the probe is read-only (or writes only to /tmp scratch).

    v16 fix: quoted spans are MASKED before scanning, so interpreter-body operators —
    a python `print(a > b)` comparison, a substring like 'cp' inside a string — are no
    longer misread as shell mutations. That false positive rejected 7 of 8 legitimate
    zipfile.read() checks on one run; the real read-only guarantee (no unquoted shell
    redirect / destructive command touching graded state) is preserved exactly."""
    # 从探针原文开始扫描
    sk = probe
    for h in _HARMLESS_REDIR:
        # 先剔除无害重定向写法
        sk = sk.replace(h, " ")
    sk = _QUOTED.sub("  ", sk)                       # mask quoted interpreter bodies
    # 收集违规项
    bad = []
    for m in _HARD_MUTATING_RE.finditer(sk):
        # 命中硬性改动黑名单：记录命令词
        bad.append((m.group(1) or m.group(0)).strip())
    for m in _REDIR.finditer(sk):                   # shell output redirection
        # shell 重定向：目标不在 /tmp 临时区即违规
        if not m.group(1).startswith(_SCRATCH):
            bad.append("redirect to " + m.group(1)[:40])
    for m in _MKFILE.finditer(sk):                  # file-creating commands
        # v34: validate the WRITE TARGET. For copy-class commands that is the LAST
        # path argument (cp SRC DST) — the old code checked the first argument (the
        # SOURCE), which rejected the documented stage-to-/tmp pattern
        # (`cp ~/x.pdf /tmp/c.pdf`) while PASSING a genuine overwrite of graded
        # state (`cp /tmp/planted ~/deliverable`). Inverted protection.
        # （中文）v34 校验“写入目标”：cp 类命令的目标是最后一个路径参数（cp SRC DST）；
        # 旧代码检查第一个参数（源），错把 `cp ~/x.pdf /tmp/c.pdf` 拒之门外，
        # 却放行 `cp /tmp/planted ~/deliverable` 覆盖被评状态——保护方向反了。
        cmd = m.group(1)
        if cmd == "cp":
            # 截取该命令段（到 | ; && 为止）以找最后一个非选项参数
            tail = sk[m.start():].split("|")[0].split(";")[0].split("&&")[0]
            args = [a for a in tail.split()[1:] if not a.startswith("-")]
            # 目标 = 最后一个参数（参数不足两个时退回正则捕获的路径）
            target = args[-1] if len(args) >= 2 else m.group(3)
        else:
            # 其余命令（tee/mkdir/touch/truncate）目标就是捕获的路径
            target = m.group(3)
        if not target.startswith(_SCRATCH):
            # 写入非临时区即违规
            bad.append(cmd + " " + target[:30])
    for _q, body in _INTERP_BODY.findall(probe):    # scan UNMASKED interpreter bodies
        # 解释器体单独扫描（用未遮蔽原文）
        if _BODY_DELETE.search(body):
            # 体内出现删除/移动/重命名调用：一律违规
            bad.append("interpreter-body delete/move/rename")
        elif _BODY_WRITE_OPEN.search(body) and _SCRATCH not in body:
            # 体内以写模式 open 且不涉及 /tmp：违规
            bad.append("interpreter-body write to non-scratch")
    return bad


# 存在性探测：test -e/-f、[ -f ]、ls 等
_EXIST = re.compile(r"\btest\s+-[efdrsL]|\[\s*-[efdrsL]\b|\bls\b")
# 元数据探测：ffprobe/stat/file/wc/du/identify/zipinfo/pdfinfo/head/tail 等
_META = re.compile(r"\bffprobe\b|\bstat\b|\bfile\b|\bwc\b|\bdu\b|identify\b|"
                   r"zipinfo|unzip\s+-l|pdfinfo|\bhead\b|\btail\b")
# 内容级探测：diff/md5/哈希/psnr/ssim/grep/python/awk/jq/pdftotext 等
_CONTENT = re.compile(r"diff|cmp|md5|sha[0-9]|psnr|ssim|compare|==|!=|<=|>=|"
                      r"grep|python|awk|jq|pdftotext|json")


def _category(probe: str) -> str:
    """Value tier of a check, for capacity eviction (v16). content > metadata >
    existence. Conservative: anything not clearly existence/metadata is content, so a
    borderline check is KEPT under capacity pressure, never evicted for a proxy."""
    # 小写化后按价值阶梯分类
    p = probe.lower()
    if _CONTENT.search(p):
        # 命中内容级：最高价值
        return "content"
    if _META.search(p):
        # 命中元数据级
        return "metadata"
    if _EXIST.search(p):
        # 命中存在性级：最低价值
        return "existence"
    # 归类不明的保守视为 content（容量压力下优先保留）
    return "content"


# 价值优先级：content 最高（0），existence 最低（2）
_PRIORITY = {"content": 0, "metadata": 1, "existence": 2}


def validate(raw_checks, max_checks: int = 16, instruction: str = "",
             witness_gates: bool = True):
    """Split model-proposed checks into (accepted, rejections, dropped).

    ``rejections`` are SOUNDNESS failures (malformed / mutating / vacuous) — they veto
    a done declaration. ``dropped`` is capacity overflow beyond ``max_checks`` — noted,
    but NOT a veto (a real seed earned done with 8/8 passing and was refused because
    two overflow drops were counted as rejections; that was a harness bug). v16: when
    over capacity, evict the LEAST valuable checks first (existence/metadata before
    content), so a run's content-vs-reference checks survive a crowd of structural
    proxies (one run had every content check evicted while 12 proxies passed at 0)."""
    import re as _re
    # 任务指令小写副本（用于“任务点名的文件”豁免判断）
    _instr_l = (instruction or "").lower()
    # 接受 / 拒绝（可靠性否决）/ 超容量丢弃 三个桶
    ok, rejections, dropped = [], [], []
    for c in (raw_checks or []):
        # 结构健全性：必须是含非空 probe 的对象
        if not isinstance(c, dict) or not str(c.get("probe", "")).strip():
            rejections.append(f"malformed check (need desc + probe): {str(c)[:120]}")
            continue
        # 描述缺省记为 “(no desc)”
        desc = str(c.get("desc", "")).strip() or "(no desc)"
        # 探针命令去首尾空白
        probe = str(c["probe"]).strip()
        muts = _nonscratch_mutations(probe)
        if muts:
            # 可读性否决：探针必须只读，改动非临时区即拒绝
            rejections.append(f"probe must be READ-ONLY (mutates: {'; '.join(muts)[:90]}): {probe[:100]}")
            continue
        if not any(ch in probe for ch in "/~$|`"):
            # 探针必须真的检查机器状态（含路径/变量/管道），否则是空话
            rejections.append(f"probe does not inspect the machine (no path/var/pipe): {probe[:120]}")
            continue
        if probe.startswith("echo") and not any(ch in probe for ch in "|;&$`"):
            # 纯常量 echo 无可证伪性：拒绝
            rejections.append(f"probe is a constant echo — not falsifiable: {probe[:120]}")
            continue
        # Fix D-1 (E2 autopsy): a probe that reads the agent's own this-run
        # self-report file certifies the reporter, not the work. Structural
        # name-pattern rule (content-blind); a file the TASK itself names is
        # exempt (pass instruction via validate(..., instruction=...)).
        # （中文）见证门（witness gate）：读自己本轮自述文件的探针证明的是“报告者”
        # 而不是“工作成果”。纯结构性名称规则（不看内容）；任务指令本身点名的
        # 文件豁免。
        _sr = None if not witness_gates else _re.search(r"[\w~/.-]*(?:evidence|verification|_proof|self_?report)[\w.-]*",
                         probe, _re.IGNORECASE)
        if _sr and _sr.group(0).split("/")[-1].lower() not in _instr_l:
            # 命中自述文件名且不在指令中：要求改走独立验证通道
            rejections.append(
                "probe reads a self-authored report file — verify the WORK "
                f"through an independent channel instead: {probe[:100]}")
            continue
        # 通过全部闸门：接受该检查
        ok.append(Check(desc=desc, probe=probe))
    # capacity eviction by value tier (stable within a tier)
    if len(ok) > max_checks:
        # 超容量：按价值层级排序（同层内保持原顺序），保留最有价值的前 max_checks 条
        order = sorted(range(len(ok)),
                       key=lambda i: (_PRIORITY[_category(ok[i].probe)], i))
        keep = set(order[:max_checks])
        accepted = [ok[i] for i in range(len(ok)) if i in keep]
        dropped = [f"over capacity (max {max_checks}, kept higher-value checks) — "
                   f"not run: {ok[i].desc[:70]}" for i in range(len(ok)) if i not in keep]
    else:
        # 未超容量：全部接受
        accepted = ok
    if not accepted and not rejections:
        # 一条检查都没有：视为拒绝，要求从任务需求派生至少一条可证伪检查
        rejections.append("no checks provided — derive at least one falsifiable check "
                          "from the task's own requirements")
    return accepted, rejections, dropped


def run_checks(accepted, vm, timeout: int = 30):
    """Run accepted probes in the VM; conservative PASS semantics."""
    # 在 VM 中逐条运行接受的探针；结果保守：输出含 PASS 且不含 FAIL 才算通过
    results = []
    for c in accepted:
        out = vm.run_command(c.probe, timeout=timeout) or ""
        passed = bool(re.search(r"\bPASS\b", out)) and not re.search(r"\bFAIL\b", out)
        # 记录结果（输出只保留尾部 400 字符）
        results.append(CheckResult(desc=c.desc, probe=c.probe, passed=passed,
                                   output=out[-400:]))
    return results
