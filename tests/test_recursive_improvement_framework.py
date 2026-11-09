# 【中文注释·模块定位】三阶段递归自我改进框架（recursive self-improvement）的
# 契约测试。被测对象是 benchmarks/osworld/pipeline.py（协议加载/预检/锁）、
# explore/phase1_wave.py（并行波次交接解析）、explore/charter.py（课程章程）、
# benchmarks/osworld/phase2.py 与 task.py（阶段运行器）以及 config/roles/*.yaml。
# 测试策略分两层：
# 1) 行为契约：协议预检（official_grader 只在 phase3 可用、记忆在 phase3 冻结、
#    课程在 phase3 关闭）、开发集与留出集不相交、目标条件模式要求同目标集、
#    phase1 分布文档不得点名留出任务、并行波次拓扑自由但拒绝跨项目依赖；
# 2) 防回归的源码哨兵：直接检查关键源文件字符串（如 "--memory-dir"、
#    "host_writeback": False、唯一一处 .evaluate( 调用），防止有人悄悄给学习
#    阶段接上官方评测器——这些哨兵比行为测试更能挡住"顺手加一行"的破防。
"""Contracts for the three-phase recursive self-improvement framework."""
# 模块 docstring：三阶段递归自我改进框架的（行为+源码）契约。

from dataclasses import fields
# 导入 fields：枚举 Config 的全部合法配置键。
import json
# 导入 json：协议文件/锁文件/发行标记的读写。
from pathlib import Path
# 导入 Path：路径对象处理。

import pytest
# 导入 pytest：异常断言与 fixture。
import yaml
# 导入 yaml：解析角色配置文件。

from config.settings import Config, load
# 导入配置 dataclass 与加载器：校验角色配置键与取值。
from core.self_evolving_loop import SelfEvolvingLoopHooks
# 导入目标自进化循环钩子：断言其字段面里绝无 evaluator/score。
from explore.charter import phase1_wave_curriculum_charter
# 导入 Phase-1 波次课程章程生成器：验证提示词契约。
from explore.phase1_wave import parse_wave_handoff
# 导入波次交接解析器：验证拓扑自由/依赖拒绝。
import benchmarks.osworld.pipeline as protocol
# 以 protocol 别名导入管线：协议加载/预检/锁/phase1 执行。
import benchmarks.osworld.phase2 as phase2_runner
# 以 phase2_runner 别名导入 Phase-2 运行器：配置装载与练习 Verifier 控制。


REPO = Path(__file__).resolve().parents[1]
# 仓库根（tests/ 的上一级）。
EXAMPLE = REPO / "config/osworld/rsi.example.json"
# 协议示例模板（发布版的真实协议）。
PHASE2_CONFIG = REPO / "config/roles/target.yaml"
# Phase-2（目标定向学习）的角色配置。
PHASE3_CONFIG = REPO / "config/osworld/baseline.yaml"
# Phase-3（正式评测）的角色配置。
PHASE3_LOCK = REPO / "config/osworld/evaluation.lock.json"
# Phase-3 评测锁（冻结评测器配置与镜像指纹）。


@pytest.fixture(autouse=True)
def prepared_release_assets(tmp_path, monkeypatch):
    """Exercise real release validation using a local metadata fixture."""
    # 自动 fixture：用本地元数据冒充发行资产，让"真实发行校验"在本文件
    # 的所有测试中都能离线通过（不必下载 OSWorld-V2 数据集）。
    lock = json.loads(PHASE3_LOCK.read_text())
    # 读取评测锁，拿任务资产的登记元数据。
    assets = lock['task_assets']
    # 任务资产清单（本地目录、发行标记文件名、仓库/提交等）。
    root = tmp_path / 'OSWorld-V2'
    folder = root / assets['local_dir']
    folder.mkdir(parents=True)
    # 在临时目录搭出 OSWorld 根与资产子目录。
    marker = {'benchmark_release': lock['task_release'],
              **{key: assets[key] for key in ('repository', 'repo_type', 'tag', 'commit')}}
    (folder / assets['release_marker']).write_text(json.dumps(marker))
    # 写发行标记文件：内容与锁登记逐字段一致（校验只核对元数据）。
    monkeypatch.setattr(protocol, 'OSWORLD_ROOT', root)
    # 管线视角的 OSWORLD_ROOT 指向伪造根。
    monkeypatch.setenv('OSWORLD_FILE_BASE_URL', '')
    monkeypatch.setenv('WEBSITE_HOST_SUFFIX', '')
    # 清空两个环境变量：保证预检读到的 host 后缀/文件源由协议决定而非外部环境。


def _spec(tmp_path: Path, *, development=None, held_out=None,
          distribution=None) -> Path:
    # 测试工具：以示例协议为底本生成一份可定制的协议文件。
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    # 读模板。
    value["run_name"] = "unit_protocol"
    value["study_design"] = "held_out_generalization"
    # 指定运行名与研究设计（留出泛化）。
    value["phase1"]["distribution_file"] = "config/osworld_v2_training_distribution.md"
    # phase1 使用仓库内置的训练分布文档。
    value["phase2"]["development_tasks"] = development or []
    value["phase3"]["held_out_tasks"] = held_out or []
    # 覆盖开发集/留出集。
    if distribution is not None:
        value["phase1"]["distribution_file"] = str(distribution)
    # 可选：换成自定义分布文档。
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    # 落盘并返回路径。
    return path


def test_example_declares_target_conditioned_phases_and_budget_checkpoints():
    # 测试：示例协议必须声明目标条件化的三阶段与预算检查点，且预检/锁
    # 如实反映阶段边界（官方评测器只在 phase3、phase3 记忆/课程皆冻结）。
    spec = protocol.load_protocol(EXAMPLE)
    # 加载示例协议。

    assert spec["phase1"]["project_budget"] == 8
    # phase1 项目预算为 8。
    assert spec["phase1"]["checkpoints"] == [0, 4, 8]
    # 检查点恰为 [0, 4, 8]：每 4 个项目可恢复一次。
    assert spec["_resolved"]["development_tasks"] == ("task_080",)
    assert spec["_resolved"]["held_out_tasks"] == ("task_080",)
    # 解析后的开发集与留出集都是 task_080（目标条件化：同一个目标）。
    assert protocol.preflight(spec, "phase1")[
        "official_grader_available_in_phase"] is False
    assert protocol.preflight(spec, "phase2")[
        "official_grader_available_in_phase"] is False
    assert protocol.preflight(spec, "phase3")[
        "official_grader_available_in_phase"] is True
    # 预检：phase1/phase2 官方评分器不可用，phase3 才可用。
    assert protocol.preflight(spec, "phase3")[
        "memory_mutable_in_phase"] is False
    # phase3 记忆不可变（评测阶段冻结学习）。
    assert protocol.preflight(spec, "phase3")[
        "curriculum_enabled_in_phase"] is False
    # phase3 课程关闭（评测阶段没有课程 Agent）。
    lock = protocol._protocol_lock(spec, EXAMPLE.read_bytes())
    # 生成协议锁（绑定源字节）。
    assert lock["boundaries"] == {
        "official_evaluator_calls_phase1": 0,
        "official_evaluator_calls_phase2": 0,
        "official_evaluator_phase3_only_after_agent_handoff": True,
        "phase3_memory_writeback": False,
        "learning_after_phase3_begins": False,
    }
    # 锁里的边界恰为这五条硬承诺：phase1/2 零官方调用、phase3 在 Agent 交接
    # 之后才评测、phase3 不回写记忆、phase3 开始后不再学习。
    assert "verifier_control_config" in lock["configs"]["phase1"]
    # phase1 锁登记 Verifier 控制配置：验证编排被纳入冻结范围。


def test_preflight_and_child_share_release_setup_without_mutating_parent(
        tmp_path, monkeypatch):
    # 测试：预检解析出的"发行环境"（用户模拟模型/资产快照/host 后缀/文件源）
    # 必须原样传给 phase1 子进程环境，且预检与子进程启动都不得污染父进程环境。
    monkeypatch.setattr(protocol, "RESULTS_ROOT", tmp_path / "results")
    # 结果根指向临时目录。
    spec = protocol.load_protocol(_spec(tmp_path))
    # 加载定制协议。
    before = dict(protocol.os.environ)
    # 给父进程环境拍快照。
    report = protocol.preflight(spec, "phase1")
    # 执行 phase1 预检。
    release = report["release_environment"]
    # 取解析出的发行环境。
    assert release["user_simulator"]["model"] == "openai/gpt-4o"
    # 用户模拟器模型与锁一致。
    assert release["task_assets"]["mode"] == "release_bound_local_snapshot"
    # 资产模式：绑定发行版的本地快照（不用网络随机源）。
    assert dict(protocol.os.environ) == before
    # 预检没有改父进程任何环境变量。

    def launch(_argv, **kwargs):
        # 伪 phase1 子进程启动器：校验子进程环境后写一个终止结果。
        environment = kwargs["environment"]
        assert environment["OSWORLD_USER_SIM_MODEL"] == "openai/gpt-4o"
        # 子进程拿到同样的用户模拟模型。
        assert environment["WEBSITE_HOST_SUFFIX"] == release["website_host_suffix"]
        # host 后缀与预检解析值一致（预检↔子进程共享发行设定）。
        assert environment["OSWORLD_FILE_BASE_URL"] == release["task_assets"]["path"]
        # 文件源与资产快照路径一致。
        protocol._write_json_atomic(
            spec["_resolved"]["run_root"] / "phase1/result.json", {
                "status": "budget_exhausted", "official_evaluator_calls": 0})
        # 原子写 phase1 终止结果（预算耗尽、零官方调用）。

    monkeypatch.setattr(protocol, "_run", launch)
    # 替换真实子进程启动。
    protocol.execute_phase1(spec)
    # 执行 phase1。
    assert dict(protocol.os.environ) == before
    # 父进程环境依旧原样：启动子进程也不污染父环境。


def test_parallel_phase1_is_agent_authored_not_a_static_search_menu(tmp_path):
    # 测试：并行 Phase-1 必须是"Agent 自主书写波次"而非静态搜索菜单——
    # 协议透传 parallel_waves/parallelism 进预检与锁；课程章程明确"任意正数
    # 项目数、同一波前记忆快照、收敛是语义规则"，且不含固定路线词（DEEPEN 等）。
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run_name"] = "parallel_wave_unit"
    value["phase1"]["parallel_waves"] = True
    value["phase1"]["parallelism"] = 4
    path = tmp_path / "parallel.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    # 写一份打开并行波次（并行度 4）的协议。

    loaded = protocol.load_protocol(path)
    report = protocol.preflight(loaded, "phase1")
    lock = protocol._protocol_lock(loaded, path.read_bytes())
    # 加载 + 预检 + 生成锁。

    assert report["phase1_parallel_waves"] is True
    assert report["phase1_parallelism"] == 4
    # 预检如实报告并行开关与并行度。
    assert lock["phase1_parallel_waves"] is True
    assert lock["phase1_parallelism"] == 4
    # 锁同样冻结这两个值：并行设置属于被绑定协议的一部分。

    prompt = phase1_wave_curriculum_charter(
        "a target direction", previous_wave_outcomes="",
        handoff_path="/home/user/phase1_wave.json",
        wave_root="/home/user/evolution_wave",
        target_query_conditioned=True)
    # 生成目标条件化的波次课程章程。
    assert "any positive number" in prompt
    # 章程允许任意正数个项目数（Agent 自主决定）。
    assert "same pre-wave memory snapshot" in prompt
    # 同一波内所有分支共享同一波前记忆快照（公平并行）。
    assert "No fixed project count is a semantic convergence rule" in prompt
    # "没有固定项目数"是语义收敛规则（写进章程的承诺）。
    assert "DEEPEN" not in prompt
    assert "BROADEN" not in prompt
    assert "MIXED" not in prompt
    # 章程不含静态搜索路线词：不是预设菜单，Agent 自由拓扑。


def test_parallel_wave_transport_accepts_free_topology_and_rejects_dependencies():
    # 测试：波次交接传输格式——两个共享工作目录的独立项目合法；
    # 指令里写"sibling output"（跨项目依赖）被拒绝；SATURATED 空项目列表合法。
    raw = json.dumps({
        "decision": "WAVE",
        "rationale": "test two independent hypotheses",
        "projects": [
            {"id": "writer-format", "instruction":
             "Work in /home/user/evolution_project and save the deliverable there."},
            {"id": "mail-attach", "instruction":
             "Work in /home/user/evolution_project and keep all outputs there."},
        ],
    })
    decision = parse_wave_handoff(raw)
    # 解析自由拓扑的波次交接。
    assert decision is not None
    # 两个独立项目（同一工作目录但互不依赖）合法。
    assert [project.project_id for project in decision.projects] == [
        "writer-format", "mail-attach"]
    # 项目 ID 按原序解析。
    assert parse_wave_handoff(json.dumps({
        "decision": "WAVE", "rationale": "invalid",
        "projects": [{"id": "bad", "instruction": "use sibling output"}],
    })) is None
    # 指令要求使用兄弟项目的输出 = 跨项目依赖：解析为 None（拒绝）。
    saturated = parse_wave_handoff(json.dumps({
        "decision": "SATURATED", "rationale": "marginal value is low",
        "projects": [],
    }))
    assert saturated is not None and saturated.projects == ()
    # SATURATED 决策允许空项目列表：语义收敛即空波。


def test_development_and_held_out_tasks_must_be_disjoint(tmp_path):
    # 测试：开发集与留出集重叠的协议必须被拒绝（防数据泄漏）。
    path = _spec(
        tmp_path, development=["task_003"], held_out=["task_003"])
    # 两个集合都含 task_003。
    with pytest.raises(protocol.ProtocolError, match="overlap"):
        protocol.load_protocol(path)
    # 加载必须抛 ProtocolError，消息含 "overlap"。


def test_target_conditioned_mode_requires_and_allows_same_target_set(tmp_path):
    # 测试：target_conditioned_adaptation 设计要求且只允许 phase2/phase3 使用
    # 同一目标集——相同合法，不同即拒绝（并豁免不相交检查）。
    value = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    value["run_name"] = "target_conditioned_unit"
    value["study_design"] = "target_conditioned_adaptation"
    value["phase2"]["development_tasks"] = ["task_095"]
    value["phase3"]["held_out_tasks"] = ["task_095"]
    path = tmp_path / "target.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    # 写一份目标条件化协议：两个阶段同用 task_095。

    loaded = protocol.load_protocol(path)
    # 加载必须成功。

    assert loaded["_resolved"]["study_design"] == \
        "target_conditioned_adaptation"
    # 研究设计被解析登记。
    assert protocol.preflight(loaded, "phase1")["task_split_disjoint"] is False
    # 预检承认任务划分不"不相交"（目标条件模式合法豁免）。

    value["phase3"]["held_out_tasks"] = ["task_099"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(protocol.ProtocolError, match="same target set"):
        protocol.load_protocol(path)
    # 两阶段目标集不一致 → 拒绝：目标条件化必须锁定同一目标。


def test_phase1_distribution_may_not_name_a_held_out_instance(tmp_path):
    # 测试：phase1 的探索分布文档不得点名任何留出任务（防间接泄漏评测目标）。
    distribution = tmp_path / "distribution.md"
    distribution.write_text(
        "Explore office workflows, especially task_019.", encoding="utf-8")
    # 分布文档点名了 task_019（留出任务）。
    path = _spec(
        tmp_path, development=["task_003"], held_out=["task_019"],
        distribution=distribution)
    # 生成使用该分布的协议。
    with pytest.raises(protocol.ProtocolError, match="names held-out"):
        protocol.load_protocol(path)
    # 加载必须拒绝（消息含 "names held-out"）。


def test_phase_profiles_keep_roles_agentic_and_phase_boundaries_distinct():
    # 测试：phase2/phase3 角色配置（a）只用 Config 的合法键；（b）保持 Agent 化
    # 角色设定（模型/验证执行模式/评测隔离一致）；（c）阶段边界差异成立——
    # 练习配置允许"验证失败开进化/进化路线"，评测配置一律关闭；（d）评测锁
    # 冻结 actor 配置指纹、评测器封存、主机零回写。
    known = {field.name for field in fields(Config)}
    # Config 的全部合法字段名。
    for path in (PHASE2_CONFIG, PHASE3_CONFIG):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw) <= known
    # 两个角色配置的键都是 Config 认识的（无拼写漂移）。

    practice = load(str(PHASE2_CONFIG))
    evaluation = load(str(PHASE3_CONFIG))
    # 分别加载练习与评测配置。
    for cfg in (practice, evaluation):
        assert cfg.model == "z-ai/glm-5.3"
        assert cfg.verifier_model == "moonshotai/kimi-k3"
        # 两个阶段用同一对模型（Actor=GLM、Verifier=Kimi）。
        assert cfg.look_ensemble == 1
        # 单次观察（无 ensemble）。
        assert cfg.verifier_continuity is True
        # Verifier 跨轮持续（持久上下文）。
        assert cfg.verifier_stage_lifecycle is False
        # 不启用阶段化生命周期。
        assert cfg.verifier_unverified_evidence is True
        # 允许未验证证据入上下文。
        assert cfg.verifier_execution_mode == "rollback_mirror"
        # 验证执行模式：回滚镜像（不污染 Actor 环境）。
        assert cfg.actor_evaluator_isolation is True
        # Actor 与评测器隔离。
        assert cfg.env_memory_dir == ""
        # 不用共享环境记忆目录。
    assert practice.verifier_failure_starts_evolution is True
    assert practice.verifier_evolve_route is True
    # 练习阶段：验证失败可触发进化/进化路线开放。
    assert evaluation.verifier_failure_starts_evolution is False
    assert evaluation.verifier_evolve_route is False
    # 评测阶段：一律关闭（评测不触发任何学习）。

    lock = json.loads(PHASE3_LOCK.read_text(encoding="utf-8"))
    # 读评测锁。
    assert lock["actor_agent"]["sha256"] == protocol._sha256(PHASE3_CONFIG)
    # 锁登记的 actor 配置指纹与当前文件一致（配置未被改动）。
    assert lock["evaluator"]["visibility"] == \
        "sealed_after_actor_verifier_loop"
    # 评测器可见性：Actor-Verifier 循环结束后才封存暴露。
    assert lock["memory"]["host_writeback"] is False
    # 主机零回写。


def test_phase2_runner_accepts_clean_profile_and_has_no_evaluator_hook():
    # 测试：Phase-2 运行器用干净角色 profile 装载配置成功，且自进化循环的
    # 钩子面里根本没有 evaluator/score 字段（评测器无处可挂）。
    class Args:
        # 伪命令行参数：全部指向仓库默认角色配置。
        target_config = str(PHASE2_CONFIG)
        practice_actor_config = str(
            phase2_runner.DEFAULT_PRACTICE_ACTOR_CONFIG)
        practice_verifier_config = str(
            phase2_runner.DEFAULT_PRACTICE_VERIFIER_CONFIG)
        curriculum_config = str(phase2_runner.DEFAULT_CURRICULUM_CONFIG)
        memory_config = str(phase2_runner.DEFAULT_MEMORY_CONFIG)

    configs, _paths = phase2_runner._load_configs(
        Args())
    # 用默认 profile 装载全部角色配置。
    assert configs["target_actor"].verifier_stage_lifecycle is False
    # 目标 Actor 配置不带阶段化生命周期。
    assert "evaluator" not in SelfEvolvingLoopHooks.__dataclass_fields__
    assert "score" not in SelfEvolvingLoopHooks.__dataclass_fields__
    # 循环钩子的字段面里没有 evaluator/score：结构上就不可能接评测器。


def test_phase2_selects_release_specific_benchmark_provenance():
    # 测试：Phase-2 必须选用与发行版对应的基准溯源 profile（osworld-v2-2026.08.08
    # → baseline.lock.json），且确以 --benchmark-profile 参数传给任务运行器。
    profile = protocol.BENCHMARK_PROVENANCE_PROFILES[
        "osworld-v2-2026.08.08"]
    # 取该发行版的溯源 profile。
    assert profile.name == \
        "baseline.lock.json"
    # profile 名字是评测锁文件名：溯源与锁一一对应。
    source = (REPO / "benchmarks/osworld/pipeline.py").read_text(
        encoding="utf-8")
    # 读管线源码。
    assert '"--benchmark-profile", str(benchmark_profile)' in source
    # 源码哨兵：任务运行器确实收到 --benchmark-profile 参数。


def test_explicit_practice_verifier_controls_full_agentic_verifier(tmp_path):
    # 测试：显式指定的练习 Verifier 配置必须完全接管 agentic verifier——
    # 覆盖目标配置里的残留路径，且原目标配置对象保持不变（不可变选择）。
    selected = tmp_path / "selected_verifier.yaml"
    selected.write_text("model: moonshotai/kimi-k3\n", encoding="utf-8")
    # 用户选定的练习 Verifier 配置。
    target = Config(
        agentic_verifier_config="config/should_not_survive.yaml")
    # 目标配置里残留了一个不该存活的路径。

    effective = phase2_runner._practice_agentic_control_config(
        target, selected)
    # 计算练习阶段的 agentic verifier 控制配置。

    assert effective.agentic_verifier_config == str(selected.resolve())
    # 生效配置指向用户选定的文件（解析为绝对路径）。
    assert target.agentic_verifier_config == \
        "config/should_not_survive.yaml"
    # 原目标配置对象未被就地修改：选择是纯函数式的。


def test_phase2_learns_from_both_outcomes_with_explicit_pass_policy():
    # 测试（源码哨兵）：Phase-2 必须从 PASS/FAIL 两种结果都学习，停止策略与
    # 触发授权、终局判定回写、记忆接地门槛都有对应的源码承诺。
    source = (REPO / "benchmarks/osworld/phase2.py").read_text(encoding="utf-8")
    # 读 Phase-2 运行器源码。
    assert "learn_on_pass=True" in source
    # PASS 结果同样进入学习（不只从失败学）。
    assert 'args.phase2_stop_policy == "curriculum_review"' in source
    # 停止策略 curriculum_review 被显式判定。
    assert "phase2_outcome_protocol" in source
    # 触发授权使用 phase2_outcome_protocol。
    assert "terminal_outcome=verdict.value" in source
    # 终局判定取自 Verdict 枚举值。
    assert 'only a grounded PASS/FAIL may enter durable memory' in source
    # 记忆门槛原文在源码中：只有接地的 PASS/FAIL 才能进持久记忆。


def test_phase3_runner_has_explicit_copy_in_only_memory_transport():
    # 测试（源码哨兵）：Phase-3 任务运行器的记忆传输是"只复制进来"——
    # 有 --memory-dir 参数与 host_writeback: False 声明，无 pull_memory 回传，
    # 且整份源码里恰好只有一处 .evaluate( 调用（唯一的评分入口）。
    source = (REPO / "benchmarks/osworld/task.py").read_text(encoding="utf-8")
    # 读任务运行器源码。
    assert '"--memory-dir"' in source
    # 记忆以 --memory-dir 显式复制进 guest。
    assert '"host_writeback": False' in source
    # 主机回写声明为 False。
    assert "pull_memory" not in source
    # 不存在 pull_memory：记忆绝不回传主机。
    assert source.count(".evaluate(") == 1
    # 全文件恰一次 .evaluate( 调用：评分入口唯一且受控。


def test_learning_entrypoints_do_not_call_official_evaluator():
    # 测试（源码哨兵）：phase1 与编排器的源码里绝不出现官方评测调用或任务
    # 加载器引用——学习入口物理上接触不到评测器；编排器把唯一的评分阶段
    # 委托给任务运行器，自己不持有、不序列化任何评测器对象。
    phase1 = (REPO / "benchmarks/osworld/phase1.py").read_text(encoding="utf-8")
    orchestrator = (
        REPO / "benchmarks/osworld/pipeline.py").read_text(encoding="utf-8")
    # 读两个学习入口的源码。
    assert ".evaluate(" not in phase1
    assert "task_loader" not in phase1
    # phase1 无评测调用、无任务加载器。
    assert '"official_evaluator_calls": 0' in phase1
    # phase1 结果里官方调用计数恒为 0（显式登记）。
    # The orchestrator delegates the sole scored phase to the task runner; it never owns
    # or serializes an evaluator object itself.
    # 编排器把唯一的评分阶段委托给任务运行器；自己从不持有或序列化评测器对象。
    assert ".evaluate(" not in orchestrator
    assert "task_loader" not in orchestrator
    # 编排器同样无评测调用、无任务加载器。
