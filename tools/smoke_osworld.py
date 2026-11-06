#!/usr/bin/env python3
"""Check a disposable OSWorld VM without model calls, tasks, or official grading."""

# 中文注释（模块概览）：对一次性（disposable）OSWorld 虚拟机做基础设施冒烟检查——不调用模型、
# 不加载任务、不触发官方评测（official grading）。逐项验证：命令/程序往返执行、超时行为、
# 私有记忆上传、Agentic 验证器执行器的隔离性（hide_actor_memory + rollback_mirror 回滚镜像）
# 以及验证后候选与记忆的回滚恢复；全部证据写入 --output 指定的新目录。
# 启用 PEP 563 延迟注解求值，类型注解不在定义时求值
from __future__ import annotations

# 解析命令行参数
import argparse
# 写出冒烟结果 JSON
import json
# 设置环境变量（VM 属主、启动诊断目录）
import os
# 调整模块搜索路径
import sys
# 生成随机 VM 属主标识
import uuid
# 面向对象的文件路径操作
from pathlib import Path

# 仓库根目录（tools/ 的上一级）
ROOT = Path(__file__).resolve().parents[1]
# 避免重复插入
if str(ROOT) not in sys.path:
    # 将仓库根加入 sys.path，以便导入项目包
    sys.path.insert(0, str(ROOT))


def main():
    # 冒烟检查主流程：只验证基础设施能力，零模型调用、零官方评测调用
    # 以模块 docstring 作为命令行描述
    parser = argparse.ArgumentParser(description=__doc__)
    # 必填参数：存放冒烟证据的新目录
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for smoke evidence"
    )
    # 解析命令行参数
    args = parser.parse_args()
    # 展开 ~ 并转为绝对路径
    output = args.output.expanduser().resolve()
    # 创建全新输出目录（已存在则失败，防止覆盖旧证据）
    output.mkdir(parents=True, exist_ok=False)
    # 导入运行时路径安装函数（须先于其他项目导入）
    from benchmarks.osworld.runtime import _install_paths

    # 安装 OSWorld 运行所需的依赖路径
    _install_paths()
    # 计算记忆文件 SHA256（延迟导入，先装好路径）
    import hashlib

    # OSWorld 桌面环境
    from desktop_env.desktop_env import DesktopEnv

    # 准备可检查点的 Docker 供应器
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    # Agentic 验证器执行器（含隐藏记忆与回滚能力）
    from core.verifier_runtime import AgenticVerifierExecutor
    # 项目自研的 VM 包装层
    from env.vm import VM
    # 上传私有记忆到虚拟机
    from explore.commit import push_memory
    # 计算记忆树的整体哈希
    from explore.memory_hash import tree_sha256
    # 读取记忆目录为 {路径: 字节} 树
    from explore.practice_loop import _read_memory_tree

    # 用随机属主标记本次冒烟 VM，便于追溯与清理
    os.environ["RSIAGENT_VM_OWNER"] = "public_smoke_" + uuid.uuid4().hex
    # 启动诊断信息写入输出目录的 boot 子目录
    os.environ["RSIAGENT_BOOT_DIAGNOSTICS_DIR"] = str(output / "boot")
    # 以“回滚镜像”（rollback mirror）模式准备 Docker 供应器，验证后可回滚副作用
    prepare_checkpointable_docker_provider("rollback_mirror")
    # 冒烟结果字典：初始为 running；checks 收集通过的检查项；两类调用数恒为 0
    result = {
        "status": "running",
        "checks": [],
        "model_calls": 0,
        "official_evaluator_calls": 0,
    }
    # 桌面环境与验证执行器先置空，便于 finally 中安全清理
    environment = executor = None

    def check(name, condition):
        # 单项检查：不通过立即断言失败；通过则记录并打印
        # 条件不成立即冒烟失败
        if not condition:
            # 以检查项名称作为失败原因抛出
            raise AssertionError(name)
        # 记录通过的检查项
        result["checks"].append(name)
        # 实时打印通过信息
        print("PASS", name, flush=True)

    # 以下所有检查包裹在 try 中：任何失败都先记录进结果再抛出
    try:
        # 创建一次性 Docker 桌面环境：pyautogui 动作空间、Ubuntu 无头、1920x1080、60GB 数据卷，缓存写入输出目录
        environment = DesktopEnv(
            provider_name="docker",
            action_space="pyautogui",
            os_type="Ubuntu",
            headless=True,
            require_a11y_tree=False,
            cache_dir=str(output / "cache"),
            volume_size=60,
        )
        # 用项目 VM 包装层封装桌面环境
        vm = VM(environment)
        # 检查 1：命令往返执行输出正确
        check("command roundtrip", "SMOKE_OK" in vm.run_command("printf SMOKE_OK"))
        # 在 VM 内运行一段 Python 程序
        trace = vm.run_script("python", "print('PROGRAM_OK')")
        # 检查 2：程序执行往返（退出码为 0 且输出含标记）
        check(
            "program roundtrip", trace.exit_code == 0 and "PROGRAM_OK" in trace.stdout
        )
        # 故意超时的脚本，检验超时行为
        trace = vm.run_script("bash", "echo PARTIAL; sleep 99", timeout=45)
        # 记录超时痕迹（退出码/是否超时/已产生输出）作为证据
        result["timeout_trace"] = {
            "exit_code": trace.exit_code,
            "timed_out": trace.timed_out,
            "output": trace.stdout,
        }
        # 检查 3：超时后仍保留已产生的输出
        check("timeout preserves output", trace.timed_out and "PARTIAL" in trace.stdout)
        # 检查 4：超时后控制器仍可用
        check("controller survives timeout", "READY" in vm.run_command("printf READY"))
        # 宿主机上的私有记忆目录
        memory = output / "memory"
        # 创建记忆目录
        memory.mkdir()
        # 写入一条“私有 Actor 记忆”
        (memory / "lesson.txt").write_text("PRIVATE_ACTOR_MEMORY")
        # 检查 5：记忆可上传进虚拟机
        check("memory upload", push_memory(vm, str(memory)))
        # 候选文件路径（模拟候选解产物）
        candidate = "/home/user/rsiagent_smoke_candidate.txt"
        # 在 VM 内写入初始内容 ORIGINAL
        vm.run_command(f"printf ORIGINAL > {candidate}")
        # 创建验证器执行器：隐藏 Actor 私有记忆、以回滚镜像模式执行、声明私有路径
        executor = AgenticVerifierExecutor(
            vm,
            hide_actor_memory=True,
            execution_mode="rollback_mirror",
            private_paths=("/home/user/.memory",),
        )
        # 在隔离沙箱中运行验证程序（f-string 内为待执行源码，勿改动其内容）：
        # 断言读到 ORIGINAL → 改写候选 → 尝试读私有记忆（必须失败）→ 写暂存区并 chmod 0 → 打印 ISOLATION_OK
        trace = executor(
            "python",
            f"""import pathlib, os
p = pathlib.Path({candidate!r})
assert p.read_text() == 'ORIGINAL'
p.write_text('VERIFIER_EFFECT')
try:
    pathlib.Path('/home/user/.memory/lesson.txt').read_text()
except (FileNotFoundError, PermissionError):
    pass
else:
    raise AssertionError('private memory leaked')
s = pathlib.Path(os.environ['VERIFIER_SCRATCH']) / 'evidence.txt'
s.write_text('SCRATCH_PERSISTS')
s.chmod(0)
print('ISOLATION_OK')
""",
            timeout=90,
        )
        # 检查 6：验证器隔离成立（可改候选、不可读私有记忆、暂存区可写且打印成功标记）
        check(
            "verifier isolation",
            trace.exit_code == 0 and "ISOLATION_OK" in trace.stdout,
        )
        # 导出验证器暂存区（含 chmod 0 的不可读文件也应能导出）
        scratch = executor.export_scratch()
        # 检查 7：暂存区非空导出成功
        check("unreadable scratch exported", bool(scratch))
        # 主动关闭执行器：触发回滚，恢复候选与私有记忆
        executor.close()
        # 置空避免 finally 中重复关闭
        executor = None
        # 检查 8：候选文件被回滚为 ORIGINAL
        check(
            "candidate rollback",
            vm.run_command(f"cat {candidate}").strip() == "ORIGINAL",
        )
        # 检查 9：VM 内私有记忆已恢复原样
        check(
            "actor memory restored",
            vm.run_command("cat /home/user/.memory/lesson.txt").strip()
            == "PRIVATE_ACTOR_MEMORY",
        )
        # 检查 10：宿主机记忆从未被改动
        check(
            "host memory immutable",
            (memory / "lesson.txt").read_text() == "PRIVATE_ACTOR_MEMORY",
        )
        # 记录宿主记忆树的整体 SHA256（逐文件哈希再聚合成树哈希）作为审计证据
        result["memory_sha256"] = tree_sha256(
            {
                k: hashlib.sha256(v).hexdigest()
                for k, v in _read_memory_tree(str(memory)).items()
            }
        )
        # 全部检查通过，冒烟成功
        result["status"] = "passed"
    # 任何失败都先记录到结果中再抛出
    except Exception as exc:
        # 记录失败状态与错误信息
        result.update(status="failed", error=str(exc))
        # 原样向上抛出
        raise
    # 无论成败都要清理资源并写出结果
    finally:
        # 先处理执行器，再释放环境
        try:
            # 若执行器仍开着（异常路径），关闭并回滚
            if executor is not None:
                # 关闭执行器
                executor.close()
        # 无论执行器是否成功关闭，都必须释放环境并写出结果
        finally:
            # 若桌面环境仍开着则释放
            if environment is not None:
                # 关闭桌面环境
                environment.close()
            # 无论成败都写出结果 JSON（含缩进，末尾带换行）
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    # 返回成功退出码
    return 0


# 作为脚本直接运行时的入口
if __name__ == "__main__":
    # 以 main() 的返回值作为进程退出码
    raise SystemExit(main())
