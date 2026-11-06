#!/usr/bin/env python3
"""Orchestrate the three leakage-separated recursive-improvement phases.

Each invocation executes exactly one phase. This is intentional: official scores
created in Phase 3 cannot flow into a later learning phase in the same protocol
run. Agent stopping remains Agent-owned; the Phase-1 project budget is an external
budget over complete project/memory lifecycles only.
"""
# 【中文注释·模块定位】三阶段"泄漏隔离"递归自我改进协议的编排器：
# 每次调用只执行恰好一个阶段（phase1 BRS 广度探索 → phase2 DRS 深度定向练习 →
# phase3 sealed evaluation 密封评测）。刻意一次一阶段：Phase 3 产生的官方分数
# 不可能回流到同一次协议运行的后续学习阶段。智能体停止权始终归智能体所有；
# Phase-1 项目预算只是覆盖完整项目/记忆生命周期的外部预算。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值。

import argparse
# 导入 argparse：解析命令行参数。
import hashlib
# 导入 hashlib：协议/配置/分布文件的内容指纹。
import json
# 导入 json：协议、锁、结果与事件的序列化。
import os
# 导入 os：环境变量继承与文件原子替换、fsync。
from pathlib import Path
# 导入 Path：路径对象处理。
import re
# 导入 re：安全目录名白名单正则。
import shutil
# 导入 shutil：把协议文件复制进运行目录。
import subprocess
# 导入 subprocess：以子进程方式运行各阶段入口。
import sys
# 导入 sys：用当前解释器启动子进程。
import time
# 导入 time：事件 UTC 时间戳。
from typing import Any
# 导入 Any：协议字典等动态结构的类型标注。

from config.benchmark_runtime import configure_user_simulator
# 导入用户模拟器配置器：按基准锁绑定任务网站与模拟器凭据。
from config.runtime_paths import resolve_root, resolve_osworld_root
# 导入路径解析：仓库根与 OSWorld 根。
from core.self_evolving_loop import DEFAULT_PHASE2_STOP_POLICY, Phase2StopPolicy
# 导入 Phase-2 停止策略枚举与默认值（curriculum_review / verifier_pass）。
from explore.practice_loop import _atomic_install_memory, _manifest, _read_memory_tree
# 导入记忆设施：原子安装、清单构造、记忆树读取。
from explore.target_learning import _memory_tree_sha256
# 导入记忆树整体 sha256 计算（血脉校验用）。


RSIAGENT_ROOT = resolve_root()
# 仓库根目录。
OSWORLD_ROOT = resolve_osworld_root(repo_root=RSIAGENT_ROOT)
# OSWorld-V2 检出根目录。
RESULTS_ROOT = RSIAGENT_ROOT / "results" / "recursive_improvement"
# 协议运行公共结果根。
TASKS = tuple(f"task_{index:03d}" for index in range(1, 109))
# 全部 108 个任务 ID（task_001..task_108）。
TASK_RELEASES = {
    "osworld-v2-2026.08.08": frozenset(),
}
# 受审任务发布版登记表：发布名 → 该发布中被排除的任务集合（当前发布无排除项）。
BENCHMARK_PROVENANCE_PROFILES = {
    "osworld-v2-2026.08.08": (
        RSIAGENT_ROOT / "config/osworld/baseline.lock.json"),
}
# 发布版 → 基准溯源锁文件（Phase-2 运行需绑定该 profile）。
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")
# 安全目录名白名单：防路径注入。
_ACK = {
    "phase1": "RUN-RECURSIVE-IMPROVEMENT-PHASE1",
    "phase2": "RUN-RECURSIVE-IMPROVEMENT-PHASE2",
    "phase3": "RUN-RECURSIVE-IMPROVEMENT-PHASE3",
}
# 各阶段付费执行确认口令：--execute 必须逐字匹配对应口令。


class ProtocolError(RuntimeError):
    """The declared study violates a protocol or artifact boundary."""
    # 【中文注释】协议专用异常：声明的研究违反了协议约束或产物边界时抛出。
    # 仅继承 RuntimeError，不添加行为。


def _sha256(path: Path) -> str:
    # 【中文注释】分块计算文件 sha256：按 1MB 流式读取。
    digest = hashlib.sha256()
    # 初始化哈希对象。
    with path.open("rb") as handle:
        # 二进制只读打开。
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            # 每次读 1MB 直到文件尾。
            digest.update(chunk)
            # 滚动喂入哈希。
    return digest.hexdigest()
    # 返回 hex 摘要。


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    # 【中文注释】原子写 JSON：临时文件 + os.replace，保证读者只见完整新/旧内容。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保父目录存在。
    temporary = path.with_name(path.name + ".tmp")
    # 同目录临时文件。
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    # 确定性键序序列化写入。
    os.replace(temporary, path)
    # 原子改名替换。


def _read_object(path: Path) -> dict[str, Any]:
    # 【中文注释】读取 JSON 对象文件：读取/解析失败或顶层不是对象都折算为协议错误。
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # 读入并解析 JSON。
    except (OSError, json.JSONDecodeError) as exc:
        # 捕获 IO 与解析错误。
        raise ProtocolError(f"cannot read JSON object {path}: {exc}") from exc
        # 折算为协议错误并保留原因链。
    if not isinstance(value, dict):
        # 顶层必须是 JSON 对象。
        raise ProtocolError(f"expected a JSON object: {path}")
    return value
    # 返回解析结果。


def _resolve_file(value: Any, *, label: str) -> Path:
    # 【中文注释】把协议中的文件引用解析为真实存在的仓库内文件路径：
    # 相对路径以仓库根为基，strict 解析并要求确实是普通文件。
    if not isinstance(value, str) or not value.strip():
        # 引用必须是非空字符串。
        raise ProtocolError(f"{label} must name one repository file")
    raw = Path(value).expanduser()
    # 展开 ~ 缩写。
    path = raw if raw.is_absolute() else RSIAGENT_ROOT / raw
    # 绝对路径直用；相对路径相对仓库根。
    try:
        path = path.resolve(strict=True)
        # strict 解析：必须真实存在。
    except OSError as exc:
        # 不存在等 IO 错误折算为协议错误。
        raise ProtocolError(f"{label} does not exist: {path}") from exc
    if not path.is_file():
        # 必须是普通文件（拒绝目录等）。
        raise ProtocolError(f"{label} is not a file: {path}")
    return path
    # 返回解析后的文件路径。


def _safe_run_root(run_name: str) -> Path:
    # 【中文注释】校验 run_name 为安全 basename，且结果根未逃出 results/recursive_improvement。
    if (not isinstance(run_name, str) or not _SAFE_NAME.fullmatch(run_name)
            or run_name in {".", ".."}):
        # 非字符串、不匹配白名单、或为 "."/".." 都拒绝。
        raise ProtocolError("run_name must be one safe basename")
    root = (RESULTS_ROOT / run_name).resolve()
    # 解析运行根目录。
    if root.parent != RESULTS_ROOT.resolve():
        # 一级父目录必须正好是 RESULTS_ROOT。
        raise ProtocolError("run_name escapes results/recursive_improvement")
    return root
    # 返回通过校验的运行根。


def _task_list(value: Any, *, label: str,
               valid_tasks: frozenset[str]) -> tuple[str, ...]:
    # 【中文注释】校验任务 ID 列表：必须是 JSON 字符串列表、无重复、
    # 且全部属于"有效任务集"（全集减去发布排除项）。
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        # 类型校验：列表且元素全为字符串。
        raise ProtocolError(f"{label} must be a JSON list of task IDs")
    tasks = tuple(value)
    # 转成元组保留顺序。
    if len(set(tasks)) != len(tasks):
        # 出现重复任务 ID 即拒绝。
        raise ProtocolError(f"{label} contains duplicates")
    invalid = sorted(set(tasks) - valid_tasks)
    # 求出不在有效集合中的未知/被排除任务。
    if invalid:
        # 存在无效任务即拒绝。
        raise ProtocolError(
            f"{label} contains unknown or excluded tasks: {invalid}")
    return tasks
    # 返回校验后的任务元组。


def load_protocol(path: Path) -> dict[str, Any]:
    # 【中文注释】加载并全面校验协议 JSON：版本、发布版、研究设计、运行名、
    # 三个阶段配置、任务集合与分布文本的交叉约束——任何违规立即拒绝。
    spec = _read_object(path)
    # 读入协议顶层对象。
    if spec.get("schema_version") != 1:
        # 协议模式版本必须为 1。
        raise ProtocolError("protocol schema_version must be 1")
    task_release = spec.get("task_release")
    # 读取任务发布版声明。
    if task_release not in TASK_RELEASES:
        # 只接受登记过的受审发布版。
        raise ProtocolError(
            "task_release must be one reviewed release: "
            + ", ".join(sorted(TASK_RELEASES)))
    valid_tasks = frozenset(TASKS) - TASK_RELEASES[task_release]
    # 计算有效任务集：全集减去该发布版的排除项。
    study_design = spec.get("study_design", "held_out_generalization")
    # 读取研究设计（缺省为留出泛化）。
    if study_design not in {
            "held_out_generalization", "target_conditioned_adaptation"}:
        # 研究设计只允许两种。
        raise ProtocolError(
            "study_design must be held_out_generalization or "
            "target_conditioned_adaptation")
    run_root = _safe_run_root(spec.get("run_name"))
    # 校验运行名与结果根。
    for phase in ("phase1", "phase2", "phase3"):
        # 三个阶段段都必须是对象。
        if not isinstance(spec.get(phase), dict):
            raise ProtocolError(f"protocol lacks object {phase}")

    phase1 = spec["phase1"]
    # 取 Phase-1 段。
    budget = phase1.get("project_budget")
    # 读取项目预算。
    checkpoints = phase1.get("checkpoints")
    # 读取记忆检查点。
    if type(budget) is not int or budget < 0:
        # 预算必须是非负整数（不接受 bool 等伪装 int）。
        raise ProtocolError("phase1.project_budget must be nonnegative")
    if (not isinstance(checkpoints, list)
            or any(type(item) is not int for item in checkpoints)
            or checkpoints != sorted(set(checkpoints))
            or any(item < 0 or item > budget for item in checkpoints)):
        # 检查点必须是严格去重、升序、且都在 [0, 预算] 内的整数列表。
        raise ProtocolError(
            "phase1.checkpoints must be sorted unique integers within the budget")
    parallel_waves = phase1.get("parallel_waves", False)
    # 读取是否并行波次。
    parallelism = phase1.get("parallelism", 1)
    # 读取并行度。
    if type(parallel_waves) is not bool:
        # 必须是布尔值。
        raise ProtocolError("phase1.parallel_waves must be a boolean")
    if type(parallelism) is not int or parallelism < 1:
        # 并行度必须是正整数。
        raise ProtocolError("phase1.parallelism must be a positive integer")
    if not parallel_waves and parallelism != 1:
        # 非并行波次时并行度必须为 1（语义一致）。
        raise ProtocolError(
            "phase1.parallelism must be 1 when parallel_waves is false")
    distribution = _resolve_file(
        phase1.get("distribution_file"), label="phase1.distribution_file")
    # 解析分布描述文件。
    distribution_text = distribution.read_text(encoding="utf-8")
    # 读入分布文本。
    if not distribution_text.strip():
        # 空分布描述无意义，拒绝。
        raise ProtocolError("Phase-1 distribution description is empty")

    development = _task_list(
        spec["phase2"].get("development_tasks"),
        label="phase2.development_tasks", valid_tasks=valid_tasks)
    # 校验 Phase-2 开发任务集（预提交）。
    held_out = _task_list(
        spec["phase3"].get("held_out_tasks"),
        label="phase3.held_out_tasks", valid_tasks=valid_tasks)
    # 校验 Phase-3 留出任务集（预提交）。
    overlap = sorted(set(development) & set(held_out))
    # 求开发集与留出集的交集。
    if overlap and study_design == "held_out_generalization":
        # 留出泛化设计要求两集合完全不相交（防训练/评测泄漏）。
        raise ProtocolError(
            "development and held-out task sets overlap: " + ", ".join(overlap))
    if (study_design == "target_conditioned_adaptation"
            and set(development) != set(held_out)):
        # 目标条件化适配要求两集合相同（在目标自身上学习并评测）。
        raise ProtocolError(
            "target-conditioned development_tasks and held_out_tasks must name "
            "the same target set")
    named_held_out = sorted(
        task for task in held_out if task.lower() in distribution_text.lower())
    # 检查分布文本是否点名了留出任务（大小写不敏感的实例名匹配）。
    if named_held_out and study_design == "held_out_generalization":
        # 留出泛化设计的 Phase-1 分布不得提及留出实例（防分布层泄漏）。
        raise ProtocolError(
            "Phase-1 distribution text names held-out instances: "
            + ", ".join(named_held_out))

    # 各阶段要求的配置文件字段清单：phase1=五个角色配置（actor/verifier/
    # verifier_control/curriculum/memory）；phase2=目标/练习 Actor/练习 Verifier/
    # 课程/记忆配置；phase3=评测配置与基准锁。
    config_fields = {
        "phase1": ("actor_config", "verifier_config",
                   "verifier_control_config", "curriculum_config",
                   "memory_config"),
        "phase2": ("target_config", "practice_actor_config",
                   "practice_verifier_config", "curriculum_config",
                   "memory_config"),
        "phase3": ("config", "benchmark_lock"),
    }
    resolved_configs: dict[str, dict[str, Path]] = {}
    # 解析后的配置路径表（按阶段分组）。
    for phase, fields in config_fields.items():
        # 逐阶段逐字段解析文件引用。
        resolved_configs[phase] = {
            field: _resolve_file(
                spec[phase].get(field), label=f"{phase}.{field}")
            for field in fields
        }
        # 每个字段都必须解析为真实文件。
    for phase in ("phase2", "phase3"):
        # Phase-2/3 各需一个非负随机种子。
        seed = spec[phase].get("seed")
        # 读取种子。
        if type(seed) is not int or seed < 0:
            # 种子必须是非负整数。
            raise ProtocolError(f"{phase}.seed must be nonnegative")
    try:
        stop_policy = Phase2StopPolicy(
            spec["phase2"].get("stop_policy", DEFAULT_PHASE2_STOP_POLICY))
        # 解析 Phase-2 停止策略（缺省为默认策略）。
    except (ValueError, TypeError) as exc:
        # 非法策略值折算为协议错误。
        raise ProtocolError(
            "phase2.stop_policy must be verifier_pass or curriculum_review") from exc

    # 把解析/校验产物挂到协议对象上，供后续阶段执行使用：
    # protocol_path=协议文件路径；run_root=运行根目录；distribution=分布文件路径；
    # development_tasks/held_out_tasks=开发/留出任务集；study_design=研究设计；
    # phase2_stop_policy=停止策略取值；configs=解析后的配置路径表。
    spec["_resolved"] = {
        "protocol_path": path,
        "run_root": run_root,
        "distribution": distribution,
        "development_tasks": development,
        "held_out_tasks": held_out,
        "study_design": study_design,
        "phase2_stop_policy": stop_policy.value,
        "configs": resolved_configs,
    }
    return spec
    # 返回带解析结果的协议对象。


def _memory_record(path: Path) -> dict[str, Any]:
    # 【中文注释】构造记忆目录的指纹记录：路径、文件数、字节数、记忆树 sha256、
    # 清单 sha256——用于跨阶段血脉一致性校验。
    memory = _read_memory_tree(str(path))
    # 读取整个记忆树（文件名→内容字节）。
    manifest = _manifest(memory)
    # 构造记忆清单。
    # 返回指纹记录：path=规范化的记忆目录路径；files=文件数量；bytes=总字节数；
    # tree_sha256=记忆树整体 sha256；manifest_sha256=清单规范化 JSON 的 sha256。
    return {
        "path": str(path.resolve()),
        "files": len(memory),
        "bytes": sum(len(data) for data in memory.values()),
        "tree_sha256": _memory_tree_sha256(memory),
        "manifest_sha256": hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
    }


def _configure_release_environment(
        spec: dict[str, Any], *, environment: dict[str, str]) -> dict[str, Any]:
    """Bind task setup to the protocol's benchmark release before any phase."""
    # 【中文注释】在任何阶段开始前，把任务安装绑定到协议声明的基准发布版：
    # 校验 Phase-3 锁与发布一致 → 绑定网站后缀 → 配置用户模拟器（不加载凭据）→
    # 校验并绑定任务资产快照。返回绑定记录。

    release = spec["task_release"]
    # 取协议声明的发布版。
    lock_path = spec["_resolved"]["configs"]["phase3"]["benchmark_lock"]
    # 取 Phase-3 基准锁文件路径。
    lock = _read_object(lock_path)
    # 读入基准锁。
    if lock.get("task_release") != release:
        # 锁中发布版与协议不一致即冲突。
        raise ProtocolError(
            "Phase-3 lock task release conflicts with the protocol")
    evaluator = lock.get("evaluator") or {}
    # 取评测器段（可能缺失）。
    website = evaluator.get("website_host_suffix")
    # 读取评测网站主机后缀。
    if not isinstance(website, str) or not website:
        # 必须有网站发布绑定，否则环境不可用。
        raise ProtocolError("Phase-3 lock lacks a website release binding")
    environment["WEBSITE_HOST_SUFFIX"] = website
    # 把网站后缀注入子进程环境（评测与任务安装都按该发布访问站点）。
    # 配置用户模拟器：绑定基准锁但不加载凭据（load_credential=False）；
    # 下方英文注释为原文件自带：凭据解析留给子进程既有的 OSWorld dotenv 流程，
    # 此处预加载共享密钥会改变 Actor 的密钥优先级。
    user_simulator = configure_user_simulator(
        lock.get("user_simulator"), lock_path=lock_path,
        repo_root=RSIAGENT_ROOT, environment=environment,
        # Resolve credentials only after the child's existing OSWorld dotenv
        # setup; loading shared keys here would change Actor key precedence.
        load_credential=False)

    assets = lock.get("task_assets")
    # 读取任务资产段（可能为 None 表示继承检出树）。
    asset_record: dict[str, Any] = {"mode": "inherited"}
    # 默认资产记录：继承模式。
    if assets is not None:
        # 锁声明了独立资产快照时进行校验与绑定。
        if not isinstance(assets, dict):
            # 资产段必须是对象。
            raise ProtocolError("Phase-3 lock task_assets must be an object")
        local_dir = assets.get("local_dir")
        # 本地快照目录（相对 OSWorld 根）。
        marker_name = assets.get("release_marker")
        # 发布标记文件名。
        if (not isinstance(local_dir, str) or not local_dir
                or not isinstance(marker_name, str) or not marker_name):
            # 两个字段都必须是非空字符串。
            raise ProtocolError("Phase-3 lock task_assets is incomplete")
        root = (OSWORLD_ROOT / local_dir).resolve(strict=True)
        # 解析资产快照根目录（必须存在）。
        marker_path = (root / marker_name).resolve(strict=True)
        # 解析发布标记文件（必须存在）。
        marker = _read_object(marker_path)
        # 读入标记内容。
        # 标记必须与锁声明的发布元数据逐项一致：benchmark_release=基准发布名；
        # repository/repo_type/tag/commit=期望的仓库地址/类型/标签/提交哈希。
        expected = {
            "benchmark_release": release,
            "repository": assets.get("repository"),
            "repo_type": assets.get("repo_type"),
            "tag": assets.get("tag"),
            "commit": assets.get("commit"),
        }
        mismatch = {
            key: {"expected": value, "observed": marker.get(key)}
            for key, value in expected.items() if marker.get(key) != value}
        # 求出标记与期望不一致的字段。
        if mismatch:
            # 任一不一致都拒绝绑定（资产与协议发布版冲突）。
            raise ProtocolError(
                f"prepared task assets conflict with the protocol: {mismatch}")
        environment["OSWORLD_FILE_BASE_URL"] = str(root)
        # 把资产下载基址指向本地快照（离线/确定性供给）。
        # 资产记录：mode=发布绑定的本地快照；path=快照路径；marker=标记文件路径；
        # marker_sha256=标记内容指纹。
        asset_record = {
            "mode": "release_bound_local_snapshot",
            "path": str(root),
            "marker": str(marker_path),
            "marker_sha256": _sha256(marker_path),
        }
    # 返回发布环境绑定记录（写入预检报告/审计）：task_release=发布版名；
    # website_host_suffix=评测网站后缀；task_assets=资产绑定记录；
    # user_simulator=用户模拟器配置记录。
    return {
        "task_release": release,
        "website_host_suffix": website,
        "task_assets": asset_record,
        "user_simulator": user_simulator,
    }


def _append_event(run_root: Path, event: str, **payload: Any) -> None:
    # 【中文注释】向运行根的 protocol_events.jsonl 追加一条事件：
    # UTC 时间戳 + 事件名 + 负载；flush+fsync 保证审计链崩溃安全。
    run_root.mkdir(parents=True, exist_ok=True)
    # 确保运行根存在。
    # 组装事件行：timestamp_utc=UTC 时间戳；event=事件名；payload=事件负载。
    row = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "payload": payload,
    }
    with (run_root / "protocol_events.jsonl").open("a", encoding="utf-8") as out:
        # 追加方式打开协议事件日志。
        out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        # 写一行 JSON。
        out.flush()
        # 冲刷缓冲。
        os.fsync(out.fileno())
        # fsync 落盘。


def _run(command: list[str], *, run_root: Path, phase: str,
         task: str = "", environment: dict[str, str] | None = None) -> None:
    # 【中文注释】运行阶段子进程：前后各记一条事件（开始/完成含退出码），
    # 非 0 退出折算为协议错误；使用当前仓库根作为工作目录。
    _append_event(run_root, "SUBPROCESS_STARTED", phase=phase, task=task,
                  argv=command)
    # 记录子进程启动事件（含完整 argv）。
    completed = subprocess.run(
        command, cwd=RSIAGENT_ROOT, check=False, env=environment)
    # 同步运行子进程（不抛异常，靠返回码判断）。
    _append_event(
        run_root, "SUBPROCESS_COMPLETED", phase=phase, task=task,
        returncode=completed.returncode)
    # 记录子进程完成事件（含退出码）。
    if completed.returncode != 0:
        # 非 0 退出码视为该阶段失败。
        raise ProtocolError(
            f"{phase} subprocess failed for {task or 'lineage'} "
            f"with exit code {completed.returncode}")


def _ensure_no_later_phase(run_root: Path, phase: str) -> None:
    # 【中文注释】阶段顺序守卫：执行某阶段前，确认更晚阶段的产物目录不存在——
    # 官方分数绝不允许先于学习阶段存在（时间顺序即泄漏边界）。
    # phase1 之后是 phase2/phase3；phase2 之后是 phase3；phase3 是最后阶段。
    later = {
        "phase1": ("phase2", "phase3"),
        "phase2": ("phase3",),
        "phase3": (),
    }[phase]
    # 取当前阶段的更晚阶段列表。
    present = [name for name in later if (run_root / name).exists()]
    # 检查更晚阶段的产物目录是否已存在。
    if present:
        # 存在即违反"先学习后评测"的顺序约束。
        raise ProtocolError(
            f"cannot execute {phase} after later phase artifacts exist: {present}")


def _configure_phase3_evaluator(
        lock_path: Path, config_path: Path,
        task_release: str) -> dict[str, Any]:
    """Bind secret-free evaluator identity before any held-out Actor starts."""
    # 【中文注释】在任何留出任务 Actor 启动之前，绑定"无密钥"评测器身份：
    # 校验基准锁的发布元数据与各角色配置指纹，然后装配 sealed evaluation
    # （密封评测）环境并做传输预检。返回密封评测配置记录。
    lock = _read_object(lock_path)
    # 读入基准锁。
    excluded = TASK_RELEASES[task_release]
    # 取该发布版的排除任务集合。
    # 锁必须精确匹配的元数据：schema_version=锁模式版本；benchmark=基准名；
    # task_release=发布版名；source_task_count=源任务总数；
    # valid_task_count=有效任务数（总数减排除项）。
    required = {
        "schema_version": 1,
        "benchmark": "OSWorld-v2",
        "task_release": task_release,
        "source_task_count": 108,
        "valid_task_count": 108 - len(excluded),
    }
    drift = {
        key: {"expected": expected, "observed": lock.get(key)}
        for key, expected in required.items() if lock.get(key) != expected}
    # 求出锁与期望不一致的元数据字段。
    if drift:
        # 锁元数据漂移即拒绝。
        raise ProtocolError(f"Phase-3 benchmark lock drifted: {drift}")
    if set(lock.get("excluded_tasks") or ()) != excluded:
        # 排除任务集合也必须与登记表一致。
        raise ProtocolError("Phase-3 excluded task set drifted")
    actor = lock.get("actor_agent") or {}
    # 取 Actor 锁记录。
    if (actor.get("config") != str(config_path.relative_to(RSIAGENT_ROOT))
            or actor.get("sha256") != _sha256(config_path)
            or actor.get("model") != "z-ai/glm-5.3"):
        # Actor 配置必须与锁绑定：相对路径一致、内容 sha256 一致、模型为 GLM-5.3。
        raise ProtocolError("Phase-3 Actor Agent config is not lock-bound")
    for role in ("verifier_agent", "escalated_verifier_agent"):
        # 校验两个 Verifier 角色的锁绑定。
        record = lock.get(role) or {}
        # 取该角色的锁记录。
        path = _resolve_file(record.get("config"), label=f"lock.{role}.config")
        # 解析其配置文件引用。
        if record.get("sha256") != _sha256(path):
            # 配置内容指纹必须与锁一致。
            raise ProtocolError(f"Phase-3 {role} config drifted")

    from config.settings import load
    from config.runtime_paths import normalize_verifier_config_paths
    from benchmarks.osworld.runtime import (
        _configure_sealed_evaluator,
        _probe_sealed_evaluator_transport,
    )
    # 【中文注释】延迟导入密封评测装配与传输预检工具（及配置加载器）。

    cfg = load(str(config_path))
    # 加载 Phase-3 评测配置。
    normalize_verifier_config_paths(cfg, RSIAGENT_ROOT)
    # 归一化其中的相对路径。
    sealed = _configure_sealed_evaluator(lock_path, cfg, config_path)
    # 装配密封评测环境（评测器在 Agent 交接后才运行，密钥不暴露给智能体）。
    sealed["transport_preflight"] = _probe_sealed_evaluator_transport()
    # 附上评测传输通道的预检结果。
    return sealed
    # 返回密封评测配置记录。


def execute_phase1(spec: dict[str, Any], *, resume_completed_boundary=False) -> dict[str, Any]:
    # 【中文注释】执行 Phase-1：顺序守卫 → 已完成则直接复用结果（或校验恢复边界）→
    # 组装 phase1 子进程命令并运行 → 校验结果转换就绪且无评测器调用。
    resolved = spec["_resolved"]
    # 取解析产物。
    run_root: Path = resolved["run_root"]
    # 运行根目录。
    _ensure_no_later_phase(run_root, "phase1")
    # 确认 phase2/phase3 产物不存在。
    phase1_result = run_root / "phase1/result.json"
    # Phase-1 结果文件路径。
    if phase1_result.is_file() and not resume_completed_boundary:
        # 已有结果且非恢复模式：直接读取复用（幂等重入）。
        result = _read_object(phase1_result)
        if result.get("status") not in {"saturated", "budget_exhausted"}:
            # 只有语义终止的结果才可转换到 Phase-2。
            raise ProtocolError("existing Phase-1 result is not transition-ready")
        return result
        # 返回既有结果。
    cfg = resolved["configs"]["phase1"]
    # 取 Phase-1 各配置路径。
    # 组装 Phase-1 子进程命令：用当前解释器运行 benchmarks.osworld.phase1 模块；
    # 依次传递运行名、分布描述文件、项目预算、检查点列表（逗号拼接）、
    # Actor/Verifier/Agentic Verifier 控制面/Curriculum/记忆五个配置路径，
    # 以及执行确认口令 RUN-PHASE1-DISTRIBUTION-EXPLORATION。
    command = [
        sys.executable, "-m", "benchmarks.osworld.phase1",
        "--run-name", spec["run_name"],
        "--distribution-file", str(resolved["distribution"]),
        "--project-budget", str(spec["phase1"]["project_budget"]),
        "--checkpoints", ",".join(map(str, spec["phase1"]["checkpoints"])),
        "--actor-config", str(cfg["actor_config"]),
        "--verifier-config", str(cfg["verifier_config"]),
        "--verifier-control-config", str(cfg["verifier_control_config"]),
        "--curriculum-config", str(cfg["curriculum_config"]),
        "--memory-config", str(cfg["memory_config"]),
        "--execute", "RUN-PHASE1-DISTRIBUTION-EXPLORATION",
    ]
    if spec["phase1"].get("parallel_waves", False):
        # 并行波次模式：插入相应开关与并行度。
        command[-2:-2] = [
            "--parallel-waves", "--max-parallel",
            str(spec["phase1"]["parallelism"]),
        ]
        # 在末尾口令参数之前插入两个选项。
    if resolved["study_design"] == "target_conditioned_adaptation":
        # 查询条件化设计：插入对应开关。
        command.insert(-2, "--target-query-conditioned")
    if resume_completed_boundary:
        # 恢复模式：插入恢复边界开关。
        command.insert(-2, "--resume-completed-boundary")
    environment = dict(os.environ)
    # 继承当前环境变量。
    _configure_release_environment(spec, environment=environment)
    # 绑定发布环境（网站后缀/资产/模拟器）到子进程环境。
    _run(command, run_root=run_root, phase="phase1", environment=environment)
    # 运行 Phase-1 子进程。
    result = _read_object(phase1_result)
    # 读取运行产出的结果。
    if result.get("official_evaluator_calls") != 0:
        # Phase-1 绝不允许评测器调用。
        raise ProtocolError("Phase 1 recorded an evaluator call")
    return result
    # 返回 Phase-1 结果。


def execute_phase2(spec: dict[str, Any]) -> dict[str, Any]:
    # 【中文注释】执行 Phase-2（DRS）：校验 Phase-1 就绪 → 逐任务串行运行
    # 失败条件化练习（每个任务以 phase1 冻结记忆为起点，血脉逐任务演进）→
    # 校验各任务终态与评测器边界 → 冻结最终记忆并写汇总结果。
    resolved = spec["_resolved"]
    # 取解析产物。
    run_root: Path = resolved["run_root"]
    # 运行根目录。
    _ensure_no_later_phase(run_root, "phase2")
    # 确认 phase3 产物不存在。
    phase1_result = _read_object(run_root / "phase1/result.json")
    # 读取 Phase-1 结果。
    if phase1_result.get("status") not in {"saturated", "budget_exhausted"}:
        # Phase-1 必须已语义终止才能进入 Phase-2。
        raise ProtocolError("Phase 1 is not transition-ready")
    tasks = resolved["development_tasks"]
    # 取预提交的开发任务集。
    if not tasks:
        # 开发任务集必须非空。
        raise ProtocolError(
            "Phase 2 needs a precommitted nonempty development task set")
    memory_path = Path(phase1_result["memory_frozen_path"]).resolve(strict=True)
    # 解析 Phase-1 冻结记忆目录（血脉起点）。
    initial_record = _memory_record(memory_path)
    # 记录初始记忆指纹。
    cfg = resolved["configs"]["phase2"]
    # 取 Phase-2 各配置路径。
    stop_policy = resolved["phase2_stop_policy"]
    # 取停止策略。
    benchmark_profile = BENCHMARK_PROVENANCE_PROFILES[
        spec["task_release"]].resolve(strict=True)
    # 解析发布版对应的基准溯源锁文件。
    task_results = []
    # 收集每个任务的结果行。
    for position, task in enumerate(tasks):
        # 按协议顺序逐任务执行（顺序确定、可复现）。
        seed = int(spec["phase2"]["seed"]) + position
        # 每个任务的种子 = 基础种子 + 位置序号（确定性派生）。
        result_path = (run_root / "phase2" / task
                       / f"attempt_{seed:04d}" / "result.json")
        # 该任务该种子的结果文件路径。
        if not result_path.is_file():
            # 结果缺失才真正运行（幂等重入：已有结果直接校验复用）。
            # 组装 Phase-2 子进程命令：运行 benchmarks.osworld.phase2 模块处理该任务；
            # 依次传递协议血脉名 --protocol-run、种子与初始记忆（上一任务演进的冻结记忆）、
            # 停止策略、目标/练习 Actor/练习 Verifier/Curriculum/记忆五个配置路径、
            # 基准溯源锁，以及执行确认口令 RUN-PHASE2-FAILURE-CONDITIONED-PRACTICE。
            command = [
                sys.executable, "-m", "benchmarks.osworld.phase2", task,
                 "--protocol-run", spec["run_name"],
                "--seed", str(seed), "--initial-memory", str(memory_path),
                "--phase2-stop-policy", stop_policy,
                "--target-config", str(cfg["target_config"]),
                "--practice-actor-config", str(cfg["practice_actor_config"]),
                "--practice-verifier-config", str(cfg["practice_verifier_config"]),
                "--curriculum-config", str(cfg["curriculum_config"]),
                "--memory-config", str(cfg["memory_config"]),
                "--benchmark-profile", str(benchmark_profile),
                "--execute", "RUN-PHASE2-FAILURE-CONDITIONED-PRACTICE",
            ]
            environment = dict(os.environ)
            # 继承环境变量。
            _configure_release_environment(spec, environment=environment)
            # 绑定发布环境。
            _run(command, run_root=run_root, phase="phase2", task=task,
                 environment=environment)
            # 运行该任务的 Phase-2 子进程。
        result = _read_object(result_path)
        # 读取任务结果（新跑或缓存）。
        _validate_phase2_terminal(result, stop_policy=stop_policy, task=task)
        # 校验任务终态与停止策略标签一致。
        if (result.get("official_evaluator_calls") != 0
                or result.get("official_evaluator_feedback_entered_learning")
                is not False):
            # 评测器边界：Phase-2 既不得调用官方评测器，也不得让评测反馈进入学习。
            raise ProtocolError(f"Phase-2 grader boundary failed for {task}")
        if result.get("initial_memory_tree_sha256") != \
                _memory_record(memory_path)["tree_sha256"]:
            # 任务声明的初始记忆血脉必须与当前记忆指纹一致。
            raise ProtocolError(f"Phase-2 memory lineage mismatch for {task}")
        memory_path = Path(result["final_memory_path"]).resolve(strict=True)
        # 血脉推进：以下一任务的输入为该任务产出的最终记忆。
        if _memory_record(memory_path)["tree_sha256"] != \
                result.get("final_memory_tree_sha256"):
            # 实际落盘的最终记忆必须与结果声明的指纹一致。
            raise ProtocolError(f"Phase-2 final memory mismatch for {task}")
        # 摘录该任务的关键结果字段：task=任务 ID；seed=种子；status=终止状态；
        # stop_policy=停止策略；verdict=目标 Verifier 判定（PASS/FAIL）；
        # evolutions=进化轮数；practice_projects=练习项目数；
        # memory_tree_sha256=最终记忆树指纹。
        task_results.append({
            "task": task,
            "seed": seed,
            "status": result.get("status"),
            "stop_policy": result["phase2_stop_policy"],
            "verdict": result.get("target_verifier_verdict"),
            "evolutions": result.get("evolutions"),
            "practice_projects": result.get("practice_projects"),
            "memory_tree_sha256": result.get("final_memory_tree_sha256"),
        })

    frozen = run_root / "phase2/memory_frozen"
    # Phase-2 冻结记忆目录（Phase-3 的输入）。
    if frozen.exists():
        # 已存在冻结：校验与血脉终态一致（幂等重入防漂移）。
        existing = _memory_record(frozen)
        source = _memory_record(memory_path)
        if existing["tree_sha256"] != source["tree_sha256"]:
            raise ProtocolError("existing Phase-2 frozen memory conflicts")
    else:
        # 不存在则原子安装血脉终态记忆。
        _atomic_install_memory(
            str(frozen), _read_memory_tree(str(memory_path)))
    # 组装 Phase-2 汇总结果：schema_version=结果模式版本；
    # phase=阶段名（结果接地的定向练习）；phase2_stop_policy=停止策略；
    # status=完成；development_tasks/task_results=开发任务列表与各任务结果；
    # memory_before/memory_frozen=初始与冻结记忆指纹；
    # official_evaluator_calls=官方评测器调用次数（0）；
    # official_evaluator_feedback_entered_learning=评测反馈进入学习（否）。
    payload = {
        "schema_version": 1,
        "phase": "outcome_grounded_targeted_practice",
        "phase2_stop_policy": stop_policy,
        "status": "complete",
        "development_tasks": list(tasks),
        "task_results": task_results,
        "memory_before": initial_record,
        "memory_frozen": _memory_record(frozen),
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
    }
    _write_json_atomic(run_root / "phase2/result.json", payload)
    # 原子写入 Phase-2 汇总结果。
    return payload
    # 返回汇总结果。


def _validate_phase2_terminal(
        result: dict[str, Any], *, stop_policy: str, task: str) -> None:
    """Validate new and cached results before advancing to another task/phase."""
    # 【中文注释】校验 Phase-2 任务终态（新跑与缓存一视同仁）后才允许推进：
    # 停止策略必须带标签且一致；状态与判定组合必须语义可转换。
    if result.get("phase2_stop_policy") != stop_policy:
        # 结果必须标注与本协议一致的停止策略（禁止复用无标签/不同策略的结果）。
        raise ProtocolError(
            f"Phase-2 stopping policy is absent or differs for {task}; "
            "use a new lineage rather than reuse an unlabelled/different policy")
    status = result.get("status")
    # 读取终止状态。
    verdict = result.get("target_verifier_verdict")
    # 读取目标判定。
    allowed = {"verifier_pass", "curriculum_stalled_final_target_test"}
    # verifier_pass 策略下允许的终态集合。
    if stop_policy == Phase2StopPolicy.CURRICULUM_REVIEW.value:
        # curriculum_review 策略额外允许两种"通过后课程审查"终态。
        allowed |= {"verifier_pass_curriculum_ready", "verifier_pass_curriculum_stalled"}
    if status not in allowed or verdict not in {"PASS", "FAIL"}:
        # 状态必须在允许集合内，且判定必须是明确的 PASS/FAIL。
        raise ProtocolError(
            f"Phase-2 target {task} is not transition-ready: {status!r}, {verdict!r}")
    if status.startswith("verifier_pass") and verdict != "PASS":
        # "通过"类终态必须有 Verifier 的 PASS 判定背书。
        raise ProtocolError(f"Phase-2 PASS termination lacks a Verifier PASS for {task}")


def execute_phase3(spec: dict[str, Any]) -> dict[str, Any]:
    # 【中文注释】执行 Phase-3（sealed evaluation，密封评测）：校验 Phase-2 完成 →
    # 冻结记忆锁（禁止写回、禁用课程）→ 绑定密封评测器 → 逐留出任务运行官方评测 →
    # 校验记忆零变化与反馈边界 → 汇总分数。
    resolved = spec["_resolved"]
    # 取解析产物。
    target_conditioned = (
        resolved["study_design"] == "target_conditioned_adaptation")
    # 是否目标条件化设计（决定结果阶段名）。
    run_root: Path = resolved["run_root"]
    # 运行根目录。
    phase2 = _read_object(run_root / "phase2/result.json")
    # 读取 Phase-2 汇总结果。
    if phase2.get("status") != "complete":
        # Phase-2 必须已完成。
        raise ProtocolError("Phase 2 is not complete")
    tasks = resolved["held_out_tasks"]
    # 取预提交的留出任务集。
    if not tasks:
        # 留出任务集必须非空。
        raise ProtocolError("Phase 3 needs a precommitted held-out task set")
    source = Path(phase2["memory_frozen"]["path"]).resolve(strict=True)
    # 解析 Phase-2 冻结记忆目录（评测用记忆源）。
    phase3_root = run_root / "phase3"
    # Phase-3 结果根。
    frozen = phase3_root / "memory_frozen"
    # Phase-3 自己的冻结记忆副本。
    if not frozen.exists():
        # 尚无副本则从 Phase-2 冻结原子安装一份。
        _atomic_install_memory(str(frozen), _read_memory_tree(str(source)))
    frozen_record = _memory_record(frozen)
    # 计算评测记忆指纹。
    if frozen_record["tree_sha256"] != phase2["memory_frozen"]["tree_sha256"]:
        # 副本必须与 Phase-2 冻结逐字节一致。
        raise ProtocolError("Phase-3 memory does not match the Phase-2 freeze")
    # 写记忆锁：声明评测期记忆不可变。字段：schema_version=锁模式版本；
    # **frozen_record=展开记忆指纹字段；writeback=False 禁止写回（评测不改记忆）；
    # curriculum_enabled=False 禁用课程智能体（评测期无学习）。
    _write_json_atomic(phase3_root / "memory_lock.json", {
        "schema_version": 1,
        **frozen_record,
        "writeback": False,
        "curriculum_enabled": False,
    })

    tag = f"{spec['run_name']}_phase3"
    # 评测运行标签（用于结果目录命名）。
    cfg = resolved["configs"]["phase3"]["config"]
    # Phase-3 评测配置。
    evaluator_lock = resolved["configs"]["phase3"]["benchmark_lock"]
    # 基准锁路径。
    sealed_evaluator = _configure_phase3_evaluator(
        evaluator_lock, cfg, spec["task_release"])
    # 绑定密封评测器身份（在任何留出 Actor 启动前）。
    task_results = []
    # 收集各任务评测结果。
    for position, task in enumerate(tasks):
        # 按协议顺序逐任务评测。
        seed = int(spec["phase3"]["seed"]) + position
        # 确定性派生种子。
        result_path = (RSIAGENT_ROOT / "results" / task
                       / f"seed{seed}_{tag}" / "result.json")
        # 评测结果文件路径（与单任务基准入口的输出约定一致）。
        if not result_path.is_file():
            # 缺结果才运行（幂等重入）。
            # 组装 Phase-3 子进程命令：运行单任务基准入口 benchmarks.osworld.task，
            # 传递种子、运行标签、评测配置与冻结记忆目录。
            command = [
                sys.executable, "-m", "benchmarks.osworld.task", task,
                "--seed", str(seed), "--tag", tag,
                "--config", str(cfg), "--memory-dir", str(frozen),
            ]
            environment = dict(os.environ)
            # 继承环境变量。
            _configure_release_environment(spec, environment=environment)
            # 绑定发布环境。
            _run(command, run_root=run_root, phase="phase3", task=task,
                 environment=environment)
            # 运行官方评测子进程（Agent 交接后才有官方评测调用）。
        result = _read_object(result_path)
        # 读取评测结果。
        after = _memory_record(frozen)
        # 评测后的记忆指纹。
        if after["tree_sha256"] != frozen_record["tree_sha256"]:
            # 评测绝不允许改动冻结记忆（隔离边界硬校验）。
            raise ProtocolError("Phase-3 evaluation mutated frozen host memory")
        memory = result.get("memory") or {}
        # 读取结果中的记忆边界记录。
        if (memory.get("tree_sha256") != frozen_record["tree_sha256"]
                or memory.get("host_writeback") is not False
                or result.get("official_evaluator_feedback_entered_agent")
                is not False):
            # 三重边界：记忆树未变、宿主无写回、官方评测反馈未进入智能体。
            raise ProtocolError(f"Phase-3 boundary record failed for {task}")
        # 摘录该任务的评测结果：task=任务 ID；seed=种子；score=官方分数（浮点）；
        # status=运行状态；official_evaluator_attempts=官方评测器调用尝试次数（缺省 1）；
        # source_result=原始结果文件路径。
        task_results.append({
            "task": task,
            "seed": seed,
            "score": float(result["score"]),
            "status": result.get("status"),
            "official_evaluator_attempts":
                int(result.get("official_evaluator_attempts", 1)),
            "source_result": str(result_path),
        })
    scores = [row["score"] for row in task_results]
    # 汇总所有分数。
    # 组装 Phase-3 汇总结果：schema_version=结果模式版本；
    # phase=阶段名（冻结记忆的目标条件化评测 / 冻结记忆的留出评测）；
    # study_design=研究设计；status=完成；held_out_tasks=留出任务列表；
    # memory_frozen=冻结记忆指纹；curriculum_enabled=False 课程已禁用；
    # memory_updates=记忆更新次数（0）；official_evaluator_calls/
    # official_evaluator_successful_calls=官方评测调用次数（每任务一次）；
    # official_evaluator_attempts=总尝试次数；
    # official_evaluator_feedback_entered_learning=评测反馈进入学习（否，
    # 协议终结后无学习阶段）；sealed_evaluator=密封评测器配置记录；
    # mean_score=平均分；task_results=各任务结果。
    payload = {
        "schema_version": 1,
        "phase": ("frozen_memory_target_conditioned_evaluation"
                  if target_conditioned
                  else "frozen_memory_held_out_evaluation"),
        "study_design": resolved["study_design"],
        "status": "complete",
        "held_out_tasks": list(tasks),
        "memory_frozen": frozen_record,
        "curriculum_enabled": False,
        "memory_updates": 0,
        "official_evaluator_calls": len(tasks),
        "official_evaluator_successful_calls": len(tasks),
        "official_evaluator_attempts": sum(
            row["official_evaluator_attempts"] for row in task_results),
        "official_evaluator_feedback_entered_learning": False,
        "sealed_evaluator": sealed_evaluator,
        "mean_score": sum(scores) / len(scores),
        "task_results": task_results,
    }
    _write_json_atomic(phase3_root / "result.json", payload)
    # 原子写入 Phase-3 结果。
    return payload
    # 返回汇总结果。


def preflight(spec: dict[str, Any], phase: str) -> dict[str, Any]:
    # 【中文注释】预检报告：汇总该阶段的协议画像、前置条件阻塞项、各配置指纹
    # 与边界声明；不启动任何昂贵操作。
    resolved = spec["_resolved"]
    # 取解析产物。
    # 各阶段各配置的路径与内容指纹（按阶段、按字段两级构造）。
    configs = {
        name: {
            field: {"path": str(path), "sha256": _sha256(path)}
            for field, path in values.items()
        }
        for name, values in resolved["configs"].items()
    }
    blockers = []
    # 前置条件阻塞项列表。
    if phase == "phase2":
        # Phase-2 前置：开发任务非空、Phase-1 结果存在。
        if not resolved["development_tasks"]:
            blockers.append("phase2.development_tasks is empty")
        if not (resolved["run_root"] / "phase1/result.json").is_file():
            blockers.append("Phase 1 result is not present")
    elif phase == "phase3":
        # Phase-3 前置：留出任务非空、Phase-2 结果存在。
        if not resolved["held_out_tasks"]:
            blockers.append("phase3.held_out_tasks is empty")
        if not (resolved["run_root"] / "phase2/result.json").is_file():
            blockers.append("Phase 2 result is not present")
    # A preflight must not leave this study's user route in the process for a
    # later study. Execution passes its own environment to each child instead.
    # 【中文注释】预检不得把本研究的环境绑定泄漏进本进程（影响后续研究）；
    # 真正执行时由各阶段向子进程传递自己的环境副本。
    release_environment = _configure_release_environment(
        spec, environment=dict(os.environ))
    # 在环境副本上做发布绑定（仅用于报告展示，不影响本进程真实环境）。
    # 返回预检报告：status=就绪或需要补全配置（configuration_required）；
    # blockers=阻塞项列表；phase=目标阶段；required_execute_ack=该阶段的执行确认口令；
    # run_name/run_root=运行名与运行根目录；task_release=任务发布版；
    # release_environment=发布环境绑定记录；phase1_project_budget/phase1_checkpoints/
    # phase1_parallel_waves/phase1_parallelism=Phase-1 预算、检查点、并行波次与并行度；
    # phase2_stop_policy=Phase-2 停止策略；development_tasks/held_out_tasks=
    # 开发/留出任务列表；study_design=研究设计；task_split_disjoint=开发/留出两集合
    # 是否不相交；distribution_file/distribution_sha256=分布文件路径与指纹；
    # official_grader_available_in_phase=仅 Phase-3 允许官方判分器；
    # memory_mutable_in_phase/curriculum_enabled_in_phase=仅学习阶段允许记忆变更/
    # 启用课程；configs=该阶段各配置的路径与指纹。
    return {
        "status": "ready" if not blockers else "configuration_required",
        "blockers": blockers,
        "phase": phase,
        "required_execute_ack": _ACK[phase],
        "run_name": spec["run_name"],
        "run_root": str(resolved["run_root"]),
        "task_release": spec["task_release"],
        "release_environment": release_environment,
        "phase1_project_budget": spec["phase1"]["project_budget"],
        "phase1_checkpoints": spec["phase1"]["checkpoints"],
        "phase1_parallel_waves": spec["phase1"].get(
            "parallel_waves", False),
        "phase1_parallelism": spec["phase1"].get("parallelism", 1),
        "phase2_stop_policy": resolved["phase2_stop_policy"],
        "development_tasks": list(resolved["development_tasks"]),
        "held_out_tasks": list(resolved["held_out_tasks"]),
        "study_design": resolved["study_design"],
        "task_split_disjoint": not bool(
            set(resolved["development_tasks"])
            & set(resolved["held_out_tasks"])),
        "distribution_file": str(resolved["distribution"]),
        "distribution_sha256": _sha256(resolved["distribution"]),
        "official_grader_available_in_phase": phase == "phase3",
        "memory_mutable_in_phase": phase in {"phase1", "phase2"},
        "curriculum_enabled_in_phase": phase in {"phase1", "phase2"},
        "configs": configs[phase],
    }


def _protocol_lock(spec: dict[str, Any], source_bytes: bytes) -> dict[str, Any]:
    # 【中文注释】构造协议锁：把协议原文 sha256、发布版、研究设计、各阶段参数、
    # 配置指纹与全部边界声明固化为一个可比对的对象——运行开始后任何漂移都会被检出。
    resolved = spec["_resolved"]
    # 取解析产物。
    # 返回锁对象：schema_version=锁模式版本；kind=锁种类；
    # protocol_sha256=协议原文 sha256；task_release=发布版；run_name=运行名；
    # study_design=研究设计；distribution_sha256=分布文件指纹；
    # phase1_project_budget/phase1_checkpoints/phase1_parallel_waves/
    # phase1_parallelism=Phase-1 预算、检查点、并行波次与并行度；
    # phase2_stop_policy=Phase-2 停止策略；development_tasks/held_out_tasks=
    # 开发/留出任务集；held_out_environment_families=留出环境族声明（可选）；
    # configs=各阶段各配置的路径与内容指纹；boundaries=协议边界声明（锁死后不可漂移）：
    # Phase-1/Phase-2 官方评测调用为 0；官方评测仅在 Phase-3 Agent 交接后进行；
    # Phase-3 禁止记忆写回；Phase-3 开始后不再有任何学习。
    return {
        "schema_version": 1,
        "kind": "recursive_self_improvement_protocol_lock",
        "protocol_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "task_release": spec["task_release"],
        "run_name": spec["run_name"],
        "study_design": resolved["study_design"],
        "distribution_sha256": _sha256(resolved["distribution"]),
        "phase1_project_budget": spec["phase1"]["project_budget"],
        "phase1_checkpoints": spec["phase1"]["checkpoints"],
        "phase1_parallel_waves": spec["phase1"].get(
            "parallel_waves", False),
        "phase1_parallelism": spec["phase1"].get("parallelism", 1),
        "phase2_stop_policy": resolved["phase2_stop_policy"],
        "development_tasks": list(resolved["development_tasks"]),
        "held_out_tasks": list(resolved["held_out_tasks"]),
        "held_out_environment_families": list(
            spec["phase3"].get("held_out_environment_families") or []),
        "configs": {
            phase: {
                field: {
                    "path": str(path),
                    "sha256": _sha256(path),
                }
                for field, path in values.items()
            }
            for phase, values in resolved["configs"].items()
        },
        "boundaries": {
            "official_evaluator_calls_phase1": 0,
            "official_evaluator_calls_phase2": 0,
            "official_evaluator_phase3_only_after_agent_handoff": True,
            "phase3_memory_writeback": False,
            "learning_after_phase3_begins": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    # 【中文注释】命令行解析器：协议文件、目标阶段、恢复开关，以及
    # 预检/执行（互斥、必选）。
    parser = argparse.ArgumentParser(description=__doc__)
    # 用模块 docstring 作帮助文本。
    parser.add_argument("--protocol", required=True)
    # 必填：协议 JSON 文件路径。
    parser.add_argument("--phase", choices=("phase1", "phase2", "phase3"),
                        required=True)
    # 必填：要执行的阶段。
    parser.add_argument("--resume-completed-boundary", action="store_true")
    # 开关：允许恢复已完成边界的基础设施停止血脉（仅 phase1 有效）。
    action = parser.add_mutually_exclusive_group(required=True)
    # 预检与执行互斥且必选。
    action.add_argument("--preflight", action="store_true")
    # 预检模式。
    action.add_argument("--execute", default="")
    # 执行模式（值必须匹配阶段口令）。
    return parser
    # 返回解析器。


def main(argv: list[str] | None = None) -> int:
    # 【中文注释】主流程：解析→加载协议→预检（或校验执行口令）→固化协议副本与
    # 协议锁→记录阶段开始事件→执行对应阶段→记录完成事件并打印结果。
    args = _parser().parse_args(argv)
    # 解析命令行。
    if args.resume_completed_boundary and args.phase != "phase1":
        # 恢复开关仅对 Phase-1 有意义。
        raise ProtocolError("--resume-completed-boundary requires phase1")
    protocol_path = Path(args.protocol).expanduser().resolve(strict=True)
    # 解析协议文件（必须存在）。
    spec = load_protocol(protocol_path)
    # 加载并校验协议。
    report = preflight(spec, args.phase)
    # 生成预检报告（同时完成所有前置校验）。
    if args.preflight:
        # 预检模式：打印报告即返回。
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.execute != _ACK[args.phase]:
        # 执行口令必须与阶段匹配。
        raise ProtocolError(
            f"--execute must equal {_ACK[args.phase]} for {args.phase}")
    run_root = spec["_resolved"]["run_root"]
    # 取运行根。
    run_root.mkdir(parents=True, exist_ok=True)
    # 确保运行根存在。
    protocol_copy = run_root / "protocol.json"
    # 协议副本路径。
    source_bytes = protocol_path.read_bytes()
    # 读入协议原文字节。
    if protocol_copy.exists() and protocol_copy.read_bytes() != source_bytes:
        # 运行开始后协议原文被改动即拒绝。
        raise ProtocolError("protocol changed after this run began")
    if not protocol_copy.exists():
        # 首次运行：固化协议副本进运行目录。
        shutil.copyfile(protocol_path, protocol_copy)
    lock_path = run_root / "protocol_lock.json"
    # 协议锁路径。
    expected_lock = _protocol_lock(spec, source_bytes)
    # 构造期望的协议锁。
    if lock_path.exists():
        # 已有锁则严格比对（输入与角色配置漂移检测）。
        if _read_object(lock_path) != expected_lock:
            raise ProtocolError(
                "protocol inputs or role configs drifted after this run began")
    else:
        # 首次运行：写入协议锁。
        _write_json_atomic(lock_path, expected_lock)
    _append_event(run_root, "PHASE_STARTED", phase=args.phase,
                  protocol_sha256=hashlib.sha256(source_bytes).hexdigest())
    # 记录阶段开始事件（含协议指纹）。
    runner = {
        "phase1": execute_phase1,
        "phase2": execute_phase2,
        "phase3": execute_phase3,
    }[args.phase]
    # 按阶段选择执行器。
    result = (runner(spec, resume_completed_boundary=True)
              if args.resume_completed_boundary else runner(spec))
    # 执行该阶段（恢复模式传恢复边界开关）。
    _append_event(run_root, "PHASE_COMPLETED", phase=args.phase,
                  status=result.get("status"))
    # 记录阶段完成事件。
    print("\n=== RECURSIVE IMPROVEMENT " + args.phase.upper() + " "
          + json.dumps(result, sort_keys=True))
    # 打印阶段结果摘要。
    return 0
    # 成功返回 0。


if __name__ == "__main__":
    # 作为脚本运行：以 main 返回值作为退出码。
    raise SystemExit(main())
