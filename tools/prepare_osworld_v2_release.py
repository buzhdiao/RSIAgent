#!/usr/bin/env python3
"""Prepare and verify the benchmark inputs pinned by a RSIAgent baseline lock."""
# 准备并校验"RSIAgent 基线锁（baseline lock）"钉死的基准输入。
# 任务源与资产快照都必须与锁文件逐字节一致，否则拒绝（PreflightError）。
from __future__ import annotations

import argparse
# argparse：命令行参数解析。
from datetime import datetime, timezone
# datetime：生成 UTC 时间戳（资产标记 created_at）。
import hashlib
# hashlib：发布清单的 SHA-256 校验。
import json
# json：写资产标记（release marker）。
import os
# os：原子替换文件。
from pathlib import Path
# Path：路径对象。
import subprocess
# subprocess：调用 OSWorld 的下载脚本。
import sys
# sys：调整模块搜索路径。


REPO = Path(__file__).resolve().parents[1]
# 仓库根目录（脚本上一级）。
if str(REPO) not in sys.path:
    # 仓库根尚未在搜索路径中。
    sys.path.insert(0, str(REPO))
    # 加入以便导入 benchmarks 包。

from benchmarks.osworld.assets import (  # noqa: E402
    PreflightError,
    asset_content_identity,
    read_json,
    task_content_identity,
)
# 延迟导入共享工具（在 sys.path 调整后；保留原 noqa 注释）：
# 预检错误、资产内容指纹、JSON 读取、任务内容指纹。


DEFAULT_LOCK = (
    REPO / "config/osworld/baseline.lock.json")
# 缺省基线锁文件路径。
DEFAULT_OSWORLD_ROOT = REPO.parent / "OSWorld-V2"
# 缺省 OSWorld-V2 检出根（与仓库平级）。


def _git(repo: Path, *args: str) -> str:
    # 在指定仓库执行只读 git 命令并返回输出。
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True,
            stderr=subprocess.STDOUT).strip()
        # 捕获 stdout+stderr 的输出并去首尾空白。
    except (OSError, subprocess.CalledProcessError) as exc:
        # git 不可用或命令失败：统一转为预检错误。
        raise PreflightError(f"cannot inspect Git checkout: {repo}") from exc
        # 保留原始异常链。


def _run(command: list[str], cwd: Path) -> None:
    # 带回显地运行子命令（失败即抛 CalledProcessError）。
    print("+ " + " ".join(command), flush=True)
    # 先回显将要执行的命令。
    subprocess.run(command, cwd=cwd, check=True)
    # 在 cwd 下运行，失败即抛错。


def _write_json_atomic(path: Path, value: dict) -> None:
    # 原子写 JSON 文件（先写临时文件再改名）。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保父目录存在。
    temporary = path.with_name(path.name + ".tmp")
    # 临时文件路径。
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # 写入排序缩进的 JSON（带结尾换行）。
    os.replace(temporary, path)
    # 原子替换目标文件。


def _utc_now() -> str:
    # 当前 UTC 时间的 ISO 字符串（以 Z 结尾）。
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # 把 +00:00 后缀替换为 Z。


def _require_remote_revision(metadata: dict) -> None:
    # 校验远端仓库在指定 tag 上解析出的提交与锁一致。
    from huggingface_hub import HfApi
    # 延迟导入 HF API（仅此函数需要）。

    observed = HfApi().repo_info(
        repo_id=str(metadata["repository"]),
        repo_type=str(metadata.get("repo_type", "dataset")),
        revision=str(metadata["tag"]),
    ).sha
    # 查询远端仓库该 tag 当前指向的提交。
    if observed != metadata["commit"]:
        # 远端 tag 被移动：拒绝准备。
        raise PreflightError(
            f"{metadata['repository']}@{metadata['tag']} resolved to {observed}, "
            f"not locked commit {metadata['commit']}")
        # 报告期望与观测提交。


def main() -> int:
    # 主流程：校验锁 -> 下载任务与资产 -> 内容指纹校验 -> 写资产标记。
    parser = argparse.ArgumentParser(description=__doc__)
    # 参数解析器（描述取模块 docstring）。
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    # 基线锁文件路径。
    parser.add_argument("--osworld-root", type=Path, default=DEFAULT_OSWORLD_ROOT)
    # OSWorld-V2 检出根。
    parser.add_argument(
        "--verify-only", action="store_true",
        help="verify an already-prepared release without downloading anything")
    # 开关：只校验已准备好的发布，不做任何下载。
    parser.add_argument(
        "--refresh-assets", action="store_true",
        help="replace the dedicated release asset directory before downloading")
    # 开关：下载前清空重建专用资产目录。
    parser.add_argument("--dry-run", action="store_true")
    # 开关：空跑（只打印将执行的下载命令）。
    args = parser.parse_args()
    # 解析参数。

    lock_path = args.lock.resolve()
    # 锁文件绝对路径。
    osworld_root = args.osworld_root.resolve()
    # OSWorld 根绝对路径。
    lock = read_json(lock_path)
    # 读取锁文件。
    release = str(lock.get("benchmark_release", ""))
    # 锁定的基准发布名。
    if not release:
        # 缺少发布名：拒绝。
        raise PreflightError("the selected lock has no benchmark_release")
        # 预检错误。

    expected_osworld = str((lock.get("osworld_code") or {}).get("commit", ""))
    # 锁定的 OSWorld 代码提交。
    observed_osworld = _git(osworld_root, "rev-parse", "HEAD")
    # 观测本地 OSWorld 检出的 HEAD。
    if observed_osworld != expected_osworld:
        # 本地代码不是锁定版本：拒绝。
        raise PreflightError(
            "switch OSWorld-V2 to the locked release before preparation: "
            f"expected {expected_osworld}, observed {observed_osworld}")
        # 提示先切换到锁定发布。

    task_meta = lock.get("task_files") or {}
    # 锁中的任务源元数据。
    asset_meta = lock.get("task_assets") or {}
    # 锁中的资产元数据。
    _require_remote_revision(task_meta)
    # 校验任务源远端 tag 指向锁定提交。
    _require_remote_revision(asset_meta)
    # 校验资产远端 tag 指向锁定提交。

    release_manifest = (
        osworld_root / "benchmark_releases" / f"{release}.json")
    # 发布清单文件路径。
    if not release_manifest.is_file():
        # 清单缺失：拒绝。
        raise PreflightError(
            f"OSWorld release manifest is missing: {release_manifest}")
        # 预检错误。
    release_meta = lock.get("release_manifest") or {}
    # 锁中的发布清单元数据。
    expected_manifest_hash = str(release_meta.get("sha256", ""))
    # 期望的清单哈希。
    observed_manifest_hash = hashlib.sha256(
        release_manifest.read_bytes()).hexdigest()
    # 观测的清单哈希。
    if observed_manifest_hash != expected_manifest_hash:
        # 清单与锁不一致：拒绝。
        raise PreflightError(
            "OSWorld release manifest does not match the lock: "
            f"expected {expected_manifest_hash}, observed {observed_manifest_hash}")
        # 预检错误。

    task_root = osworld_root / "evaluation_examples/task_class"
    # 任务源目标目录。
    asset_root = osworld_root / str(asset_meta["local_dir"])
    # 资产快照目标目录。
    marker_name = str(asset_meta.get(
        "release_marker", ".rsiagent_osworld_release.json"))
    # 资产发布标记文件名。
    marker_path = asset_root / marker_name
    # 标记文件路径。

    if not args.verify_only:
        # 非只校验模式：执行下载准备。
        if asset_root.exists() and any(asset_root.iterdir()) and not args.refresh_assets:
            # 资产目录非空且未要求刷新：先验证既有目录归属。
            if not marker_path.is_file():
                # 非空却无发布标记：状态不明，拒绝。
                raise PreflightError(
                    "the dedicated asset directory is non-empty but has no valid "
                    "release marker; inspect it or pass --refresh-assets explicitly: "
                    + str(asset_root))
                # 提示人工检查或显式刷新。
            prior_marker = read_json(marker_path)
            # 读取既有标记。
            if (prior_marker.get("benchmark_release") != release
                    or prior_marker.get("commit") != asset_meta.get("commit")):
                # 既有目录属于其他发布：拒绝（防混用）。
                raise PreflightError(
                    "the asset directory belongs to another release; pass "
                    "--refresh-assets explicitly: " + str(asset_root))
                # 提示显式刷新。
        task_command = [
            str(Path(sys.executable).resolve()),
            str(osworld_root / "scripts/tools/download_osworld_v2_tasks.py"),
            "--benchmark-release", str(release_manifest),
            "--target-dir", str(task_root),
        ]
        # 任务源下载命令（用当前解释器，指向发布清单与目标目录）。
        asset_command = [
            str(Path(sys.executable).resolve()),
            str(osworld_root / "scripts/tools/download_osworld_v2_assets.py"),
            "--benchmark-release", str(release_manifest),
            "--target-dir", str(asset_root),
        ]
        # 资产下载命令（同上）。
        if args.refresh_assets:
            # 刷新模式：下载脚本先清空目标目录。
            asset_command.append("--clean")
            # 追加 --clean。
        if args.dry_run:
            # 空跑模式：两个命令都加 --dry-run。
            task_command.append("--dry-run")
            # 任务命令空跑。
            asset_command.append("--dry-run")
            # 资产命令空跑。
        _run(task_command, osworld_root)
        # 执行任务源下载。
        _run(asset_command, osworld_root)
        # 执行资产下载。
        if args.dry_run:
            # 空跑到此为止。
            return 0
            # 成功返回。

    task_identity = task_content_identity(task_root)
    # 计算任务源内容指纹（树哈希、总字节、任务数）。
    expected_task_identity = {
        "content_tree_sha256": task_meta.get("content_tree_sha256"),
        "total_bytes": task_meta.get("total_bytes"),
        "task_count": int(lock["task_count"]),
    }
    # 期望的任务源指纹（来自锁）。
    if task_identity != expected_task_identity:
        # 下载的任务源与锁不符：拒绝。
        raise PreflightError(
            "downloaded task sources do not match the lock: "
            f"expected={expected_task_identity}, observed={task_identity}")
        # 预检错误。

    if args.verify_only:
        # 只校验模式：核对资产标记与内容指纹。
        if not marker_path.is_file():
            # 标记缺失：拒绝。
            raise PreflightError(f"asset release marker is missing: {marker_path}")
            # 预检错误。
        marker = read_json(marker_path)
        # 读取标记。
        expected_marker = {
            "benchmark_release": release,
            "repository": asset_meta.get("repository"),
            "repo_type": asset_meta.get("repo_type"),
            "tag": asset_meta.get("tag"),
            "commit": asset_meta.get("commit"),
        }
        # 期望标记字段（来自锁）。
        mismatch = {
            key: {"expected": value, "observed": marker.get(key)}
            for key, value in expected_marker.items()
            if marker.get(key) != value}
        # 找出所有不一致的字段。
        if mismatch:
            # 任一字段不符：拒绝。
            raise PreflightError(f"asset release marker mismatch: {mismatch}")
            # 预检错误。
        observed_asset_identity = asset_content_identity(asset_root, marker_name)
        # 计算资产目录当前内容指纹（排除标记文件自身）。
        if marker.get("content_identity") != observed_asset_identity:
            # 资产在准备后发生了变化：拒绝。
            raise PreflightError(
                "asset snapshot changed after preparation: "
                f"expected={marker.get('content_identity')}, "
                f"observed={observed_asset_identity}")
            # 预检错误。
    else:
        # 准备模式：写新的资产发布标记。
        content_identity = asset_content_identity(asset_root, marker_name)
        # 计算刚下载资产的内容指纹。
        if content_identity["file_count"] == 0:
            # 空快照：拒绝。
            raise PreflightError("asset download produced an empty snapshot")
            # 预检错误。
        marker = {
            "schema_version": 1,
            "created_at": _utc_now(),
            "benchmark_release": release,
            "repository": asset_meta["repository"],
            "repo_type": asset_meta.get("repo_type", "dataset"),
            "tag": asset_meta["tag"],
            "commit": asset_meta["commit"],
            "content_identity": content_identity,
        }
        # 组装发布标记：出处（仓库/类型/tag/提交）、时间与内容指纹。
        _write_json_atomic(marker_path, marker)
        # 原子写入标记。

    print(f"benchmark_release={release}")
    # 输出发布名。
    print(f"osworld_commit={observed_osworld}")
    # 输出 OSWorld 代码提交。
    print(f"task_content_tree_sha256={task_identity['content_tree_sha256']}")
    # 输出任务源树哈希。
    print(f"asset_root={asset_root}")
    # 输出资产目录。
    print(f"asset_content_tree_sha256={marker['content_identity']['content_tree_sha256']}")
    # 输出资产树哈希。
    print("release_preparation=PASS")
    # 输出准备通过。
    return 0
    # 成功返回。


if __name__ == "__main__":
    # 命令行入口。
    try:
        raise SystemExit(main())
        # 运行主流程并透传退出码。
    except (PreflightError, subprocess.CalledProcessError) as exc:
        # 预检失败或子命令失败：统一报错退出码 2。
        print(f"PREPARATION ERROR: {exc}", file=sys.stderr)
        # 向 stderr 打印错误。
        raise SystemExit(2)
        # 以 2 退出（区别于 pytest 风格的 1）。
