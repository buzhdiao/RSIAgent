"""Resolve runtime locations without assuming one person's home directory."""
# 运行时路径解析模块：所有关键位置（仓库根、OSWorld 检出、凭据文件）都允许环境变量
# 覆盖，不绑定某个人的家目录，保证在不同机器/挂载下可复现。
from __future__ import annotations

# 引入 Mapping 抽象：环境来源既可以是真实 os.environ，也可以是调用方注入的映射
from collections.abc import Mapping
import os
from pathlib import Path


# 仓库根目录默认取本文件（config/）的上一级目录，即 RSIAgent 检出根
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: str | Path) -> Path:
    """Normalize configured paths while allowing targets created later."""
    # 展开 ~ 并归一化为绝对路径；strict=False 允许目标路径稍后才创建
    return Path(path).expanduser().resolve(strict=False)


def _environment(environment: Mapping[str, str] | None) -> Mapping[str, str]:
    # 环境来源兜底：未显式传入映射时使用真实进程环境变量
    return os.environ if environment is None else environment


def resolve_root(
        environment: Mapping[str, str] | None = None) -> Path:
    """Return this checkout by default, or an explicit ``RSIAGENT_ROOT``."""
    # 读取环境映射（默认真实环境变量）
    values = _environment(environment)
    # RSIAgent 根：优先显式环境变量 RSIAGENT_ROOT，否则默认本检出目录
    return _resolve(values.get("RSIAGENT_ROOT", _REPO_ROOT))


def resolve_osworld_root(
        environment: Mapping[str, str] | None = None,
        repo_root: Path | None = None) -> Path:
    """Return the sibling OSWorld-V2 checkout unless explicitly configured."""
    # 读取环境映射
    values = _environment(environment)
    # RSIAgent 根：入参优先，否则自行解析
    resolved_rsiagent = repo_root or resolve_root(values)
    # OSWorld-V2 检出根：优先环境变量 OSWORLD_ROOT，否则默认 RSIAgent 的同级兄弟目录
    return _resolve(values.get(
        "OSWORLD_ROOT", resolved_rsiagent.parent / "OSWorld-V2"))


def resolve_env_file(
        environment: Mapping[str, str] | None = None,
        repo_root: Path | None = None) -> Path:
    """Return the RSIAgent credential file, with an override for secret mounts."""
    # 读取环境映射
    values = _environment(environment)
    # RSIAgent 根：入参优先，否则自行解析
    resolved_rsiagent = repo_root or resolve_root(values)
    # 凭据文件：优先环境变量 RSIAGENT_ENV_FILE（支持密钥挂载覆盖），默认 <根>/.env
    return _resolve(values.get("RSIAGENT_ENV_FILE", resolved_rsiagent / ".env"))


def resolve_path(
        path: str | Path,
        environment: Mapping[str, str] | None = None,
        repo_root: Path | None = None) -> Path:
    """Resolve a RSIAgent-owned path independently of the process CWD."""
    # 展开 ~ 得到候选路径（先不归一化，便于判断绝对/相对）
    candidate = Path(path).expanduser()
    # 绝对路径直接归一化返回，与当前工作目录无关
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    # 相对路径以 RSIAgent 根为基准拼接（而不是进程 CWD），保证结果可重现
    resolved_rsiagent = repo_root or resolve_root(environment)
    return (resolved_rsiagent / candidate).resolve(strict=False)


def normalize_verifier_config_paths(config, repo_root: Path):
    """Make deferred Verifier config loads safe after entering OSWorld's CWD."""
    # 遍历两个延迟加载的 Verifier（验证者）配置路径字段
    for attribute in (
            "agentic_verifier_config",
            "escalation_agentic_verifier_config"):
        # 读取当前字段值（空表示未配置）
        value = getattr(config, attribute, "")
        if value:
            # 进入 OSWorld 工作目录后 CWD 会改变，这里预先把相对配置路径锚定到仓库根
            setattr(config, attribute, str(resolve_path(
                value, repo_root=repo_root)))
    # 原样返回配置对象（已就地修改），便于链式使用
    return config
