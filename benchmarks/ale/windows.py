"""Windows realization of RSIAgent's Program/Look and rollback verifier contracts."""
# 【中文注释·模块定位】RSIAgent 的 Program/Look 与"回滚式 Verifier"契约的 Windows 实现：
# ①WindowsVM——Git Bash/原生 Python 双解释器、作业级超时、文档/图像渲染 Look；
# ②WindowsVerifierExecutor——受限令牌 + 回滚镜像执行边界：Actor 私有文件先隔离
# （ACL 保护）再检查，检查后强制回滚候选（close() 回滚失败即阻塞评分，sealed evaluation）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# base64：Look 图像与文件内容的编码。
import base64
# json：作业 spec 与传输结果解析。
import json
# os：随机 token、路径。
import os
# re：受限读结果解析。
import re
# time：耗时统计。
import time
# Path：下发 windows_job.py 时读取源文件。
from pathlib import Path

# Verifier 基类、基础设施错误与轻量 trace 构造器。
from core.verifier_runtime import (
    AgenticVerifierExecutor,
    AgenticVerifierInfrastructureError,
    _trace,
)
# Trace 数据类与"分块输出信封"解码器（长输出回传协议）。
from env.vm import Trace, _decode_run_output_envelopes

# AleVM 基类与 CUA 传输错误。
from .transport import AleVM, CuaTransportError


class WindowsVM(AleVM):
    # 【中文注释】Windows 客户机 VM：在 CUA 传输之上补齐 Windows 特有的路径翻译、
    # Git Bash、依赖安装与作业式 Program 执行。
    def __init__(self, sandbox):
        # 基类初始化（传输、home 等通用状态）。
        super().__init__(sandbox)
        # 受信 helper 的根目录（客户机内）。
        self.helper_root = self.home + "/.ale/rsiagent-runtime"
        # Git Bash 路径（Program 的 bash 解释器）。
        self.bash = "C:/Program Files/Git/bin/bash.exe"
        # 模型可用的 Python（prepare 时探测；空串表示未探测）。
        self.model_python = ""

    # Windows 客户机准备：建 helper 目录、探测解释器、装依赖、下发脚本、建工作目录。
    def prepare(self):
        # helper 根目录的反斜杠形式（cmd 用）。
        root = self.helper_root.replace("/", "\\")
        # 不存在则创建 helper 目录。
        self.native_command(f'cmd /c if not exist "{root}" mkdir "{root}"')
        # 探测客户机内的 python 与 bash 实际路径（JSON 输出）。
        result = self.trusted_python(
            'import json,shutil,sys\nprint(json.dumps({"python":shutil.which("python"),"bash":shutil.which("bash")}))\n'
        )
        # 记录模型 Python；缺失时退回传输层自带的 python。
        self.model_python = json.loads(result.strip())["python"] or self.python
        # 断言 Git Bash 存在（Program 的 bash 依赖）。
        self.trusted_python(
            f'import pathlib\nassert pathlib.Path({self.bash!r}).is_file(), "Git Bash missing"\n'
        )
        # 安装缺失的 Windows 依赖：pywin32（作业/安全 API）与 PyMuPDF（PDF 渲染）。
        self.trusted_python(
            "import importlib.util,subprocess,sys\n"
            'deps={"win32job":"pywin32==311", "fitz":"PyMuPDF==1.26.7"}\n'
            "missing=[package for module,package in deps.items() if importlib.util.find_spec(module) is None]\n"
            'if missing: subprocess.run([sys.executable,"-m","pip","install","--disable-pip-version-check","--no-input",*missing],check=True)\n',
            timeout=300,
        )
        if self.model_python.casefold().replace(
            "\\", "/"
        ) != self.python.casefold().replace("\\", "/"):
            # 模型 Python 与传输 Python 不是同一个：给模型 Python 也装 PyMuPDF（Look 渲染用）。
            self.trusted_python(
                "import subprocess\nsubprocess.run(["
                + repr(self.model_python)
                + ',"-m","pip","install","--disable-pip-version-check","--no-input","PyMuPDF==1.26.7"],check=True)\n',
                timeout=300,
            )
        # 下发受信作业助手 windows_job.py 到 helper 目录。
        self.write_text(
            self.helper_root + "/windows_job.py",
            Path(__file__).with_name("windows_job.py").read_text(),
        )
        # 写一个 python3 包装脚本（bash 语法），把 python3 调用转给模型 Python——
        # 兼容 Linux 风格脚本里对 python3 的调用。
        self.write_text(
            self.helper_root + "/python3",
            '#!/bin/bash\nexec "' + self.model_python.replace("\\", "/") + '" "$@"\n',
        )
        # 建工作目录 home/work/.rsiagent-actions（Program 执行痕迹）。
        self.trusted_python(
            "import pathlib\npathlib.Path("
            + repr(self.home + "/work/.rsiagent-actions")
            + ").mkdir(parents=True,exist_ok=True)\n"
        )
        # 建临时目录 home/.ale/tmp（POSIX /tmp 路径的映射目标）。
        self.trusted_python(
            "import pathlib\npathlib.Path("
            + repr(self.home + "/.ale/tmp")
            + ").mkdir(parents=True,exist_ok=True)\n"
        )

    # 路径翻译：把宿主侧按 POSIX 写的路径映射为客户机 Windows 实际路径。
    def host_path(self, path):
        """Translate RSIAgent's host-authored POSIX paths for native Windows I/O."""
        # /home/user 前缀替换为实际 home。
        value = path.replace("/home/user", self.home)
        # 独立的 /tmp/ 段映射到客户机 .ale/tmp（前面不能是路径字符，避免误伤 URL 等）。
        return re.sub(r"(?<![\w/.:])/tmp/", self.home + "/.ale/tmp/", value)

    # 写文件：先做路径翻译再走基类二进制写。
    def write_text(self, path, content):
        return super().write_text(self.host_path(path), content)

    # 读文件：先做路径翻译再走基类分块读。
    def fetch_file(self, path, max_bytes=None):
        return super().fetch_file(self.host_path(path), max_bytes=max_bytes)

    # 上传文件：目标路径先翻译再走基类。
    def push_file(self, local_path, guest_path, attempts=3, timeout=3600):
        return super().push_file(
            local_path, self.host_path(guest_path), attempts=attempts, timeout=timeout
        )

    # 执行一条 Windows Program：写脚本与作业 spec → windows_job.py 运行 → 解析精确结果。
    def run_windows_program(
        self, lang, code, timeout=600, *, restricted=False, workspace=None
    ):
        # 记录开始时间。
        started = time.monotonic()
        # 随机 token。
        token = os.urandom(12).hex()
        # 本次动作的基路径：工作区（默认 Actor 动作目录）下的 action_<token>。
        base = (
            (workspace or (self.home + "/work/.rsiagent-actions")) + "/action_" + token
        )
        # 按语言选择脚本扩展名（py→.py，其余一律 .sh）。
        suffix = ".py" if str(lang).startswith("py") else ".sh"
        # 脚本路径。
        script = base + suffix
        # 作业 spec 路径。
        specpath = base + ".json"
        # 输出日志路径。
        output = base + ".log"
        try:
            # 写入脚本内容。
            self.write_text(script, code)
            if str(lang).startswith("py"):
                # Python：用模型 Python 无缓冲执行。
                argv = [self.model_python or self.python, "-u", script]
            elif lang in ("bash", "sh"):
                # Bash：Git Bash 且不加载 profile/rc（可复现、无噪音）。
                argv = [self.bash, "--noprofile", "--norc", script]
            else:
                # 不支持的语言：返回 126（命令不可执行）。
                return Trace(f"Unsupported Program language: {lang}\n[exit 126]", 126)
            # 作业环境：HOME、PATH（helper 目录 + Git 的 usr/bin + 客户机原 PATH）；
            # MSYS2_ARG_CONV_EXCL 禁止 Git Bash 自动改写参数（保持原样路径）；
            # TAR_OPTIONS 让 tar 使用本地文件语义（盘符路径不误判为远程主机）。
            env = {
                "HOME": self.home,
                "PATH": self.helper_root
                + ";C:/Program Files/Git/usr/bin;"
                + self._guest_path(),
                "MSYS2_ARG_CONV_EXCL": "*",
                "TAR_OPTIONS": "--force-local",
            }
            if workspace:
                # 指定了工作区：作为 VERIFIER_SCRATCH 暴露。
                env["VERIFIER_SCRATCH"] = workspace
            # 组装作业 spec：argv、作业超时（给宿主通道留 30 秒缓冲）、输出路径、
            # 是否受限令牌、工作目录、环境。
            spec = {
                "argv": argv,
                "timeout": max(1, timeout - 30) if timeout > 30 else max(1, timeout),
                "output": output,
                "restricted": restricted,
                "cwd": self.home,
                "env": env,
            }
            # 写入 spec 文件。
            self.write_text(specpath, json.dumps(spec))
            # 运行受信作业助手（用传输 Python；比作业超时多 15 秒通道余量）。
            result = self.native_command(
                f'"{self.python}" "{self.helper_root}/windows_job.py" "{specpath}"',
                timeout=timeout + 15,
            )
            # 取 stdout。
            out = result.get("stdout") or ""
            # 结果标记。
            marker = "RSIAGENT_WINDOWS_RESULT:"
            if int(result.get("return_code", 0)) or marker not in out:
                # 助手失败或没给结果：抛传输错误（附全部输出）。
                raise CuaTransportError(
                    out + (result.get("stderr") or "Windows runner returned no result")
                )
            # 解析结果载荷。
            payload = json.loads(out.split(marker, 1)[1])
            # 作业退出码。
            rc = int(payload["exit_code"])
            # 解码分块输出信封：base64 输出还原为带分隔的精确文本，并抽出 context_stdout。
            archived, context = _decode_run_output_envelopes(
                "RSIAGENT_RUN_OUTPUT_BASE64:"
                + payload["output_b64"]
                + "\n"
                + f"\n[exit {rc}]"
            )
            # 返回 Trace：精确输出、退出码、耗时、是否超时、非基础设施失败、上下文输出。
            return Trace(
                archived,
                rc,
                time.monotonic() - started,
                bool(payload["timed_out"]),
                False,
                context,
            )
        except Exception as exc:
            # 通道异常：转为 infra_fail 的 Trace（上层按基础设施错误处理）。
            return Trace(
                f"[channel error: {type(exc).__name__}: {exc}]",
                None,
                time.monotonic() - started,
                False,
                True,
            )

    # 读取并缓存客户机的 PATH 环境变量（作业环境继承用）。
    def _guest_path(self):
        if not hasattr(self, "_path_cache"):
            # 首次：在客户机内打印 PATH 并缓存。
            self._path_cache = self.trusted_python(
                'import os\nprint(os.environ["PATH"])\n'
            ).strip()
        # 返回缓存值。
        return self._path_cache

    # 共享运行时的 run_script 钩子：等价于 run_windows_program。
    def run_script(self, lang, code, timeout=600, cap=0, allow_staging_fallback=False):
        return self.run_windows_program(lang, code, timeout)

    # run_command：以 bash Program 执行并返回清洗后的输出。
    def run_command(self, cmd, timeout=30, cap=4000):
        # 经 bash 程序执行命令（先做路径翻译）。
        trace = self.run_windows_program("bash", self.host_path(cmd), timeout)
        # 优先使用上下文输出（精确 stdout/stderr 划分），否则退回完整输出。
        out = trace.context_stdout if trace.context_stdout is not None else trace.stdout
        # 去掉尾部追加的 [exit N] 标记行。
        out = re.sub(r"\n\[exit \d+\]$", "", out)
        return (
            out
            if not cap or len(out) <= cap
            else out[:cap] + f"\n[output truncated: {len(out)} chars]"
        )

    # Look 实现：截图/图片直读/文档渲染三类，可选经受限执行器读取。
    def fetch_look_image(self, path, executor=None):
        # 读取目标文件：直接传输或经受限执行器回传 base64。
        def read_file(target):
            if executor is None:
                # 无执行器：直接走传输层读文件。
                return self.fetch_file(target)
            # 经受限执行器跑 Python：把文件 base64 打印出来（受限令牌 + ACL 生效边界内）。
            trace = executor(
                "python",
                'import pathlib,base64\nprint("RSIAGENT_LOOK_BASE64:"+base64.b64encode(pathlib.Path('
                + repr(target)
                + ").read_bytes()).decode())\n",
                timeout=120,
            )
            if trace.infra_fail or trace.exit_code != 0:
                # 受限读取失败：返回 None 与输出。
                return None, trace.stdout
            # 从输出中提取 base64。
            match = re.search(r"RSIAGENT_LOOK_BASE64:([A-Za-z0-9+/=]*)", trace.stdout)
            if match is None:
                # 没有数据：报错。
                return None, "Restricted file read returned no data"
            # 解码返回。
            return base64.b64decode(match.group(1), validate=True), ""

        if path.lower().startswith("screen:"):
            # 截屏：只支持主桌面（空/0/:0）。
            display = path.split(":", 1)[1].strip()
            if display not in ("", "0", ":0"):
                return None, "Windows supports the primary desktop screen: or screen:0."
            try:
                # 经 CUA screenshot 命令截屏。
                data = self.rpc("screenshot", {}, timeout=30)
                if not data.get("success"):
                    # 截屏失败。
                    return None, str(data.get("error") or "Screenshot failed")
                # 取图像数据（data URL 或裸 base64）。
                value = data.get("image_data")
                if not isinstance(value, str):
                    # 类型不支持。
                    return None, "CUA returned an unsupported screenshot response"
                # 剥掉 data URL 前缀后解码。
                return base64.b64decode(value.split(",")[-1], validate=True), ""
            except Exception as exc:
                # 异常转错误信息。
                return None, str(exc)
        if path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
            # 已是图片格式：直接读取。
            return read_file(path)
        # 其他格式（PDF/Office 等）：先渲染成 PNG 再读取。
        # 随机 token 的渲染输出路径。
        token = os.urandom(12).hex()
        target = self.helper_root + "/render_" + token + ".png"
        # Rendering is generic format transport. Never inspect hidden answer data.
        # 【中文注释】渲染只是通用格式转换通道，绝不窥探隐藏答案数据。
        # 渲染脚本：PDF 用 PyMuPDF 首页 120DPI 出 PNG；其余用 LibreOffice 无头转 PNG
        #（独立用户配置目录避免并发冲突）；产出为空则报错提示改用应用 + screen:。
        code = f"""import pathlib, subprocess, shutil, tempfile
source=pathlib.Path({path!r})
target=pathlib.Path({target!r})
if source.suffix.lower()=='.pdf':
 import fitz
 with fitz.open(source) as doc: doc[0].get_pixmap(matrix=fitz.Matrix(120/72,120/72)).save(target)
else:
 directory=pathlib.Path(tempfile.mkdtemp(prefix='rsiagent_render_'))
 exe=shutil.which('soffice') or 'C:/Program Files/LibreOffice/program/soffice.exe'
 subprocess.run([exe,'--headless','-env:UserInstallation='+str((directory/'profile').as_uri()),'--convert-to','png','--outdir',str(directory),str(source)],check=True,timeout=110)
 images=list(directory.glob('*.png'))
 if not images: raise RuntimeError('Document renderer produced no PNG; use the application and screen:')
 shutil.copyfile(images[0],target)
print(target)
"""
        try:
            if executor is None:
                # 无执行器：直接以受信 Python 渲染。
                self.trusted_python(code, timeout=140)
            else:
                # 有执行器：在受限边界内渲染。
                trace = executor("python", code, timeout=140)
                if trace.infra_fail or trace.exit_code != 0:
                    # 渲染失败。
                    return None, trace.stdout
            # 读取渲染出的 PNG。
            return read_file(target)
        except Exception as exc:
            # 渲染/读取异常转错误信息。
            return None, str(exc)


class WindowsVerifierExecutor(AgenticVerifierExecutor):
    # 【中文注释】Windows 版 Verifier 执行器：强制 rollback_mirror 边界；初始化时把
    # Actor 私有文件移入 ACL 保护的隔离区（对受限令牌不可见），检查结束 close() 时
    # 回滚全部候选副作用——回滚失败必须阻塞评分。
    def __init__(self, vm, **kwargs):
        # 基类初始化（私有路径、暂存归档等）。
        super().__init__(vm, **kwargs)
        if self._execution_mode != "rollback_mirror":
            # Windows 只允许回滚镜像模式（其他模式的隔离语义无法实现）。
            raise AgenticVerifierInfrastructureError(
                "Windows requires the configured rollback_mirror boundary"
            )
        # 本执行器的独立工作区（VERIFIER_SCRATCH）。
        self._workspace = vm.home + "/.ale/rsiagent-verifier-" + os.urandom(16).hex()

    # 惰性初始化：隔离私有文件 + 建工作区 + 恢复暂存归档；失败返回 infra trace。
    def _ensure_workspace(self, timeout):
        if self._closed:
            # 已关闭：拒绝。
            return _trace("Verifier workspace is closed", 125, infra_fail=True)
        if self._initialized:
            # 已初始化：无需重复。
            return None
        if self._init_reason:
            # 之前失败过：原样返回失败原因。
            return _trace(self._init_reason, 125, infra_fail=True)
        try:
            # 开启回滚事务（快照当前状态，close 时回滚）。
            self._rollback_transaction.begin()
            # 把私有路径中的 /home/user 前缀映射为该 VM 的实际 home。
            mapped = [
                self._vm.home + p[len("/home/user") :]
                if p.startswith("/home/user/")
                else p
                for p in self._private_paths
            ]
            # 系统盘上的隔离目录（随机名）。
            quarantine = "C:/ProgramData/rsiagent-private-" + os.urandom(16).hex()
            # 隔离脚本：建隔离区并仅授予 SYSTEM/Administrators 完全访问（受限令牌被
            # 降权的 Administrators 不再有效）；把每个私有路径 move 进隔离区并逐项
            # 保护（含子树，拒绝 reparse point）；最后建 Verifier 工作区。
            code = f"""import os,pathlib,shutil,win32security,ntsecuritycon,win32api
root=pathlib.Path({quarantine!r});root.mkdir()
system=win32security.CreateWellKnownSid(win32security.WinLocalSystemSid,None)
admins=win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid,None)
token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32security.TOKEN_ADJUST_PRIVILEGES|win32security.TOKEN_QUERY)
win32security.AdjustTokenPrivileges(token,False,[(win32security.LookupPrivilegeValue(None,'SeRestorePrivilege'),win32security.SE_PRIVILEGE_ENABLED)])
def protect(p):
 acl=win32security.ACL()
 for sid in (system,admins): acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,3,ntsecuritycon.FILE_ALL_ACCESS,sid)
 win32security.SetNamedSecurityInfo(str(p),win32security.SE_FILE_OBJECT,win32security.OWNER_SECURITY_INFORMATION|win32security.DACL_SECURITY_INFORMATION|win32security.PROTECTED_DACL_SECURITY_INFORMATION, system,None,acl,None)
protect(root)
for i,p in enumerate({mapped!r}):
 p=pathlib.Path(p)
 if p.exists() or p.is_symlink():
  dst=root/str(i);shutil.move(p,dst);protect(dst)
  if dst.is_dir():
   for current,dirs,files in os.walk(dst,followlinks=False):
    for name in dirs+files:
     child=pathlib.Path(current,name)
     if child.is_symlink(): raise RuntimeError('Actor-private tree has an unsupported reparse point')
     protect(child)
pathlib.Path({self._workspace!r}).mkdir(parents=True)
"""
            # 以受信 Python 执行隔离脚本（需要 SeRestore 特权 move 文件）。
            self._vm.trusted_python(code, timeout=timeout)
            # 标记初始化成功。
            self._initialized = True
            # 记录：私有路径已隐藏。
            self._rollback_private_paths_hidden = True
            if self._scratch_archive:
                # 有跨检查的暂存归档：恢复到新工作区（Verifier scratch 持久性）。
                import tempfile

                # 写入宿主临时文件。
                with tempfile.NamedTemporaryFile() as incoming:
                    incoming.write(self._scratch_archive)
                    incoming.flush()
                    # 客户机侧归档路径。
                    archive = self._workspace + "/.restore.tar"
                    # 上传归档。
                    ok, why = self._vm.push_file(incoming.name, archive)
                    if not ok:
                        # 上传失败。
                        raise CuaTransportError(why)
                # 解包脚本：逐成员校验路径不逃逸工作区（防 tar 路径穿越），安全解包后删除归档。
                self._vm.trusted_python(
                    f"""import pathlib,tarfile
root=pathlib.Path({self._workspace!r}).resolve()
archive=pathlib.Path({archive!r})
with tarfile.open(archive) as tf:
 for m in tf.getmembers():
  dest=(root/m.name).resolve()
  if not (m.isfile() or m.isdir()) or root not in dest.parents: raise RuntimeError('Unsafe scratch archive')
 tf.extractall(root,filter='data')
archive.unlink()
""",
                    timeout=600,
                )
                # 归档已恢复：清空宿主侧缓存。
                self._scratch_archive = None
            # 成功：返回 None 表示无错误。
            return None
        except Exception as exc:
            # 失败：回退初始化状态并记录原因。
            self._initialized = False
            self._init_reason = (
                f"Windows verifier preflight failed: {type(exc).__name__}: {exc}"
            )
            # 返回基础设施失败 trace。
            return _trace(self._init_reason, 125, infra_fail=True)

    # 受限执行：以受限令牌在工作区内运行代码。
    def _run_isolated(self, interpreter, extension, code, timeout):
        return self._vm.run_windows_program(
            "python" if extension == "py" else "bash",
            code,
            timeout,
            restricted=True,
            workspace=self._workspace,
        )

    # 导出工作区为 tar 归档（跨检查持久的 Verifier scratch）。
    def export_scratch(self):
        if not self._initialized or self._closed:
            # 未初始化或已关闭：原样返回当前归档缓存。
            return self._scratch_archive
        # 客户机侧归档路径（helper 目录，随机名）。
        archive = self._vm.helper_root + "/scratch-" + os.urandom(16).hex() + ".tar"
        # 打包脚本：遍历工作区（不跟随符号链接），逐项加入 tar（不递归，自己控制顺序）。
        self._vm.trusted_python(
            f"""import pathlib,tarfile,os
root=pathlib.Path({self._workspace!r})
with tarfile.open({archive!r},'w') as tf:
 for current,dirs,files in os.walk(root,followlinks=False):
  dirs[:]=sorted(d for d in dirs if not (pathlib.Path(current)/d).is_symlink())
  for name in dirs+sorted(files):
   p=pathlib.Path(current)/name
   if not p.is_symlink() and (p.is_dir() or p.is_file()): tf.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
""",
            timeout=3600,
        )
        # 取回归档字节。
        data, why = self._vm.fetch_file(archive)
        if data is None:
            # 拉取失败：基础设施错误。
            raise AgenticVerifierInfrastructureError(
                "Could not preserve verifier scratch: " + why
            )
        # 返回归档内容。
        return data

    # 受限版 Look：确保工作区就绪后经执行器读取/渲染。
    def fetch_look_image(self, path):
        ready = self._ensure_workspace(180)
        if ready:
            # 预检失败：基础设施错误。
            raise AgenticVerifierInfrastructureError(ready.stdout)
        # 经受限执行器读取/渲染 Look。
        return self._vm.fetch_look_image(path, executor=self)

    # 关停：强制回滚候选副作用；失败必须抛错以阻塞评分。
    def close(self):
        if self._closed:
            # 幂等。
            return
        # 标记关闭。
        self._closed = True
        try:
            # 执行回滚事务（撤销本次检查期间的全部候选修改）。
            self._rollback_transaction.rollback()
        except Exception as exc:
            # 回滚失败：候选污染未消除——评分必须被阻塞（sealed evaluation 纪律）。
            raise AgenticVerifierInfrastructureError(
                "Windows candidate rollback failed; grading is blocked"
            ) from exc
