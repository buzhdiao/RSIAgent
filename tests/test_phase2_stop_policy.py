# 【中文注释·模块定位】Phase-2 停止策略（stop policy）的声明即锁定契约测试
# （benchmarks/osworld/pipeline.execute_phase2 + benchmarks/osworld/phase2
# + config/osworld/rsi.example.json）。被钉死的安全承诺：停止策略必须在执行前
# 声明、写入 protocol_lock 并随参数传给子 runner；缓存的子结果只有与声明的
# 停止策略及终局校验完全一致时才允许断点续跑（restart），任何策略漂移或绕过
# 完成校验的缓存结果都必须 fail-closed 抛 ProtocolError，且绝不产生 phase2
# 官方评分调用。全部用 monkeypatch 伪造子进程 _run 与 preflight，不起真实
# runner/VM。
"""Phase-2 policy is declared before execution and enforced on cached results."""
# 模块 docstring：Phase-2 停止策略在执行前声明，并在缓存结果上强制执行。

import json
# 导入 json：读写协议 spec 与结果 JSON。
from pathlib import Path
# 导入 Path：定位仓库根与构造临时路径。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import Config
# 导入配置 dataclass：给 manifest 写入提供 target_actor 配置。
import benchmarks.osworld.pipeline as protocol
# 导入协议主管线：execute_phase2/load_protocol/_protocol_lock 等被测入口。
import benchmarks.osworld.phase2 as runner
# 导入 Phase-2 直跑 runner：策略解析先于配置加载的行为被测对象。


REPO = Path(__file__).resolve().parents[1]
# 仓库根（tests/ 的上一级），用于读取公开示例协议文件。


def make_spec(tmp_path, monkeypatch, policy="verifier_pass"):
    """从 rsi.example.json 派生一份最小 Phase-2 协议 spec 落盘。

    结果根目录重定向到 tmp_path（隔离真实 results/）；policy 为要声明的
    停止策略（None 表示声明后删除字段，模拟缺省场景）。
    """
    monkeypatch.setattr(protocol, "RESULTS_ROOT", tmp_path / "results")
    # 把主管线的结果根重定向到临时目录。
    value = json.loads((REPO / "config/osworld/rsi.example.json").read_text())
    # 读入公开示例协议作为底稿。
    value["run_name"] = "policy_test"
    # 固定 run 名，保证每次测试落在独立的 run 根目录。
    value["study_design"] = "held_out_generalization"
    # 设计为"留出泛化"：Phase-3 用 held_out_tasks。
    value["phase1"]["distribution_file"] = "config/osworld_v2_training_distribution.md"
    # Phase-1 指向真实存在的训练分布文件（load 校验路径合法性）。
    value["phase2"].update(development_tasks=["task_095"], stop_policy=policy)
    # Phase-2 只带一个开发任务，并写入被参数化的停止策略。
    value["phase3"]["held_out_tasks"] = ["task_099"]
    # Phase-3 只带一个留出任务（本文件不执行 Phase-3）。
    path = tmp_path / "protocol.json"
    # 协议文件落盘位置。
    path.write_text(json.dumps(value))
    # 序列化写盘，供 load_protocol 读取。
    return path
    # 返回协议文件路径。


def learning_files(spec):
    """伪造一份"合法完成"的 Phase-1 冻结记忆 + Phase-2 子结果文件。

    初末记忆树 sha256 都取自真实 _memory_record，保证证据自洽；
    返回（结果文件路径, 结果字典）。
    """
    root = spec["_resolved"]["run_root"]
    # 本次 run 的根目录（由 load_protocol 解析得到）。
    memory = root / "phase1/memory_frozen"
    # Phase-1 冻结记忆目录。
    memory.mkdir(parents=True)
    # 创建目录树。
    (memory / "lesson.md").write_text("A practice lesson")
    # 冻结记忆里放一节练习课。
    protocol._write_json_atomic(root / "phase1/result.json", {
        "status": "budget_exhausted", "memory_frozen_path": str(memory)})
    # Phase-1 结果：预算耗尽 + 冻结记忆路径（Phase-2 的合法前置状态）。
    seed = spec["phase2"]["seed"]
    # Phase-2 的尝试种子编号。
    attempt = root / "phase2/task_095" / f"attempt_{seed:04d}"
    # 开发任务首个尝试的目录（按 seed 命名）。
    frozen = attempt / "memory_frozen"
    # 该尝试结束后的最终冻结记忆。
    frozen.mkdir(parents=True)
    # 创建最终记忆目录。
    (frozen / "lesson.md").write_text("A practice lesson and target experience")
    # 最终记忆 = 练习课 + 目标体验（比初始记忆多了目标经验）。
    result = {
        "phase2_stop_policy": spec["_resolved"]["phase2_stop_policy"],
        "status": "verifier_pass",
        "target_verifier_verdict": "PASS",
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
        "initial_memory_tree_sha256": protocol._memory_record(memory)["tree_sha256"],
        "final_memory_path": str(frozen),
        "final_memory_tree_sha256": protocol._memory_record(frozen)["tree_sha256"],
    }
    # 一份完全合法的子结果：策略与 spec 一致、Verifier PASS、零官方评分调用、
    # 官方反馈从未进入学习环、初/末记忆树哈希都来自真实记录。
    return attempt / "result.json", result
    # 返回结果文件路径与结果字典。


@pytest.mark.parametrize("declared_policy,policy", [
    (None, "curriculum_review"),
    ("verifier_pass", "verifier_pass"),
    ("curriculum_review", "curriculum_review"),
])
# 参数化：声明策略为 None（缺省，落到 curriculum_review）、verifier_pass、
# curriculum_review 三种声明形态；policy 是期望生效的最终策略。
def test_policy_reaches_child_result_and_protocol_lock(
        tmp_path, monkeypatch, declared_policy, policy):
    """声明的停止策略必须一路贯通到子进程参数、子结果、汇总输出与协议锁。

    且续跑只消费同策略缓存——不重复启动子进程。
    """
    path = make_spec(tmp_path, monkeypatch, declared_policy)
    # 生成带声明策略（或 None）的协议文件。
    if declared_policy is None:
        value = json.loads(path.read_text())
        del value["phase2"]["stop_policy"]
        path.write_text(json.dumps(value))
    # 声明为 None 时把字段整个删掉，模拟"协议未声明策略"的缺省场景。
    spec = protocol.load_protocol(path)
    # 加载协议：缺省策略应解析为 curriculum_review。
    result_path, result = learning_files(spec)
    # 准备合法完成的子结果文件。
    result["status"] = ("verifier_pass_curriculum_ready" if policy == "curriculum_review"
                        else "verifier_pass")
    # 子结果终局状态与策略匹配：curriculum_review 用"通过且课程就绪"状态。
    calls = []
    # 记录子进程启动次数。

    def launch(argv, **kwargs):
        # 伪 _run：校验传给子 runner 的参数后，落盘子结果。
        calls.append(argv)
        # 记录启动参数。
        assert argv[argv.index("--phase2-stop-policy") + 1] == policy
        # 关键断言：--phase2-stop-policy 的值等于期望的生效策略。
        assert kwargs["phase"] == "phase2"
        # 子进程被指派执行 Phase-2。
        protocol._write_json_atomic(result_path, result)
        # 子 runner 正常完成后把结果写到结果文件。

    monkeypatch.setattr(protocol, "_run", launch)
    # 用伪 _run 替换真实子进程启动。
    monkeypatch.setattr(protocol, "_configure_release_environment",
                        lambda *_args, **_kwargs: {})
    # 跳过发布环境配置（与本测试无关）。
    completed = protocol.execute_phase2(spec)
    # 执行 Phase-2（子进程被伪替）。
    assert completed["phase2_stop_policy"] == policy
    # 汇总输出记录的停止策略与声明一致。
    assert completed["task_results"][0]["stop_policy"] == policy
    # 任务级结果也携带同一停止策略。
    assert completed["task_results"][0]["verdict"] == "PASS"
    # 任务判定来自子结果的 PASS。
    assert completed["memory_frozen"]["tree_sha256"] == result["final_memory_tree_sha256"]
    # 汇总的冻结记忆树哈希 = 子结果里的最终记忆树哈希。
    assert protocol._protocol_lock(spec, path.read_bytes())["phase2_stop_policy"] == policy
    # 协议锁内容里也记录了同一策略（声明与锁一致）。
    # A restart may consume only a result with the same declared stopping policy.
    assert protocol.execute_phase2(spec) == completed
    # 用相同 spec 续跑：直接复用缓存结果，输出与首次一致。
    assert len(calls) == 1
    # 关键：子进程只启动过一次——续跑没有重复启动。


@pytest.mark.parametrize("mutation", [
    {"phase2_stop_policy": "curriculum_review"},
    {"phase2_stop_policy": None},
    {"status": "target_harness_unverified", "target_verifier_verdict": "UNVERIFIED"},
    {"status": "verifier_pass", "target_verifier_verdict": "FAIL"},
    {"status": "verifier_pass_curriculum_ready"},
])
# 参数化：五种被篡改/不一致的缓存结果——策略漂移（声明 verifier_pass 却缓存
# curriculum_review）、策略缺失、终局未验证、判定 FAIL 而状态 PASS、状态与
# 声明策略不匹配。
def test_cached_results_cannot_bypass_policy_or_completion_checks(tmp_path, monkeypatch, mutation):
    """与声明策略或完成校验不一致的缓存结果必须 fail-closed 拒绝。

    绝不启动子进程，也不留下 phase2/result.json。
    """
    spec = protocol.load_protocol(make_spec(tmp_path, monkeypatch))
    # 用缺省声明（verifier_pass）加载协议。
    result_path, result = learning_files(spec)
    # 准备合法底稿。
    result.update(mutation)
    # 按参数化注入不一致/篡改。
    protocol._write_json_atomic(result_path, result)
    # 把被篡改的结果落盘（模拟上次运行留下的缓存）。
    monkeypatch.setattr(protocol, "_run", lambda *_a, **_kw: pytest.fail("must not launch"))
    # 伪 _run：只要被调用即测试失败——缓存消费绝不重跑子进程。
    with pytest.raises(protocol.ProtocolError):
        protocol.execute_phase2(spec)
        # 续跑必须因结果校验失败而抛 ProtocolError。
    assert not (spec["_resolved"]["run_root"] / "phase2/result.json").exists()
    # 汇总结果文件不得落盘（坏缓存没有被采纳）。


def test_new_default_cannot_resume_a_run_locked_to_stop_on_pass(tmp_path, monkeypatch):
    """旧锁（verifier_pass）与新默认（curriculum_review）冲突时重启必须拒绝。

    拒绝原因是协议漂移（drifted）；旧锁原样保留，不产生 Phase-2 结果。
    """
    path = make_spec(tmp_path, monkeypatch)
    # 生成缺省（verifier_pass）协议文件。
    value = json.loads(path.read_text())
    del value["phase2"]["stop_policy"]
    path.write_text(json.dumps(value))
    # 删掉声明，使本次解析得到新版默认 curriculum_review。
    spec = protocol.load_protocol(path)
    # 加载出"新默认"的 spec。
    root = spec["_resolved"]["run_root"]
    # run 根目录。
    root.mkdir(parents=True)
    # 创建 run 根（模拟已有一次运行）。
    (root / "protocol.json").write_bytes(path.read_bytes())
    # 把当前协议文件复制进 run 根（锁校验要求逐字节一致）。
    old_lock = protocol._protocol_lock(spec, path.read_bytes())
    old_lock["phase2_stop_policy"] = "verifier_pass"
    protocol._write_json_atomic(root / "protocol_lock.json", old_lock)
    # 手工写一个"旧策略 verifier_pass"的协议锁——模拟旧版运行留下的锁。
    monkeypatch.setattr(protocol, "preflight", lambda *_a: {})
    # 跳过 preflight（与本测试无关）。
    monkeypatch.setattr(protocol, "execute_phase2", lambda *_a: pytest.fail("must not launch"))
    # 伪 execute_phase2：一旦被调用即失败——漂移必须在其之前被拒绝。

    with pytest.raises(protocol.ProtocolError, match="drifted"):
        protocol.main(["--protocol", str(path), "--phase", "phase2",
                       "--execute", "RUN-RECURSIVE-IMPROVEMENT-PHASE2"])
        # 入口重启必须抛"协议漂移"错误（锁策略 != 新解析策略）。
    assert json.loads((root / "protocol_lock.json").read_text()) == old_lock
    # 旧锁原样保留：拒绝续跑绝不覆写锁。
    assert not (root / "phase2/result.json").exists()
    # 不产生 Phase-2 汇总结果。


@pytest.mark.parametrize("declared_policy,policy", [
    (None, "curriculum_review"),
    ("verifier_pass", "verifier_pass"),
    ("curriculum_review", "curriculum_review"),
])
# 参数化：三种声明形态下的直跑入口策略解析。
def test_direct_runner_resolves_policy_before_loading_configs(
        tmp_path, monkeypatch, declared_policy, policy):
    """直跑 runner 必须在加载任何配置/模型之前先解析停止策略。

    策略解析是纯参数级检查，不能依赖配置加载成功。
    """
    class ConfigsReached(Exception):
        # 哨兵异常：_load_configs 被调用时抛出，证明控制流到达该点。
        pass

    def load_configs(args):
        # 伪 _load_configs：校验解析后的策略并探测元数据，然后中止流程。
        assert args.phase2_stop_policy == policy
        # 到达配置加载时，args 上的停止策略必须已是期望值。
        metadata = runner.phase2_protocol_metadata(args.phase2_stop_policy)
        # 由策略推导协议元数据。
        assert metadata["curriculum_after_pass"] is (policy == "curriculum_review")
        # 元数据：PASS 后是否进入课程复查，与策略一一对应。
        raise ConfigsReached
        # 中止后续（真实配置加载/模型构建）流程。

    monkeypatch.setattr(runner, "_install_paths", lambda: None)
    # 跳过路径安装。
    monkeypatch.setattr(runner, "RECURSIVE_RESULTS_ROOT", tmp_path / "results")
    # 结果根重定向到临时目录。
    monkeypatch.setattr(runner, "_load_configs", load_configs)
    # 用伪 _load_configs 替换真实配置加载。
    argv = ["task_080",  "--protocol-run", "policy_test",
            "--initial-memory", str(tmp_path), "--preflight"]
    # 最小直跑参数：任务、协议 run 名、初始记忆、preflight 标记。
    if declared_policy is not None:
        argv.extend(["--phase2-stop-policy", declared_policy])
    # 声明非 None 时把策略加进命令行（None 时省略，走缺省）。
    with pytest.raises(ConfigsReached):
        runner.main(argv)
        # 执行到 _load_configs 即被哨兵中止——证明策略解析先于配置加载。




@pytest.mark.parametrize("invalid", ["converged", "", None, True, []])
# 参数化：五种种非法停止策略取值（含类型错误）。
def test_unknown_stop_policies_fail_before_launch(tmp_path, monkeypatch, invalid):
    """未知/非法停止策略必须在加载协议时就被拒绝（fail-closed）。"""
    path = make_spec(tmp_path, monkeypatch, invalid)
    # 把非法值写进协议文件。
    with pytest.raises(protocol.ProtocolError, match="phase2.stop_policy"):
        protocol.load_protocol(path)
        # 加载必须抛错，且错误信息指向 phase2.stop_policy 字段。


@pytest.mark.parametrize("policy,review", [
    (None, True), ("verifier_pass", False), ("curriculum_review", True),
])
# 参数化：None（缺省）应与 curriculum_review 同义；两种显式策略各自成立。
def test_manifest_records_actual_pass_transition(tmp_path, policy, review):
    """manifest 的 protocol 段必须如实记录实际生效的停止策略与通过转换。

    且官方评分调用计数为零（Phase-2 绝不调用官方评分器）。
    """
    actor = tmp_path / "actor.yaml"
    actor.write_text("model: test-actor\n")
    # 一个占位 actor 配置文件（manifest 只记录路径）。
    root = tmp_path / "attempt"
    # manifest 输出目录。
    runner._write_phase2_manifest(
        root, task_id="task_080", protocol_run="test_policy",
        instruction="task instruction", target_direction="task direction",
        initial_memory_path=tmp_path, initial_memory={},
        configs={"target_actor": Config()}, paths={"target_actor": actor},
        benchmark_provenance={}, **({"stop_policy": policy} if policy is not None else {}))
    # 写 manifest：显式策略才传 stop_policy 键（模拟"声明缺省"与"显式声明"
    # 两种调用形态）。
    record = json.loads((root / "manifest.json").read_text())["protocol"]
    # 读回 manifest 的 protocol 段。
    assert record["stop_policy"] == (policy or "curriculum_review")
    # 记录的策略：缺省展开为 curriculum_review。
    assert record["curriculum_after_pass"] is review
    # PASS 后是否进课程复查与期望一致。
    assert ("curriculum" in record["pass_transition"]) is review
    # 通过转换里出现 curriculum 当且仅当策略是 curriculum_review。
    assert record["official_evaluator_calls"] == 0
    # manifest 声明官方评分调用为零（学习环内零官方评分）。


@pytest.mark.parametrize("example", [
    "osworld/rsi.example.json",
])
# 参数化：公开示例协议文件清单。
def test_public_examples_declare_curriculum_review(example):
    """公开示例协议必须声明 curriculum_review（文档与代码默认保持一致）。"""
    value = json.loads((REPO / "config" / example).read_text())
    # 读入示例协议。
    assert value["phase2"]["stop_policy"] == "curriculum_review"
    # Phase-2 停止策略是 curriculum_review。


def test_curriculum_stall_is_recorded_as_a_failed_final_test_not_a_pass():
    """课程停滞必须如实保留 FAIL 判定，绝不能被改写成通过。

    停滞是"失败的最终目标测试"，终局校验接受它但绝不篡改判定。
    """
    result = {"phase2_stop_policy": "curriculum_review",
              "status": "curriculum_stalled_final_target_test",
              "target_verifier_verdict": "FAIL"}
    # 一份课程停滞、Verifier 判 FAIL 的终局结果。
    protocol._validate_phase2_terminal(result, stop_policy="curriculum_review", task="task_080")
    # 终局校验应接受该合法组合（不抛错）。
    assert result["target_verifier_verdict"] == "FAIL"
    # 校验过程绝不篡改判定：FAIL 原样保留。
