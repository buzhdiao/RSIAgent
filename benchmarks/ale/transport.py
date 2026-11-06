"""Synchronous CUA transport; no task metadata or grader enters this module."""
# 【中文注释·模块定位】ALE 客户机的同步 CUA（Computer-Use Agent）传输层：
# 封装 rpc 命令通道、原生命令执行、受信 Python、文件读写与上传、控制器就绪探测。
# 刻意保持"零任务信息"：本模块不含任务元数据或评分器——它是 Actor/Verifier 共用的
# 通用传输底座（Windows 子类见 windows.py）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# base64：文件内容与命令输出的文本化编码。
import base64
# json：RPC 结果与辅助协议的解析。
import json
# os：随机 token、进程环境。
import os
# shlex：把命令安全地引用进 shell。
import shlex
# time：单调时钟超时控制。
import time
# Path：本地文件存在性检查。
from pathlib import Path
# SimpleNamespace：构造 VM 基类所需的最小环境对象。
from types import SimpleNamespace

# docker：连接容器（继承 VM 基类需要）。
import docker
# requests：与 CUA HTTP 控制端点通信。
import requests

# VM：共享运行时的 VM 基类（Program/Look 契约）。
from env.vm import VM


class CuaTransportError(RuntimeError):
    # 【中文注释】CUA 传输层错误：命令失败、无结果事件、非对象应答等。
    pass


class AleVM(VM):
    # 【中文注释】ALE Linux 客户机的 VM 实现：以 docker 容器承载，经 CUA HTTP 端点收发命令。
    def __init__(self, sandbox: dict):
        # 保存沙箱描述副本。
        self.sandbox = dict(sandbox)
        # 操作系统类型字符串。
        self.os_type = sandbox["os"]
        # CUA HTTP 控制端点（去掉尾部斜杠）。
        self.endpoint = sandbox["endpoint"].rstrip("/")
        # 客户机内 Python 解释器路径。
        self.python = sandbox["python"]
        # 是否 Windows（本类固定 False，子类覆盖）。
        self.is_windows = self.os_type == "windows"
        # 客户机 home 目录（Linux 固定 /home/user）。
        self.home = "C:/Users/User" if self.is_windows else "/home/user"
        # A populated 32 GiB ALE guest can need more than five minutes to save
        # its RAM under concurrent disk load. This is a host-operation deadline;
        # the Actor/Verifier active-work watchdog remains unchanged.
        # 【中文注释】满载的 32GiB ALE 客户机在并发磁盘负载下保存 RAM 可能超过五分钟。
        # 这是"宿主操作"的截止时间（checkpoint 超时 1800 秒）；Actor/Verifier 的
        # 活动工作看门狗保持不变。
        self.verifier_checkpoint_timeout = 1800
        # 连接本机 docker 守护进程。
        self.client = docker.from_env()
        # 取得本沙箱对应的容器对象。
        container = self.client.containers.get(sandbox["id"])
        # 构造 VM 基类所需的最小环境：provider 指向容器、密码占位、空 controller。
        env = SimpleNamespace(
            provider_name="docker",
            provider=SimpleNamespace(container=container),
            client_password="ale-harness-nopasswd",
            controller=SimpleNamespace(),
        )
        # 调用基类初始化。
        super().__init__(env)

    # 单次 CUA RPC：POST /cmd 并从 SSE 流中解析首个 data: 结果事件。
    def rpc(self, command: str, params: dict, timeout=60):
        # Do not replay mutating commands following an ambiguous transport failure.
        # 【中文注释】传输失败存在歧义时绝不重放有副作用的命令：本方法只发一次。
        # 计算单调时钟截止时间。
        deadline = time.monotonic() + timeout
        # 流式 POST 命令与参数。
        with requests.post(
            self.endpoint + "/cmd",
            json={"command": command, "params": params},
            stream=True,
            timeout=(10, timeout),
        ) as response:
            # 非 2xx 直接抛错。
            response.raise_for_status()
            # 未解析的缓冲区。
            pending = bytearray()
            # 当前行起点。
            start = 0
            # 扫描位置。
            scan = 0
            # 逐块读取响应流。
            for chunk in response.iter_content(chunk_size=None):
                # 整体超时检查。
                if time.monotonic() > deadline:
                    raise TimeoutError("CUA result deadline exceeded")
                # 追加到缓冲。
                pending.extend(chunk)
                # 扫描缓冲内所有完整行。
                while (newline := pending.find(b"\n", scan)) >= 0:
                    # 取出一行（去掉行尾 \r）。
                    line = bytes(pending[start:newline]).rstrip(b"\r")
                    # 推进行起点与扫描位置。
                    start = scan = newline + 1
                    if line.startswith(b"data:"):
                        # SSE data 事件：解析并返回（首 个结果即返回）。
                        return self._parse_rpc(line[5:])
                # 丢弃已扫描部分之前的垃圾（保留未完行）。
                scan = len(pending)
            # 流结束：检查残余的最后一段。
            tail = bytes(pending[start:]).strip()
            if tail.startswith(b"data:"):
                # 尾部也是结果事件：解析返回。
                return self._parse_rpc(tail[5:])
        # 整个流里没有完整结果事件：传输层失败。
        raise CuaTransportError("CUA returned no complete result event")

    @staticmethod
    # 解析 RPC data 载荷：容忍 UTF-8 BOM，必须为 JSON 对象。
    def _parse_rpc(raw):
        # 去掉可能的 BOM 后解析 JSON。
        data = json.loads(raw.strip().removeprefix(b"\xef\xbb\xbf"))
        # 必须是对象（字典），否则传输层失败。
        if not isinstance(data, dict):
            raise CuaTransportError("CUA result is not an object")
        # 返回解析结果。
        return data

    # 原生命令执行：Windows 直发；Linux 用辅助 Python 进程精确收集 stdout/退出码。
    def native_command(self, command, timeout=60):
        if self.is_windows:
            # Windows：直接经 CUA run_command 执行。
            data = self.rpc("run_command", {"command": command}, timeout=timeout)
        else:
            # CUA captures pipe EOF, which a persistent namespace keeper can
            # inherit. Capture into ordinary files and wait for the direct shell
            # process instead. Child daemons cannot keep the RPC response open.
            # 【中文注释】CUA 会捕获管道 EOF，而常驻的命名空间保持进程可能继承这些管道，
            # 导致响应悬挂。改为把输出捕获进普通文件并等待直接 shell 进程结束；
            # 子守护进程无法再拖住 RPC 响应。
            # 生成的辅助脚本：以独立会话启动 /bin/bash -lc 命令，stdout/stderr 重定向进
            # 临时文件；超时则杀进程组返回 124；最后打印 RSIAGENT_CUA_COMMAND 标记行
            # （含 base64 编码的 stdout/stderr 与退出码）。
            helper = (
                f"command={command!r}\ntimeout_value={float(timeout)!r}\n"
                + """import base64,json,os,signal,subprocess,tempfile
with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
 p=subprocess.Popen(['/bin/bash','-lc',command],stdout=out,stderr=err,stdin=subprocess.DEVNULL,start_new_session=True,close_fds=True)
 try: rc=p.wait(timeout=timeout_value)
 except subprocess.TimeoutExpired:
  os.killpg(p.pid,signal.SIGKILL);p.wait();rc=124
 out.seek(0);err.seek(0)
 print('RSIAGENT_CUA_COMMAND:'+json.dumps({'return_code':rc,'stdout_b64':base64.b64encode(out.read()).decode(),'stderr_b64':base64.b64encode(err.read()).decode()}))
"""
            )
            # 用 python3 -c 执行（shlex.quote 防注入）。
            wrapped = "python3 -c " + shlex.quote(helper)
            # 经 CUA 执行辅助脚本（额外 20 秒缓冲）。
            data = self.rpc("run_command", {"command": wrapped}, timeout=timeout + 20)
            if data.get("success") and "RSIAGENT_CUA_COMMAND:" in (
                data.get("stdout") or ""
            ):
                # 从 stdout 截取标记之后的 JSON 载荷。
                payload = json.loads(
                    data["stdout"].split("RSIAGENT_CUA_COMMAND:", 1)[1]
                )
                # 还原为标准命令结果结构（base64 解码，容错替换）。
                data = {
                    "success": True,
                    "return_code": int(payload["return_code"]),
                    "stdout": base64.b64decode(
                        payload["stdout_b64"], validate=True
                    ).decode("utf-8", errors="backslashreplace"),
                    "stderr": base64.b64decode(
                        payload["stderr_b64"], validate=True
                    ).decode("utf-8", errors="backslashreplace"),
                }
            else:
                # 辅助脚本没能给出结果：把 stderr/error 抛为传输错误。
                raise CuaTransportError(
                    str(
                        data.get("stderr")
                        or data.get("error")
                        or "Linux command helper returned no result"
                    )
                )
        # CUA 报失败：统一抛错。
        if not data.get("success"):
            raise CuaTransportError(str(data.get("error") or "CUA command failed"))
        # 返回标准结果字典。
        return data

    # 面向模型的命令执行：合并 stdout/stderr、截断到 cap、异常转错误文本。
    def run_command(self, cmd: str, timeout=30, cap=4000):
        try:
            # 执行原生命令。
            data = self.native_command(cmd, timeout=timeout)
            # 合并标准输出与标准错误。
            out = (data.get("stdout") or "") + (data.get("stderr") or "")
            # 读取退出码（默认 0）。
            code = int(data.get("return_code", 0))
            if code and not out.strip():
                # 非零退出但无输出：生成提示行，避免模型困惑。
                out = f"[command exited {code} without output]"
            if cap and len(out) > cap:
                # 超长输出截断并注明总长度。
                out = out[:cap] + f"\n[output truncated: {len(out)} chars]"
            # 返回文本。
            return out
        except Exception as exc:
            # 任何异常都转成错误文本返回（模型可见，不中断会话）。
            return f"[channel error: {type(exc).__name__}: {exc}]"

    # 写文本文件：UTF-8 编码按 1MiB 分块经 write_bytes 追加。
    def write_text(self, path, content):
        # CUA's legacy write_text command uses the Windows locale encoding.
        # Its current SDK uses write_bytes for UTF-8; follow that binary contract.
        # 【中文注释】旧版 CUA write_text 用 Windows 本地编码会弄乱内容；现行 SDK 用
        # write_bytes 传 UTF-8——遵循该二进制契约。
        # 全文 UTF-8 编码。
        encoded = content.encode("utf-8")
        # 按 1MiB 步长分块（至少一轮，兼容空内容）。
        for offset in range(0, max(1, len(encoded)), 1024 * 1024):
            # 当前块。
            chunk = encoded[offset : offset + 1024 * 1024]
            # 经 RPC 写入：首块覆盖，后续块追加。
            result = self.rpc(
                "write_bytes",
                {
                    "path": path,
                    "content_b64": base64.b64encode(chunk).decode("ascii"),
                    "append": offset > 0,
                },
                timeout=120,
            )
            if not result.get("success"):
                # 写失败即抛错。
                raise CuaTransportError(str(result.get("error") or "CUA write failed"))

    # 读取客户机文件：8MiB 分块循环读取；返回 (bytes|None, 错误信息)。
    def fetch_file(self, path, max_bytes=None):
        try:
            # 已读取分块的累计。
            parts = []
            # 当前读取偏移。
            offset = 0
            # 每块 8MiB。
            chunk_size = 8 * 1024 * 1024
            # 循环读取直到 EOF 或超限。
            while True:
                # 经 RPC 读一块。
                result = self.rpc(
                    "read_bytes",
                    {"path": path, "offset": offset, "length": chunk_size},
                    timeout=120,
                )
                if not result.get("success"):
                    # 读失败：返回 None 与错误信息。
                    return None, str(result.get("error") or "CUA read failed")
                # 解码本块字节。
                raw = base64.b64decode(result.get("content_b64") or "", validate=True)
                # 累计。
                parts.append(raw)
                # 推进偏移。
                offset += len(raw)
                if max_bytes is not None and offset > max_bytes:
                    # 超过显式上限：拒绝返回内容。
                    return None, f"file exceeds the explicit {max_bytes}-byte limit"
                if len(raw) < chunk_size:
                    # 不满一块：已到文件尾，拼接返回。
                    return b"".join(parts), ""
        except Exception as exc:
            # 异常转为 (None, 错误信息)。
            return None, f"{type(exc).__name__}: {exc}"

    # 上传宿主文件到客户机：base64 分块写入 + 客户机内解码拼接 + 哈希校验 + 原子改名。
    def push_file(self, local_path, guest_path, attempts=3, timeout=3600):
        # Fixed-size base64 chunks keep command lines small without a file-size cap.
        # 【中文注释】固定尺寸 base64 分块让命令行保持小巧，且不设文件大小上限。
        # 源必须是普通文件（非目录/符号链接）。
        if not Path(local_path).is_file() or Path(local_path).is_symlink():
            return False, "source is not an ordinary file"
        # 随机 token 防碰撞。
        token = os.urandom(12).hex()
        # 客户机上的暂存路径（.rsiagent-stage-<token> 后缀）。
        staged = guest_path + ".rsiagent-stage-" + token
        # 上传截止时间。
        deadline = time.monotonic() + timeout
        try:
            # 延迟导入哈希库。
            import hashlib

            # 本地内容摘要器。
            digest = hashlib.sha256()
            # 打开本地文件。
            with open(local_path, "rb") as incoming:
                # 写入偏移。
                offset = 0
                while True:
                    # 读 512KiB。
                    data = incoming.read(512 * 1024)
                    # 读到 EOF 且已写过数据：结束。
                    if not data and offset:
                        break
                    if time.monotonic() >= deadline:
                        # 超时。
                        raise TimeoutError("upload deadline exceeded")
                    # 累积摘要。
                    digest.update(data)
                    # base64 暂存文件路径。
                    encoded_path = staged + ".b64"
                    # 写入本块 base64 文本。
                    self.write_text(encoded_path, base64.b64encode(data).decode())
                    # 客户机内脚本：解码 b64 文件并按偏移写进暂存文件（首写 wb 后续 r+b+truncate），随后删 b64。
                    code = (
                        "import base64,pathlib\n"
                        f"p=pathlib.Path({staged!r});p.parent.mkdir(parents=True,exist_ok=True)\n"
                        f"b=base64.b64decode(pathlib.Path({encoded_path!r}).read_text(),validate=True)\n"
                        f"with p.open({'wb' if offset == 0 else 'r+b'!r}) as f: f.seek({offset});f.write(b);f.truncate()\n"
                        f"pathlib.Path({encoded_path!r}).unlink()\n"
                    )
                    # 以受信 Python 执行拼接。
                    self.trusted_python(code, timeout=120)
                    # 推进偏移。
                    offset += len(data)
                    if not data:
                        # 空块（空文件场景）：结束。
                        break
            # 校验脚本：客户机内重新计算整文件 SHA-256 与大小，与宿主一致才原子改名到位。
            code = (
                "import pathlib,hashlib,os\n"
                f"p=pathlib.Path({staged!r})\n"
                "h=hashlib.sha256()\n"
                'with p.open("rb") as f:\n'
                " while b:=f.read(8388608): h.update(b)\n"
                f"assert p.stat().st_size=={offset} and h.hexdigest()=={digest.hexdigest()!r}\n"
                f"os.replace(p,{guest_path!r})\n"
            )
            # 以剩余时间预算执行校验与改名。
            self.trusted_python(
                code, timeout=max(120, int(deadline - time.monotonic()))
            )
            # 成功。
            return True, ""
        except Exception as exc:
            # 失败：返回错误信息（调用方决定重试/中止）。
            return False, f"{type(exc).__name__}: {exc}"

    # 受信 Python：脚本落盘到 .ale 下再执行，结束后清理；返回 stdout。
    def trusted_python(self, code, timeout=120):
        # File staging also works on Windows without cmd.exe's 8 KiB argument limit.
        # 【中文注释】用文件落盘而非命令行参数传代码：绕开 cmd.exe 8KiB 参数上限，Windows 也适用。
        # 随机 token 防碰撞。
        token = os.urandom(12).hex()
        # 脚本在客户机内的落盘路径。
        path = f"{self.home}/.ale/rsiagent-host-{token}.py"
        # 写入脚本内容。
        self.write_text(path, code)
        # 组装执行命令（Windows 双引号风格，POSIX shlex 引用）。
        command = (
            f'"{self.python}" "{path}"'
            if self.is_windows
            else f"{shlex.quote(self.python)} {shlex.quote(path)}"
        )
        try:
            # 执行脚本。
            result = self.native_command(command, timeout=timeout)
            if int(result.get("return_code", 0)):
                # 非零退出：把输出抛为传输错误。
                raise CuaTransportError(
                    (result.get("stdout") or "")
                    + (result.get("stderr") or "trusted Python failed")
                )
            # 返回标准输出。
            return result.get("stdout") or ""
        finally:
            # 无论成败都删除客户机上的临时脚本（Windows cmd del / POSIX rm）。
            cleanup = (
                f'cmd /c del /q "{path.replace(chr(47), chr(92))}"'
                if self.is_windows
                else "rm -f -- " + shlex.quote(path)
            )
            # 执行清理（30 秒超时）。
            self.native_command(cleanup, timeout=30)

    # 探测 CUA 控制器就绪：连续 N 次探活成功才算稳定。
    def wait_for_controller(self, timeout=180, probe_interval=5.0, stable_probes=2):
        # 截止时间。
        deadline = time.monotonic() + timeout
        # 连续成功计数。
        stable = 0
        # 最近一次探测结果。
        last = ""
        # 在截止时间前循环探测。
        while time.monotonic() < deadline:
            try:
                # 发送探活 echo。
                data = self.native_command("echo RSIAGENT_CONTROLLER_READY", timeout=15)
                # 记录输出。
                last = data.get("stdout") or ""
                # 含回显则计数 +1，否则清零。
                stable = stable + 1 if "RSIAGENT_CONTROLLER_READY" in last else 0
                if stable >= max(1, stable_probes):
                    # 连续达标：就绪。
                    return True, last
            except Exception as exc:
                # 探测失败：清零并记录原因。
                stable, last = 0, str(exc)
            # 间隔睡眠（不超过剩余预算）。
            time.sleep(min(probe_interval, max(0, deadline - time.monotonic())))
        # 超时未就绪。
        return False, last
