"""Statically read an OSWorld public surface without importing evaluator code."""

# 以纯静态方式读取 OSWorld 任务的公开面（instruction 等元数据），绝不 import 评审代码，
# 从根上避免评审逻辑/评分信号进入 Agent 可见路径。
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path


# 公开任务面数据类（冻结不可变）：只含公开、可审计的字段
@dataclass(frozen=True)
class PublicTaskSurface:
    # 任务类文件路径
    class_path: Path
    # 公开指令文本
    instruction: str
    # 任务是否声明需要代理（proxy）
    proxy: bool
    # 是否禁用 VNC
    disable_vnc: bool
    # 是否禁用录屏
    disable_recording: bool
    # 任务类是否定义了 setup 方法
    has_setup: bool


def read_public_env_value(osworld_root: str | Path, name: str) -> str:
    # 优先取真实进程环境变量
    osworld_root = Path(osworld_root)
    value = os.environ.get(name)
    if value is not None:
        return value
    # 否则回退到 OSWorld 检出目录下的 .env 文件
    env_path = osworld_root / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        # 公开指令依赖的变量既不在环境也不在 .env：直接失败
        raise RuntimeError(f"public task instruction requires unset {name}") from exc
    # 逐行找 “NAME=” 前缀
    prefix = name + "="
    for line in lines:
        if line.startswith(prefix):
            # 去掉前缀并剥离空白与引号
            return line[len(prefix) :].strip().strip("'\"")
    # 找不到该变量同样失败
    raise RuntimeError(f"public task instruction requires unset {name}")


def load_public_task_surface(
    osworld_root: str | Path, task_id: str, *, website_host_suffix: str | None = None
) -> PublicTaskSurface:
    """Statically extract reviewed public task metadata.

    Task modules are parsed as data and are never imported.  The deliberately
    small renderer accepts only static string expressions needed by the public
    instruction, whitespace stripping, and canonical website URLs. Arbitrary
    calls and evaluator code are never executed. Run-generated credentials are
    represented by placeholders for removal from learning directions.
    """
    # 解析 OSWorld 根目录
    root = Path(osworld_root).resolve()
    # 从任务 id 中去掉 “task_” 前缀得到数字编号
    suffix = task_id.removeprefix("task_")
    if not suffix.isdigit() or not suffix:
        raise ValueError(f"invalid OSWorld task id: {task_id}")
    # 拼出任务类源文件路径（不导入，只当数据解析）
    path = root / "evaluation_examples" / "task_class" / f"task_{suffix}.py"
    # 把源码解析为 AST（语法树），静态读取
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # 收集模块级简单赋值：名字 -> 表达式 AST（供 instruction 内的名字引用解析）
    module_values: dict[str, ast.expr] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            module_values[node.targets[0].id] = node.value

    # 期望的任务类名为 Task<编号>
    expected_class = f"Task{suffix}"
    task_class = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == expected_class
        ),
        None,
    )
    if task_class is None:
        raise RuntimeError(f"public task class {expected_class} not found in {path}")
    # 收集类体中的简单赋值：属性名 -> 表达式 AST
    class_values: dict[str, ast.expr] = {}
    for item in task_class.body:
        if (
            isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
        ):
            class_values[item.targets[0].id] = item.value
    if "instruction" not in class_values:
        raise RuntimeError(f"public instruction not found in {path}")

    # 已解析名字的缓存；resolving 记录解析中的名字（检测循环引用）
    resolved: dict[str, str] = {}
    resolving: set[str] = set()

    def render(node: ast.expr) -> str:
        # 仅支持静态字符串表达式：字符串常量直接返回
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            # 名字已解析过则用缓存
            if node.id in resolved:
                return resolved[node.id]
            # HOST_SUFFIX：站点域名后缀，取显式参数或环境/.env 值
            if node.id == "HOST_SUFFIX":
                resolved[node.id] = website_host_suffix or read_public_env_value(
                    root, "WEBSITE_HOST_SUFFIX"
                )
                return resolved[node.id]
            # 运行期才生成的凭据名：以占位符表示（从学习方向中移除）
            if node.id in {
                "_OVERLEAF_USER_EMAIL",
                "_OVERLEAF_USER_PASSWORD",
                "OVERLEAF_USER_EMAIL",
                "OVERLEAF_USER_PASSWORD",
            }:
                return "<run-generated>"
            # 其余名字必须在模块级/类级赋值里找得到
            expression = class_values.get(node.id, module_values.get(node.id))
            if node.id in resolving or expression is None:
                raise RuntimeError(f"unsupported public instruction name: {node.id}")
            # 登记为“解析中”以防循环引用
            resolving.add(node.id)
            try:
                resolved[node.id] = render(expression)
            finally:
                resolving.remove(node.id)
            return resolved[node.id]
        if isinstance(node, ast.JoinedStr):
            # f-string：逐段拼接（仅允许纯字符串段与无格式规格的插值）
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    if value.conversion != -1 or value.format_spec is not None:
                        raise RuntimeError(
                            "formatted public instruction values are unsupported"
                        )
                    parts.append(render(value.value))
                else:
                    raise TypeError("unsupported public instruction f-string component")
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # 字符串拼接表达式：左右两边分别渲染后相连
            return render(node.left) + render(node.right)
        if isinstance(node, ast.Call) and not node.keywords:
            # 仅放行两种白名单调用：str.strip()（无参）
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "strip"
                and not node.args
            ):
                return render(node.func.value).strip()
            # 与 build_website_url(host)（构造规范化站点 URL）
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "build_website_url"
                and len(node.args) == 1
            ):
                host = website_host_suffix or read_public_env_value(
                    root, "WEBSITE_HOST_SUFFIX"
                )
                return f"https://{render(node.args[0])}.{host}"
        # 其余任何表达式（任意调用/评审代码）一律拒绝执行
        raise RuntimeError("public instruction must be a static string expression")

    def public_bool(name: str) -> bool:
        # 读取公开布尔字段：未定义视为 False
        node = class_values.get(name)
        if node is None:
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        # 字段存在但不是静态布尔：拒绝
        raise RuntimeError(f"public task field {name} must be a static boolean")

    # 渲染公开指令文本
    instruction = render(class_values["instruction"])
    if not instruction.strip():
        raise RuntimeError(f"public instruction is empty in {path}")
    # 组装公开任务面（含 setup 方法存在性探测）
    return PublicTaskSurface(
        class_path=path.resolve(),
        instruction=instruction,
        proxy=public_bool("proxy"),
        disable_vnc=public_bool("disable_vnc"),
        disable_recording=public_bool("disable_recording"),
        has_setup=any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "setup"
            for item in task_class.body
        ),
    )


# 模块公开 API
__all__ = [
    "PublicTaskSurface",
    "load_public_task_surface",
    "read_public_env_value",
]
