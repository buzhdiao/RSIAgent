"""Read-only render helpers for the R2a appearance-gate — PURE INFRASTRUCTURE.

CONTRACT: this module must contain NO model-facing prompt literals — every string
here is either a shell command template or a filename pattern. It is therefore not
an audited prompt surface (tests/test_no_leakage.py SURFACES); anything that goes
into model context (questions, evidence framing) lives in core/verifier.py and IS
audited. Keep it that way: if you add a prompt string here, move it to verifier.py.

Path discovery is provenance-only: candidates come from the run's file-change
ledger, the task instruction, and the inspector's own probe output — never from
names known to the harness.
"""
import os
import re

# 从自由文本中提取客户机（guest）内候选文件路径的正则：匹配 /home/user/... 或 ~/...
# 开头的连续非空白片段（排除引号、管道等 shell 元字符）
_PATH_RE = re.compile(r"(/home/user/[^\s'\"`|;)\]}>]+|~/[^\s'\"`|;)\]}>]+)")

# filetype -> renderer family (general file-format knowledge, not task knowledge)
# 文件扩展名 -> 渲染器家族映射（通用文件格式知识，不含任务知识）
_RENDERABLE = {".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
               ".webm": "video",
               ".pptx": "office", ".docx": "office", ".odp": "office",
               ".odt": "office", ".xlsx": "office",
               ".pdf": "pdfdoc",
               ".png": "image", ".jpg": "image", ".jpeg": "image",
               ".bmp": "image", ".gif": "image"}


def harvest_paths(instruction: str, context: str, history) -> list:
    """Candidate deliverable paths, ranked: instruction-mentioned first, then
    provenance/probe order. Renderable types only; deduped."""
    # 扫描语料 = 上下文正文 + 历史中所有 user 角色消息的内容
    blobs = [context or ""] + [str(m.get("content", "")) for m in history
                               if isinstance(m, dict) and m.get("role") == "user"]
    # seen 用于去重，cands 保持发现顺序
    seen, cands = set(), []
    # 逐段语料扫描
    for blob in blobs:
        # 提取该语料中所有候选路径
        for p in _PATH_RE.findall(blob):
            # 去掉句尾粘连的标点（句号、逗号、冒号）
            p = p.rstrip(".,:")
            # 只保留可渲染类型且未出现过的路径
            if os.path.splitext(p)[1].lower() in _RENDERABLE and p not in seen:
                seen.add(p)
                cands.append(p)
    # 排序：任务指令中点名的文件优先（键 0），其余按发现顺序（键 1）
    return sorted(cands, key=lambda p: (0 if os.path.basename(p) in instruction
                                        else 1, cands.index(p)))


# P1 destination-binding: filename-looking tokens in TASK TEXT (pure lexical — no
# comprehension, no task-specific names). Extension must be alphabetic 2-5 chars
# (rejects version numbers like "4.6"); web TLDs rejected so domain names in
# instructions don't masquerade as files.
# （中文）P1 目的地绑定：纯词法识别任务文本中“像文件名”的 token；扩展名须为 2-5 个
# 字母（排除 “4.6” 这类版本号），并拒绝常见 Web 顶级域，防止域名被误判为文件。
_FILENAME_RE = re.compile(r"[\w./\\-]{1,80}\.([A-Za-z]{2,5})\b")
# 这些“扩展名”其实是域名后缀/协议，不是文件扩展名
_NONFILE_EXT = {"com", "org", "net", "edu", "gov", "io", "ai", "icu", "www",
                "http", "https", "html", "htm", "php", "aspx"}


def named_files(text: str, cap: int = 8) -> list:
    """Basenames of file-looking tokens mentioned in ``text``, deduped, capped."""
    # seen 去重，out 收集文件名（按出现顺序）
    seen, out = set(), []
    # 在文本中逐个寻找“文件名样”token
    for m in _FILENAME_RE.finditer(text or ""):
        # 取扩展名并小写化
        ext = m.group(1).lower()
        # 域名后缀类 token 不是文件，跳过
        if ext in _NONFILE_EXT:
            continue
        # 去掉尾部标点后只留文件名（basename）
        base = os.path.basename(m.group(0).rstrip(".,:"))
        # 非空且未收集过则记录
        if base and base not in seen:
            seen.add(base)
            out.append(base)
        # 达到上限即停（成本约束）
        if len(out) >= cap:
            break
    return out


def render_cmd(path: str, out_png: str) -> str:
    """Shell command rendering ``path`` to ``out_png`` inside the guest.
    Read-only w.r.t. the source; writes only under /tmp."""
    # 按扩展名查出渲染器家族
    kind = _RENDERABLE.get(os.path.splitext(path)[1].lower())
    if kind == "image":
        # 图片本身可直接使用：拷贝到输出路径（源只读）
        return f"cp {path!r} {out_png!r}"
    if kind == "video":       # middle frame (fallback 1s if duration unreadable)
        # 视频：先读时长，抽取中间帧；时长读不到则退回第 1 秒处抽帧
        return ("D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "
                f"{path!r} 2>/dev/null | cut -d. -f1); "
                f"ffmpeg -v error -y -ss ${{D:-2}}s -i {path!r} -frames:v 1 "
                f"{out_png!r} 2>/dev/null || ffmpeg -v error -y -ss 1 -i {path!r} "
                f"-frames:v 1 {out_png!r} 2>/dev/null")
    if kind == "pdfdoc":
        # PDF：pdftoppm 渲染第 1 页为 PNG（-singlefile 不加页码后缀）
        return (f"pdftoppm -f 1 -l 1 -png -singlefile {path!r} "
                f"{out_png[:-4]!r} 2>/dev/null")
    if kind == "office":      # page 1 via headless LO, move to target name
        # Office/ODF 文档：用无头 LibreOffice 转 PNG（第 1 页），再移动到目标名
        return (f"soffice --headless --convert-to png --outdir /tmp/rsiagent_insp "
                f"{path!r} >/dev/null 2>&1; mv -f /tmp/rsiagent_insp/*.png {out_png!r} "
                "2>/dev/null")
    # 未知/不可渲染类型：返回空串表示无命令可用
    return ""
