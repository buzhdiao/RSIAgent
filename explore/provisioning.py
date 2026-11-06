"""Capture and replay exact project materials.

At project ACCEPTANCE (during the authoring session's boot, AFTER the
provisioner has generated raw materials) the harness tars the material dirs
in-guest, pulls the tar into host-side project state, and writes a per-file
sha256 manifest. Every later project night pushes the SAME tar back
byte-verbatim and verifies the manifest in-guest BEFORE the gate.

Determinism: there is NO re-generation path here — ffmpeg
re-generation is not trusted for byte-stability (verified: mvhd/tkhd
timestamps differ). Replay is byte-verbatim by construction; any capture or
verify failure is an infra failure the loop records as provision_failed.

Transport uses the OSWorld server's trusted streaming artifact channel in
both directions.  A compatibility fallback retains chunked base64 replay for
lightweight VM adapters that do not expose ``push_file``.
"""
# 项目材料的精确捕获与回放模块。
# 在项目"验收"（authoring 引导期间，provisioner 生成原始材料之后）时，harness
# 在 guest 内打包材料目录，拉回宿主侧项目状态，并写逐文件 sha256 清单。
# 此后每个项目夜都把同一 tar 逐字节推回，并在门禁（gate）之前于 guest 内校验清单。
# 确定性：这里不存在任何"再生成"路径——ffmpeg 再生成不被信任具备字节稳定性
# （已验证：mvhd/tkhd 时间戳不同）。回放按构造逐字节一致；任何捕获/校验失败都是
# 基础设施失败，主循环记为 provision_failed。
# 传输双向使用 OSWorld server 的可信流式工件通道；为不暴露 ``push_file`` 的轻量
# VM 适配器保留分块 base64 回放的兼容回退。
import base64
# base64：兼容回退通道的编解码。
import hashlib
# hashlib：文件与归档的 SHA-256 摘要。
import io
# io：内存字节流（tar 打包/分块拼接）。
import json
# json：读写清单、根目录与符号链接边车文件。
import logging
# logging：记录捕获/回放过程。
import os
# os：路径拼接与文件操作。
from pathlib import PurePosixPath
# PurePosixPath：以 POSIX 语义校验归档内路径。
import re
# re：sha 清单行等正则解析。
import shlex
# shlex：把路径安全引用进 guest 命令。
import tarfile
# tarfile：直接检视归档字节，不在宿主上解包。
import time
# time：重试间隔与清单时间戳。

from explore.commit import _CHUNK
# 共享的传输分块大小（兼容回退通道使用）。

log = logging.getLogger("rsiagent.explore.provisioning")
# 本模块的日志器。

GUEST_HOME = "/home/user"
# guest 内的项目主目录。
GUEST_TGZ_OUT = "/tmp/proj_materials.tgz"
# guest 内捕获输出的 tar 路径。
GUEST_TGZ_IN = "/tmp/proj_in.tgz"
# guest 内回放输入的 tar 路径。
GUEST_B64_IN = "/tmp/proj_in.b64"
# 兼容回退通道的 base64 暂存文件。
GUEST_EXPECTED = "/tmp/proj_expected.list"
# 精确集合校验的期望文件列表。
GUEST_ACTUAL = "/tmp/proj_actual.list"
# 精确集合校验的实际文件列表。
MATERIALS = "materials.tgz"
# 宿主侧材料归档文件名。
MANIFEST = "materials.MANIFEST.sha256"
# 宿主侧逐文件 sha256 清单文件名。
ROOTS = "materials.ROOTS.json"
# 项目拥有的顶层根目录记录文件名。
OWNED = "materials.OWNED.json"
# 项目可评分/可改动的顶层路径记录文件名。
SYMLINKS = "materials.SYMLINKS.json"
# 符号链接边车文件名（链接目标逐一绑定）。
DEFAULT_GUEST_DIRS = ["raw"]
# 默认捕获的 guest 顶层目录。

_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+?)\s*$")
# sha256sum 输出行解析：64 位十六进制 + 可选二进制标记 + 路径。
_VERIFY_BATCH = 24                 # command transport batch, never an output cap
# 逐批校验的批量大小（保留原行内英文注释：这是命令传输批量，绝非输出上限）。
_PULL_CHUNK = 1_000_000            # transport frame, never a total-size limit
# 兼容回退拉取的分块大小（保留原行内英文注释：传输帧，绝非总大小上限）。
_ROOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# 安全顶层目录名的白名单模式（不以点开头）。


def _symlink_digest(target: str) -> str:
    """Bind a link target into the ordinary immutable manifest namespace."""

    # 把链接目标绑定进普通不可变清单命名空间（以 symlink 前缀域哈希）。
    return hashlib.sha256(b"symlink\0" + os.fsencode(target)).hexdigest()
    # 对 "symlink\0目标" 求 SHA-256，使链接身份可被同一清单校验。


# ----------------------------------------------------------------- capture --
# --------------------------- 捕获段 ---------------------------
def capture_project_materials(vm, project_key: str, host_dir: str,
                              guest_dirs=None, attempts: int = 3) -> dict:
    """Guest material dirs -> host_dir/{materials.tgz, MANIFEST}. Runs ONCE,
    at project acceptance, after the provisioner has generated raw materials
    on the authoring boot. Returns {ok, files, bytes}; not-ok = infra
    failure (provision_failed), never silently retried by re-generation."""
    # guest 材料目录 -> 宿主 {materials.tgz, MANIFEST}。仅在项目验收时运行一次；
    # 返回 {ok, files, bytes}；not-ok 即基础设施失败，绝不通过再生成静默重试。
    dirs = sorted(set(guest_dirs or DEFAULT_GUEST_DIRS))
    # 去重排序待捕获目录（缺省为 ["raw"]）。
    if (not dirs or any(not isinstance(d, str) or not _ROOT_NAME.fullmatch(d)
                        or d.startswith(".") for d in dirs)):
        # 校验目录名：非空、字符串、符合安全名模式、不以点开头。
        return {"ok": False, "files": 0, "bytes": 0,
                "error": "guest project roots must be safe top-level names"}
        # 不安全目录名：直接失败（fail-closed）。
    dir_args = " ".join(shlex.quote(d) for d in dirs)
    # 把目录名安全引用后拼成命令参数。
    data = None
    # 捕获到的 tar 字节。
    for att in range(attempts):
        # 最多尝试 attempts 次。
        out = vm.run_command(
            f"rm -f {GUEST_TGZ_OUT} && tar czf {GUEST_TGZ_OUT} "
            f"-C {GUEST_HOME} {dir_args} 2>/dev/null; echo TAR_RC=$?",
            timeout=120)
        # 在 guest 内删除旧 tar 并重新打包材料目录（回显退出码）。
        if "TAR_RC=0" not in (out or ""):
            # 打包失败：记警告并等待重试。
            log.warning("capture attempt %d: guest tar failed (out=%r)",
                        att + 1, (out or "")[:120])
            # 记录失败输出。
            time.sleep(15)
            # 等 15 秒再试。
            continue
            # 进入下一次尝试。
        # Project capture is lossless artifact transport, not a look action.
        # Never impose an arbitrary project-size ceiling here: candidate size
        # is determined by the Actor's work and the complete byte stream is
        # part of the experiment.  ``None`` explicitly selects unbounded
        # transfer while ordinary visual/file inspection remains bounded.
        # （中文）项目捕获是无损工件传输，不是"看一眼"动作：绝不在这里施加任意的
        # 项目大小上限——候选大小由 Actor 的工作决定，完整字节流是实验的一部分。
        # ``None`` 显式选择无界传输；普通视觉/文件检视仍是有界的。
        try:
            data = vm.fetch_file(GUEST_TGZ_OUT, max_bytes=None)
            # 首选：经流式通道无界拉取 tar。
        except TypeError:
            # Compatibility with lightweight/older VM transports that expose
            # fetch_file(path) but no size keyword.  Those transports own their
            # transfer semantics; the fallback below still preserves arbitrary
            # total sizes through independently decoded chunks.
            # （中文）兼容只支持 fetch_file(path) 的轻量/旧传输：它们自带传输语义；
            # 下面的回退仍可通过独立解码的分块保住任意总大小。
            data = vm.fetch_file(GUEST_TGZ_OUT)
            # 无 max_bytes 关键字的旧签名调用。
        if isinstance(data, tuple):        # fetch_file returns (bytes|None, note)
            # fetch_file 返回 (字节|None, 说明) 二元组（保留原行内英文注释）。
            data = data[0]
            # 取字节部分。
        if data:
            # 已拿到数据：跳出重试循环。
            break
        log.warning("capture attempt %d: fetch returned empty", att + 1)
        # 拉取为空：记警告。
        time.sleep(15)
        # 等 15 秒再试。
    if not data:
        # 所有直接拉取都失败：走分块 base64 回退。
        # chunked-b64 fallback: pull the tgz through run_command in 1MB
        # slices (transport-agnostic; same channel replay pushes through)
        # （中文）分块 b64 回退：经 run_command 以 1MB 切片拉取 tgz
        # （与传输无关；回放推流走同一通道）。
        try:
            sz = int((vm.run_command(f"stat -c %s {GUEST_TGZ_OUT}",
                                     timeout=30) or "0").split()[0])
            # 先查 tar 的字节大小。
        except (ValueError, IndexError):
            # 解析失败则按 0 处理。
            sz = 0
        if sz:
            # 有大小才尝试分块拉取。
            import base64 as _b64
            # 局部导入 base64（避免遮蔽顶层同名模块引用）。
            CH = _PULL_CHUNK
            # 分块大小（1MB）。
            blob = io.BytesIO()
            # 内存缓冲：拼接全部分块。
            fallback_ok = True
            # 回退是否成功。
            for k in range((sz + CH - 1) // CH):
                # 逐块（含尾块）拉取。
                p = vm.run_command(
                    f"dd if={GUEST_TGZ_OUT} bs={CH} skip={k} count=1 "
                    "2>/dev/null | base64 -w0", timeout=180, cap=2_500_000)
                # 用 dd 取第 k 块并 base64 编码输出。
                try:
                    chunk = _b64.b64decode((p or "").strip(), validate=True)
                    # 严格解码该分块。
                except Exception as e:                     # noqa: BLE001
                    # 解码失败：记警告并放弃回退（保留原行内 noqa）。
                    log.warning("capture b64 fallback chunk %d decode failed: %s",
                                k, e)
                    # 记录失败块号。
                    fallback_ok = False
                    # 标记回退失败。
                    break
                    # 退出分块循环。
                expected = min(CH, sz - k * CH)
                # 该块应有的字节数（尾块更小）。
                if len(chunk) != expected:
                    # 块大小不符：放弃回退。
                    log.warning("capture b64 fallback chunk %d size mismatch "
                                "%d != %d", k, len(chunk), expected)
                    # 记录大小不一致。
                    fallback_ok = False
                    # 标记失败。
                    break
                    # 退出循环。
                blob.write(chunk)
                # 追加到缓冲。
            if fallback_ok:
                # 全部分块成功。
                pulled = blob.getvalue()
                # 取出完整字节。
                if len(pulled) == sz:
                    # 总长与 stat 结果一致才接受。
                    data = pulled
                    # 作为捕获数据。
                    log.info("capture: b64 fallback pulled %d bytes", sz)
                    # 记录回退成功。
                else:
                    log.warning("capture b64 fallback size mismatch "
                                "%d != %d", len(pulled), sz)
                    # 总长不符：记警告。
    if not data:
        # 彻底失败：返回 not-ok（基础设施失败，不静默再生成）。
        log.warning("capture FAILED for project %s after %d attempts",
                    project_key, attempts)
        # 记录捕获失败。
        return {"ok": False, "files": 0, "bytes": 0}
        # 返回失败结果。
    manifest, symlinks = _inventory_from_tgz(data)
    # 直接从归档字节盘点：逐文件摘要与符号链接表。
    if not manifest:
        # 空归档：视为失败（防止把空项目当验收成功）。
        log.warning("capture for project %s produced an EMPTY tar "
                    "(dirs=%s)", project_key, dirs)
        # 记录空 tar 警告。
        return {"ok": False, "files": 0, "bytes": len(data)}
        # 返回失败（带字节数便于诊断）。
    os.makedirs(host_dir, exist_ok=True)
    # 确保宿主侧项目目录存在。
    with open(os.path.join(host_dir, MATERIALS), "wb") as f:
        # 写入材料归档。
        f.write(data)
        # 原样保存 tar 字节（回放时逐字节推回）。
    with open(os.path.join(host_dir, MANIFEST), "w") as f:
        # 写入 sha256 清单。
        f.write(f"# project: {project_key}  dirs: {' '.join(dirs)}  "
                f"captured: {time.strftime('%F %T')}\n")
        # 头部为出处注释行（项目、目录、捕获时间）。
        for rel in sorted(manifest):
            # 逐条写"哈希  相对路径"。
            f.write(f"{manifest[rel]}  {rel}\n")
            # 与 sha256sum -c 兼容的格式。
    with open(os.path.join(host_dir, ROOTS), "w", encoding="utf-8") as f:
        # 写入根目录记录。
        json.dump({"schema_version": 1, "roots": dirs}, f,
                  indent=2, sort_keys=True)
        # 结构化记录顶层根（保证空目录也留在 wipe/精确集合边界内）。
        f.write("\n")
        # 结尾换行。
    with open(os.path.join(host_dir, SYMLINKS), "w", encoding="utf-8") as f:
        # 写入符号链接边车。
        json.dump({"schema_version": 1, "links": symlinks}, f,
                  ensure_ascii=False, indent=2, sort_keys=True)
        # 记录每个链接的精确目标。
        f.write("\n")
        # 结尾换行。
    return {"ok": True, "files": len(manifest), "bytes": len(data)}
    # 返回成功：文件数与字节数。


def _inventory_from_tgz(data: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Return immutable digests and exact symlink targets from an archive.

    Inspect archive bytes directly, without extracting or dereferencing symlinks
    on the host. Absolute guest symlinks (for example a venv's Python executable)
    are ordinary opaque link targets. Unsafe member paths, special files, and
    children beneath a non-directory entry are rejected before replay.
    """

    # 直接检视归档字节：不在宿主上解包、不跟随符号链接。
    # guest 内的绝对路径符号链接（如 venv 的 python 可执行文件）就是普通不透明目标。
    # 不安全成员路径、特殊文件与非目录条目的子项在回放前即被拒绝。
    manifest: dict[str, str] = {}
    # 相对路径 -> 不可变摘要。
    symlinks: dict[str, str] = {}
    # 相对路径 -> 精确链接目标。
    def safe_name(value: str) -> str:
        # 归档内路径安全校验函数。
        path = PurePosixPath(value)
        # 以 POSIX 语义解析。
        if (path.is_absolute() or ".." in path.parts
                or any(c in value for c in "\0\n\r") or not path.parts):
            # 绝对路径、上级引用、控制字符或空路径均不安全。
            raise ValueError(f"unsafe project archive path: {value!r}")
            # 拒绝并报错。
        return path.as_posix()
        # 返回规范化的 POSIX 相对路径。

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        # 以只读方式打开内存中的 gz 压缩 tar。
        members = {}
        # 相对路径 -> 归档成员。
        for member in tf:
            # 遍历全部成员。
            rel = safe_name(member.name)
            # 校验并规范化路径。
            if rel in members:
                # 路径重复：拒绝。
                raise ValueError(f"duplicate project archive path: {rel}")
                # 报重复路径错误。
            if not (member.isdir() or member.isfile()
                    or member.issym() or member.islnk()):
                # 目录/普通文件/软链接/硬链接之外的条目（设备、fifo 等）拒绝。
                raise ValueError(f"project archive contains a special entry: {rel}")
                # 报特殊条目错误。
            members[rel] = member
            # 记录成员。
        for rel, member in members.items():
            # 第二遍：检查父子一致性与内容摘要。
            for parent in PurePosixPath(rel).parents:
                # 遍历该路径的所有父级。
                entry = members.get(parent.as_posix())
                # 查找父级条目。
                if entry is not None and not entry.isdir():
                    # 父级存在但不是目录：拒绝（防止文件"包含"子项）。
                    raise ValueError(f"project archive traverses a non-directory: {rel}")
                    # 报穿越错误。
            if member.isdir():
                # 目录条目无内容摘要。
                continue
                # 跳过。
            if member.issym():
                # 软链接：记录目标并用链接域哈希入清单。
                symlinks[rel] = member.linkname
                # 记录精确目标。
                manifest[rel] = _symlink_digest(member.linkname)
                # 以 symlink 前缀域哈希绑定身份。
                continue
                # 处理下一条。
            seen = {rel}
            # 硬链接解析的已见集合（防环）。
            source = member
            # 当前追溯到的基础条目。
            while source.islnk():
                # 循环追溯硬链接目标。
                target = safe_name(source.linkname)
                # 校验目标路径。
                if target in seen or target not in members:
                    # 成环或目标不存在：拒绝。
                    raise ValueError(f"invalid project archive hardlink: {rel}")
                    # 报硬链接错误。
                seen.add(target)
                # 记入已见。
                source = members[target]
                # 继续追溯。
            if not source.isfile():
                # 硬链最终不指向普通文件：拒绝。
                raise ValueError(f"project hardlink does not name regular bytes: {rel}")
                # 报错误。
            h = hashlib.sha256()
            # 内容摘要器。
            with tf.extractfile(source) as stream:
                # 从归档读取基础条目内容。
                for block in iter(lambda: stream.read(1 << 20), b""):
                    # 按 1MB 逐块读取。
                    h.update(block)
                    # 累积哈希。
            manifest[rel] = h.hexdigest()
            # 记录该（硬链解析后）文件的摘要。
    return dict(sorted(manifest.items())), dict(sorted(symlinks.items()))
    # 返回排序后的清单与链接表。


def _manifest_from_tgz(data: bytes) -> dict:
    """Compatibility helper returning all immutable archive-entry digests."""

    # 兼容辅助：只返回全部不可变条目摘要。
    return _inventory_from_tgz(data)[0]
    # 复用盘点函数取清单部分。


def read_manifest(host_dir: str) -> dict:
    """{relpath: sha256hex} from host_dir/materials.MANIFEST.sha256 (empty
    dict if absent). '#'-prefixed lines are provenance comments."""
    # 从清单文件读 {相对路径: sha256}；文件缺失返回空 dict；# 开头为出处注释。
    p = os.path.join(host_dir, MANIFEST)
    # 清单文件路径。
    if not os.path.exists(p):
        # 缺失则返回空。
        return {}
        # 空映射。
    out = {}
    # 结果映射。
    with open(p) as f:
        # 逐行解析清单。
        for line in f:
            # 每行。
            line = line.strip()
            # 去首尾空白。
            if not line or line.startswith("#"):
                # 空行或注释行跳过。
                continue
                # 下一行。
            m = _SHA_LINE.match(line)
            # 匹配 sha256sum 行格式。
            if m:
                # 命中则记录。
                out[m.group(2)] = m.group(1)
                # 路径 -> 哈希。
    return out
    # 返回映射。


def read_symlinks(host_dir: str) -> dict[str, str]:
    """Return the exact link targets bound by a captured project manifest.

    Link targets are opaque guest strings, including absolute paths used by
    virtual environments. Verification compares readlink() without following
    the target. Only the link's own manifest path must stay inside the archive.
    Legacy archives have no sidecar and therefore return an empty mapping.
    Invalid sidecars also fail closed as empty: their symlink paths remain in
    the main manifest and regular-file verification will reject them.
    """

    # 返回清单绑定的精确链接目标。目标是不透明 guest 字符串（含 venv 的绝对路径）。
    # 校验只比较 readlink() 结果、不跟随目标；仅要求链接自身路径在归档内。
    # 旧归档无边车返回空映射；非法边车也 fail-closed 为空——这些链接路径仍留在主
    # 清单中，普通文件校验会拒绝它们。
    path = os.path.join(host_dir, SYMLINKS)
    # 边车文件路径。
    if not os.path.exists(path):
        # 旧归档无边车。
        return {}
        # 空映射。
    try:
        with open(path, encoding="utf-8") as handle:
            # 读取 JSON 边车。
            record = json.load(handle)
            # 解析记录。
    except (OSError, UnicodeError, ValueError, TypeError):
        # 读取/解析失败：fail-closed。
        return {}
        # 空映射。
    links = record.get("links") if isinstance(record, dict) else None
    # 取 links 字段（类型不对则 None）。
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "links"}
            or record.get("schema_version") != 1
            or not isinstance(links, dict)):
        # 结构校验：恰好两个字段、版本为 1、links 是 dict。
        return {}
        # 不合法则空映射。
    result: dict[str, str] = {}
    # 校验后的链接表。
    for rel, target in links.items():
        # 逐条校验。
        if (not isinstance(rel, str) or not rel or rel.startswith("/")
                or ".." in rel.split("/") or not isinstance(target, str)
                or any(c in rel for c in "\0\n\r")
                or not target or "\0" in target):
            # 键必须是安全的归档内相对路径；目标必须是非空不含 NUL 的字符串。
            return {}
            # 任一不合法整体作废（fail-closed）。
        result[rel] = target
        # 记录。
    return dict(sorted(result.items()))
    # 返回排序后的链接表。


def read_roots(host_dir: str) -> list[str]:
    """Return the owned top-level guest roots for an archive.

    New captures record roots structurally so empty directories remain inside
    the wipe/exact-set boundary. Legacy captures predate that file and fall
    back to the top-level components represented by their file manifest.
    """
    # 返回归档拥有的顶层 guest 根目录。新捕获用结构化记录（保证空目录也纳入
    # wipe/精确集合边界）；旧捕获无该文件，退回用文件清单推出的顶层分量。
    p = os.path.join(host_dir, ROOTS)
    # 根目录记录文件路径。
    if not os.path.exists(p):
        # 旧捕获：从清单推顶层分量。
        return sorted({rel.split("/", 1)[0]
                       for rel in read_manifest(host_dir) if rel})
        # 去重排序的首级目录名。
    try:
        record = json.load(open(p, encoding="utf-8"))
        # 读取 JSON 记录。
    except (OSError, ValueError, TypeError):
        # 读取/解析失败：fail-closed。
        return []
        # 空列表。
    roots = record.get("roots") if isinstance(record, dict) else None
    # 取 roots 字段。
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "roots"}
            or record.get("schema_version") != 1
            or not isinstance(roots, list)
            or roots != sorted(set(roots))
            or not roots
            or any(not isinstance(d, str) or not _ROOT_NAME.fullmatch(d)
                   or d.startswith(".") for d in roots)):
        # 结构与内容校验：恰好两字段、版本 1、去重有序、非空、全部为安全顶层名。
        return []
        # 不合法则空列表。
    return roots
    # 返回根目录列表。


def write_owned_paths(host_dir: str, paths: list[str]) -> None:
    """Persist the immutable top-level paths a project may grade or mutate."""
    # 持久化项目可评分/可改动的不可变顶层路径。
    values = sorted(set(paths))
    # 去重排序。
    if (not values
            or any(not isinstance(p, str) or not _ROOT_NAME.fullmatch(p)
                   or p.startswith(".") for p in values)):
        # 非空且全部为安全顶层名。
        raise ValueError("owned project paths must be safe top-level names")
        # 不合法则报错。
    with open(os.path.join(host_dir, OWNED), "w", encoding="utf-8") as f:
        # 写入 OWNED 记录。
        json.dump({"schema_version": 1, "paths": values}, f,
                  indent=2, sort_keys=True)
        # 结构化保存。
        f.write("\n")
        # 结尾换行。


def read_owned_paths(host_dir: str) -> list[str]:
    """Return graded ownership scope; legacy archives fall back to roots."""
    # 返回可评分的所有权范围；旧归档退回根目录列表。
    p = os.path.join(host_dir, OWNED)
    # OWNED 文件路径。
    if not os.path.exists(p):
        # 旧归档：退回根目录。
        return read_roots(host_dir)
        # 用根目录充当所有权范围。
    try:
        record = json.load(open(p, encoding="utf-8"))
        # 读取 JSON 记录。
    except (OSError, ValueError, TypeError):
        # 读取/解析失败：fail-closed。
        return []
        # 空列表。
    paths = record.get("paths") if isinstance(record, dict) else None
    # 取 paths 字段。
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "paths"}
            or record.get("schema_version") != 1
            or not isinstance(paths, list)
            or paths != sorted(set(paths))
            or not paths
            or any(not isinstance(v, str) or not _ROOT_NAME.fullmatch(v)
                   or v.startswith(".") for v in paths)):
        # 与 ROOTS 相同的结构校验。
        return []
        # 不合法则空列表。
    return paths
    # 返回所有权路径列表。


def audit_captured_materials(host_dir: str,
                             authorized_instruction: str = "") -> list:
    """Fence captured names and payload bytes before an archive is replayed.

    Rich application artifacts are often binary, so opaque binaries cannot be
    rejected as a class without forbidding the work this pipeline is designed
    to learn. In target-aware mode we audit a loss-tolerant UTF-8 projection of
    every payload, which catches ordinary ASCII/UTF-8 signals embedded in a
    binary, as well as each archive member name. Compressed/encrypted content
    remains opaque and is therefore a documented detection boundary rather
    than a claim of semantic classification. Blind mode retains its historical
    strict-UTF-8 scan. Findings stay match-free.
    """
    # 在归档回放前对"名称与负载字节"做围栏（fence）检查。
    # 富应用工件常是二进制，无法整体拒绝二进制而不封杀本管线要学的对象。
    # 目标感知模式审计"容忍丢失的 UTF-8 投影"（可抓出嵌在二进制里的 ASCII/UTF-8
    # 信号）以及每个成员名。压缩/加密内容仍不透明——这是文档化的检测边界，而非
    # 语义分类声明。盲模式保留历史严格 UTF-8 扫描。结果不含匹配原文（match-free）。
    from tools.exam_fence import audit_text
    # 延迟导入围栏审计函数。
    tgz = os.path.join(host_dir, MATERIALS)
    # 归档路径。
    if not os.path.isfile(tgz):
        # 归档缺失本身就是发现项。
        return [{"file": MATERIALS, "kinds": ["missing-archive"]}]
        # 返回缺失发现。
    findings = []
    # 发现列表。
    try:
        with tarfile.open(tgz, mode="r:gz") as tf:
            # 打开归档。
            for member in tf.getmembers():
                # 遍历成员。
                if authorized_instruction:
                    # Target-aware mode strengthens the historical fixture
                    # check: ``errors=ignore`` preserves ASCII/UTF-8 substrings
                    # embedded in otherwise binary payloads, and the member
                    # name is part of the captured exposure surface too. It
                    # deliberately does not claim to unpack arbitrary formats.
                    # （中文）目标感知模式强化了历史夹具检查：errors=ignore 保留二进制
                    # 负载中嵌入的 ASCII/UTF-8 子串，成员名也属于暴露面；刻意不承诺
                    # 解包任意格式。
                    hits = audit_text(
                        member.name, mode="practice",
                        authorized_instruction=authorized_instruction)
                    # 先审计成员名。
                    if member.issym() or member.islnk():
                        # 链接条目再审计其目标。
                        hits += audit_text(
                            member.linkname, mode="practice",
                            authorized_instruction=authorized_instruction)
                        # 累计目标审计命中。
                    elif member.isfile():
                        # 普通文件审计其负载的容错 UTF-8 投影。
                        handle = tf.extractfile(member)
                        # 打开成员内容流。
                        if handle is not None:
                            # 流可用才审计。
                            text = handle.read().decode(
                                "utf-8", errors="ignore")
                            # 忽略解码错误，保留可读子串。
                            hits += audit_text(
                                text, mode="practice",
                                authorized_instruction=authorized_instruction)
                            # 累计负载审计命中。
                else:
                    # Preserve the frozen blind-mode behavior exactly: only
                    # wholly UTF-8 fixture payloads are in that regime's scan.
                    # （中文）盲模式行为逐字冻结：只有完全可解码为 UTF-8 的夹具负载才扫描。
                    if member.issym() or member.islnk():
                        # 链接：审计目标。
                        hits = audit_text(member.linkname, mode="practice")
                        # 记录命中。
                    elif member.isfile():
                        # 普通文件：严格 UTF-8 解码后才审计。
                        handle = tf.extractfile(member)
                        # 打开内容流。
                        if handle is None:
                            # 流不可用则跳过。
                            continue
                            # 下一成员。
                        try:
                            text = handle.read().decode(
                                "utf-8", errors="strict")
                            # 严格解码。
                        except UnicodeDecodeError:
                            # 解码失败（二进制）则按冻结行为跳过。
                            continue
                            # 下一成员。
                        hits = audit_text(text, mode="practice")
                        # 记录命中。
                    else:
                        # 目录等无需审计。
                        continue
                        # 下一成员。
                if hits:
                    # 有命中则记为发现。
                    findings.append({
                        "file": member.name,
                        "kinds": sorted({h.get("kind", "unknown")
                                         for h in hits})})
                    # 记录文件名与去重排序的命中类别。
    except (OSError, tarfile.TarError) as exc:
        # 归档不可读也记为发现（fail-closed）。
        return [{"file": MATERIALS,
                 "kinds": [f"unreadable-archive:{type(exc).__name__}"]}]
        # 返回不可读发现。
    return findings
    # 返回全部发现。


# ------------------------------------------------------------------ replay --
# --------------------------- 回放段 ---------------------------
def replay_project_materials(vm, host_dir: str) -> dict:
    """host_dir/materials.tgz -> guest, byte-verbatim, then VERIFY in-guest
    (sha256sum each manifest file vs the manifest). Returns {ok, mismatches}
    with mismatches = [{file, expected, got}] (got=None => missing/unread).
    Any not-ok result is an infra failure -> the loop records
    provision_failed; there is no re-generation fallback (§3 B7)."""
    # 把宿主 materials.tgz 逐字节推回 guest，然后在 guest 内校验（逐文件 sha256
    # 对照清单）。返回 {ok, mismatches}；mismatches 元素 {file, expected, got}
    # （got=None 表示缺失/不可读）。任何 not-ok 都是基础设施失败 -> 主循环记
    # provision_failed；没有再生成回退（§3 B7）。
    tgz = os.path.join(host_dir, MATERIALS)
    # 归档路径。
    manifest = read_manifest(host_dir)
    # 读取清单。
    roots = read_roots(host_dir)
    # 读取根目录记录。
    if not manifest or not roots or not os.path.exists(tgz):
        # 三者缺一不可。
        log.warning("replay: missing %s in %s",
                    MATERIALS if not os.path.exists(tgz) else MANIFEST,
                    host_dir)
        # 记录缺失项。
        return {"ok": False, "mismatches": [],
                "error": "missing materials, manifest, or project-root state"}
        # 返回失败。
    archive_bytes = os.path.getsize(tgz)
    # 归档宿主侧字节数。
    archive_digest = hashlib.sha256()
    # 归档摘要器。
    with open(tgz, "rb") as source:
        # 逐块读取归档。
        for block in iter(lambda: source.read(1 << 20), b""):
            # 1MB 分块。
            archive_digest.update(block)
            # 累积哈希。
    archive_sha256 = archive_digest.hexdigest()
    # 归档整体 SHA-256（传输前后对照）。
    # wipe the captured top-level dirs so replay restores EXACTLY the
    # captured file set (byte-verbatim by construction, no stale extras)
    # （中文）先擦除已捕获的顶层目录，使回放"恰好"恢复捕获时的文件集合
    # （按构造逐字节一致，无陈旧多余文件）。
    wipe = " ".join(shlex.quote(f"{GUEST_HOME}/{d}") for d in roots)
    # 拼出各根目录的绝对路径（安全引用）。
    wipe_out = vm.run_command(
        f"rm -rf {GUEST_B64_IN} {GUEST_TGZ_IN} {GUEST_EXPECTED} "
        f"{GUEST_ACTUAL} {wipe} ; echo PROJECT_WIPE_RC=$?",
        timeout=120)
    # 在 guest 内删除临时文件与全部项目根目录，并回显退出码。
    if "PROJECT_WIPE_RC=0" not in (wipe_out or ""):
        # 擦除失败：不能回放（否则可能残留陈旧文件）。
        log.warning("replay wipe failed (out=%r)", (wipe_out or "")[:160])
        # 记录擦除失败输出。
        return {"ok": False, "mismatches": [],
                "error": "could not wipe prior guest project state"}
        # 返回失败。
    push_file = getattr(vm, "push_file", None)
    # 探测 VM 是否支持流式推送通道。
    if callable(push_file):
        # 支持则走首选通道。
        pushed = push_file(tgz, GUEST_TGZ_IN)
        # 流式上传归档。
        if isinstance(pushed, tuple):
            # 返回 (ok, note) 二元组。
            push_ok, push_note = pushed
            # 解包。
        else:
            push_ok, push_note = bool(pushed), ""
            # 兼容只返回布尔的适配器。
        if not push_ok:
            # 上传失败。
            log.warning("replay streaming upload failed: %s", push_note)
            # 记录失败说明。
            return {"ok": False, "mismatches": [],
                    "error": "streaming artifact upload failed"}
            # 返回失败。
    else:
        # Compatibility for minimal/legacy VM adapters.  This is a transport
        # frame size, never a total artifact-size ceiling.
        # （中文）最小/旧 VM 适配器的兼容通道。这只是传输帧大小，绝非工件总大小上限。
        with open(tgz, "rb") as source:
            # 打开归档。
            while True:
                # 逐块读取。
                raw = source.read((_CHUNK // 4) * 3)
                # 读取 3/4 块大小的原始字节（保证 base64 后不超块）。
                if not raw:
                    # 读完退出。
                    break
                    # 跳出循环。
                chunk = base64.b64encode(raw).decode("ascii")
                # 编码为 ASCII base64。
                pushed = vm.run_command(
                    f"printf %s '{chunk}' >> {GUEST_B64_IN}", timeout=30)
                # 追加写入 guest 内的 b64 暂存文件。
                if (pushed or "").startswith(("[channel error:",
                                                "[command timed out")):
                    # 通道错误或超时：失败。
                    return {"ok": False, "mismatches": [],
                            "error": "chunked artifact upload failed"}
                    # 返回失败。
        decoded = vm.run_command(
            f"base64 -d {GUEST_B64_IN} > {GUEST_TGZ_IN} "
            f"2>/tmp/proj_err; echo DECODE_RC=$?", timeout=120)
        # 在 guest 内解码 b64 还原 tar。
        if "DECODE_RC=0" not in (decoded or ""):
            # 解码失败。
            return {"ok": False, "mismatches": [],
                    "error": "chunked artifact decode failed"}
            # 返回失败。

    size_out = vm.run_command(f"stat -c %s {GUEST_TGZ_IN}", timeout=30)
    # 查询 guest 内 tar 的字节数。
    sha_out = vm.run_command(f"sha256sum {GUEST_TGZ_IN}", timeout=120)
    # 计算 guest 内 tar 的 SHA-256。
    try:
        guest_bytes = int((size_out or "").strip().splitlines()[0])
        # 解析字节数。
    except (ValueError, IndexError):
        # 解析失败记 -1。
        guest_bytes = -1
    match = re.search(r"(?m)^([0-9a-f]{64})\s+", sha_out or "")
    # 提取 guest 侧哈希。
    guest_sha256 = match.group(1) if match else ""
    # 无则空串。
    if guest_bytes != archive_bytes or guest_sha256 != archive_sha256:
        # 传输前后字节级对照：任一不符即失败。
        log.warning("replay archive transport mismatch: bytes %s/%s sha %s/%s",
                    guest_bytes, archive_bytes, guest_sha256, archive_sha256)
        # 记录不匹配详情。
        return {"ok": False, "mismatches": [],
                "error": "uploaded archive failed byte verification"}
        # 返回失败。

    out = vm.run_command(
        f"tar xzf {GUEST_TGZ_IN} -C {GUEST_HOME} 2>>/tmp/proj_err; "
        f"echo SYNC_RC=$?", timeout=120)
    # 在 guest 内解包 tar 到主目录。
    if "SYNC_RC=0" not in (out or ""):
        # 解包失败。
        log.warning("replay push/extract failed (out=%r)", (out or "")[:120])
        # 记录失败输出。
        return {"ok": False, "mismatches": [],
                "error": "push/extract failed in guest"}
        # 返回失败。
    verified = verify_project_materials(vm, host_dir)
    # 在 guest 内逐文件校验清单与精确文件集合。
    mismatches = verified.get("mismatches", [])
    # 取不匹配列表。
    if mismatches:
        # 有不匹配文件。
        log.warning("replay verify: %d/%d files mismatched (first: %s)",
                    len(mismatches), len(manifest), mismatches[0]["file"])
        # 记录不匹配数量与首个文件。
        return verified
        # 返回失败详情。
    if not verified.get("ok"):
        # 精确集合校验失败。
        log.warning("replay exact-set verify failed (out=%r)",
                    verified.get("set_verify", "")[:160])
        # 记录集合校验输出。
        return verified
        # 返回失败详情。
    return verified
    # 返回成功。


def verify_project_materials(vm, host_dir: str) -> dict:
    """Verify the live guest against a captured project without mutating it.

    This is the read-only counterpart to :func:`replay_project_materials`:
    every captured regular file must retain its exact hash, and no extra file
    or symlink may exist beneath any owned project root. It is suitable for
    detecting an Agent-side candidate mutation before accepting a handoff.
    """
    # 只读地校验 guest 与捕获项目一致：全部普通文件哈希精确一致，且拥有的根目录
    # 下不得有多余文件或符号链接。适合在接收交接前检测 Agent 侧对候选的篡改。
    manifest = read_manifest(host_dir)
    # 读取清单。
    symlinks = read_symlinks(host_dir)
    # 读取链接边车。
    roots = read_roots(host_dir)
    # 读取根目录记录。
    if not manifest or not roots:
        # 清单或根目录缺失：失败。
        return {"ok": False, "mismatches": [],
                "error": "missing manifest or project-root state"}
        # 返回失败。
    if any(manifest.get(rel) != _symlink_digest(target)
           for rel, target in symlinks.items()):
        # 边车中每个链接都必须被主清单以链接域哈希绑定（防边车与清单不一致）。
        return {"ok": False, "mismatches": [],
                "error": "symlink sidecar is not bound by the manifest"}
        # 返回失败。
    regular = {rel: digest for rel, digest in manifest.items()
               if rel not in symlinks}
    # 从清单中剔除链接条目，得到普通文件集合。
    mismatches = _verify_in_guest(vm, regular)
    # 在 guest 内逐文件校验哈希。
    mismatches.extend(_verify_symlinks_in_guest(vm, symlinks))
    # 追加链接身份校验结果。
    if mismatches:
        # 有不匹配：返回失败。
        return {"ok": False, "mismatches": mismatches}
        # 返回详情。
    exact = _verify_exact_file_set(vm, manifest, roots)
    # 精确集合校验：不多不少。
    if not exact.get("ok"):
        # 集合不符。
        return {"ok": False, "mismatches": [],
                "error": "live project file set is not exact",
                "set_verify": exact.get("output", "")[:500]}
        # 返回失败与截断的输出。
    return {"ok": True, "mismatches": []}
    # 全部一致：成功。


def _verify_in_guest(vm, manifest: dict) -> list:
    """sha256sum each manifest file in-guest; [{file, expected, got}] for
    every file whose hash differs or that cannot be read."""
    # 在 guest 内对清单文件批量 sha256sum；哈希不符或不可读的文件返回不匹配记录。
    rels = sorted(manifest)
    # 排序后的相对路径。
    mismatches = []
    # 不匹配列表。
    for i in range(0, len(rels), _VERIFY_BATCH):
        # 按批量分批校验（传输批量，不是输出上限）。
        batch = rels[i:i + _VERIFY_BATCH]
        # 当前批次。
        args = " ".join(shlex.quote(r) for r in batch)
        # 安全引用全部路径。
        out = vm.run_command(
            f"cd {GUEST_HOME} && sha256sum {args} 2>&1",
            timeout=300, cap=0) or ""
        # 在 guest 内计算该批哈希（cap=0 不截断输出）。
        got = {}
        # 实际哈希映射。
        for line in out.splitlines():
            # 逐行解析。
            m = _SHA_LINE.match(line.strip())
            # 匹配 sha256sum 行。
            if m:
                # 命中则记录。
                got[m.group(2)] = m.group(1)
                # 路径 -> 实际哈希。
        for rel in batch:
            # 逐一对照期望。
            if got.get(rel) != manifest[rel]:
                # 哈希不符或文件缺失/不可读。
                mismatches.append({"file": rel, "expected": manifest[rel],
                                   "got": got.get(rel)})
                # 记录期望与实际（None 表示不可读）。
    return mismatches
    # 返回全部不匹配。


def _verify_symlinks_in_guest(vm, symlinks: dict[str, str]) -> list:
    """Verify link identity without following either valid or broken links."""

    # 校验链接身份：无论链接有效或悬空都不跟随目标。
    mismatches = []
    # 不匹配列表。
    probe = (
        "import base64,json,os,sys;"
        "p,t=json.loads(base64.b64decode(sys.argv[1]));"
        "ok=os.path.islink(p) and os.readlink(p)==t;"
        "print('PROJECT_LINK_RC='+('0' if ok else '1'))")
    # guest 内探针脚本：解码参数后校验"是链接且 readlink 精确等于目标"。
    for rel, target in sorted(symlinks.items()):
        # 逐条校验。
        payload = base64.b64encode(json.dumps(
            [rel, target], ensure_ascii=False).encode("utf-8")).decode("ascii")
        # 把 (路径, 目标) 编码为 base64 参数，避免引号/转义问题。
        out = vm.run_command(
            f"cd {GUEST_HOME} && python3 -c {shlex.quote(probe)} "
            f"{shlex.quote(payload)}",
            timeout=60) or ""
        # 在 guest 内运行探针。
        if "PROJECT_LINK_RC=0" not in out:
            # 不是链接或目标不符：记不匹配。
            mismatches.append({
                "file": rel,
                "expected": _symlink_digest(target),
                "got": None,
            })
            # got=None 表示链接身份校验失败。
    return mismatches
    # 返回全部不匹配。


def _verify_exact_file_set(vm, manifest: dict, roots: list[str]) -> dict:
    """Require exactly the regular manifest and no other non-directories."""
    # 要求 guest 内恰好是清单中的非目录条目集合，无任何多余。
    expected = ("\n".join(sorted(manifest)) + "\n").encode("utf-8")
    # 期望列表文件内容（排序路径逐行）。
    encoded = base64.b64encode(expected).decode("ascii")
    # base64 编码以便安全传输。
    vm.run_command(
        f"rm -f {GUEST_B64_IN} {GUEST_EXPECTED} {GUEST_ACTUAL}", timeout=30)
    # 清理 guest 内旧临时文件。
    for i in range(0, len(encoded), _CHUNK):
        # 分块追加写入。
        chunk = encoded[i:i + _CHUNK]
        # 当前块。
        vm.run_command(
            f"printf %s '{chunk}' >> {GUEST_B64_IN}", timeout=30)
        # 追加到 b64 暂存。
    args = " ".join(shlex.quote(top) for top in roots)
    # 安全引用根目录参数。
    out = vm.run_command(
        f"base64 -d {GUEST_B64_IN} > {GUEST_EXPECTED} && "
        f"cd {GUEST_HOME} && "
        f"find -- {args} ! -type d -print | "
        f"LC_ALL=C sort > {GUEST_ACTUAL} && "
        f"cmp -s {GUEST_EXPECTED} {GUEST_ACTUAL}; "
        f"echo PROJECT_SET_RC=$?",
        timeout=300) or ""
    # 在 guest 内：解码期望列表 -> find 列出各根下全部非目录项（排序）-> 逐一比对。
    return {"ok": "PROJECT_SET_RC=0" in out, "output": out}
    # 返回集合校验结果与原始输出。
