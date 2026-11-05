# 【中文注释·模块定位】Phase 2 记忆的"传输 + 提交门禁"（宿主侧）。
# 设计核心（v2.0 控制权分离）：Agent 的 MEMORY 是它在客户机上完全拥有的目录
# （~/.memory）。本模块只做三件事——搬运（push/pull 同步进出）、归档（journal）、
# 合法性扫描（silent audit 静默审计）——绝不代写内容。Agent 永远是自己记忆的唯一作者，
# 宿主只当"守门人"，这是论文中记忆读写协议的实现基础。
"""Phase-2 memory transport + commit gates (host side).

The agent's MEMORY is a directory it fully owns in the guest (~/.memory). This
module only MOVES it (sync in/out), ARCHIVES it (journal), and legality-scans
it (silent audit) — it never authors content (v2.0 control split).
"""
# base64：记忆归档在宿主与客户机之间的文本化传输编码。
import base64
# hashlib：语料清单的 SHA-256 校验。
import hashlib
# io：BytesIO 在内存中构造 tar 归档。
import io
# json：语料与拒绝日志的序列化。
import json
# logging：模块级日志器（点分命名，便于过滤）。
import logging
# os：宿主目录与文件操作。
import os
# re：8-gram 分词与 Overleaf 凭据归一化。
import re
# tarfile：gzip 压缩的 tar 归档（记忆树打包/解包）。
import tarfile
# time：日志时间戳。
import time

# 命名空间化 logger：工具与测试可按名字精确配置级别。
log = logging.getLogger("rsiagent.explore.commit")

# 客户机侧记忆目录（Agent 全权拥有）。
GUEST_MEM = "~/.memory"
# 【中文注释】每个 run_command 追加的 base64 字符数分块大小（3000）：
# 受 shell 参数长度限制（arg-length safe），分块写入避免命令行溢出。
_CHUNK = 3000          # base64 chars per run_command append (arg-length safe)


# ---------------------------------------------------------------- transport --
# 【中文注释】传输区：宿主目录 ↔ 客户机 ~/.memory 的双向同步。
def push_memory(vm, memory_dir: str) -> bool:
    """memory (host dir) -> guest ~/.memory. Full replace; guest dir recreated."""
    # 宿主记忆目录 → 客户机 ~/.memory。全量替换：客户机目录先删后建。
    # 在内存 BytesIO 中构造 gzip tar 归档。
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if os.path.isdir(memory_dir):
            # sorted(listdir) 保证归档成员顺序确定（可复现的字节流）。
            for name in sorted(os.listdir(memory_dir)):
                # arcname=name：归档内只保留相对名（不带宿主绝对路径）。
                tf.add(os.path.join(memory_dir, name), arcname=name)
    # tar.gz → base64 文本（ASCII），可用普通 shell 命令安全传输。
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    # 先清空客户机侧旧记忆与上一轮的暂存文件，再重建空目录（全量替换语义）。
    vm.run_command(f"rm -rf {GUEST_MEM} /tmp/mem_in.b64 && mkdir -p {GUEST_MEM}")
    # 按 _CHUNK 分块追加写 base64 文本（printf %s 不解释转义，安全）。
    for i in range(0, len(b64), _CHUNK):
        chunk = b64[i:i + _CHUNK]
        vm.run_command(f"printf %s '{chunk}' >> /tmp/mem_in.b64", timeout=30)
    # 在客户机内解码并解包；SYNC_RC 回显退出码，ls 取前 5 项作回执。
    out = vm.run_command(
        f"base64 -d /tmp/mem_in.b64 > /tmp/mem_in.tgz 2>/tmp/mem_err && "
        f"tar xzf /tmp/mem_in.tgz -C {GUEST_MEM} 2>>/tmp/mem_err; "
        f"echo SYNC_RC=$?; ls {GUEST_MEM} | head -5")
    # 以 SYNC_RC=0 判定推送成功。
    return "SYNC_RC=0" in out


def push_dir(vm, host_dir: str, guest_path: str) -> bool:
    """host dir -> arbitrary guest path. Full replace; guest dir recreated.
    Generic sibling of push_memory using the same chunked base64 archive. Uses its own /tmp staging names so an interleaved
    push_memory can never collide."""
    # 任意宿主目录 → 任意客户机路径（push_memory 的泛化版本）。
    # 【中文注释】使用独立的 /tmp 暂存名（dir_in.*），与 push_memory 的 mem_in.*
    # 互不冲突——即使交错调用也绝不会互相覆盖。
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if os.path.isdir(host_dir):
            for name in sorted(os.listdir(host_dir)):
                tf.add(os.path.join(host_dir, name), arcname=name)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    vm.run_command(f"rm -rf {guest_path} /tmp/dir_in.b64 && "
                   f"mkdir -p {guest_path}")
    for i in range(0, len(b64), _CHUNK):
        chunk = b64[i:i + _CHUNK]
        vm.run_command(f"printf %s '{chunk}' >> /tmp/dir_in.b64", timeout=30)
    out = vm.run_command(
        f"base64 -d /tmp/dir_in.b64 > /tmp/dir_in.tgz 2>/tmp/dir_err && "
        f"tar xzf /tmp/dir_in.tgz -C {guest_path} 2>>/tmp/dir_err; "
        f"echo SYNC_RC=$?; ls {guest_path} | head -5")
    return "SYNC_RC=0" in out


def rm_guest_dir(vm, guest_path: str) -> str:
    """rm -rf a guest dir and return the verification ls output — the ABSENCE
    PROOF string (M7: the archive is torn down with logged proof before any
    actor turn). The loop logs this string verbatim; tools.practice_audit.
    assert_no_archive() judges it mechanically."""
    # 删除客户机目录并返回验证性 ls 输出——"缺席证明"字符串。
    # 【中文注释】M7 防泄漏机制：在任何 Actor 回合之前把审计档案（archive）拆除，
    # 并把 ls 输出（No such file...）原样写进日志；tools/practice_audit.py 的
    # assert_no_archive() 据此机械判定"档案确实不存在"。删除与取证在同一条命令里，
    # 防止"删了但没证据"。
    return vm.run_command(
        f"rm -rf {guest_path}; ls {guest_path} 2>&1 | head -3")


def pull_memory(vm, attempts: int = 3) -> dict:
    """guest ~/.memory -> {relpath: bytes}. Empty dict if no memory exists.
    RETRIED (e1-v2 ep008: one silent transient empty-fetch cost a 46-iter
    episode's memory delta — the wipe-guard saved the memory, but the work was
    lost; a flake must not cost an episode). Logs every failed attempt."""
    # 客户机 ~/.memory → {相对路径: 字节}。记忆不存在时返回空字典。
    # 【中文注释】必须重试（e1-v2 ep008 教训）：一次静默的瞬时空拉取曾让一个 46 迭代
    # 回合的记忆增量全部丢失——防抹除护栏保住了旧记忆，但那轮工作白做。
    # "偶发抖动不应让整个回合买单"。每次失败都记日志。
    # 函数内延迟导入（避免模块导入期的依赖开销），建专用 logger。
    import logging as _logging
    import time as _time
    _log = _logging.getLogger("rsiagent.memory")
    data = None
    # 最多 attempts 次尝试：先 tar 打包（stderr 丢弃以容忍无记忆场景），
    # TAR_RC 回显打包结果。
    for att in range(attempts):
        out = vm.run_command(
            "cd ~ && tar czf /tmp/mem_out.tgz .memory 2>/dev/null; echo TAR_RC=$?")
        if "TAR_RC=0" not in out:
            # 打包失败：警告 + 等待 15 秒后重试。
            _log.warning("pull attempt %d: guest tar failed (out=%r)",
                         att + 1, out[:120])
            _time.sleep(15)
            continue
        # 取回归档字节；fetch_file 可能返回 (bytes|None, note) 元组，取首元素。
        data = vm.fetch_file("/tmp/mem_out.tgz")
        if isinstance(data, tuple):        # fetch_file returns (bytes|None, note)
            data = data[0]
        if data:
            # 非空即成功，跳出重试循环。
            break
        _log.warning("pull attempt %d: fetch returned empty", att + 1)
        _time.sleep(15)
    if not data:
        # 全部尝试失败：告警并返回空字典（上层 wipe-guard 决定是否保留旧记忆）。
        _log.warning("pull FAILED after %d attempts", attempts)
        return {}
    files = {}
    # 解包 tar.gz，遍历成员。
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            # 只收普通文件（目录/链接跳过）。
            if not m.isfile():
                continue
            rel = m.name
            # 去掉 tar 内的前缀 .memory/，得到记忆树内相对路径。
            if rel.startswith(".memory/"):
                rel = rel[len(".memory/"):]
            # v1.1 (EXP-2026-007 bug #12b): ep009's armed import deposited
            # __pycache__/*.pyc into ~/.memory — bytecode is a build
            # artifact, not experience; ingesting it polluted mem_delta
            # (+7213 = -571 true page delta + 7784B pyc). Filter it.
            # 【中文注释】v1.1 过滤器（EXP-2026-007 bug #12b）：ep009 的武装导入把
            # __pycache__/*.pyc 写进了 ~/.memory——字节码是构建产物而非经验，
            # 混入记忆会污染 mem_delta 统计（+7213 = 真实页 -571 + pyc 7784B）。
            # 一律过滤。
            if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            # extractfile 读取成员字节；None（稀疏成员）跳过。
            f = tf.extractfile(m)
            if f is not None:
                files[rel] = f.read()
    # 返回 {相对路径: 字节内容} 的记忆树。
    return files


def write_memory(memory_dir: str, files: dict) -> None:
    """Replace the host memory with ``files`` ({relpath: bytes})."""
    # 用 files 全量替换宿主记忆目录（先清空再写入；原子性由调用方保证）。
    os.makedirs(memory_dir, exist_ok=True)
    # 清空现有内容：文件直接删，目录递归删（shutil 函数内导入以减依赖）。
    for name in os.listdir(memory_dir):
        p = os.path.join(memory_dir, name)
        if os.path.isfile(p):
            os.remove(p)
        else:
            import shutil
            shutil.rmtree(p)
    # 逐文件写入：os.makedirs 建立父目录（or memory_dir 兜顶空 dirname）。
    for rel, data in files.items():
        p = os.path.join(memory_dir, rel)
        os.makedirs(os.path.dirname(p) or memory_dir, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


# ------------------------------------------------------------------ journal --
# 【中文注释】归档区：每次记忆提交的追加式快照与元数据行（对 Agent 不可见）。
def journal(journal_dir: str, episode: int, files: dict, meta: dict) -> None:
    """Append-only per-episode snapshot (tgz) + one meta line. Invisible to the
    agent; pure rollback/audit insurance."""
    # 追加式（append-only）每回合快照：ep{episode:03d}.tgz + journal.jsonl 一行元数据。
    # 【中文注释】Agent 看不到日志；它纯粹是回滚与审计保险——记忆可以回放到任意
    # 回合边界（论文中"记忆提交仅在有效边界发生"的物理证据链）。
    os.makedirs(journal_dir, exist_ok=True)
    # 快照文件名按回合序号零填充，保证字典序即时间序。
    p = os.path.join(journal_dir, f"ep{episode:03d}.tgz")
    with tarfile.open(p, "w:gz") as tf:
        for rel, data in sorted(files.items()):
            # 手工构造 TarInfo（不经磁盘），从内存字节流直接添加成员。
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    # journal.jsonl 追加一行：回合号、文件数、字节数、时间戳 + 调用方 meta。
    with open(os.path.join(journal_dir, "journal.jsonl"), "a") as f:
        f.write(json.dumps({"episode": episode, "files": len(files),
                            "bytes": sum(len(d) for d in files.values()),
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **meta}) + "\n")


# -------------------------------------------------------------- silent audit --
# 【中文注释】静默审计区：防"任务泄漏"的提交门禁。语言指纹用 8-gram shingle。
# 分词模式：小写字母/数字连续段。
_WORD = re.compile(r"[a-z0-9]+")
_N = 8                                     # shingle length (words)
# 【中文注释】shingle 长度：8 个词的滑动窗口——8 连词足以唯一指纹化任务指令文本。
# 语料归一化版本号（清单校验用），换归一化规则必须同步升版。
CORPUS_NORMALIZATION = "strip-dynamic-overleaf-credentials-v1"
# 动态 Overleaf 凭据模式：OSWorld-V2 的任务模块在导入时插值新生成的登录凭据，
# 这类"运行期生成的秘密"不是稳定的基准语义。
_DYNAMIC_OVERLEAF_CREDENTIALS = re.compile(
    r"(?:\r?\n)*Overleaf login credentials\s*[-—]\s*"
    r"Email:[^\r\n]*?\s+Password:[^\r\n]*(?:\r?\n)*",
    re.IGNORECASE,
)


def _shingles(text: str) -> set:
    # 计算 8-gram shingle 集合：全部小写化后分词，再取所有 8 连词窗口。
    # 技法：集合推导式 + range(len-N+1) 滑窗；返回 set 便于交并差。
    words = _WORD.findall(text.lower())
    return {" ".join(words[i:i + _N]) for i in range(len(words) - _N + 1)}


def normalize_instruction_for_corpus(instruction: str) -> str:
    """Remove run-generated Overleaf credentials from benchmark text.

    OSWorld-V2 tasks 057 and 072 interpolate fresh credentials when their task
    modules are imported.  Those secrets are not stable benchmark semantics;
    retaining them makes an otherwise identical host-only leakage corpus hash
    differently on every build.  Leave all instructions without that exact
    credential label byte-for-byte unchanged.
    """
    # 从基准题面文本中剔除"运行期生成的 Overleaf 凭据"。
    # 【中文注释】OSWorld-V2 的 057/072 号任务在模块导入时插值新凭据；保留它们会让
    # "内容相同的宿主侧泄漏语料"每次构建哈希都不同，破坏 provenance 校验。
    # 不含该确切凭据标签的指令一律逐字节原样返回（不碰任何其他内容）。
    if not _DYNAMIC_OVERLEAF_CREDENTIALS.search(instruction):
        return instruction
    # 命中则把凭据段替换为两个换行并去除尾部空白。
    return _DYNAMIC_OVERLEAF_CREDENTIALS.sub("\n\n", instruction).rstrip()


def build_corpus(corpus_path: str, instructions: list) -> None:
    """Host-side ONLY (grader-diagnostic class): 8-gram shingles of all task
    instructions. Never enters any agent context."""
    # 构建"任务指令语料"：所有任务指令的 8-gram shingle 并集。
    # 【中文注释】仅限宿主侧（评分器诊断级）：语料文件永不进入任何智能体上下文——
    # 否则它自己就成了泄漏源。|= 对集合并集累加。
    sh = set()
    for ins in instructions:
        sh |= _shingles(ins)
    with open(corpus_path, "w") as f:
        # sorted 保证 JSON 序列化确定，便于哈希比对。
        json.dump(sorted(sh), f)


def validate_instruction_corpus(corpus_path: str,
                                expected_instruction_count: int = 108) -> dict:
    """Validate the host-only OSWorld-V2 instruction-corpus provenance."""
    # 校验指令语料的来源证明（provenance）：文件 + 清单（.MANIFEST.json）双重验证。
    manifest_path = corpus_path + ".MANIFEST.json"
    try:
        # 读语料字节（供哈希）与 JSON 内容，读清单 JSON。
        raw = open(corpus_path, "rb").read()
        corpus = json.loads(raw)
        record = json.load(open(manifest_path, encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        # 缺失/不可读/坏 JSON → RuntimeError（调用方 fail fast）。
        raise RuntimeError(
            "instruction corpus or its manifest is missing/unreadable") from exc
    # 清单必须恰好包含这些键（set 相等），且各字段满足约束：
    # 语料是非空字符串列表、已排序去重、schema_version==2、基准与评估版本匹配、
    # 归一化版本匹配、指令数与期望一致、absent_task_ids 是列表、shingle 数与语料
    # 行数一致、清单里的 corpus_sha256 与实际字节哈希一致。
    required = {"schema_version", "benchmark", "evaluation_version",
                "normalization",
                "instruction_count", "absent_task_ids", "shingle_count",
                "corpus_sha256"}
    if (not isinstance(corpus, list) or not corpus
            or any(not isinstance(v, str) or not v for v in corpus)
            or corpus != sorted(set(corpus))
            or set(record) != required
            or record.get("schema_version") != 2
            or record.get("benchmark") != "OSWorld-V2"
            or record.get("evaluation_version") != "v2"
            or record.get("normalization") != CORPUS_NORMALIZATION
            or record.get("instruction_count") != expected_instruction_count
            or not isinstance(record.get("absent_task_ids"), list)
            or record.get("shingle_count") != len(corpus)
            or record.get("corpus_sha256") != hashlib.sha256(raw).hexdigest()):
        raise RuntimeError("instruction corpus provenance validation failed")
    # 全部通过，返回清单记录。
    return record


def silent_audit(files: dict, corpus_path: str, reject_log: str,
                 authorized_instruction: str = "",
                 require_corpus: bool = False) -> dict:
    """Return the accepted subset of ``files``. A file is rejected if it shares
    any 8-gram shingle with a task instruction (task-leakage tripwire — should
    ~never fire). Rejections are logged host-side ONLY: no agent feedback."""
    # 静默审计：返回 files 的"被接受子集"。
    # 【中文注释】门禁规则：任何与任务指令共享 8-gram shingle 的文件都被拒绝
    # （任务泄漏绊线——设计上几乎不该触发）。拒绝记录只写宿主侧日志，
    # 绝不反馈给智能体（silent 的含义：不给模型任何"哪里泄漏了"的信号，
    # 防止它学着绕过）。
    if not os.path.exists(corpus_path):
        # 语料缺失：require_corpus=True 时目标感知审计必须有语料，直接报错；
        # 否则放行全部（普通记忆同步场景）。
        if require_corpus:
            raise RuntimeError(
                "target-aware memory audit requires the instruction corpus")
        return files
    corpus = set(json.load(open(corpus_path)))
    if require_corpus and not corpus:
        # 语料文件存在但为空：同样视为不可审计，报错（fail closed）。
        raise RuntimeError(
            "target-aware memory audit instruction corpus is empty")
    # 授权指令自身的 shingle 与词表（指令里出现自己的内容是合法的）。
    authorized = _shingles(authorized_instruction) \
        if authorized_instruction else set()
    authorized_words = _WORD.findall(authorized_instruction.lower()) \
        if authorized_instruction else []
    # Keep transcript and durable-memory target exceptions identical. Import
    # locally so the generic memory transport remains usable without the RSI
    # practice fence at module-import time.
    # 【中文注释】保持"轨迹"与"持久记忆"两处目标豁免逻辑一致：函数内延迟导入
    # exam_fence，让通用记忆传输在模块导入期不依赖 RSI 练习围栏。
    from tools.exam_fence import _authorized_near_shingle
    accepted = {}
    for rel, data in files.items():
        try:
            # 解码为文本做指纹比对（容错解码，坏字节忽略）。
            text = data.decode("utf-8", errors="ignore")
        except Exception:                    # noqa: BLE001
            # 连解码都不可能的二进制（理论上到不了这里）：保守放行。
            accepted[rel] = data
            continue
        # 命中集 = (文件 shingle ∩ 语料) - 授权 shingle，且逐个过
        # _authorized_near_shingle 近邻豁免（任务可见输入路径等合法重合）。
        hits = {
            gram for gram in (_shingles(text) & corpus) - authorized
            if not _authorized_near_shingle(gram, authorized_words)
        }
        if hits:
            # 有命中 → 拒绝该文件：追加 JSONL 拒绝日志（文件名、前 3 个命中、时间），
            # 同时打日志；绝不进入 accepted（静默，无 Agent 反馈）。
            with open(reject_log, "a") as f:
                f.write(json.dumps({"file": rel, "hits": sorted(hits)[:3],
                                    "ts": time.strftime("%H:%M:%S")}) + "\n")
            log.warning("silent audit rejected %s (%d shingle hits)", rel, len(hits))
            continue
        # 无命中：接受。
        accepted[rel] = data
    # 返回被接受的记忆子集。
    return accepted
