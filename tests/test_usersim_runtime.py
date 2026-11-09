# 【中文注释·模块定位】用户模拟器（user simulator）通道的运行时配置契约测试：
# 用户通道路由必须到达各阶段子进程，且绝不顺带配置评分器（config/
# benchmark_runtime.configure_user_simulator + benchmarks/osworld/pipeline 的
# _configure_release_environment/execute_phase2 + 真实子进程初始化
# benchmarks.osworld.phase1/runtime._install_paths + llm.client._api_key）。
# 被钉死的承诺：
# 1) Phase-2 启动把用户路由（模型/供应商/base_url/密钥环境变量名/max_tokens）
#    以 OSWORLD_USER_SIM_* 环境变量传给子进程，密钥值只进环境不进 argv；
# 2) 评分器侧（OSWORLD_EVAL_*）零配置——学习只需要任务搭建；
# 3) 父进程环境零污染：启动前后 os.environ 逐键相等，Actor 系统提示确定性
#    不受影响；后续研究不继承前一个研究的用户路由；
# 4) 用户路由是可选的：缺失/未声明时凭据缺失绝不阻塞学习；
# 5) 发布锁与基线锁的用户模拟器配置必须一致；
# 6) configure_user_simulator：匹配的既有配置原样保留；冲突配置报错且错误
#    信息不泄漏密钥；凭据可从 RSIAGENT_ENV_FILE 指定的 env 文件加载但回执
#    不含密钥；声明不完整（空列表/空字典/空 model）仍是错误且零写入；
# 7) 真实子进程凭据优先级：Actor 密钥取 shell > OSWorld .env > 仓库 .env 的
#    原有优先级；仓库回退密钥只进用户通道；OSWorld 自有用户密钥与显式
#    OSWORLD_USER_SIM_API_KEY 覆盖各有其位；无用户凭据也能初始化；
# 8) 基线用户绑定仍要求声明的凭据环境变量存在。
# isolated_environment fixture 把 os.environ 整体替换为空并重定向仓库根。
"""User-channel routing reaches phase children without configuring the grader."""
# 模块 docstring：用户通道路由到达各阶段子进程，且不配置评分器。

import json
# 导入 json：读写锁文件与子进程输出。
import os
# 导入 os：环境变量操作（整体隔离与注入）。
from pathlib import Path
# 导入 Path：仓库根与盘上路径。
import subprocess
# 导入 subprocess：真实子进程初始化实验。
import sys
# 导入 sys：子进程用当前解释器。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 CompletedProcess 形状。

import pytest
# 导入 pytest：fixture、参数化与异常断言。

import benchmarks.osworld.pipeline as pipeline
# 导入 OSWorld 协议主管线：_configure_release_environment/execute_phase2。
from config.benchmark_runtime import (
    BenchmarkRuntimeError,
    configure_user_simulator,
)
# 导入基准运行时配置：用户模拟器绑定与运行时错误类型。
from config.settings import load
# 导入配置加载：读目标角色 YAML。
from core.actor import build_system
# 导入系统提示构建：验证其确定性不受路由配置影响。


REPO = Path(__file__).resolve().parents[1]
# 仓库根（tests/ 的上一级）。
ROUTE = {
    "provider": "openai_compatible",
    "model": "openai/test-user",
    "base_url": "https://user.invalid/v1",
    "api_key_env": "TEST_USER_KEY",
    "max_tokens": 256,
}
# 标准用户路由：五要素齐全（供应商/模型/base_url/密钥环境变量名/最大 token）。


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """每个测试自动隔离：os.environ 整体替换为空，仓库根重定向到 tmp_path。"""
    monkeypatch.setattr(os, "environ", {})
    # 空环境：真实环境变量绝不影响测试，也不被测试污染。
    monkeypatch.setattr(pipeline, "RSIAGENT_ROOT", tmp_path)
    # 主管线的仓库根重定向（env 文件查找等以此为准）。


def make_study(tmp_path, monkeypatch, *, name="first", route=ROUTE):
    """构造一份带 evaluation.lock 的最小研究 spec（Phase-2 可执行）。"""
    root = tmp_path / name
    # 研究 run 根目录。
    memory = root / "phase1/memory_frozen"
    memory.mkdir(parents=True)
    (memory / "lesson.md").write_text("A practice lesson")
    # Phase-1 冻结记忆（一节练习课）。
    pipeline._write_json_atomic(root / "phase1/result.json", {
        "status": "budget_exhausted", "memory_frozen_path": str(memory)})
    # Phase-1 结果：预算耗尽 + 冻结记忆路径（Phase-2 的合法前置）。
    lock = root / "evaluation.lock.json"
    # Deliberately no evaluator model/credentials: learning needs only task setup.
    lock.write_text(json.dumps({
        "task_release": "osworld-v2-2026.08.08",
        "evaluator": {"website_host_suffix": "site.invalid"},
        "user_simulator": route,
    }))
    # 评测锁：刻意不含评分器模型/凭据——只有网站后缀与用户路由（None 表示
    # 未声明用户模拟器）。
    monkeypatch.setattr(pipeline, "BENCHMARK_PROVENANCE_PROFILES", {
        "osworld-v2-2026.08.08": lock})
    # 把发布档案 profile 指向本研究的锁文件。
    return {
        "run_name": name,
        "task_release": "osworld-v2-2026.08.08",
        "phase2": {"seed": 1},
        "_resolved": {
            "run_root": root,
            "development_tasks": ("task_024",),
            "phase2_stop_policy": "verifier_pass",
            "configs": {
                "phase2": {field: REPO / "config/roles/target.yaml" for field in (
                    "target_config", "practice_actor_config",
                    "practice_verifier_config", "curriculum_config", "memory_config")},
                "phase3": {"benchmark_lock": lock},
            },
        },
    }
    # 最小 spec：run 名、发布版本、种子、解析结果（run 根、单开发任务、
    # 停止策略、Phase-2 五配置全指向真实 target.yaml、Phase-3 持锁）。


def complete_child(spec):
    """伪造子进程已完成：写入一份合法 PASS 的 Phase-2 子结果。"""
    root = spec["_resolved"]["run_root"]
    memory = root / "phase1/memory_frozen"
    memory_hash = pipeline._memory_record(memory)["tree_sha256"]
    # 冻结记忆树哈希（初末一致）。
    pipeline._write_json_atomic(root / "phase2/task_024/attempt_0001/result.json", {
        "status": "verifier_pass", "target_verifier_verdict": "PASS",
        "phase2_stop_policy": "verifier_pass",
        "official_evaluator_calls": 0,
        "official_evaluator_feedback_entered_learning": False,
        "initial_memory_tree_sha256": memory_hash,
        "final_memory_path": str(memory), "final_memory_tree_sha256": memory_hash,
    })
    # 合法子结果：Verifier PASS、策略一致、零官方评分、记忆哈希自洽。


def test_phase2_launch_passes_user_route_and_preserves_actor_input(
        tmp_path, monkeypatch):
    """Phase-2 启动传递用户路由且不污染父环境/Actor 系统提示。"""
    spec = make_study(tmp_path, monkeypatch)
    # 构造带用户路由的研究。
    os.environ["TEST_USER_KEY"] = "synthetic-key"
    # 注入用户模拟器密钥（isolated fixture 已清空环境）。
    cfg = load(str(REPO / "config/roles/target.yaml"))
    prompts = {enabled: build_system(cfg, ask_enabled=enabled)
               for enabled in (False, True)}
    # 基线系统提示（ask 开/关两种形态）。
    before = dict(os.environ)
    # 父环境快照。
    calls = []
    # 收集子进程启动记录。

    def launch(argv, **kwargs):
        # 伪 subprocess.run：校验子环境与 argv 后伪造子进程完成。
        child = kwargs.get("env") or dict(os.environ)
        # 子进程拿到的环境（pipeline 应显式传 env）。
        calls.append(child)
        assert child["OSWORLD_USER_SIM_MODEL"] == ROUTE["model"]
        # 模型名按路由传递。
        assert child["OSWORLD_USER_SIM_PROVIDER"] == ROUTE["provider"]
        # 供应商按路由传递。
        assert child["OSWORLD_USER_SIM_BASE_URL"] == ROUTE["base_url"]
        # base_url 按路由传递。
        assert child["OSWORLD_USER_SIM_API_KEY_ENV"] == "TEST_USER_KEY"
        # 密钥环境变量"名字"按路由传递（传名字不传值）。
        assert child["OSWORLD_USER_SIM_MAX_TOKENS"] == "256"
        # max_tokens 以字符串形式传递。
        assert child["TEST_USER_KEY"] == "synthetic-key"
        # 密钥值出现在子环境（供子进程按名读取）。
        assert child["WEBSITE_HOST_SUFFIX"] == "site.invalid"
        # 评测锁里的网站后缀也下发（任务搭建所需）。
        assert not any(key.startswith("OSWORLD_EVAL_") for key in child)
        # 关键：评分器侧零配置（学习不需要评分器）。
        assert argv[argv.index("--target-config") + 1] == str(
            REPO / "config/roles/target.yaml")
        # argv 指向真实目标配置。
        assert not any("synthetic-key" in argument for argument in argv)
        # 关键：密钥值绝不进 argv（命令行参数可能被进程列表看到）。
        complete_child(spec)
        # 子进程"完成"：落盘合法 PASS 结果。
        return SimpleNamespace(returncode=0)
        # 返回成功退出码。

    monkeypatch.setattr(pipeline.subprocess, "run", launch)
    # 替换真实子进程启动。
    assert pipeline.execute_phase2(spec)["status"] == "complete"
    # Phase-2 执行完成（消费子结果）。
    assert len(calls) == 1
    # 子进程恰好启动一次。
    assert dict(os.environ) == before
    # 父环境逐键未变（零污染）。
    assert {enabled: build_system(cfg, ask_enabled=enabled)
            for enabled in (False, True)} == prompts
    # Actor 系统提示构建确定性不受路由配置影响（逐字相等）。


def test_successive_studies_do_not_inherit_previous_user_route(tmp_path, monkeypatch):
    """后续研究绝不继承前一个研究的用户路由（跨研究零残留）。"""
    before = dict(os.environ)
    seen = []
    for name, route in (("first", ROUTE), ("second", None)):
        spec = make_study(tmp_path, monkeypatch, name=name, route=route)
        # 第一个研究带路由，第二个不带。

        def launch(_argv, **kwargs):
            seen.append(kwargs.get("env") or dict(os.environ))
            complete_child(spec)
            return SimpleNamespace(returncode=0)
            # 记录子环境并伪造完成。

        monkeypatch.setattr(pipeline.subprocess, "run", launch)
        pipeline.execute_phase2(spec)
        # 依次执行两个研究。
    assert seen[0]["OSWORLD_USER_SIM_MODEL"] == ROUTE["model"]
    # 第一个子进程拿到路由模型。
    assert not any(key.startswith("OSWORLD_USER_SIM_") for key in seen[1])
    # 关键：第二个子进程环境里没有任何用户路由残留。
    assert dict(os.environ) == before
    # 父环境依旧零污染。


def test_user_route_is_optional_and_missing_credential_does_not_block_learning(
        tmp_path, monkeypatch):
    """用户路由可选：缺失路由或缺失凭据都不阻塞学习（学习只需任务搭建）。"""
    for name, route in (("absent", None), ("declared", ROUTE)):
        spec = make_study(tmp_path, monkeypatch, name=name, route=route)
        # 两种形态：未声明路由 / 声明了路由但凭据缺失（TEST_USER_KEY 未注入）。
        environment = {"UNRELATED": "unchanged"}
        receipt = pipeline._configure_release_environment(
            spec, environment=environment)
        # 执行发布环境配置。
        assert receipt["user_simulator"] == route
        # 回执如实记录用户路由（存在或 None）。
        assert environment["UNRELATED"] == "unchanged"
        # 无关键不被触碰。
        assert "TEST_USER_KEY" not in environment
        # 凭据缺失时不注入占位值（绝不伪造密钥）。
        assert not any(key.startswith("OSWORLD_EVAL_") for key in environment)
        # 评分器侧依旧零配置。


def test_release_user_route_matches_baseline():
    """发布锁与基线锁的用户模拟器配置必须一致（发布配置不失真）。"""
    baseline = json.loads((REPO / "config/osworld/baseline.lock.json").read_text())
    evaluation = json.loads((REPO / "config/osworld/evaluation.lock.json").read_text())
    # 读取两份真实锁文件。
    assert evaluation["user_simulator"] == baseline["user_simulator"]
    # 用户模拟器段逐键一致。


def test_matching_user_configuration_is_preserved(tmp_path):
    """与路由匹配的既有用户配置原样保留（不重复注入/不覆盖）。"""
    environment = {
        **{f"OSWORLD_USER_SIM_{key.upper()}": str(value)
           for key, value in ROUTE.items()},
        "TEST_USER_KEY": "synthetic-key",
        "OSWORLD_USER_SIM_API_KEY": "explicit-direct-key",
        "OSWORLD_USER_SIM_TEMPERATURE": "0.3",
    }
    # 环境已带完整路由变量、凭据、显式直连密钥与额外温度项。
    before = dict(environment)
    configure_user_simulator(
        ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
        environment=environment)
    # 执行绑定。
    assert environment == before
    # 关键：环境逐键未变（匹配配置被保留而非改写）。


def test_conflicting_user_configuration_is_not_silently_replaced(tmp_path):
    """冲突的既有用户配置必须报错，绝不静默替换，错误信息不泄漏密钥。"""
    environment = {"TEST_USER_KEY": "synthetic-key",
                   "OSWORLD_USER_SIM_MODEL": "other/model"}
    # 环境里已有不同模型的用户路由。
    before = dict(environment)
    with pytest.raises(BenchmarkRuntimeError) as caught:
        configure_user_simulator(
            ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment)
        # 必须抛基准运行时错误。
    assert environment == before
    # 冲突环境不被改写。
    assert "synthetic-key" not in str(caught.value)
    # 错误信息绝不泄漏密钥值。


def test_baseline_credential_loads_from_configured_env_file(tmp_path):
    """基线凭据可从 RSIAGENT_ENV_FILE 指定的 env 文件加载，回执不含密钥。"""
    env_file = tmp_path / "runtime.env"
    env_file.write_text("TEST_USER_KEY=synthetic-key\n")
    # env 文件里放凭据。
    environment = {"RSIAGENT_ENV_FILE": str(env_file)}
    receipt = configure_user_simulator(
        ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
        environment=environment)
    # 执行绑定（凭据应从 env 文件加载）。
    assert environment["TEST_USER_KEY"] == "synthetic-key"
    # 凭据进入环境。
    assert "synthetic-key" not in json.dumps(receipt)
    # 关键：回执（可能被日志/审计消费）绝不含密钥值。


@pytest.mark.parametrize("route", [[], {}, {**ROUTE, "model": ""}])
def test_incomplete_declared_user_route_remains_an_error(tmp_path, route):
    """声明不完整的用户路由（空列表/空字典/空 model）仍是错误且零写入。"""
    environment = {}
    with pytest.raises(BenchmarkRuntimeError):
        configure_user_simulator(
            route, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment, load_credential=False)
        # 三种残缺路由都必须报错（None 才表示"未声明"）。
    assert environment == {}
    # 失败时环境零写入。


def initialized_child(tmp_path, monkeypatch, *, module, route=ROUTE,
                      shell=None, repo_env="", osworld_env=""):
    """Exercise real child initialization after the pipeline constructs its env."""
    # 函数 docstring：在 pipeline 构造好 env 之后，用真实子进程做初始化实验。
    spec = make_study(tmp_path, monkeypatch, route=route)
    osworld = tmp_path / "OSWorld-V2"
    osworld.mkdir()
    (osworld / ".env").write_text(osworld_env)
    (tmp_path / ".env").write_text(repo_env)
    # 分别放置 OSWorld .env 与仓库 .env（凭据查找的两级回退）。
    environment = {
        "PYTHONPATH": str(REPO),
        "RSIAGENT_ROOT": str(tmp_path),
        "OSWORLD_ROOT": str(osworld),
        **(shell or {}),
    }
    # 子环境：可导入仓库、两个根都指到临时目录、可选 shell 级密钥。
    pipeline._configure_release_environment(spec, environment=environment)
    # 先按真实管线逻辑配置发布环境（用户路由绑定在这一步发生）。
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib, json, os, sys
from llm.client import _api_key
importlib.import_module(sys.argv[1])._install_paths()
name = os.environ.get("OSWORLD_USER_SIM_API_KEY_ENV", "")
print(json.dumps({
    "actor_key": _api_key(),
    "shared_key": os.environ.get("OPENROUTER_API_KEY"),
    "named_key": os.environ.get(name),
    "user_key": os.environ.get("OSWORLD_USER_SIM_API_KEY"),
}))
""", module],
        cwd=REPO, env=environment, text=True, capture_output=True, check=True)
    # 真实子进程：导入被测模块并 _install_paths()，然后报告四个密钥通道的
    # 终态（Actor 密钥/共享密钥/按名密钥/用户直连密钥）。
    return environment, json.loads(result.stdout)
    # 返回（子环境, 子进程报告）。


@pytest.mark.parametrize("module", [
    "benchmarks.osworld.phase1", "benchmarks.osworld.runtime"])
@pytest.mark.parametrize("shell_key", [None, "shell-key"])
def test_real_child_keeps_original_actor_credential_precedence(
        tmp_path, monkeypatch, module, shell_key):
    """真实子进程保持 Actor 凭据原有优先级：shell > OSWorld .env > 仓库 .env。"""
    _, child = initialized_child(
        tmp_path, monkeypatch, module=module,
        route={**ROUTE, "api_key_env": "OPENROUTER_API_KEY"},
        shell={"OPENROUTER_API_KEY": shell_key} if shell_key else {},
        repo_env="OPENROUTER_API_KEY=repo-stale-key\n",
        osworld_env="OPENROUTER_API_KEY=osworld-working-key\n")
    # 用户路由把密钥环境变量指到 OPENROUTER_API_KEY；三个层级各放不同值。
    expected = shell_key or "osworld-working-key"
    # 期望值：shell 键优先，否则 OSWorld .env 的键（仓库 .env 的"陈旧键"
    # 必须被 OSWorld .env 覆盖）。
    assert child == {
        "actor_key": expected, "shared_key": expected,
        "named_key": expected, "user_key": None}
    # 四通道终态：Actor/共享/按名都是期望密钥；用户直连密钥不存在。


@pytest.mark.parametrize("key_env", ["OPENROUTER_API_KEY", "CUSTOM_USER_KEY"])
def test_real_runtime_loads_repo_fallback_only_into_user_channel(
        tmp_path, monkeypatch, key_env):
    """仓库回退密钥只进用户通道：共享密钥通道不被仓库 .env 污染。"""
    repo_env = "OPENROUTER_API_KEY=repo-actor-key\n"
    expected = "repo-actor-key"
    if key_env != "OPENROUTER_API_KEY":
        repo_env += "CUSTOM_USER_KEY=repo-user-key\n"
        expected = "repo-user-key"
    # 仓库 .env 提供 Actor 密钥（以及自定义用户密钥，若路由指向它）。
    environment, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        route={**ROUTE, "api_key_env": key_env}, repo_env=repo_env)
    assert key_env not in environment
    # 按名密钥环境变量不回灌进父环境 dict。
    assert "OSWORLD_USER_SIM_API_KEY" not in environment
    # 用户直连密钥也不注入环境。
    assert child == {
        "actor_key": "repo-actor-key", "shared_key": None,
        "named_key": None, "user_key": expected}
    # 关键：Actor 密钥来自仓库 .env；共享密钥为 None；用户通道拿到路由指向
    # 的仓库回退密钥（仓库密钥只进用户通道）。


def test_real_runtime_preserves_custom_osworld_user_credential(
        tmp_path, monkeypatch):
    """OSWorld 自有用户密钥（TEST_USER_KEY）被保留，不与 Actor 密钥混淆。"""
    _, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        repo_env="TEST_USER_KEY=repo-stale-key\n",
        osworld_env="TEST_USER_KEY=osworld-user-key\nOPENROUTER_API_KEY=actor-key\n")
    # OSWorld .env 提供用户密钥与 Actor 密钥；仓库 .env 的用户密钥是陈旧值。
    assert child == {
        "actor_key": "actor-key", "shared_key": "actor-key",
        "named_key": "osworld-user-key", "user_key": None}
    # Actor/共享通道用 OSWorld 的 actor-key；按名通道读到 OSWorld 用户密钥；
    # 用户直连密钥不存在。


def test_real_runtime_keeps_explicit_user_override_without_loading_other_keys(
        tmp_path, monkeypatch):
    """显式 OSWORLD_USER_SIM_API_KEY 覆盖生效，且不加载其他密钥通道。"""
    environment, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime",
        shell={"OSWORLD_USER_SIM_API_KEY": "direct-user-key"},
        repo_env="TEST_USER_KEY=repo-stale-key\nOPENROUTER_API_KEY=actor-key\n")
    # shell 直接给用户密钥；仓库 .env 有陈旧用户密钥与 Actor 密钥。
    assert "TEST_USER_KEY" not in environment
    # 陈旧的按名密钥不被加载进环境。
    assert child == {
        "actor_key": "actor-key", "shared_key": None,
        "named_key": None, "user_key": "direct-user-key"}
    # Actor 用 actor-key；共享/按名为 None；用户直连密钥原样保留。


@pytest.mark.parametrize("route", [None, ROUTE])
def test_real_runtime_does_not_require_a_user_credential(
        tmp_path, monkeypatch, route):
    """无用户凭据（未声明路由或声明了但无凭据）也能完成初始化。"""
    _, child = initialized_child(
        tmp_path, monkeypatch, module="benchmarks.osworld.runtime", route=route)
    assert child == {
        "actor_key": "", "shared_key": None,
        "named_key": None, "user_key": None}
    # 全部密钥通道为空——初始化不因缺少用户凭据而失败。


def test_baseline_user_binding_still_requires_the_declared_credential(tmp_path):
    """基线用户绑定仍要求声明的凭据环境变量存在（覆盖直连密钥不算数）。"""
    environment = {"OSWORLD_USER_SIM_API_KEY": "direct-user-key"}
    with pytest.raises(BenchmarkRuntimeError, match="TEST_USER_KEY"):
        configure_user_simulator(
            ROUTE, lock_path=tmp_path / "lock.json", repo_root=tmp_path,
            environment=environment)
        # 路由声明了 api_key_env=TEST_USER_KEY，但该名字的凭据缺失——
        # 即使有直连密钥也必须报错（错误信息指向缺失的变量名）。
