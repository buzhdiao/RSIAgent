"""Pinned OSWorld task and asset content identities."""
# 【中文注释·模块定位】固化（pinned）OSWorld 任务源码与任务资产的内容身份：
# 通过逐文件 sha256 构造"内容树哈希"，在预检（preflight）阶段把基准发布版钉死，
# 防止学习/评测所用任务集在任何环节被悄悄替换（供应链完整性）。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值，兼容新类型语法。

import hashlib
# 导入 hashlib：计算文件的 sha256 内容摘要。
import json
# 导入 json：读取并校验 JSON 文件。
from pathlib import Path
# 导入 Path：以路径对象统一处理任务源码与资产目录。

EXPECTED_TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))
# 期望的任务编号全集：task_001 到 task_108，共 108 个 OSWorld-v2 任务源文件。


class PreflightError(RuntimeError):
    # 【中文注释】预检失败专用异常：任务源缺失、哈希不可读、JSON 损坏等均抛出此错误，
    # 让调用方能在进入昂贵流程前快速失败（fail-fast）。
    pass
    # 仅继承 RuntimeError，不添加额外行为。


def sha256_bytes(data: bytes) -> str:
    # 【中文注释】计算字节串的 sha256 十六进制摘要：用于对拼接后的清单行做内容树哈希。
    return hashlib.sha256(data).hexdigest()
    # 一次性对整段字节求 sha256 并返回 hex 字符串。


def sha256_file(path: Path) -> str:
    # 【中文注释】分块计算单个文件的 sha256：按 1MB 流式读取，避免大文件占用内存。
    digest = hashlib.sha256()
    # 初始化 sha256 哈希对象。
    with path.open("rb") as handle:
        # 以二进制只读方式打开文件。
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            # 每次读取 1MB，直到读到空字节串（文件结束）。
            digest.update(chunk)
            # 把当前块喂进哈希，滚动累计。
    return digest.hexdigest()
    # 返回整个文件的 sha256 hex 摘要。


def task_content_identity(task_root: Path) -> dict:
    """Return a release-sensitive identity for the complete task source set."""
    # 【中文注释】计算完整任务源码集的"发布敏感"内容身份：对 108 个任务文件逐一求
    # (文件名, 字节数, sha256) 清单行，再对清单整体求哈希；任何一处改动都会改变结果。
    rows = []
    # 收集每个任务文件的清单行。
    total_bytes = 0
    # 累计全部任务源码的总字节数。
    for task in EXPECTED_TASKS:
        # 逐个遍历期望任务编号，保证集合完整且顺序确定。
        path = task_root / f"{task}.py"
        # 拼出该任务的源码文件路径。
        if not path.is_file():
            # 缺任一任务源码都视为基准被破坏，预检直接失败。
            raise PreflightError(f"task source is missing: {path}")
        size = path.stat().st_size
        # 读取文件大小作为清单的一部分（大小不匹配也能检出）。
        total_bytes += size
        # 累加总字节数。
        rows.append(f"{path.name}\t{size}\t{sha256_file(path)}")
        # 记录一行"文件名\t大小\tsha256"，构成确定性清单。
    # 返回内容树哈希与统计信息作为该发布版的身份指纹：
    # content_tree_sha256 = 把所有清单行用换行拼接后整体求 sha256（内容树哈希）；
    # total_bytes = 任务源码总字节数；task_count = 任务文件数量（应为 108）。
    return {
        "content_tree_sha256": sha256_bytes("\n".join(rows).encode("utf-8")),
        "total_bytes": total_bytes,
        "task_count": len(rows),
    }


def asset_content_identity(asset_root: Path, marker_name: str) -> dict:
    """Hash the prepared asset snapshot while excluding downloader metadata."""
    # 【中文注释】计算已准备好的资产快照内容身份：遍历资产目录全部文件求清单哈希，
    # 但排除下载器元数据（.cache 目录与发布标记文件本身），使身份只反映任务资产内容。
    rows = []
    # 收集每个资产文件的清单行。
    total_bytes = 0
    # 累计资产总字节数。
    for path in sorted(asset_root.rglob("*")):
        # 递归枚举资产目录下所有路径，排序保证遍历顺序确定（哈希才可复现）。
        if not path.is_file():
            # 跳过目录等非普通文件条目。
            continue
        relative = path.relative_to(asset_root)
        # 计算相对路径，用于清单记录与排除规则判断。
        if relative.parts and relative.parts[0] == ".cache":
            # 排除下载缓存目录：它随下载过程变化，不属于发布内容。
            continue
        if relative.as_posix() == marker_name:
            # 排除发布标记文件自身：标记记录的是身份，不能参与身份计算（自我引用）。
            continue
        size = path.stat().st_size
        # 记录该资产文件大小。
        total_bytes += size
        # 累加总字节数。
        rows.append(f"{relative.as_posix()}\t{size}\t{sha256_file(path)}")
        # 记录一行"相对路径\t大小\tsha256"。
    # 返回资产快照的内容树哈希与统计信息：
    # content_tree_sha256 = 全部清单行拼接后的整体 sha256（资产内容树哈希）；
    # total_bytes = 资产总字节数；file_count = 参与哈希的资产文件数量。
    return {
        "content_tree_sha256": sha256_bytes("\n".join(rows).encode("utf-8")),
        "total_bytes": total_bytes,
        "file_count": len(rows),
    }


def read_json(path: Path) -> dict:
    # 【中文注释】安全读取 JSON 对象文件：任何 IO/编码/解析错误都折算成预检异常，
    # 且要求顶层必须是 JSON 对象（dict），否则视为基准文件损坏。
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # 以 UTF-8 读入并解析 JSON。
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # 捕获读文件失败、编码失败、解析失败三类错误。
        raise PreflightError(f"cannot read JSON: {path}") from exc
        # 统一折算为预检异常并保留原始原因链。
    if not isinstance(value, dict):
        # 顶层不是 JSON 对象则结构不符合约定。
        raise PreflightError(f"expected a JSON object: {path}")
    return value
    # 返回解析出的 JSON 对象。
