"""Pinned cohort, role settings, and immutable phase boundaries."""
# 【中文注释·模块定位】ALE 固定协议层：钉死（pinned）实验队列、各角色配置的来源
# 与"不可变的阶段边界"不变量。核心保证：①角色配置漂移即失败；②67 任务队列唯一完整；
# ③冻结记忆（frozen memory）跨阶段传递时指纹必须逐字节一致；④源码指纹锁定发布内容。
# ALE host 进程只依赖本模块与 config/，绝不导入 RSIAgent core（防止与 CUA 的顶层
# core 包冲突，也维持评分侧与学习侧的隔离）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# hashlib：文件与冻结记忆的 SHA-256 指纹。
import hashlib
# json：协议锁与指纹清单的序列化。
import json
# os：写文件时的 fsync（确保持久化）。
import os
# re：指令 8-gram 分词。
import re
# subprocess：读取上游 ALE 仓库的 git 提交号。
import subprocess
# datetime/timezone：事件时间戳（UTC ISO 格式）。
from datetime import datetime, timezone
# Path：仓库与配置文件路径。
from pathlib import Path

# RSIAgent 仓库根目录：本文件位于 benchmarks/ale/ 之下，向上两级即仓库根。
RSIAGENT = Path(__file__).resolve().parents[2]
# 协议锁文件：记录固定队列、角色配置哈希、任务卡哈希、ALE 提交号等。
LOCK_PATH = RSIAGENT / "config/ale/protocol.lock.json"
# 角色名 → 配置文件映射：baseline/target/actor/verifier/curriculum/memory 各自加载
# 哪份 YAML（memory 复用 actor 配置；verifier 用练习版 practice_verifier）。
ROLES = {
    "baseline": "osworld/baseline.yaml",
    "target": "roles/target.yaml",
    "actor": "roles/actor.yaml",
    "verifier": "roles/practice_verifier.yaml",
    "curriculum": "roles/curriculum.yaml",
    "memory": "roles/actor.yaml",
}


# 原子写 JSON：临时文件 + fsync + rename，防半写损坏审计文件。
def write_json(path, value):
    # 统一为 Path。
    path = Path(path)
    # 确保父目录存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录下的临时文件名。
    temp = path.with_name(path.name + ".tmp")
    # 写入临时文件。
    with temp.open("w") as f:
        # indent=2 便于人读；default=str 兜底序列化不可 JSON 化的对象。
        json.dump(value, f, indent=2, default=str)
        # 补一个换行（POSIX 文本文件约定）。
        f.write("\n")
        # 刷到内核缓冲。
        f.flush()
        # fsync 落盘：即使断电，临时文件内容也是完整的。
        os.fsync(f.fileno())
    # 原子替换为正式文件名（rename 在同一文件系统上是原子的）。
    temp.replace(path)


# 计算文件 SHA-256（分块读取，支持大文件）。
def sha(path):
    # 新建摘要器。
    digest = hashlib.sha256()
    # 以二进制打开目标文件。
    with Path(path).open("rb") as f:
        # 每次 1 MiB 分块读取直到 EOF。
        for block in iter(lambda: f.read(1 << 20), b""):
            # 累积更新摘要。
            digest.update(block)
    # 返回十六进制摘要。
    return digest.hexdigest()


# 协议校验入口：校验配置哈希、67 任务队列、（可选）上游 ALE 仓库钉死状态；返回协议锁内容。
def protocol(ale_root=None):
    # 读取协议锁 JSON。
    lock = json.loads(LOCK_PATH.read_text())
    # 逐个核对锁内登记的配置文件哈希：任何角色配置漂移立即失败（实验不可复现）。
    for relative, expected in lock["config_sha256"].items():
        if sha(RSIAGENT / relative) != expected:
            raise RuntimeError("Role configuration drift: " + relative)
    # 任务队列必须恰好 67 个且互不相同（覆盖与唯一性都不能变）。
    tasks = lock["tasks"]
    if len(tasks) != 67 or len(set(tasks)) != 67:
        raise RuntimeError("Pinned task coverage/uniqueness changed")
    # 锁内登记的任务卡集合必须与队列一致。
    if set(tasks) != set(lock["task_cards"]):
        raise RuntimeError("Pinned task cards differ from the cohort")
    # 传入上游 ALE 仓库位置时，进一步钉死上游状态。
    if ale_root is not None:
        # ALE 仓库路径。
        ale = Path(ale_root)
        # 读取上游仓库当前 HEAD 提交号。
        head = subprocess.check_output(
            ["git", "-C", str(ale), "rev-parse", "HEAD"], text=True
        ).strip()
        # 必须与锁内钉死的 ALE 提交一致。
        if head != lock["ale_commit"]:
            raise RuntimeError("ALE revision differs from the pinned release")
        # 官方 Near-Term 队列文件哈希必须一致（上游没有悄悄改队列）。
        if sha(ale / "selected_tasks/full/near-term.txt") != lock["cohort_sha256"]:
            raise RuntimeError("Canonical Near-Term cohort changed")
        # 逐个任务核对公开任务卡哈希（任务元数据漂移即失败）。
        for task, expected in lock["task_cards"].items():
            if sha(ale / "tasks" / task / "task_card.json") != expected:
                raise RuntimeError("Task metadata drift: " + task)
    # 全部通过：返回协议锁内容供调用方使用。
    return lock


# 加载指定角色的配置；可选地把记忆目录写进配置。
def role(name, memory_dir=""):
    # 延迟导入配置加载器（保持本模块在宿主进程的轻依赖）。
    from config.runtime_paths import normalize_verifier_config_paths
    from config.settings import load

    # 加载 ROLES 表中该角色对应的 YAML 配置。
    cfg = load(str(RSIAGENT / "config" / ROLES[name]))
    # 把配置中的相对路径规范化为基于仓库根的绝对路径（verifier 路径校验用）。
    normalize_verifier_config_paths(cfg, RSIAGENT)
    # 指定了记忆目录：写入配置，让 Actor 会话引用该记忆。
    if memory_dir:
        cfg.env_memory_dir = str(memory_dir)
    # 返回角色配置。
    return cfg


# 构造冻结记忆的指纹记录：真实目录 + 逐文件字节数/SHA-256 + 整树聚合摘要。
def memory_record(path):
    # Match RSIAgent's canonical memory digest without importing its core package
    # into the ALE host process (CUA also has a top-level package named core).
    # 【中文注释】与 RSIAgent 规范记忆摘要算法保持一致，但绝不把 RSIAgent core 顶层包
    # 导入 ALE 宿主进程——CUA 也有同名顶层包 core，导入会冲突。
    # 记忆根必须是真实目录（符号链接或缺失都视为非法）。
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Frozen memory must be a real directory without a symlink")
    # 逐文件记录表。
    files = {}
    # 排序遍历整棵树（保证摘要确定性）。
    for candidate in sorted(root.rglob("*")):
        # 任何符号链接都拒绝：防止把指针冒充内容、或经链接逃逸出记忆树。
        if candidate.is_symlink():
            raise RuntimeError("Frozen memory contains a symlink: " + str(candidate))
        # 普通文件：记录字节数与内容 SHA-256。
        if candidate.is_file():
            content = candidate.read_bytes()
            files[candidate.relative_to(root).as_posix()] = {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        elif not candidate.is_dir():
            # 既不是文件也不是目录（设备/管道等特殊文件）：拒绝。
            raise RuntimeError(
                "Frozen memory contains a special file: " + str(candidate)
            )
    # 文件名 → 内容哈希的映射。
    hashes = {name: record["sha256"] for name, record in files.items()}
    # 整树聚合摘要：对规范化 JSON（排序键、紧凑分隔符）再做一次 SHA-256。
    digest = hashlib.sha256(
        json.dumps(
            hashes,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    # 返回指纹记录：解析后的绝对路径、聚合摘要与逐文件明细。
    return {"path": str(root.resolve()), "sha256": digest, "files": files}


# 指令文本 → 8 词 shingle 集合（审计用指纹）。
def instruction_shingles(text):
    """Same eight-word normalization as RSIAgent's audit, with no agent imports."""
    # 【中文注释】与 RSIAgent 审计相同的 8 词归一化，且不导入任何 Agent 模块。
    # 全小写后抽取连续字母/数字词元。
    words = re.findall(r"[a-z0-9]+", text.lower())
    # 滑动窗口构造全部 8-gram（不足 8 词则为空集）。
    return {" ".join(words[i : i + 8]) for i in range(len(words) - 7)}


# 阶段边界校验：重算冻结记忆指纹，必须与传入记录完全一致。
def verify_memory(record):
    # 按记录中的路径重新构造指纹记录。
    actual = memory_record(record["path"])
    # 任何一个字节变了都会导致记录不相等：阶段边界被破坏，立即失败。
    if actual != record:
        raise RuntimeError("Frozen memory changed after its phase boundary")
    # 返回重算结果（调用方可直接复用）。
    return actual


# 计算发布源码的整体指纹：全部源文件哈希的规范化 JSON 再取 SHA-256。
def source_fingerprint(root=None):
    # 未指定根目录时默认仓库根。
    root = Path(root) if root is not None else RSIAGENT
    # 相对路径 → 文件哈希 的清单。
    files = {}
    # 遍历七个源码目录（与 freeze_source 的冻结范围一致）。
    for directory in ("benchmarks", "config", "core", "env", "explore", "llm", "tools"):
        # 排序遍历保证清单确定性。
        for p in sorted((root / directory).rglob("*")):
            # 只要普通源文件：排除缓存目录与 .pyc。
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith(".pyc")
            ):
                # 以仓库相对 POSIX 路径为键登记哈希。
                files[p.relative_to(root).as_posix()] = sha(p)
    # 仓库根下的顶层 .py 文件（如 run_ale.py）也纳入。
    for p in sorted(root.glob("*.py")):
        files[p.name] = sha(p)
    # 清单 JSON（排序键）的整体 SHA-256 即源码指纹。
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


# 从钉死队列中挑选子集；无子集文件时返回全队列。
def select_tasks(lock, task_file=None):
    """Select a reviewed cohort subset without operator identities."""
    # 【中文注释】从受审阅的队列中选子集，不暴露任何操作者身份信息。
    # 钉死队列。
    tasks = lock["tasks"]
    # 提供了任务清单文件：逐行解析。
    if task_file is not None:
        # 去空白、跳过空行与 # 注释行。
        selected = [
            line.strip()
            for line in Path(task_file).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        # 校验：非空、无重复、且必须是钉死队列的子集。
        if (
            not selected
            or len(set(selected)) != len(selected)
            or not set(selected) <= set(tasks)
        ):
            raise ValueError(
                "Task list must be a nonempty unique subset of the pinned cohort"
            )
        # 返回选中的子集。
        return selected
    # 未指定文件：完整队列。
    return list(tasks)


# 追加一条事件到 events.jsonl（审计时间线）。
def event(root, kind, payload):
    # 组装事件记录：UTC ISO 时间 + 事件类型 + 任意负载。
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": kind, **payload}
    # 以追加模式写入事件文件。
    with (Path(root) / "events.jsonl").open("a") as f:
        # 写一行 JSON（default=str 兜底）并补换行。
        f.write(json.dumps(record, default=str) + "\n")
        # 立即刷盘：进程崩溃也尽量保留事件痕迹。
        f.flush()
