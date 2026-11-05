# 【中文注释·模块定位】不可变记忆快照的"规范内容哈希"。
# 冻结记忆（test-time memory freeze）要能被机械验证：给出一个快照，任何人都能用
# 同一算法算出同一个根哈希，确认"测期用的记忆和学习结束时的记忆逐字节一致"。
# 关键在于规范化（canonicalization）：同样的 JSON 数据不管键序/空白如何，都必须
# 产生唯一确定的自节序列，这样哈希才可复现。
"""Canonical content hashes for immutable memory snapshots."""

# 延迟注解求值（PEP 563）：dict[str, str] 等注解在低版本 Python 也可用。
from __future__ import annotations

# hashlib：SHA-256 摘要。
import hashlib
# json：规范化序列化。
import json
# re：SHA-256 十六进制格式校验。
import re
# Path：检查相对路径中是否包含 ".." 逃逸段。
from pathlib import Path
# Any/Mapping：类型注解（Mapping 只读映射接口，比 dict 更宽）。
from typing import Any, Mapping


# 记忆状态错误：清单不是规范 JSON 数据时抛出（区别于一般 IO 错误）。
class MemoryStateError(RuntimeError):
    """The memory manifest is not canonical JSON data."""


# SHA-256 十六进制串格式：64 位小写十六进制字符，fullmatch 严格整串匹配。
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    # 把任意 JSON 值渲染成"规范字节串"：
    #   sort_keys=True      键按字典序排序（键序不再影响哈希）
    #   separators=(",",":") 无多余空白（紧凑形式，空白数量不再影响哈希）
    #   ensure_ascii=False  直接输出 UTF-8（避免 \uXXXX 转义歧义）
    #   allow_nan=False     拒绝 NaN/Infinity（非标准 JSON，规范数据不应有）
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        # 不可 JSON 化的数据（集合/自定义对象/NaN）→ 记忆状态错误。
        raise MemoryStateError(f"state is not canonical JSON data: {exc}") from exc
    # newline=True 时追加换行（POSIX 文本文件惯例，供写盘场景）。
    return (rendered + ("\n" if newline else "")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 of canonical UTF-8 JSON (sorted keys, no insignificant space)."""

    # 【中文注释】规范 JSON 的 SHA-256：排序键 + 无无效空白。
    # 这是"冻结记忆哈希"的原子操作——任何状态变化都会改变此哈希。
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def tree_sha256(hashes: Mapping[str, str]) -> str:
    """Canonical root digest for a ``tree_hashes`` mapping."""

    # 【中文注释】记忆树的根哈希：输入是 {相对路径: 各文件 SHA-256} 映射。
    # 先逐项校验，再排序规范化，最后对整个映射做 canonical_sha256。
    normalized: dict[str, str] = {}
    for rel, digest in hashes.items():
        # 逐项校验：键必须是非空相对路径（不以 / 开头、不含 ".." 逃逸段），
        # 值必须是合法的 SHA-256 十六进制串。任何一项不合法就整体拒绝——
        # 哈希承诺必须建立在完全可信的输入上（fail closed）。
        if (
            not isinstance(rel, str)
            or not rel
            or rel.startswith("/")
            or ".." in Path(rel).parts
            or not _SHA256_RE.fullmatch(str(digest))
        ):
            raise MemoryStateError("invalid tree-hash mapping")
        normalized[rel] = str(digest)
    # dict(sorted(...)) 固定键序；canonical_sha256 产出可复现的根摘要。
    # 论文中的 frozen configuration/memory hashes 即由本函数（与
    # _memory_tree_sha256 组合）计算，写入状态文件供 sealed evaluation 审计。
    return canonical_sha256(dict(sorted(normalized.items())))
