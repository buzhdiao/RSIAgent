"""Sealed agent worker. Receives public task inputs and VM transport, no grader."""
# 【中文注释·模块定位】密封（sealed）智能体 worker 进程：只接收公开任务输入与 VM
# 传输层，不携带任何评分器——评分只发生在外层宿主进程（sealed evaluation 纪律）。
# 职责分派：baseline/phase3 调 evaluation() 做最终任务执行；phase1/phase2 分别调用
# learning.phase1（BRS）/phase2（DRS）做无评估器的递归自我改进学习。

# argparse：解析 worker-spec 文件路径参数。
import argparse
# json：读取 worker-spec 与写出结果。
import json
# logging：进程级日志配置。
import logging
# signal：注册 SIGTERM 处理（外部停止时以约定退出码结束）。
import signal
# sys：退出码与 SIGTERM 回调。
import sys
# time：统计阶段耗时。
import time
# asdict：把 dataclass（配置、结果对象）转为 JSON 可序列化字典。
from dataclasses import asdict
# Path：工作目录与 spec 文件路径。
from pathlib import Path

# protocol：协议校验、角色配置加载、记忆指纹校验与原子写盘。
from .protocol import protocol, role, verify_memory, write_json
# runtime：VM 工厂、初始化与平台绑定（快照/Look 钩子按实际 OS 分派）。
from .runtime import bind_platform, initialize_vm, make_vm


# 最终执行（baseline 或 phase3）：在真实目标 VM 上按当前配置完成任务，不学习、不回写记忆。
def evaluation(spec):
    # 延迟导入共享学习运行时（worker 进程独享该环境，宿主进程不可导入）。
    from core import actor
    # run_with_resume：带断点恢复的 Actor 主循环。
    from core.loop import run_with_resume
    # ArtifactSink：交互轨迹与产物的落盘器。
    from core.trace import ArtifactSink
    # memory_preamble：把记忆清单渲染成开场提示（注入记忆内容摘要）。
    from explore.charter import memory_preamble
    # push_memory：宿主目录 → 客户机 ~/.memory 的记忆传输。
    from explore.commit import push_memory
    # _listing/_read_memory_tree：记忆清单渲染与 {相对路径: bytes} 读取。
    from explore.practice_loop import _listing, _read_memory_tree

    # 本阶段工作目录（host.phase 已建好）。
    root = Path(spec["work_dir"])
    # 加载 baseline 角色配置（sealed 评估配置，来自固定协议锁的角色表）。
    cfg = role("baseline")
    # 按 spec 里的沙箱描述构造 VM 传输对象（Windows 或 Linux）。
    vm = make_vm(spec["sandbox"])
    # 初始化客户机（装 helper、探测权限/工具）。
    initialize_vm(vm)
    # 平台绑定：替换快照/Look/系统提示词钩子，并登记本任务的可见根目录。
    bind_platform(vm, spec.get("visible_roots", ()))
    # 开场提示默认为空。
    opening = ""
    # 上游阶段传来的冻结记忆（phase2→phase3 链路；baseline 无记忆）。
    memory = spec.get("input_memory")
    if memory is not None:
        # 先校验冻结记忆指纹未被篡改（phase boundary 不变量）。
        verify_memory(memory)
        # 把记忆上传到客户机 ~/.memory；失败即终止。
        if not push_memory(vm, memory["path"]):
            raise RuntimeError("Frozen evaluation memory upload failed")
        # 配置指向客户机内记忆目录（供 Agent 会话内引用）。
        cfg.env_memory_dir = memory["path"]
        # 把记忆清单渲染为开场提示，让 Actor 知道自己带着哪些经验上场。
        opening = memory_preamble(_listing(_read_memory_tree(memory["path"])))
    # 创建轨迹落盘器。
    sink = ArtifactSink(str(root))
    # 落盘实际生效的配置快照（审计用）。
    write_json(root / "resolved-config.json", asdict(cfg))
    # Verifier 会话集合（供 run_with_resume 复用持久上下文）。
    sessions = {}
    try:
        # 运行主循环：返回最终结果与完整对话历史。
        result, history = run_with_resume(
            spec["instruction"],
            vm,
            cfg,
            sink,
            opening_extra=opening,
            verifier_sessions=sessions,
        )
    finally:
        # 无论成败关闭全部 Verifier 执行器（释放回滚镜像等资源）。
        for session in sessions.values():
            session.close_executor()
    # 循环结束后再校验一次冻结记忆未被 Agent 篡改（写入被禁止，指纹必须不变）。
    if memory is not None:
        verify_memory(memory)
    # 保存完整 transcript（系统提示词 + 全部消息）。
    sink.save_transcript(actor.build_system(cfg), history)
    # 返回密封评估所需的最终结果记录：status=Actor 主循环终止状态；agent_result=完整结果字典；
    # safe_to_grade=True 声明可安全进入评分（宿主据此才拉取输出并评分）；memory=使用的记忆
    # （指纹记录或 "off"）；memory_writeback=False——密封评估禁止记忆回写。
    return {
        "status": result.status,
        "agent_result": asdict(result),
        "safe_to_grade": True,
        "memory": memory or "off",
        "memory_writeback": False,
    }


# worker 入口：解析 spec → 分派阶段 → 写结果文件 → 以约定退出码结束。
def main():
    # 参数解析器。
    parser = argparse.ArgumentParser()
    # 唯一位置参数：worker-spec.json 路径。
    parser.add_argument("spec")
    # 解析命令行。
    args = parser.parse_args()
    # 读取并解析 worker-spec（host.phase 落盘的任务说明）。
    spec = json.loads(Path(args.spec).read_text())
    # 工作目录。
    root = Path(spec["work_dir"])
    # 幂等创建。
    root.mkdir(parents=True, exist_ok=True)
    # 配置 INFO 级日志（输出已被宿主重定向到 worker.log）。
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    # SIGTERM → 以 143 退出（128+SIGTERM，与 shell 约定一致），让宿主能优雅停机。
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    # 校验固定协议（角色配置/任务队列/ALE 提交），worker 也必须对齐协议。
    protocol()
    # 记录开始时间。
    started = time.time()
    # 先写"运行中"占位结果：宿主据此知道 worker 已启动。
    write_json(
        root / "worker-result.json", {"status": "running", "safe_to_grade": False}
    )
    try:
        # 阶段名白名单校验。
        if spec["phase"] not in ("baseline", "phase1", "phase2", "phase3"):
            raise ValueError("Unknown ALE phase: " + str(spec["phase"]))
        # baseline 与 phase3 是密封评估阶段：走 evaluation()。
        if spec["phase"] in ("baseline", "phase3"):
            result = evaluation(spec)
        else:
            # phase1/phase2 是学习阶段：延迟导入学习模块，避免评估路径加载 BRS/DRS。
            from .learning import phase1, phase2

            # phase1 调 BRS（广度探索），phase2 调 DRS（深度探索）。
            result = (phase1 if spec["phase"] == "phase1" else phase2)(spec)
            # 学习阶段产物不得直接评分：显式标记不可评分。
            result["safe_to_grade"] = False
        # 统计各 LLM 提供方的调用次数（审计用）。
        from llm.client import provider_counts

        # 补充耗时与提供方计数。
        result.update(
            elapsed_seconds=time.time() - started, providers=provider_counts()
        )
        # 写最终结果。
        write_json(root / "worker-result.json", result)
        # 退出码：评估阶段可评分（safe_to_grade）或学习阶段已就绪（transition_ready）→ 0；否则 2。
        return 0 if result.get("safe_to_grade") or result.get("transition_ready") else 2
    except BaseException as exc:
        # 任何异常都落盘为 infrastructure_error 结果（含耗时），再原样抛出让宿主感知非零退出。
        logging.exception("Worker stopped before phase completion")
        write_json(
            root / "worker-result.json",
            {
                "status": "infrastructure_error",
                "safe_to_grade": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": time.time() - started,
            },
        )
        raise


if __name__ == "__main__":
    # 以 main() 的返回值作为进程退出码。
    raise SystemExit(main())
