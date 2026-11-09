# 【中文注释·模块定位】公共批处理（public batch）回归测试：覆盖 run_osworld 批量
# 调度器与 ALE 公共入口的"对外承诺"。测试策略分三层：
# 1) 入口可用性：所有公共脚本（run_osworld / run_ale / -m 模块入口）在 OSWorld 未安装
#    时 --help 仍必须成功，保证文档中的命令永不因环境缺失而崩溃；
# 2) 计划构建正确性：baseline 计划覆盖全部 108 任务、rsi 计划逐字节保留最新协议模板、
#    方向文件原文内联，且 phase 顺序为 1→2→3；
# 3) 故障隔离与安全边界：单个任务失败只封锁其后代（lineage），其余任务照跑；
#    ALE 任务子集选择拒绝未知/重复任务；setup_ale 强制 worker 与 grader 环境分离；
#    公共任务面只解析类属性字符串（绝不执行任务代码，防任意代码注入）；
#    就绪检查在启动任何 VM 之前拦截缺失的 Windows 镜像。
# 全程不依赖真实 LLM/VM：用 monkeypatch 替换子进程与协程桩。
"""Public batch execution preserves phase order and isolates task failures."""
# 模块 docstring：公共批处理执行必须保持 phase 顺序，并隔离单任务失败。

import json
# 导入 json：解析协议模板与 status.json 断言。
import os
# 导入 os：复制当前环境变量给子进程。
from pathlib import Path
# 导入 Path：路径对象处理。
import subprocess
# 导入 subprocess：真实子进程调用（--help 冒烟）与被替换的目标。
import sys
# 导入 sys：取当前解释器路径启动子进程。
from types import SimpleNamespace
# 导入 SimpleNamespace：构造轻量伪对象（伪 CompletedProcess / 伪 args）。

import pytest
# 导入 pytest：参数化与异常断言。

import run_osworld as batch
# 以 batch 别名导入批量调度器：被测主体（build_plan/execute/ROOT/TEMPLATE）。
from scripts import setup_ale
# 导入 ALE 安装脚本：被测对象 install（worker/grader 环境分离）。
from benchmarks.ale.protocol import protocol, select_tasks
# 导入 ALE 协议：protocol() 生成锁定协议，select_tasks 解析任务子集文件。


def test_entrypoint_help_without_benchmark_installations(tmp_path):
    # 测试：即使 OSWorld/ALE 基准未安装，所有公共入口 --help 也必须成功退出。
    # 保证用户按 README 复制命令时，缺失环境只影响真实运行、不影响文档入口。
    environment = dict(
        os.environ,
        RSIAGENT_ROOT=str(batch.ROOT),
        OSWORLD_ROOT=str(tmp_path / "missing-osworld"),
    )
    # 构造子进程环境：RSIAGENT_ROOT 指向仓库根，OSWORLD_ROOT 指向不存在的目录。
    commands = [
        ["run_osworld.py"],
        ["run_ale.py"],
        *[["-m", "benchmarks.osworld." + name]
          for name in ("task", "phase1", "phase2", "pipeline")],
    ]
    # 枚举全部公共入口：两个顶层脚本 + 四个 -m 模块入口（task/phase1/phase2/pipeline）。
    for command in commands:
        # 逐个入口冒烟。
        result = subprocess.run(
            [sys.executable, *command, "--help"],
            cwd=batch.ROOT, env=environment, text=True, capture_output=True,
            timeout=30,
        )
        # 以当前解释器真实启动 --help，捕获 stdout/stderr，30 秒超时兜底。
        assert result.returncode == 0, (command, result.stderr)
        # 退出码必须为 0：入口脚本在导入期不许因基准缺失而崩溃。
        assert "usage:" in result.stdout
        # stdout 必须包含 argparse 的 usage 行：确系打印了帮助而非空转。


def test_baseline_plan_covers_full_release_without_creating_outputs():
    # 测试：baseline 计划必须完整覆盖发布版 108 个任务，且计划只是"计划"——
    # 纯内存构建，不创建任何输出目录或文件。
    plan = batch.build_plan("portable_smoke", "baseline")
    # 构建便携冒烟 profile 的 baseline 计划。
    assert len(plan["jobs"]) == 108
    # 任务数恰为 108：与发布版任务全集一一对应。
    assert len({job["task"] for job in plan["jobs"]}) == 108
    # 108 个 job 的 task 互不相同：无重复、无遗漏。
    assert all(len(job["commands"]) == 1 for job in plan["jobs"])
    # 每个 job 只有一条命令：baseline 只跑一次（无 phase1/phase2 续跑）。
    assert all("--seed" in job["commands"][0] for job in plan["jobs"])
    # 每条命令都带 --seed：结果可复现（种子是命令行的一部分）。


def test_rsi_plan_preserves_latest_protocol_and_public_direction():
    # 测试：rsi 计划必须逐字段保留最新协议模板（config/osworld/rsi.example.json），
    # 方向文件原文内联，且命令顺序严格为 phase1→phase2→phase3。
    plan = batch.build_plan("portable_rsi", "rsi", tasks=["task_080"])
    # 只构建 task_080 的 rsi 计划，缩小断言面。
    job = plan["jobs"][0]
    # 取唯一 job。
    reference = json.loads(batch.TEMPLATE.read_text())
    # 读取协议模板原文作为参照基准。
    assert (
        job["protocol"]["phase1"]["project_budget"]
        == reference["phase1"]["project_budget"]
    )
    # phase1 的 project_budget 与模板逐字相等：计划生成不得篡改预算。
    assert job["protocol"]["phase2"] == reference["phase2"]
    # phase2 整段与模板相等：协议随模板实时更新，不存在陈旧拷贝。
    assert (
        job["direction"]
        == (batch.ROOT / "config/target_queries/task_080.md").read_text()
    )
    # 公共方向 = 目标查询文件原文：不加工、不截断。
    assert [cmd[cmd.index("--phase") + 1] for cmd in job["commands"]] == [
        "phase1",
        "phase2",
        "phase3",
    ]
    # 从每条命令中抽出 --phase 的取值，验证顺序恰为 phase1/phase2/phase3：
    # 公共批处理不得乱序或漏掉任何阶段。


def test_batch_stops_failed_lineage_and_continues_other_tasks(tmp_path, monkeypatch):
    # 测试：某任务失败后，该任务后续命令（其"后代"）全部不再执行，
    # 但其他任务不受牵连继续跑完；status.json 记录成败；同目录重跑被拒防覆盖。
    jobs = [
        dict(task=name, commands=[[name, str(i)] for i in range(3)], result_paths=[])
        for name in ("fails", "passes")
    ]
    # 手工构造两个假任务：fails 与 passes，各 3 条命令（3 个后代步骤）。
    calls = []
    # 记录实际被执行的命令序列。
    def child(argv, **kwargs):
        # 伪子进程：替换 batch.subprocess.run。
        if "--verify-only" in argv:
            return SimpleNamespace(returncode=0)
        # --verify-only 前置校验总是通过（本测试不关心）。
        calls.append(argv)
        # 记录真正执行的命令。
        kwargs["stdout"].write("scripted child\n")
        # 向调用方提供的 stdout 写一行输出，模拟子进程有产出。
        return SimpleNamespace(returncode=int(argv == ["fails", "1"]))
        # 唯一失败点：fails 的第 2 条命令返回码 1，其余命令返回码 0。
    monkeypatch.setattr(batch.subprocess, "run", child)
    # 用伪子进程替换批量执行器里的 subprocess.run，实现确定性故障注入。
    plan = dict(output=str(tmp_path / "batch"), jobs=jobs)
    # 计划：输出根指向临时目录。
    assert batch.execute(plan, 1, tmp_path / "OSWorld-V2") == 1
    # 并发度 1 执行整个计划：因有任务失败，总返回码必须为 1。
    assert calls == [
        ["fails", "0"],
        ["fails", "1"],
        ["passes", "0"],
        ["passes", "1"],
        ["passes", "2"],
    ]
    # 关键断言：fails 只跑到命令 1（命令 2 被封锁），passes 三条全跑——
    # 失败只中断同任务后代，不传染其他任务。
    status = json.loads((tmp_path / "batch/status.json").read_text())
    # 读取批处理状态文件。
    assert [s["status"] for s in status] == ["failed", "completed"]
    # fails 记为 failed、passes 记为 completed：成败落盘可追溯。
    with pytest.raises(FileExistsError):
        batch.execute(plan, 1, tmp_path / "OSWorld-V2")
    # 相同输出目录再次执行必须抛 FileExistsError：防止覆盖已有批次结果。


def test_ale_batch_selection_rejects_unknown_and_duplicate_tasks(tmp_path):
    # 测试：ALE 任务子集文件的白名单校验——未知任务、空行、重复任务都拒绝，
    # 只接受"注释行 + 协议内任务"的合法形式。
    lock = protocol()
    # 生成当前 ALE 协议锁定快照（含合法任务清单）。
    path = tmp_path / "tasks.txt"
    # 子集文件路径。
    for text in ("unknown/task\n", "\n", lock["tasks"][0] + "\n" + lock["tasks"][0]):
        # 三种非法输入：未知任务、纯空行、重复任务。
        path.write_text(text)
        # 依次写入。
        with pytest.raises((ValueError, RuntimeError)):
            select_tasks(lock, path)
        # 每种都必须抛 ValueError 或 RuntimeError（fail-closed）。
    path.write_text("# subset\n" + lock["tasks"][0] + "\n")
    # 合法输入：注释行 + 一个协议内任务。
    assert select_tasks(lock, path) == [lock["tasks"][0]]
    # 解析结果恰为该任务：注释被忽略、去重合法。


def test_setup_refuses_shared_worker_and_grader_environment(tmp_path):
    # 测试：setup_ale 安装时，worker 环境不得与 grader 环境（ALE 的 .venv）共用。
    ale = tmp_path / "ale"
    # 让 worker 环境路径直接等于 ALE 根的 .venv（即 grader 自己的环境）。
    with pytest.raises(ValueError, match="dedicated worker"):
        setup_ale.install(tmp_path, ale, ale / ".venv", "uv")
    # 传入共用路径必须抛 ValueError，消息含 "dedicated worker"：防止 Agent 与
    # 评测器共享 Python 环境（否则包/缓存互相污染，破坏结果接地）。


def test_setup_uses_separate_environments_and_pinned_dependencies(
    tmp_path, monkeypatch
):
    # 测试：合法安装必须（a）以 --frozen 按 uv.lock 钉死依赖建独立 worker 环境，
    # （b）第三次 uv pip install 把本仓库 requirements.txt 装进 worker 的 python，
    # （c）全程绝不触碰仓库自带的 .venv（grader 环境不可被写入）。
    ale = tmp_path / "ale"
    # ALE 检出目录。
    ale.mkdir()
    # 创建目录以放置 uv.lock。
    (ale / "uv.lock").write_bytes(
        (batch.ROOT / "benchmarks/ale/ale.uv.lock").read_bytes()
    )
    # 拷贝仓库内的 ale.uv.lock：--frozen 安装将严格按它解析依赖。
    commands = []
    # 收集 setup_ale 发出的所有子进程命令。
    monkeypatch.setattr(
        setup_ale.subprocess, "check_output", lambda *a, **kw: protocol()["ale_commit"]
    )
    # 替换 check_output：git 查询 ALE 提交号时返回协议锁定的 ale_commit。
    monkeypatch.setattr(
        setup_ale.subprocess, "run", lambda argv, **kw: commands.append(argv)
    )
    # 替换 run：不真正执行 uv，只记录命令（worker 环境是假的）。
    worker = tmp_path / "worker"
    # 独立的 worker 环境目录。
    setup_ale.install(batch.ROOT, ale, worker, "uv")
    # 执行安装。
    assert [
        "uv",
        "sync",
        "--frozen",
        "--project",
        str(ale),
        "--python",
        "3.12",
    ] in commands
    # 必须发出 `uv sync --frozen`（按锁文件、Python 3.12）建 grader 环境。
    installs = [c for c in commands if c[:3] == ["uv", "pip", "install"]]
    # 过滤出所有 uv pip install 命令。
    assert len(installs) == 3
    # 恰好三次安装（grader 依赖、ALE 自身、rsiagent 依赖）。
    assert installs[-1][4] == str(worker / "bin/python")
    # 最后一次安装目标是 worker 环境的 python：rsiagent 装进 worker，不是 grader。
    assert installs[-1][-1] == str(batch.ROOT / "requirements.txt")
    # 安装内容是仓库 requirements.txt：worker 依赖与仓库锁定版本一致。
    assert all(str(batch.ROOT / ".venv/bin/python") not in c for c in commands)
    # 任何命令都不得引用仓库 .venv：grader 环境全程只读。


def test_public_instructions_resolve_class_strings_without_executing_task_code(
    tmp_path,
):
    # 测试：公共任务面加载只做"类属性字符串求值"，绝不执行任务模块的语句——
    # 任务文件里模块级的 raise 永远不该被触发。
    from benchmarks.osworld.task_surface import load_public_task_surface
    # 局部导入被测函数：加载公共任务面（指令等）。
    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    # 构造一个假的 OSWorld 任务类目录。
    (
        folder / "task_001.py"
    ).write_text("""raise AssertionError("must never import this task")
class Task001:
    base_instruction = "  Make a table.  "
    instruction = base_instruction.strip()
""")
    # 任务文件第一行是 raise：若真的 import/执行了该模块，测试会立刻失败。
    # 类属性 instruction 是对 base_instruction 调 .strip() 的表达式。
    assert load_public_task_surface(tmp_path, "task_001").instruction == "Make a table."
    # 只求值类属性表达式得到剥离空白后的指令字符串，且没有触发模块级 raise。


def test_public_direction_omits_generated_credentials_and_uses_pinned_website(tmp_path):
    # 测试：公共方向文本必须剔除任务运行期生成的凭据（Overleaf 账号密码），
    # 并把动态网站 URL 钉死为固定 host 后缀；语料归一化后不含生成占位符。
    from benchmarks.osworld.task_surface import load_public_task_surface
    from explore.commit import normalize_instruction_for_corpus
    # 导入任务面加载器与语料归一化函数（被测链路两环）。
    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    # 构造假任务目录。
    (
        folder / "task_001.py"
    ).write_text("""URL = f"{build_website_url('editor')}/project"
class Task001:
    instruction = f"Edit {URL}.\\n\\nOverleaf login credentials - Email: {_OVERLEAF_USER_EMAIL}   Password: {_OVERLEAF_USER_PASSWORD}\\n\\nSave the result."
""")
    # 任务指令含运行期 URL（build_website_url 调用）与凭据变量名（模块级注入）。
    text = load_public_task_surface(
        tmp_path, "task_001", website_host_suffix="site.example"
    ).instruction
    # 用钉死的 host 后缀解析任务面：URL 被重写为固定域名。
    direction = normalize_instruction_for_corpus(text)
    # 语料归一化：进一步剥离凭据等动态内容。
    assert direction == "Edit https://editor.site.example/project.\n\nSave the result."
    # 最终方向只剩任务本身：URL 已钉死为 editor.site.example。
    assert "<run-generated>" not in direction
    # 不含 <run-generated> 占位标记：凭据段被整体剔除而非占位保留。


def test_public_instruction_rejects_arbitrary_calls(tmp_path):
    # 测试：公共指令求值只允许白名单函数（如 build_website_url）与字面量，
    # 任务里出现任意调用（如 open() 读文件）必须拒绝——防代码注入/泄密。
    from benchmarks.osworld.task_surface import load_public_task_surface

    folder = tmp_path / "evaluation_examples/task_class"
    folder.mkdir(parents=True)
    # 构造假任务目录。
    (folder / "task_001.py").write_text(
        'class Task001:\n    instruction = open("private.txt").read()\n'
    )
    # instruction 属性试图调用 open() 读取宿主文件：典型的任意代码注入。
    with pytest.raises(RuntimeError, match="static string expression"):
        load_public_task_surface(tmp_path, "task_001")
    # 加载必须抛 RuntimeError，消息含 "static string expression"：
    # 公共面只接受静态字符串表达式，调用一律 fail-closed。


def test_readiness_rejects_missing_windows_image_before_any_launch(tmp_path):
    # 测试：就绪预检（validate_readiness）必须在启动任何 VM 之前完成——
    # manifest 声称 windows 就绪但 readiness/images.json 缺 windows 磁盘时，
    # 预检必须直接失败，绝不放过半启动的批次。
    from benchmarks.ale.host import IMAGE_REVISION
    from benchmarks.ale.prepare import validate_readiness
    # 导入镜像修订号与就绪校验函数。
    worker = tmp_path / "python"
    worker.touch()
    # 伪造 worker python 可执行文件。
    disk = tmp_path / "linux.qcow2"
    disk.touch()
    # 伪造 linux 磁盘镜像文件。
    ready = tmp_path / "rsiagent-readiness"
    ready.mkdir()
    # 就绪清单目录。
    (ready / "images.json").write_text(
        json.dumps({"revision": IMAGE_REVISION, "disks": {"linux": str(disk)}})
    )
    # images.json 修订号正确，但只登记了 linux 磁盘——windows 磁盘缺失。
    args = SimpleNamespace(worker_python=worker, cache=tmp_path)
    # 伪 args：worker python 与缓存目录。
    manifest = {
        "tasks": [
            {"os": "linux", "status": "ready"},
            {"os": "windows", "status": "ready"},
        ]
    }
    # 任务清单声称两个 OS 都 ready（与磁盘事实矛盾）。
    with pytest.raises(RuntimeError, match="image readiness"):
        validate_readiness(args, manifest)
    # 预检必须抛 RuntimeError（消息含 "image readiness"）：
    # manifest 的口头 ready 不能替代磁盘就绪证据。


def test_ale_cli_dispatches_to_real_smoke_entrypoint(monkeypatch, tmp_path):
    # 测试：run_ale CLI 的 smoke 子命令必须真实分发到 smoke.provision_smoke 协程，
    # 并把 --os / --output 参数原样传下去。
    import run_ale
    from benchmarks.ale import smoke
    # 导入 CLI 入口与冒烟模块（被测分发目标）。
    seen = []
    # 记录协程收到的参数。

    async def provision(args):
        # 伪冒烟协程：替换真实 provision_smoke（不起真 VM）。
        seen.append((args.os, args.output))

    monkeypatch.setattr(smoke, "provision_smoke", provision)
    # 替换冒烟入口为记录型协程。
    run_ale.main(["smoke", "--os", "linux", "--output", str(tmp_path)])
    # 走真实 CLI 解析与分发。
    assert seen == [("linux", tmp_path)]
    # 协程收到 os=linux、output=临时目录：分发链路参数无损传递。
