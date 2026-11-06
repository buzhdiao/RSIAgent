"""Process-local ALE platform bindings shared by all VMs in one task lineage."""
# 【中文注释·模块定位】进程级 ALE 平台绑定：一个 worker 进程只处理一种操作系统，
# 本模块把 core/explore 里默认针对 Linux 客户机的快照（snapshot）、Look 图像、系统
# 提示词、Verifier 执行器等钩子，替换成按实际 VM 类型分派的版本。所有同谱系 VM 共享
# 这些绑定（模块级单例 _platform 记录已绑定的 OS，混合 OS 直接报错）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# json：解析客户机内脚本输出的文件清单。
import json
# ntpath：Windows 风格路径的规范化（normpath/normcase）。
import ntpath
# re：Look 路径合法性校验。
import re

# AleVM：Linux 客户机的同步 CUA 传输实现。
from .transport import AleVM
# Windows 侧的 VM 与 Verifier 执行器实现。
from .windows import WindowsVerifierExecutor, WindowsVM

# 已绑定平台的模块级标记：None=未绑定，否则为 OS 类型字符串。
_platform = None


# VM 工厂：按沙箱描述的 OS 类型选择传输实现。
def make_vm(sandbox):
    # Windows 沙箱用 WindowsVM（含 Git Bash/Job 对象等适配），否则用通用 AleVM。
    return WindowsVM(sandbox) if sandbox["os"] == "windows" else AleVM(sandbox)


# 客户机初始化：按 OS 检查/准备运行时依赖。
def initialize_vm(vm):
    if vm.is_windows:
        # Windows：执行完整准备（helper 目录、python/bash 探测、依赖安装、脚本下发）。
        vm.prepare()
    else:
        # Linux：一条命令检查四件事——.ale 目录可建、sudo 免密可用、
        # unshare/nsenter/setpriv 命名空间工具齐全（Verifier 隔离依赖它们）。
        result = vm.native_command(
            "mkdir -p /home/user/.ale && sudo -n true && command -v unshare && command -v nsenter && command -v setpriv"
        )
        # 任一缺失都意味着 Verifier 无特权或无命名空间工具，直接失败。
        if result.get("return_code"):
            raise RuntimeError("ALE Ubuntu lacks verifier privilege or namespace tools")


# 平台绑定入口：登记可见根目录；首次绑定时装载全部钩子替换。
def bind_platform(vm, visible_roots=()):
    """A worker handles one OS; snapshots and Looks dispatch on the actual VM."""
    # 声明使用模块级单例。
    global _platform
    # 记录本 VM 的任务可见根目录（供快照钩子扩展扫描范围）。
    vm.visible_roots = tuple(visible_roots)
    # 已绑定过：校验 OS 一致后直接返回（钩子是进程级共享的，不能重复装）。
    if _platform is not None:
        if _platform != vm.os_type:
            raise RuntimeError("Mixed operating systems require separate workers")
        return
    # 记录本进程绑定的 OS。
    _platform = vm.os_type
    # 延迟导入需要被挂钩子的共享运行时模块。
    from core import actor, imagery, loop, verifier, verifier_runtime

    # 保留原始快照函数，便于非 ALE VM 回退默认实现。
    original_snapshot = loop._snapshot

    # 替换版快照：走客户机内 Python 精确枚举相对路径 + mtime。
    def snapshot(target_vm, excluded_paths=()):
        # 非 AleVM（如 OSWorld VM）保持原实现不动。
        if not isinstance(target_vm, AleVM):
            return original_snapshot(target_vm, excluded_paths)
        # 扫描根：home、桌面、文档目录加去重（dict.fromkeys 保序去重），再加任务可见根。
        roots = list(
            dict.fromkeys(
                [
                    target_vm.home,
                    target_vm.home + "/Desktop",
                    target_vm.home + "/Documents",
                    *getattr(target_vm, "visible_roots", ()),
                ]
            )
        )
        # 生成在客户机内执行的枚举脚本：os.walk 不跟随链接；跳过隐藏目录/文件与符号链接；
        # 深度超过 2 层不再下钻（顶层两层即可反映工作区状态）；输出 {绝对POSIX路径: mtime_ns}。
        code = f"""import os,json,pathlib
out={{}}
for root in {roots!r}:
 for current,dirs,files in os.walk(root,followlinks=False):
  relative=pathlib.Path(current).relative_to(root)
  dirs[:]=[d for d in dirs if not d.startswith('.') and not pathlib.Path(current,d).is_symlink()]
  if len(relative.parts)>=2: dirs[:]=[]
  for name in files:
   if name.startswith('.'): continue
   p=pathlib.Path(current,name)
   if p.is_symlink(): continue
   try: out[p.as_posix()]=str(p.stat().st_mtime_ns)
   except OSError: pass
print(json.dumps(out))
"""
        # 在客户机内执行脚本并解析输出的清单。
        result = json.loads(target_vm.trusted_python(code, timeout=60))
        # 排除路径映射为该 VM 的实际 home 并小写规范化（Windows 大小写不敏感）。
        excluded = [
            p.replace("/home/user", target_vm.home, 1).replace("\\", "/").casefold()
            for p in excluded_paths
        ]
        # 过滤掉命中排除前缀的条目后返回快照。
        return {
            p: m
            for p, m in result.items()
            if not any(
                p.casefold() == x or p.casefold().startswith(x + "/") for x in excluded
            )
        }

    # 用替换版快照覆盖 loop 模块的钩子。
    loop._snapshot = snapshot
    # Linux 到此为止；以下全部是 Windows 专属适配。
    if not vm.is_windows:
        return
    # 保留原始系统提示词构建函数。
    original_build = actor.build_system

    # Windows 版系统提示词：改写机器事实并翻译路径约定。
    def windows_system(cfg=None, **kwargs):
        # 在原提示词基础上把"真实 Ubuntu 机器"改为"真实 Windows 机器"。
        result = original_build(cfg, **kwargs).replace(
            "real Ubuntu machine", "real Windows machine"
        )
        # 把机器事实段替换为 Windows 版描述（原生 Python + Git Bash、安装方式不同）。
        result = result.replace(
            "MACHINE FACTS: Ubuntu with python3. You may install tools you need (pip3 / apt-get).",
            "MACHINE FACTS: Windows with native Python and Git Bash. You may install tools you need with pip or native Windows installers.",
        )
        # 追加 Windows 传输说明：路径写法（C:/...、E:/...）、home 位置；并把 /home/user 全部替换为实际 home。
        return (
            result.replace("/home/user", vm.home)
            + "\nWindows transport: Python runs natively; Bash runs through Git for Windows. Use C:/... or E:/... paths in Python and Looks. Home is C:/Users/User.\n"
        )

    # 同时替换 actor 与 loop 中的系统提示词构建入口。
    actor.build_system = loop.build_system = windows_system
    # 保留原始 Look 图像读取函数。
    original_look = imagery.fetch_look_image

    # 替换版 Look：Windows VM 走专属读取/渲染，否则走原实现。
    def fetch_look(target_vm, path):
        if isinstance(target_vm, WindowsVM):
            return target_vm.fetch_look_image(path)
        return original_look(target_vm, path)

    # 替换 imagery 与 loop 的 Look 入口。
    imagery.fetch_look_image = loop.fetch_look_image = fetch_look
    # verifier 模块若也暴露了同名函数则一并替换。
    if hasattr(verifier, "fetch_look_image"):
        verifier.fetch_look_image = fetch_look
    # Verifier 执行器换成 Windows 实现（受限令牌 + Job 对象 + 回滚镜像）。
    verifier_runtime.AgenticVerifierExecutor = WindowsVerifierExecutor
    if hasattr(verifier, "AgenticVerifierExecutor"):
        verifier.AgenticVerifierExecutor = WindowsVerifierExecutor
    # 保留原始 Verifier 系统提示词函数。
    original_verifier_system = verifier_runtime.agentic_verifier_system_for

    # Windows 版 Verifier 提示词：改写暂存区描述与路径约定。
    def verifier_system(*args, **kwargs):
        # 把 Linux tmpfs 概念改为"回滚镜像内的目录"。
        result = original_verifier_system(*args, **kwargs).replace(
            "tmpfs", "directory inside the rollback mirror"
        )
        # 追加 Windows 专属说明：原生 Python、Git Bash、绝对盘符路径、VERIFIER_SCRATCH 私有且跨检查持久、受限令牌运行。
        return (
            result.replace("/home/user", vm.home)
            + "\nThis is Windows. Python runs natively; Bash uses Git for Windows. Use absolute drive paths. VERIFIER_SCRATCH is private and persists across inspections. Programs run with a restricted Windows token with administrator rights disabled.\n"
        )

    # 替换 Verifier 系统提示词入口。
    verifier_runtime.agentic_verifier_system_for = verifier_system

    # Windows 版 Look 路径校验：只允许盘符绝对路径/主屏截图，且不得触及 Actor 私有路径。
    def validate_look(path, hide_actor_memory=False, private_paths=()):
        # 非字符串或含空字节/换行：直接判非法（防注入与多行路径）。
        if not isinstance(path, str) or any(c in path for c in ("\x00", "\n", "\r")):
            return False, "Malformed Look path"
        # screen: 前缀 = 截屏：Windows 只支持主桌面（空/0/:0）。
        if path.lower().startswith("screen:"):
            return path.split(":", 1)[1].strip() in (
                "",
                "0",
                ":0",
            ), "Windows supports the primary desktop only"
        # Look 必须是盘符开头的绝对路径（C:\ 或 C:/）。
        if not re.match(r"^[A-Za-z]:[/\\]", path):
            return False, "Windows Look requires an absolute drive path or screen:"
        # 规范化实际路径（大小写与分隔符归一）。
        actual = ntpath.normcase(ntpath.normpath(path))
        # 把私有路径里的 /home/user 前缀映射为该 VM 的实际 home。
        mapped = [p.replace("/home/user", vm.home, 1) for p in private_paths]
        # 需要隐藏 Actor 记忆时追加 ~/.memory。
        if hide_actor_memory:
            mapped.append(vm.home + "/.memory")
        # 实际路径命中任一私有路径（相等或位于其子树）：拒绝。
        if any(
            actual == (p := ntpath.normcase(ntpath.normpath(x)))
            or actual.startswith(p.rstrip("\\") + "\\")
            for x in mapped
        ):
            return False, "That path is private to the Actor"
        # File reads/rendering execute with the restricted token. ACLs also
        # enforce the boundary when an allowed path aliases a private file.
        # 【中文注释】即使放行，读取/渲染也以受限令牌执行；若放行路径是私有文件的别名，
        # ACL 会兜底拦下（双重防线）。
        return True, ""

    # 替换 Verifier 的 Look 路径校验函数。
    verifier_runtime.validate_verifier_look_path = validate_look
