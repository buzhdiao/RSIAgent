"""Real guest tests of RSI transport boundaries using synthetic, ungraded data."""
# 【中文注释·模块定位】RSI 冒烟测试：在真实客户机上验证传输/检查点/学习记忆等全部
# 边界机制，只用合成数据、绝不调用模型或官方评分器（因此不算密封评估的一部分）。
# 两个入口：provision_smoke（外层：领容量、起沙箱、起复位 RPC、拉起子进程执行 exercise）
# 与 exercise（子进程：逐项 check 验证记忆往返、候选捕获/篡改检测/重放、Verifier 隔离、
# scratch 持久、全新租约洁净性、冻结记忆挂载）。结果写入 readiness 记录供 run 前复核。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# argparse：解析子进程命令行参数。
import argparse
# asyncio：外层供给流程是异步的。
import asyncio
# json：沙箱/结果文件读写。
import json
# logging：子进程日志配置。
import logging
# subprocess：读取 runner 镜像 ID。
import subprocess
# asdict：沙箱对象转 JSON 字典。
from dataclasses import asdict
# Path：输出目录与文件路径。
from pathlib import Path

# protocol：仓库根、记忆指纹、源码指纹、记忆校验与原子写盘。
from .protocol import (
    RSIAGENT,
    memory_record,
    source_fingerprint,
    verify_memory,
    write_json,
)


# 冒烟外层：锁定源码 → 领容量 → 起合成沙箱与复位服务 → 子进程跑 exercise → 写 readiness 记录。
async def provision_smoke(args):
    # 延迟导入容量池与宿主工具。
    from .capacity import Capacity
    from .host import freeze_source, install_ale_path, provider_config

    # 挂载 ALE 路径并做导入隔离检查。
    install_ale_path(args.ale_root)
    # 延迟导入上游 QEMU 供给件。
    from ale_run.base_interface import SandboxSpec
    from ale_run.environments.providers.qemu import QemuProvider

    # 以固定配置实例化 provider。
    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    # 输出目录：命令行指定，或默认结果目录下按 OS 分目录。
    output = args.output or RSIAGENT / "results/ale/smoke" / args.os
    # 必须是全新目录（防覆盖历史冒烟结果）。
    output.mkdir(parents=True, exist_ok=False)
    # 记录当前源码指纹。
    fingerprint = source_fingerprint()
    # 冻结发布源码到输出目录（冒烟子进程运行的代码副本）；
    # 冻结前后指纹必须一致（冒烟结果绑定确切的源码版本）。
    freeze_source(output / "release")
    if (
        source_fingerprint() != fingerprint
        or source_fingerprint(output / "release") != fingerprint
    ):
        raise RuntimeError("Source changed while freezing the smoke")
    # 领取主机容量：1 个 VM、80GiB 磁盘预留（stop 事件为空 Event，即不响应停止）。
    capacity = Capacity(args.pool_root, output, limit=args.vm_limit)
    await capacity.acquire(1, 80, asyncio.Event(), vcpus=4, memory_gb=16)
    # 沙箱句柄先置 None（finally 判空）。
    sandbox = None
    # 复位服务先置 None。
    service = None
    # 是否通过（决定 finally 是否删除沙箱）。
    passed = False
    try:
        # 申请合成沙箱：按 OS 选快照，任务 ID 为冒烟专用合成 ID。
        sandbox = await provider.acquire(
            SandboxSpec(
                snapshot="cpu-free" if args.os == "windows" else "cpu-free-ubuntu",
                os=args.os,
                vcpus=4,
                memory_gb=16,
                task_id="rsiagent-rsi-synthetic-smoke",
            )
        )
        # 把容器登记进容量租约。
        capacity.attach(sandbox.id)
        # 沙箱描述副本并标记为一次性练习客户机（允许载荷清理）。
        spec = asdict(sandbox)
        spec.setdefault("metadata", {})["rsiagent_practice"] = True
        # 复位 RPC 服务（延迟导入避免外层不需要的场景加载）。
        from .lifecycle import ResetService

        # 冒烟复位回调：销毁旧沙箱并供应全新沙箱。
        async def reset_sandbox(sandbox_id):
            # 非本地变量：新沙箱要写回外层。
            nonlocal sandbox
            # 只允许复位本冒烟自己的合成沙箱。
            if sandbox_id != sandbox.id:
                raise RuntimeError("Smoke reset must name its owned synthetic guest")
            # 释放（删除）旧沙箱。
            await provider.release(sandbox, mode="delete")
            # 供应全新沙箱。
            sandbox = await provider.acquire(
                SandboxSpec(
                    snapshot="cpu-free" if args.os == "windows" else "cpu-free-ubuntu",
                    os=args.os,
                    vcpus=4,
                    memory_gb=16,
                    task_id="rsiagent-rsi-synthetic-smoke",
                )
            )
            # 新容器登记进租约。
            capacity.attach(sandbox.id)
            # 组装新沙箱描述并标记练习属性。
            result = asdict(sandbox)
            result.setdefault("metadata", {})["rsiagent_practice"] = True
            # 落盘当前沙箱（审计）。
            write_json(output / "current-sandbox.json", result)
            # 返回给 RPC 客户端。
            return result

        # 启动复位 RPC 服务并暴露端点。
        service = ResetService(reset_sandbox)
        # 把端点写进沙箱 spec。
        spec["reset_endpoint"] = service.path
        # 落盘沙箱 spec。
        write_json(output / "sandbox.json", spec)
        # 组装子进程命令：在冻结源码目录里以模块方式运行 smoke 的 exercise 部分。
        command = [
            str(args.worker_python),
            "-m",
            "benchmarks.ale.smoke",
            str(output / "sandbox.json"),
            str(output),
        ]
        # 输出重定向到 smoke.log。
        with (output / "smoke.log").open("wb") as log:
            # 启动子进程。
            child = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(output / "release"),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            # 等待结束取退出码。
            code = await child.wait()
        # 读取子进程写的结果。
        result = json.loads((output / "result.json").read_text())
        if code or result["status"] != "passed":
            # 退出码非零或未通过：冒烟失败。
            raise RuntimeError(
                f"{args.os} RSI smoke failed; see {output / 'smoke.log'}"
            )
        # 读取 runner 镜像 ID（结果绑定确切镜像）。
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
            text=True,
        ).strip()
        # 补充结果元数据：源码指纹、镜像 ID、作用域说明（无模型/评分器调用）。
        result.update(
            source_sha256=fingerprint,
            runner_image=image_id,
            scope="real VM transport/checkpoint/learning-memory mechanics; no model or grader calls",
        )
        # 写入 readiness 记录（run 前由 validate_readiness 核对）。
        write_json(
            Path(args.cache) / "rsiagent-readiness" / f"smoke-{args.os}.json", result
        )
        # 标记通过。
        passed = True
        # 打印结果。
        print(json.dumps(result, indent=2))
    finally:
        if service is not None:
            # 关停复位服务。
            await service.close()
        if sandbox is not None and passed:
            # 通过才删除沙箱；失败保留客户机供诊断。
            await provider.release(sandbox, mode="delete")
        # 释放容量租约。
        capacity.release()


# 冒烟内核：在真实客户机上逐项验证传输与隔离边界（全部合成数据）。
def exercise(sandbox, root):
    # 延迟导入共享运行时与工具。
    from core.verifier_runtime import AgenticVerifierExecutor
    from explore.commit import _shingles
    from explore.practice_loop import _atomic_install_memory, _pull_terminal_memory
    from explore.provisioning import (
        capture_project_materials,
        replay_project_materials,
        verify_project_materials,
    )

    # 本仓库的池/钩子与 Windows 执行器。
    from .practice import CleanPool, hooks_for
    from .windows import WindowsVerifierExecutor

    # 结果容器：状态、逐项检查、OS。
    result = {"status": "running", "checks": [], "os": sandbox["os"]}
    # 池与执行器先置 None（finally 清理）。
    pool = executor = None
    # 建输出目录。
    root.mkdir(parents=True, exist_ok=True)

    # 单项断言：失败抛 AssertionError；成功记录并落盘。
    def check(name, condition, detail=""):
        if not condition:
            raise AssertionError(name + ": " + str(detail))
        result["checks"].append(name)
        write_json(root / "result.json", result)
        print("PASS", name, flush=True)

    try:
        # 建练习池（单沙箱、复位端点来自 spec）。
        pool = CleanPool(
            [sandbox], practice=True, reset_endpoint=sandbox["reset_endpoint"]
        )
        # 领取租约。
        lease, vm = pool.acquire()
        # 合成语料文件路径。
        corpus = root / "corpus.json"
        # 合成"公开指令"（非真实任务）。
        public = "Create a synthetic CSV total from a small table of integer values."
        # 语料 = 该指令的 8-gram 排序列表。
        write_json(corpus, sorted(_shingles(public)))
        # 构造冒烟钩子（无公开任务绑定）。
        hooks = hooks_for(pool, corpus)
        # 宿主侧记忆库目录。
        bank = root / "memory"
        # 初始记忆：一个 markdown 过程 + 一个含全部 256 字节值的嵌套二进制（验无损）。
        original = {
            "procedure.md": b"Check row count and independently recompute totals.\n",
            "nested/bytes.bin": bytes(range(256)) * 17,
        }
        # 原子安装记忆。
        _atomic_install_memory(str(bank), original)
        # 检查 1：记忆上传客户机成功。
        check("upload initial memory", hooks.push_memory(vm, str(bank)))
        # 检查 2：从客户机拉回的记忆与原始逐字节一致（嵌套目录无损往返）。
        check(
            "lossless nested memory roundtrip",
            _pull_terminal_memory(hooks, vm) == original,
        )
        # 在客户机内创建合成项目：input.csv + 嵌套 Unicode 文件。
        path = vm.home + "/evolution_project"
        vm.trusted_python(
            f'import pathlib\np=pathlib.Path({path!r});p.mkdir()\n(p/"input.csv").write_text("value\\n3\\n7\\n",encoding="utf-8")\n(p/"nested").mkdir()\n(p/"nested"/"unicode ü.txt").write_text("αβγ",encoding="utf-8")\n'
        )
        # 候选捕获目录。
        captured = root / "candidate"
        # 检查 3：捕获合成项目材料成功。
        check(
            "capture synthetic project",
            capture_project_materials(
                vm, "smoke", str(captured), guest_dirs=["evolution_project"]
            )["ok"],
        )
        # 篡改客户机上的 input.csv。
        vm.trusted_python(
            f'import pathlib\n(pathlib.Path({path!r})/"input.csv").write_text("changed")\n'
        )
        # 检查 4：完整性校验能发现候选被篡改。
        check(
            "detect candidate mutation",
            not verify_project_materials(vm, str(captured))["ok"],
        )
        # 检查 5：重放候选可精确恢复（防漂移）。
        check(
            "replay exact candidate", replay_project_materials(vm, str(captured))["ok"]
        )
        # 按平台选择 Verifier 执行器类。
        cls = WindowsVerifierExecutor if vm.is_windows else AgenticVerifierExecutor
        # 建执行器：隐藏 Actor 记忆、回滚镜像模式、私有路径为记忆与 work。
        executor = cls(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory", "/home/user/work"),
        )
        # 受限检查 1：Verifier 能读候选并写入痕迹（先断言再写）。
        trace = executor(
            "python",
            f'import pathlib\np=pathlib.Path({path!r})\nassert "3" in (p/"input.csv").read_text()\n(p/"input.csv").write_text("verifier effect")\n',
            timeout=120,
        )
        # 检查 6：Verifier 可以检查候选。
        check(
            "verifier can inspect candidate",
            not trace.infra_fail and trace.exit_code == 0,
            trace.stdout,
        )
        # 受限检查 2：尝试读 Actor 私有记忆——应当 FileNotFoundError/PermissionError。
        trace = executor(
            "python",
            f'import pathlib\np=pathlib.Path({vm.home + "/.memory/procedure.md"!r})\ntry:\n p.read_bytes()\nexcept (FileNotFoundError,PermissionError):\n print("PRIVATE_HIDDEN")\nelse:\n raise AssertionError("memory leaked")\n',
            timeout=120,
        )
        # 检查 7：Actor 记忆对 Verifier 隐藏。
        check(
            "actor memory hidden from verifier",
            trace.exit_code == 0 and "PRIVATE_HIDDEN" in trace.stdout,
        )
        # 受限检查 3：Verifier 向 VERIFIER_SCRATCH 写证据文件。
        trace = executor(
            "python",
            'import os,pathlib\n(pathlib.Path(os.environ["VERIFIER_SCRATCH"])/"evidence.txt").write_text("persistent")\n',
            timeout=120,
        )
        # 检查 8：Verifier scratch 可写。
        check("verifier scratch write", trace.exit_code == 0)
        # 导出 scratch 归档并关闭执行器（触发回滚）。
        scratch = executor.export_scratch()
        executor.close()
        executor = None
        # 检查 9：检查结束后候选被回滚到检查前状态。
        check(
            "candidate restored after verification",
            verify_project_materials(vm, str(captured))["ok"],
        )
        # 重建执行器并注入 scratch 归档（验证跨检查持久）。
        executor = cls(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory", "/home/user/work"),
            scratch_archive=scratch,
        )
        # 受限检查 4：新执行器内读 scratch 证据文件。
        trace = executor(
            "python",
            'import os,pathlib\nprint((pathlib.Path(os.environ["VERIFIER_SCRATCH"])/"evidence.txt").read_text())\n',
            timeout=120,
        )
        # 检查 10：Verifier scratch 跨检查持久。
        check(
            "verifier scratch persists across inspection",
            trace.exit_code == 0 and "persistent" in trace.stdout,
        )
        # 关闭执行器。
        executor.close()
        executor = None
        # 客户机内在记忆中新增一条学习过程。
        vm.trusted_python(
            f'import pathlib\n(pathlib.Path({vm.home + "/.memory"!r})/"learned.md").write_text("Validate all rows before exporting.")\n'
        )
        # 拉回终态记忆。
        learned = _pull_terminal_memory(hooks, vm)
        # 检查 11：学习保留全部旧记忆且新增过程文件。
        check(
            "learning retains prior memory and adds procedure",
            all(learned[k] == v for k, v in original.items())
            and "learned.md" in learned,
        )
        # 原子安装为冻结记忆并取指纹。
        _atomic_install_memory(str(root / "frozen"), learned)
        frozen = memory_record(root / "frozen")
        # 归还当前租约。
        lease.close()
        # 领取全新租约（触发复位）。
        lease, fresh = pool.acquire()
        # 检查 12：全新租约上旧项目目录不复存在。
        check(
            "fresh lease removes previous project",
            fresh.trusted_python(
                f"import pathlib\nprint(pathlib.Path({path!r}).exists())\n"
            ).strip()
            == "False",
        )
        # 检查 13：全新租约上旧记忆目录不复存在。
        check(
            "fresh lease removes prior memory",
            fresh.trusted_python(
                f"import pathlib\nprint(pathlib.Path({fresh.home + '/.memory'!r}).exists())\n"
            ).strip()
            == "False",
        )
        # 检查 14：冻结记忆可挂载到全新执行。
        check(
            "frozen memory attached to fresh execution",
            hooks.push_memory(fresh, frozen["path"]),
        )
        # 检查 15：全新执行看到的记忆与冻结内容一致。
        check(
            "fresh execution sees learned procedure",
            _pull_terminal_memory(hooks, fresh) == learned,
        )
        # 宿主侧冻结记忆完整性校验。
        verify_memory(frozen)
        # 检查 16：宿主冻结记忆未被改动。
        check("host frozen memory unchanged", True)
        # 归还租约。
        lease.close()
        # 全部通过。
        result["status"] = "passed"
    except BaseException as exc:
        # 失败：记录错误与栈。
        import traceback

        result.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        try:
            if executor is not None:
                # 关闭残留执行器。
                executor.close()
            if pool is not None:
                # 关闭池。
                pool.close()
        except Exception as exc:
            # 清理失败也记入结果（不掩盖原异常）。
            result.update(status="failed", cleanup_error=str(exc))
        # 最终落盘结果。
        write_json(root / "result.json", result)
    # 返回结果。
    return result


if __name__ == "__main__":
    # 子进程入口：配置日志 → 解析参数 → 执行 exercise → 以通过与否为退出码。
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    # 参数解析器。
    parser = argparse.ArgumentParser()
    # 位置参数 1：沙箱 spec JSON。
    parser.add_argument("sandbox", type=Path)
    # 位置参数 2：输出目录。
    parser.add_argument("output", type=Path)
    # 解析参数。
    args = parser.parse_args()
    # 读取沙箱描述并执行冒烟检查。
    result = exercise(json.loads(args.sandbox.read_text()), args.output)
    # 退出码：passed→0，否则 1。
    raise SystemExit(0 if result["status"] == "passed" else 1)
