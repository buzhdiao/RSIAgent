"""Bind a direct benchmark run to the runtime declared by its config lock.

The batch runner exports evaluator and task-user-simulator routing
before it starts :mod:`benchmarks.osworld.task`.  Researchers also launch individual tasks
directly while recovering or rerunning a result.  A config such as
``foo.yaml`` may therefore carry a sibling ``foo.lock.json``; this module makes
that lock authoritative in both launch paths.

Only transport metadata and credentials are mounted.  Task content, evaluator
source, scores, and grader output remain outside the Actor/Verifier lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import MutableMapping
from pathlib import Path

from config.runtime_paths import resolve_env_file, resolve_path


# 基准运行时错误：声明的基准运行时缺失、不一致或不可用
class BenchmarkRuntimeError(RuntimeError):
    """The declared benchmark runtime is missing, inconsistent, or unusable."""


def _sha256(path: Path) -> str:
    # 计算文件整字节内容的 SHA-256 十六进制摘要
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_secret(environment: MutableMapping[str, str], name: str,
                 *, repo_root: Path, required: bool = True) -> None:
    # 凭据已在环境中：无需加载
    if environment.get(name):
        return
    # 解析仓库凭据文件（.env）
    env_path = resolve_env_file(environment, repo_root)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        # 文件读不了：可选则跳过，必需则报错
        if not required:
            return
        raise BenchmarkRuntimeError(
            f"benchmark credential {name!r} is absent and the credential file "
            f"cannot be read: {env_path}") from exc
    # 逐行找 “NAME=” 前缀
    prefix = f"{name}="
    for line in lines:
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
            if value:
                # 找到非空值：注入环境并返回
                environment[name] = value
                return
    if not required:
        # 可选凭据缺失：静默放过
        return
    # 环境与凭据文件里都没有：报错
    raise BenchmarkRuntimeError(
        f"benchmark credential {name!r} is absent from the environment and "
        f"credential file: {env_path}")


def _bind(environment: MutableMapping[str, str], name: str, value: object,
          *, lock_path: Path) -> None:
    # 把锁中声明的值渲染为字符串
    rendered = str(value)
    # 读取环境中现有值
    current = environment.get(name, "")
    if current and current != rendered:
        # 环境已有不同值：与锁冲突，拒绝绑定
        raise BenchmarkRuntimeError(
            f"{name} conflicts with associated benchmark lock {lock_path}: "
            f"environment={current!r}, lock={rendered!r}")
    # 无冲突：写入绑定
    environment[name] = rendered


def configure_user_simulator(
        user_simulator: dict | None, *, lock_path: Path, repo_root: Path,
        environment: MutableMapping[str, str],
        load_credential: bool = True) -> dict | None:
    """Bind the optional task-user channel independently of the evaluator."""
    # 未声明用户模拟器：无事可做
    if user_simulator is None:
        return None
    if not isinstance(user_simulator, dict):
        # 声明了但不是对象：配置错误
        raise BenchmarkRuntimeError(
            f"associated user_simulator runtime is not an object: {lock_path}")
    # 任务用户通道的必填字段
    required = ("provider", "model", "base_url", "api_key_env", "max_tokens")
    missing = [key for key in required if user_simulator.get(key) in (None, "")]
    if missing:
        raise BenchmarkRuntimeError(
            f"associated user-simulator runtime lacks {missing}: {lock_path}")
    # Validate on a copy so a conflicting route cannot leave a partial binding.
    # （中文）在副本上校验，避免冲突路径留下半套绑定污染原环境
    bound = dict(environment)
    for key in required:
        _bind(bound, f"OSWORLD_USER_SIM_{key.upper()}", user_simulator[key],
              lock_path=lock_path)
    if load_credential:
        # 按声明的环境变量名加载该通道的 API 凭据
        _load_secret(bound, str(user_simulator["api_key_env"]),
                     repo_root=repo_root)
    # 校验全部通过：一次性写回真实环境
    environment.update(bound)
    # 返回只含必填键的摘要（供记录/展示，不含密钥）
    return {key: user_simulator[key] for key in required}


def load_user_simulator_credential(
        *, repo_root: Path,
        environment: MutableMapping[str, str] | None = None) -> None:
    """Fill the task-user fallback after the child's normal dotenv setup."""
    # 环境来源兜底
    values = os.environ if environment is None else environment
    # 该通道声明使用的凭据环境变量名
    key_env = values.get("OSWORLD_USER_SIM_API_KEY_ENV", "")
    if (not key_env or values.get("OSWORLD_USER_SIM_API_KEY")
            or values.get(key_env)):
        # 未声明、已有回执、或直接凭据已就位：无需填充
        return
    # A repository fallback belongs only to this user channel. Injecting it as
    # OPENROUTER_API_KEY would also change the Actor's credential resolution.
    # （中文）仓库回退凭据只属于这个任务用户通道；若注入 OPENROUTER_API_KEY 会
    # 连带改变 Actor 的凭据解析，绝不允许。
    candidate = dict(values)
    _load_secret(candidate, key_env, repo_root=repo_root, required=False)
    if candidate.get(key_env):
        # 仅以专用回执变量暴露给该通道
        values["OSWORLD_USER_SIM_API_KEY"] = candidate[key_env]


def configure_associated_benchmark_lock(
        config_path: Path | None, *, repo_root: Path,
        environment: MutableMapping[str, str] | None = None) -> dict | None:
    """Apply the evaluator/user-channel runtime from a config's sibling lock.

    ``RSIAGENT_BENCHMARK_LOCK`` explicitly selects a lock.  Otherwise ``foo.yaml``
    discovers ``foo.lock.json``.  Configs without an associated lock preserve
    the historical generic ``benchmarks/osworld/task.py`` behavior.
    """
    if config_path is None:
        # 没有配置文件：沿用历史通用行为
        return None
    values = os.environ if environment is None else environment
    # 显式锁优先：RSIAGENT_BENCHMARK_LOCK 环境变量
    explicit = values.get("RSIAGENT_BENCHMARK_LOCK", "").strip()
    lock_path = (resolve_path(explicit, values, repo_root)
                 if explicit else config_path.with_suffix(".lock.json"))
    if not lock_path.is_file():
        if explicit:
            # 显式指定的锁不存在：硬错误
            raise BenchmarkRuntimeError(
                f"explicit benchmark lock does not exist: {lock_path}")
        # 自动发现的兄弟锁不存在：保持历史行为
        return None

    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 锁不可读/非法 JSON：报错
        raise BenchmarkRuntimeError(
            f"cannot read associated benchmark lock: {lock_path}") from exc

    actor = lock.get("actor_agent")
    if not isinstance(actor, dict):
        # 锁必须声明 actor_agent 对象
        raise BenchmarkRuntimeError(
            f"associated benchmark lock has no actor_agent object: {lock_path}")
    # 锁中声明的 Actor 配置路径
    declared_config = resolve_path(
        str(actor.get("config", "")), values, repo_root)
    if declared_config != config_path.resolve():
        raise BenchmarkRuntimeError(
            f"associated lock binds a different Actor config: {declared_config}")
    # 配置哈希校验：防止锁与实际配置漂移
    declared_hash = str(actor.get("sha256", ""))
    observed_hash = _sha256(config_path)
    if not declared_hash or declared_hash != observed_hash:
        raise BenchmarkRuntimeError(
            "Actor config does not match its associated benchmark lock: "
            f"expected={declared_hash!r}, observed={observed_hash!r}")

    evaluator = lock.get("evaluator")
    if not isinstance(evaluator, dict):
        # 锁必须声明 evaluator 对象
        raise BenchmarkRuntimeError(
            f"associated benchmark lock has no evaluator object: {lock_path}")
    # 评审器（sealed evaluator）运行时的必填字段
    required_eval = (
        "provider", "model", "base_url", "api_key_env",
        "retry_attempts", "retry_delay_seconds")
    missing = [key for key in required_eval if evaluator.get(key) in (None, "")]
    if missing:
        raise BenchmarkRuntimeError(
            f"associated evaluator runtime lacks {missing}: {lock_path}")
    # 加载评审器凭据（必需）
    key_env = str(evaluator["api_key_env"])
    _load_secret(values, key_env, repo_root=repo_root)
    # 评审器运行时的环境绑定表（只挂传输元数据，不碰任务内容/评分）
    evaluator_bindings = {
        "OSWORLD_EVAL_MODEL_PROVIDER": evaluator["provider"],
        "OSWORLD_EVAL_MODEL_NAME": evaluator["model"],
        "OSWORLD_EVAL_MODEL_BASE_URL": evaluator["base_url"],
        "OSWORLD_EVAL_MODEL_API_KEY_ENV": key_env,
        "OSWORLD_EVAL_MODEL_RETRY_ATTEMPTS": evaluator["retry_attempts"],
        "OSWORLD_EVAL_MODEL_RETRY_DELAY": evaluator["retry_delay_seconds"],
    }
    if evaluator.get("website_host_suffix"):
        evaluator_bindings["WEBSITE_HOST_SUFFIX"] = \
            evaluator["website_host_suffix"]
    for name, value in evaluator_bindings.items():
        # 逐项绑定（冲突即报错）
        _bind(values, name, value, lock_path=lock_path)

    # 可选的任务用户模拟器通道独立绑定
    configure_user_simulator(
        lock.get("user_simulator"), lock_path=lock_path, repo_root=repo_root,
        environment=values)

    # 记录已解析锁的路径与哈希（审计回执）
    lock_hash = _sha256(lock_path)
    values["RSIAGENT_BENCHMARK_LOCK_RESOLVED"] = str(lock_path.resolve())
    values["RSIAGENT_BENCHMARK_LOCK_SHA256"] = lock_hash
    # 返回无密钥的锁绑定摘要（供实验清单记录）
    return {
        "path": str(lock_path.resolve()),
        "sha256": lock_hash,
        "benchmark_release": str(lock.get("benchmark_release", "")),
        "evaluator_provider": str(evaluator["provider"]),
        "evaluator_model": str(evaluator["model"]),
    }
