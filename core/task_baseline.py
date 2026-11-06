"""Trusted post-setup/pre-Actor baseline capture for unified target runs.

This module records mechanical persistent state only.  It contains no task-specific
checks, evaluator, golden answer, semantic classifier, or stopping policy.  The
Verifier Agent independently inspects the same live S0 and decides what matters.
"""

from __future__ import annotations

# hashlib：文件/记录的 SHA-256 指纹
import hashlib
# json：清单序列化
import json
from typing import Any


# 嵌入式捕获程序输出的起止哨兵标记
_BEGIN = "__RSIAGENT_TASK_BASELINE_BEGIN__"
_END = "__RSIAGENT_TASK_BASELINE_END__"


def _capture_program() -> str:
    # json.dumps transports arbitrary filenames without line-oriented ambiguity.
    # ~/.memory is a harness-attached Actor policy surface, not task-start state.
    # （中文）下面是一段在客户机内执行的捕获程序源码（原样字符串，不在此展开注释）：
    # 它遍历 /home/user 下全部持久状态（排除 Actor 私有的 ~/.memory），对每个条目
    # 记录路径/权限/属主/大小/mtime，普通文件再附内容 SHA-256；目录符号链接记录
    # 本身但不深入；读不了的条目记为 unreadable。输出夹在 BEGIN/END 哨兵之间。
    return r'''import hashlib
import json
import os
import pathlib
import stat

roots = [pathlib.Path("/home/user")]
excluded = {pathlib.Path("/home/user/.memory")}
entries = []

def excluded_path(path):
    return any(path == root or root in path.parents for root in excluded)

def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

for root in roots:
    if not root.exists():
        continue
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = pathlib.Path(current)
        visible_dirs = sorted(
            name for name in dirs
            if not excluded_path(current_path / name))
        symlink_dirs = [
            name for name in visible_dirs
            if (current_path / name).is_symlink()]
        dirs[:] = [
            name for name in visible_dirs
            if not (current_path / name).is_symlink()]
        # Real directories are recorded as `current_path` when walked; directory
        # symlinks are not followed and therefore must be recorded here.
        names = ["."] + symlink_dirs + sorted(files)
        for name in names:
            path = current_path if name == "." else current_path / name
            if excluded_path(path):
                continue
            try:
                info = path.lstat()
                record = {
                    "path": str(path),
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
                if stat.S_ISREG(info.st_mode):
                    record.update(type="file", sha256=digest_file(path))
                elif stat.S_ISDIR(info.st_mode):
                    record.update(type="directory")
                elif stat.S_ISLNK(info.st_mode):
                    record.update(type="symlink", target=os.readlink(path))
                else:
                    record.update(type="special")
            except Exception as exc:
                record = {
                    "path": str(path),
                    "type": "unreadable",
                    "error": type(exc).__name__,
                }
            entries.append(record)

entries.sort(key=lambda item: (item["path"], item["type"]))
print("__RSIAGENT_TASK_BASELINE_BEGIN__")
print(json.dumps({"entries": entries}, ensure_ascii=False,
                 sort_keys=True, separators=(",", ":")))
print("__RSIAGENT_TASK_BASELINE_END__")
'''


def _digest(entries: list[dict[str, Any]], *, exact: bool) -> str:
    # 对清单条目做规范化投影后计算 SHA-256
    projected = []
    for entry in entries:
        # 复制条目避免改动原始数据
        item = dict(entry)
        if not exact:
            # 非精确模式剔除易变字段（mtime/uid/gid），只反映内容与结构
            item.pop("mtime_ns", None)
            item.pop("uid", None)
            item.pop("gid", None)
        projected.append(item)
    # 稳定序列化（键排序、紧凑分隔符、UTF-8 字节）
    encoded = json.dumps(
        projected, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    # 返回十六进制摘要
    return hashlib.sha256(encoded).hexdigest()


# 捕获 S0（Actor 进入前）家目录全部持久状态基线：纯机械记录，无任务语义
def capture_task_start_baseline(vm) -> dict[str, Any]:
    """Capture all persistent state below the task user's home without a cap.

    The live Verifier can also inspect non-file application state during its
    orientation.  External-service state cannot be snapshotted universally by this
    local harness and is explicitly marked unavailable rather than guessed.
    """
    # 在客户机里以 python 执行捕获程序：1 小时超时、不截断输出（cap=0）
    trace = vm.run_script("python", _capture_program(), timeout=3600, cap=0)
    if trace.infra_fail or trace.exit_code != 0:
        # 基础设施故障或非零退出：可信基线捕获失败，直接报错
        raise RuntimeError(
            "trusted task-start baseline capture failed: " + trace.stdout)
    # 取完整标准输出
    output = trace.stdout or ""
    # 定位 BEGIN/END 哨兵，截出清单 JSON 主体
    start = output.find(_BEGIN + "\n")
    end = output.find("\n" + _END, start + len(_BEGIN) + 1)
    if start < 0 or end < 0:
        raise RuntimeError(
            "trusted task-start baseline capture returned no complete manifest")
    # 提取哨兵之间的原始 JSON
    raw = output[start + len(_BEGIN) + 1:end]
    parsed = json.loads(raw)
    entries = parsed.get("entries")
    if not isinstance(entries, list):
        # 清单结构必须包含 entries 列表
        raise RuntimeError("trusted task-start baseline manifest is malformed")
    # 组装基线记录：范围、排除项、外部服务状态声明、条目与两种摘要
    return {
        "schema_version": 1,
        # 捕获范围仅限任务用户家目录
        "scope": ["/home/user"],
        # 明确排除的 Actor 私有面
        "excluded_actor_private": ["/home/user/.memory"],
        # 外部服务状态本地无法快照，显式声明不可用而非臆测
        "external_service_state": "unavailable_to_local_baseline_capture",
        "entries": entries,
        "entry_count": len(entries),
        # 内容摘要（剔除易变字段）
        "content_sha256": _digest(entries, exact=False),
        # 精确摘要（含 mtime/uid/gid）
        "exact_sha256": _digest(entries, exact=True),
    }


# 绑定一个干净 S0 的可复现任务可见身份（不含评审状态）
def stable_task_start_identity(
        *, task_id: str, instruction: str, setup_projection: str,
        setup_manifest: dict[str, Any],
        target_input_fingerprint: str) -> dict[str, Any]:
    """Bind the reproducible, task-visible identity of one clean S0.

    The exhaustive home manifest intentionally contains volatile application and
    cache state, so it is an audit artifact rather than a clone-equivalence key.
    This digest instead binds the immutable task, reviewed setup implementation,
    and exact bytes of every registered input. It contains no evaluator state.
    """
    # 四个身份输入都必须是非空文本
    for label, value in (
            ("task_id", task_id),
            ("instruction", instruction),
            ("setup_projection", setup_projection),
            ("target_input_fingerprint", target_input_fingerprint)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"stable S0 {label} must be nonempty text")
    if not isinstance(setup_manifest, dict) or not setup_manifest:
        # setup 实现清单必须是非空对象
        raise ValueError("stable S0 setup_manifest must be a nonempty object")
    # 组装可复现身份记录：任务 id、指令哈希、setup 投影与清单、输入指纹
    record = {
        "schema_version": 1,
        "task_id": task_id,
        "instruction_sha256": hashlib.sha256(
            instruction.encode("utf-8")).hexdigest(),
        "setup_projection": setup_projection,
        "setup_manifest": setup_manifest,
        "target_input_fingerprint": target_input_fingerprint,
    }
    # 稳定序列化后取总哈希
    encoded = json.dumps(
        record, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    # 返回记录本体 + 总 sha256
    return {**record, "sha256": hashlib.sha256(encoded).hexdigest()}


# 模块公开 API
__all__ = ["capture_task_start_baseline", "stable_task_start_identity"]
