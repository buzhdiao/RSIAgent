"""ALE provisioning/grading lives only in this outer host process."""
# 【中文注释·模块定位】ALE 的供给（provisioning）与评分（grading）只存在于这个外层
# 宿主进程：任务清单规划（plan）、冻结发布源码、按"谱系"驱动 baseline/phase1/phase2/
# phase3 各阶段、拉取输出并调用官方评分器、汇总状态。worker 子进程（benchmarks.ale.worker）
# 是密封智能体，接触不到评分——这是 sealed evaluation 的物理隔离边界。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# asyncio：并发供给沙箱、驱动子进程与信号处理。
import asyncio
# json：任务卡解析与结果读取。
import json
# logging：异常日志。
import logging
# os：环境变量与进程信息。
import os
# shutil：冻结源码时复制目录树。
import shutil
# signal：SIGTERM/SIGINT 优雅停机。
import signal
# sys：sys.path 隔离调整。
import sys
# traceback：任务失败的完整栈记录。
import traceback
# asdict：沙箱对象转 JSON 字典。
from dataclasses import asdict
# datetime/timezone：状态文件的 UTC 时间戳。
from datetime import datetime, timezone
# Path：输出目录与任务卡路径。
from pathlib import Path

# Capacity：主机容量池。
from .capacity import Capacity
# protocol：协议常量与工具（8-gram、协议校验、任务选择、哈希、指纹、记忆校验、写盘）。
from .protocol import (
    RSIAGENT,
    instruction_shingles,
    protocol,
    select_tasks,
    sha,
    source_fingerprint,
    verify_memory,
    write_json,
)

# 钉死的镜像版本号（HuggingFace qcow2 数据集 revision）。
IMAGE_REVISION = "31374caa105f15c9cf3c20fe6abcf9e40ec1a636"
# 钉死的 checkpoint 运行镜像（按摘要锁定，保证 runner 可复现）。
BASE_RUNNER = "agentslastexam/ale-qemu@sha256:cc3ead471566c27f7c394c8748ff2e69124f310dbe35fce5e4a5177bc947232f"
# 模型凭据允许下发的三个环境变量键（仅这些会进入 worker 子进程）。
MODEL_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


# 构造 QEMU provider 配置：两个快照（Ubuntu/Win10）的镜像来源与 runner。
def provider_config(cache, runner_image):
    # 返回快照配置：tag → 镜像名、qcow2 数据源（HF 数据集）、钉死 revision、缓存根、
    # runner 镜像与"绝不拉取"策略（只用本地已构建镜像）。
    return {
        "snapshots": {
            tag: {
                "image": name,
                "disk_source": f"hf://agents-last-exam/ale-images-qcow2/{name}.qcow2",
                "hf_revision": IMAGE_REVISION,
                "root": str(cache),
                "runner_image": runner_image,
                "runner_pull_policy": "never",
            }
            for tag, name in [
                ("cpu-free-ubuntu", "ale-ubuntu22"),
                ("cpu-free", "ale-win10"),
            ]
        }
    }


# 把上游 ALE 仓库挂到 sys.path 最前，并保证本进程没被 RSIAgent 包污染。
def install_ale_path(root):
    # 解析 ALE 根目录（展开 ~ 与符号链接）。
    root = Path(root).expanduser().resolve()
    # The model subprocess has its own RSIAgent environment. Keep this grader
    # process free of RSIAgent's top-level core/config/tools namespaces.
    # 【中文注释】模型子进程有它自己的 RSIAgent 环境。评分（grader）进程必须保持
    # 干净：不得先导入 RSIAgent 的顶层 core/config/tools 命名空间（会与 ALE 冲突，
    # 也破坏评分侧与学习侧隔离）。
    # 检查 core 模块是否已被导入且来自本仓库；已导入即拒绝：必须另起 run_ale.py 进程。
    core = sys.modules.get("core")
    if (
        core
        and getattr(core, "__file__", None)
        and Path(core.__file__).resolve().is_relative_to(RSIAGENT)
    ):
        raise RuntimeError(
            "ALE host already imported RSIAgent core; start a separate run_ale.py process"
        )
    # 把 sys.path 里指向本仓库的条目剔除（防后续误导入）。
    sys.path[:] = [p for p in sys.path if Path(p or os.getcwd()).resolve() != RSIAGENT]
    # 把 ALE 根目录插到最前（其 ale_run 包优先生效）。
    sys.path.insert(0, str(root))
    # 返回解析后的根目录。
    return root


# 规划动作：校验协议、加载任务卡、解析每个任务的资源形状，产出可审计的运行清单（manifest）。
def plan(args):
    # 安装 ALE 路径。
    ale = install_ale_path(args.ale_root)
    # 读取并校验协议锁（配置/队列/ALE 提交/任务卡）。
    lock = protocol(ale)
    # 延迟导入上游 ALE 供给件（sys.path 已就位）。
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.providers.qemu import QemuProvider
    from ale_run.tasks.loader import TaskLoader

    # 实例化 QEMU provider。
    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    # 按协议锁选定任务子集（--tasks 文件或全队列）。
    requested = select_tasks(lock, args.tasks)
    # 任务条目列表。
    entries = []
    # 指令语料收集（审计用）。
    instructions = []
    # 读取官方 Near-Term 队列清单并逐任务处理。
    for task in (ale / "selected_tasks/full/near-term.txt").read_text().splitlines():
        # Task cards contain public instructions and do not import graders.
        # 【中文注释】任务卡只含公开指令，不会导入评分器——host 可安全读取。
        # 解析公开任务卡。
        card = json.loads((ale / "tasks" / task / "task_card.json").read_text())
        # 收集任务卡指令（进审计语料）。
        instructions.append(card["taskPrompt"])
        if task not in requested:
            # 不在本次运行子集内：只贡献语料，不进 entries。
            continue
        # 快照类型。
        snapshot = card["vm"]["snapshot"]
        if snapshot == "gpu-free":
            # GPU 型任务：暂无 runner，标记为等待 GPU runner（不阻塞 CPU 队列）。
            entries.append(
                {
                    "task": task,
                    "variant": 0,
                    "os": "windows",
                    "snapshot": snapshot,
                    "status": "pending_gpu_runner",
                    "vcpus": 8,
                    "memory_gb": 32,
                }
            )
            continue
        # 加载任务元数据（variant 0）。
        meta = TaskLoader(str(ale / "tasks" / task)).load(variant_index=0)
        # 构造沙箱规格：快照、OS、机型、vCPU、内存。
        spec = SandboxSpec(
            snapshot=snapshot,
            os=meta["os_type"],
            machine_type=meta.get("machine_type"),
            vcpus=meta.get("vcpus"),
            memory_gb=meta.get("memory_gb"),
        )
        # 让 provider 解析出实际资源形状（默认值回填）。
        cpus, memory = provider._resolve_shape(
            provider.config.snapshots[snapshot], spec
        )
        # 登记"就绪"任务条目。
        entries.append(
            {
                "task": task,
                "variant": 0,
                "os": meta["os_type"],
                "snapshot": snapshot,
                "status": "ready",
                "vcpus": cpus,
                "memory_gb": memory,
            }
        )
        # 收集任务描述（进审计语料）。
        instructions.append(meta["description"])
    # 审计语料：全部指令文本的 8-gram 并集（排序）。
    corpus = sorted(set().union(*(instruction_shingles(text) for text in instructions)))
    # 组装 manifest（含实验臂、阶段预算、协议/源码指纹、provider 配置等）。
    return {
        "schema_version": 2,
        "benchmark": "ale",
        "cohort": "full/near-term",
        "cohort_count": 67,
        "arm": args.arm,
        "tasks": entries,
        "protocol_sha256": sha(RSIAGENT / "config/ale/protocol.lock.json"),
        "source_sha256": source_fingerprint(),
        "ale_commit": lock["ale_commit"],
        "actor_watchdog_active_seconds": 36000,
        "phase1_project_budget": 8,
        "phase1_parallelism": 4,
        "phase2_stop_policy": "curriculum_review",
        "study_design": lock["study_design"],
        "image_revision": IMAGE_REVISION,
        "judge_base_url": args.judge_base_url,
        "provider_config": provider_config(args.cache, args.runner_image),
        "max_task_lineages": args.concurrency,
        "host_vm_limit": args.vm_limit,
        "corpus": corpus,
    }


# 冻结发布源码：把七个源码目录与顶层 .py 复制到目标（运行的确切代码快照）。
def freeze_source(destination):
    # 目标目录 Path。
    destination = Path(destination)
    # 必须是不存在的新目录。
    destination.mkdir()
    # 复制七个源码目录，剔除缓存/.pyc/.env。
    for name in ("benchmarks", "config", "core", "env", "explore", "llm", "tools"):
        shutil.copytree(
            RSIAGENT / name,
            destination / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
        )
    # 复制仓库根的顶层 .py 文件（run_ale.py 等）。
    for path in RSIAGENT.glob("*.py"):
        shutil.copy2(path, destination / path.name)


# 宿主侧审计绑定：公开任务卡措辞 ↔ 发放的任务（不进入 Agent 提示词）。
def public_task_audit(task, card, instruction, visible_roots):
    """Bind public card wording to its issued task, for host-side auditing only."""
    # The pinned cards use both identity spellings. A contradictory card is
    # never a source of additional authorized wording.
    # 【中文注释】钉死的任务卡同时存在两种身份字段拼写（taskId/task_id）。相互矛盾的
    # 任务卡永远不能成为"额外授权措辞"的来源。
    # 收集任务卡中的身份字段；身份缺失/不一致或提示词非法：拒绝绑定。
    identities = [card[key] for key in ("taskId", "task_id") if key in card]
    if (not identities or any(value != task for value in identities)
            or not isinstance(card.get("taskPrompt"), str) or not card["taskPrompt"]):
        raise ValueError("Public task card does not match the selected ALE task")
    # 返回审计绑定记录：任务 ID、公开提示词、正式指令与可见根。
    return {
        "task_id": task,
        "task_prompt": card["taskPrompt"],
        "instruction": instruction,
        "visible_roots": list(visible_roots),
    }


# 正式运行：并发驱动每个任务谱系，串联 baseline/rsi 两臂的全部阶段，最终评分。
async def run(args, manifest):
    # 延迟导入上游 ALE 运行件与 dotenv（sys.path 已就位）。
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.env import ALEEnv
    from ale_run.environments.output_pull import pull_to_host
    from ale_run.environments.providers.qemu import QemuProvider
    from ale_run.environments.task_data import baked_in_sandbox
    from ale_run.tasks.driver import TaskDriver
    from ale_run.tasks.loader import TaskLoader
    from dotenv import dotenv_values

    # 输出根目录：必须是全新目录。
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    # 合并凭据：--env-file 的键值 + 环境变量中的模型密钥。
    secrets = {
        **dotenv_values(args.env_file),
        **{k: os.environ[k] for k in MODEL_KEYS if k in os.environ},
    }
    if not secrets.get("OPENROUTER_API_KEY"):
        # OpenRouter 密钥是模型调用的最低要求。
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing from --env-file or the environment"
        )
    # The existing upstream OpenAI SDK graders keep their exact model aliases.
    # Endpoint changes are recorded; these host judge variables never enter VMs.
    # 【中文注释】上游 OpenAI SDK 评分器沿用其确切的模型别名。端点变更会被记录在
    # manifest；这些宿主评分变量绝不进入 VM（密封边界）。
    if args.judge_base_url.rstrip("/") == "https://openrouter.ai/api/v1":
        # 走 OpenRouter：把 OpenRouter 密钥映射为 OpenAI SDK 变量。
        os.environ["OPENAI_API_KEY"] = secrets["OPENROUTER_API_KEY"]
    elif secrets.get("OPENAI_API_KEY"):
        # 自定义评分端点：要求显式提供 OPENAI_API_KEY。
        os.environ["OPENAI_API_KEY"] = secrets["OPENAI_API_KEY"]
    else:
        raise RuntimeError(
            "A custom judge endpoint requires OPENAI_API_KEY in --env-file"
        )
    # 设置评分器基础 URL。
    os.environ["OPENAI_BASE_URL"] = args.judge_base_url
    # 读取上游示例环境文件中的参考归档密码（评分器解密参考材料用）。
    benchmark_env = dotenv_values(Path(args.ale_root) / "secret/.env.example")
    os.environ["ALE_REFERENCE_ARCHIVE_PASSWORD"] = benchmark_env[
        "ALE_REFERENCE_ARCHIVE_PASSWORD"
    ]
    # 落盘 manifest（剔除 corpus 单独存）。
    write_json(
        root / "manifest.json", {k: v for k, v in manifest.items() if k != "corpus"}
    )
    # 单独落盘审计语料。
    write_json(root / "corpus.json", manifest["corpus"])
    # 冻结发布源码；冻结前后与 manifest 记录的指纹必须一致（运行绑定确切源码）。
    freeze_source(root / "release")
    if (
        source_fingerprint() != manifest["source_sha256"]
        or source_fingerprint(root / "release") != manifest["source_sha256"]
    ):
        raise RuntimeError("Source changed while freezing the run")
    # 实例化 provider。
    provider = QemuProvider(manifest["provider_config"])
    # 每任务状态表。
    tasks = {}
    # 存活的 worker 子进程表。
    children = {}
    # 全局停止事件。
    stop = asyncio.Event()
    # 启动信号量：同时最多 4 个 VM 在启动。
    boot_semaphore = asyncio.Semaphore(4)
    # 谱系信号量：同时最多 N 个任务谱系在跑。
    lineage_semaphore = asyncio.Semaphore(args.concurrency)

    # 更新某任务的状态并落盘 status.json（人机观察窗口）。
    def publish(task, **values):
        # 合并更新该任务的字段。
        tasks.setdefault(task, {}).update(values)
        # 写状态文件。
        write_json(
            root / "status.json",
            {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "tasks": tasks,
                "cohort_count": 67,
            },
        )

    # 全局停止：置位 stop 并向全部存活 worker 发 SIGTERM。
    def stop_all():
        # 置位停止事件。
        stop.set()
        # 逐个通知子进程。
        for child in children.values():
            if child.returncode is None:
                child.send_signal(signal.SIGTERM)

    # 注册信号处理器（事件循环级）。
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_all)

    # 执行单个任务的某个阶段：供给 VM → 跑 worker →（评估阶段）拉输出并评分。
    async def phase(entry, name, input_memory=None):
        # 任务 ID。
        task = entry["task"]
        # 阶段目录：<root>/<task>/<phase>，必须全新。
        directory = root / task / name
        directory.mkdir(parents=True, exist_ok=False)
        # 各阶段的练习沙箱数：phase1=4，phase2=1，其余 0。
        practice_count = 4 if name == "phase1" else 1 if name == "phase2" else 0
        # 需要供给的 VM 总数：练习数 +（非 phase1 再加 1 台目标机）。
        count = practice_count + (name != "phase1")
        # 磁盘预留：每台 VM 按 2 倍内存（phase1/2 需 checkpoint 快照）+32GiB 基线估算。
        reserve = count * (
            (2 if name in ("phase1", "phase2") else 1) * entry["memory_gb"] + 32
        )
        # 建容量租约对象。
        capacity = Capacity(args.pool_root, root, limit=args.vm_limit)
        # 发布"等待容量"状态。
        publish(task, phase="waiting_capacity", active_phase=name)
        # 等待并取得资源预留。
        await capacity.acquire(
            count,
            reserve,
            stop,
            vcpus=count * entry["vcpus"],
            memory_gb=count * entry["memory_gb"],
        )
        # 本阶段创建的全部环境（finally 清理）。
        environments = []
        # 任务驱动器/目标环境/复位服务先置 None。
        driver = target_env = reset_service = None
        # 阶段是否完整完成（决定 finally 是否删除客户机）。
        complete = False
        try:
            if stop.is_set():
                # 已请求停止。
                raise InterruptedError("Run stopped")
            # 加载任务元数据与任务数据。
            meta = TaskLoader(str(Path(args.ale_root) / "tasks" / task)).load()
            data = meta["task_data"]

            # 供给一台 VM：按沙箱规格创建环境并落盘沙箱描述。
            async def boot(practice, index):
                # 沙箱规格：快照/OS/资源 + 任务与 harness 标记（模型标签区分练习与正式）。
                spec = SandboxSpec(
                    snapshot=entry["snapshot"],
                    os=entry["os"],
                    vcpus=entry["vcpus"],
                    memory_gb=entry["memory_gb"],
                    task_id=task,
                    harness="rsiagent",
                    model_tag="rsi-practice" if practice else name,
                )
                # 创建 ALE 环境对象。
                env = ALEEnv(provider=provider, spec=spec)
                # 登记进本阶段环境列表。
                environments.append(env)
                # 启动限流：同时最多 4 台在 reset。
                async with boot_semaphore:
                    await env.reset_async()
                # 容器登记进容量租约。
                capacity.attach(env.sandbox.id)
                # 沙箱描述副本。
                sandbox = asdict(env.sandbox)
                # 标记是否练习客户机。
                sandbox.setdefault("metadata", {})["rsiagent_practice"] = practice
                # 落盘沙箱描述。
                write_json(directory / f"sandbox-{index}.json", sandbox)
                # 返回环境与描述。
                return env, sandbox

            # 发布"供给中"状态。
            publish(task, phase="provisioning", active_phase=name)
            # 并发供给全部 VM（练习机在前，目标机在后）。
            provisioned = await asyncio.gather(
                *(boot(i < practice_count, i) for i in range(count)),
                return_exceptions=True,
            )
            # 收集供给失败。
            failures = [item for item in provisioned if isinstance(item, BaseException)]
            if failures:
                # 任一失败：抛第一个异常（finally 清理其余）。
                raise failures[0]
            if name != "phase1":
                # phase1 之外都有目标机（最后一台）。
                target_env = provisioned[-1][0]
                # 把任务输入材料布置进目标沙箱。
                await baked_in_sandbox.stage_input(
                    target_env.sandbox, data, source="baked_in_sandbox"
                )
                # 建任务驱动器（评分/驱动用；session_rebuilder 支持跨复位重建会话）。
                driver = TaskDriver(
                    str(Path(args.ale_root) / "tasks" / task),
                    target_env.session,
                    variant=0,
                    os_type=entry["os"],
                    session_rebuilder=target_env.reset_session,
                )
                # 执行任务环境准备（部分任务需要）。
                await driver.setup()
            if name in ("phase1", "phase2"):
                # 学习阶段：提供"全新复位"RPC 服务（练习纪律）。
                from .lifecycle import ResetService

                # 本阶段全部沙箱 → 环境的映射（归属校验用）。
                owned_sandboxes = {env.sandbox.id: env for env in environments}

                # 复位回调：销毁重建指定沙箱；目标机复位后重建驱动器。
                async def reset_sandbox(sandbox_id):
                    # 引用外层 driver。
                    nonlocal driver
                    if stop.is_set():
                        # 停止后不再复位。
                        raise InterruptedError("Run stopped before a fresh reset")
                    # 找到该沙箱对应的环境（并从在管集合移除）。
                    env = owned_sandboxes.pop(sandbox_id, None)
                    if env is None:
                        # 只允许复位本阶段自己的沙箱。
                        raise RuntimeError(
                            "Reset does not name a sandbox owned by this phase"
                        )
                    # 是否目标机。
                    is_target = env is target_env
                    if is_target:
                        # 目标机复位前先关驱动器。
                        await driver.close()
                    # 记住旧沙箱。
                    previous_sandbox = env.sandbox
                    # 关闭环境但保留容器句柄信息。
                    await env.close_async(mode="keep")
                    # 删除旧沙箱。
                    await provider.release(previous_sandbox, mode="delete")
                    # 限流下重建环境。
                    async with boot_semaphore:
                        await env.reset_async()
                    # 新容器登记进租约。
                    capacity.attach(env.sandbox.id)
                    # 回到在管集合。
                    owned_sandboxes[env.sandbox.id] = env
                    if is_target:
                        # 目标机：重新布置输入并重建驱动器。
                        await baked_in_sandbox.stage_input(
                            env.sandbox, data, source="baked_in_sandbox"
                        )
                        driver = TaskDriver(
                            str(Path(args.ale_root) / "tasks" / task),
                            env.session,
                            variant=0,
                            os_type=entry["os"],
                            session_rebuilder=env.reset_session,
                        )
                        await driver.setup()
                    # 新沙箱描述。
                    sandbox = asdict(env.sandbox)
                    # 标记练习属性（非目标机即练习）。
                    sandbox.setdefault("metadata", {})[
                        "rsiagent_practice"
                    ] = not is_target
                    # 落盘复位记录。
                    write_json(
                        directory / ("reset-" + env.sandbox.id + ".json"), sandbox
                    )
                    # 返回给 RPC 客户端。
                    return sandbox

                # 启动复位 RPC 服务并把端点写进 worker-spec。
                reset_service = ResetService(reset_sandbox)
            # 组装 worker-spec：阶段、指令、工作目录、语料、练习沙箱、可见根。
            worker_spec = {
                "phase": name,
                "instruction": meta["description"],
                "work_dir": str(directory / "rsiagent"),
                "corpus": str(root / "corpus.json"),
                "practice_sandboxes": [sb for env, sb in provisioned[:practice_count]],
                "visible_roots": [
                    p for p in (data.input_dir, data.remote_output_dir) if p
                ],
            }
            if name in ("phase1", "phase2"):
                # 学习阶段：附公开任务审计绑定。
                card = json.loads(
                    (Path(args.ale_root) / "tasks" / task / "task_card.json").read_text()
                )
                # This metadata stays in Audit; the agent's instruction is unchanged.
                # 【中文注释】该元数据只进审计（Audit）；Agent 收到的指令保持原样不变。
                worker_spec["audit_public_task"] = public_task_audit(
                    task, card, meta["description"], worker_spec["visible_roots"]
                )
            if reset_service is not None:
                # 复位端点。
                worker_spec["reset_endpoint"] = reset_service.path
            if target_env is not None:
                # 目标沙箱描述。
                worker_spec["sandbox"] = provisioned[-1][1]
            if input_memory is not None:
                # 上游冻结记忆：先校验指纹再下发。
                verify_memory(input_memory)
                worker_spec["input_memory"] = input_memory
            # 落盘 worker-spec。
            write_json(directory / "worker-spec.json", worker_spec)
            # 子进程环境：只透传一组基础变量（不泄露宿主环境）。
            child_env = {
                k: os.environ[k]
                for k in (
                    "HOME",
                    "USER",
                    "LANG",
                    "PATH",
                    "TMPDIR",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                )
                if k in os.environ
            }
            # 注入模型凭据（仅 MODEL_KEYS 三个）。
            child_env.update(
                {k: v for k, v in secrets.items() if k in MODEL_KEYS and v}
            )
            # 指向冻结源码的运行环境变量。
            child_env.update(
                PYTHONPATH=str(root / "release"),
                RSIAGENT_ROOT=str(root / "release"),
                RSIAGENT_ENV_FILE=str(root / "release" / ".env"),
                PYTHONUNBUFFERED="1",
            )
            # 启动密封 worker 子进程（日志重定向到 worker.log）。
            with (directory / "worker.log").open("wb") as output:
                child = await asyncio.create_subprocess_exec(
                    str(args.worker_python),
                    "-m",
                    "benchmarks.ale.worker",
                    str(directory / "worker-spec.json"),
                    cwd=str(root / "release"),
                    env=child_env,
                    stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                )
                # 登记子进程（停机时通知）。
                children[task] = child
                # 发布"运行中"状态。
                publish(task, phase="agent_running", active_phase=name, pid=child.pid)
                # 等待 worker 退出。
                code = await child.wait()
                # 从存活表移除。
                children.pop(task, None)
            # 读取 worker 结果。
            result = json.loads((directory / "rsiagent/worker-result.json").read_text())
            if code or stop.is_set():
                # 非零退出或中途停止：阶段失败。
                raise RuntimeError(
                    "Worker did not complete phase: "
                    + str(result.get("error") or result.get("status"))
                )
            if input_memory is not None:
                # 阶段结束后再校验上游记忆未被改动。
                verify_memory(input_memory)
            if name in ("baseline", "phase3"):
                # 评估阶段：确认可安全评分 → 拉取输出 → 官方评分。
                if not result.get("safe_to_grade"):
                    # 候选回滚未确认：绝不评分。
                    raise RuntimeError(
                        "Candidate rollback not confirmed; grading blocked"
                    )
                # 发布"收集输出"状态。
                publish(task, phase="collecting_output", active_phase=name)
                # 把任务输出从沙箱拉回宿主。
                pulled = await pull_to_host(
                    target_env.sandbox, data, dest_dir=directory / "output"
                )
                # 落盘拉取记录。
                write_json(directory / "output-pull.json", pulled)
                if pulled.get("errors"):
                    # 拉取有错误：失败。
                    raise RuntimeError("Output collection failed")
                # 发布"评分中"状态。
                publish(task, phase="grading", active_phase=name)
                # 布置评分参考材料。
                await baked_in_sandbox.stage_reference(
                    target_env.sandbox, data, source="baked_in_sandbox"
                )
                # 调用官方评估器（最长 2 小时）。
                evaluation = await asyncio.wait_for(driver.evaluate(), timeout=7200)
                # 落盘评估原始结果。
                write_json(directory / "eval-result.json", evaluation)
                # 延迟导入分数校验。
                from .report import valid_score

                if evaluation.get("error") or evaluation.get("score") is None:
                    # 评估器没给出有效分数：失败。
                    raise RuntimeError(
                        "Official evaluator did not return a valid score"
                    )
                # 把校验后的分数与官方评估并入结果。
                result.update(
                    score=valid_score(evaluation["score"]),
                    official_evaluation=evaluation,
                )
            # 学习阶段必须"就绪可过渡"且确认零评估器调用。
            elif (
                not result.get("transition_ready")
                or result.get("official_evaluator_calls") != 0
            ):
                raise RuntimeError("Learning phase is not transition-ready")
            # 落盘阶段结果。
            write_json(directory / "result.json", result)
            # 标记完成。
            complete = True
            # 返回结果。
            return result
        finally:
            if reset_service is not None:
                # 关停复位 RPC 服务。
                await reset_service.close()
            if driver is not None:
                try:
                    # 关闭任务驱动器。
                    await driver.close()
                except Exception:
                    # 关闭失败只记日志。
                    logging.exception("Could not close ALE task driver")
            # Failed guests remain available for diagnosis; actual live containers
            # continue to count against capacity after their reservation releases.
            # 【中文注释】失败阶段保留客户机供诊断；租约释放后，存活的实际容器仍会被
            # 其他会话的容量统计计入（容量池的启发式覆盖）。
            for env in environments:
                try:
                    # 完成则删除客户机，失败则保留。
                    await env.close_async(mode="delete" if complete else "keep")
                except Exception:
                    logging.exception("Could not close ALE environment")
            # 释放容量租约。
            capacity.release()

    # 单个任务谱系：按实验臂串联各阶段；失败不自动重试。
    async def unit(entry):
        # 任务 ID。
        task = entry["task"]
        # 发布排队状态。
        publish(task, phase="queued", os=entry["os"])
        if entry["status"] != "ready":
            # 非 ready（如等待 GPU runner）：直接发布状态返回。
            publish(task, phase=entry["status"])
            return
        # 谱系限流：同时最多 --concurrency 个任务在跑。
        async with lineage_semaphore:
            try:
                if args.arm in ("baseline", "both"):
                    # baseline 臂：直接评估（无记忆）。
                    result = await phase(entry, "baseline")
                    publish(task, baseline_score=result["score"], baseline="completed")
                if args.arm in ("rsi", "both"):
                    # rsi 臂：phase1（BRS）→ phase2（DRS，携带 phase1 记忆）→ phase3（冻结记忆评估）。
                    p1 = await phase(entry, "phase1")
                    p2 = await phase(entry, "phase2", p1["memory"])
                    p3 = await phase(entry, "phase3", p2["memory"])
                    publish(task, rsi_score=p3["score"], rsi="completed")
                # 发布完成状态。
                publish(task, phase="completed")
            except BaseException as exc:
                # 任何失败：记录并发布中断/基础设施错误状态（不自动重试）。
                logging.exception("Task %s stopped; no automatic retry", task)
                publish(
                    task,
                    phase="interrupted" if stop.is_set() else "infrastructure_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
                # 落盘错误与完整栈。
                write_json(
                    root / task / "error.json",
                    {"error": str(exc), "traceback": traceback.format_exc()},
                )

    # 并发驱动全部任务谱系。
    await asyncio.gather(*(unit(entry) for entry in manifest["tasks"]))
    # 汇总批结果：全部任务状态与整体完成标记。
    write_json(
        root / "batch-result.json",
        {
            "tasks": tasks,
            "cohort_count": 67,
            "complete": all(t["phase"] == "completed" for t in tasks.values()),
        },
    )
