"""VM — the machine rsiagent's agent acts on. Duck-typed over a v2 ``DesktopEnv`` (never
imports it): only ``env.controller.http_server`` is used. Two verbs, both code:

- ``run_command``: one shell command (probes, checks).
- ``run_script``: a WHOLE program (python3/bash), base64-piped in — the code-as-policy
  primitive. The program's combined output is written to an in-VM log first and then
  cat'd back, so a timeout still leaves a recoverable partial trace (anchor's adapter
  lost the entire trace on timeout — the trace is our core signal, so that's fixed here).

No GUI, no grounder, no screenshots — rsiagent is a pure text+code agent.
"""
import base64
import hashlib
import ipaddress
import logging
import os
import posixpath
import re
import secrets
import shlex
import socket
import tempfile
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("rsiagent.vm")
# 模块级日志器，记录命令/脚本执行与文件传输的异常。

_RUN_LOG_PREFIX = "/tmp/rsiagent_run_"      # primary program trace location
# （既有英文注释）程序运行日志前缀：客户机 /tmp 下的主 trace 位置。
_SHM_RUN_LOG_PREFIX = "/dev/shm/rsiagent_run_"  # survives a read-only guest root
# （既有英文注释）备用日志前缀：/dev/shm 在客户机根文件系统只读时仍可写。
_RUN_OUTPUT_B64_PREFIX = "RSIAGENT_RUN_OUTPUT_BASE64:"
# 输出信封前缀：程序输出以 ASCII base64 穿越 OSWorld 的严格 UTF-8 JSON 通道。
_STAGING_FALLBACK = "[RSIAGENT STAGING FALLBACK: /tmp unavailable; using /dev/shm]"
# /tmp 落盘失败、回退 /dev/shm 时的哨兵标记。
_STAGING_UNAVAILABLE = "[RSIAGENT STAGING UNAVAILABLE: /tmp and /dev/shm both failed]"
# /tmp 与 /dev/shm 双双失败时的哨兵标记（判定为基础设施故障）。
# Linux counts the shell command, environment, and argv against one execve limit.
# Keep ordinary short actions on the low-latency inline path, but stream larger
# programs losslessly rather than imposing a content ceiling or relying on the
# guest's environment-dependent ARG_MAX headroom.
# （既有英文注释：Linux 把 shell 命令、环境与 argv 计入同一个 execve 上限；短动作
# 走低延迟内联路径，更大的程序改为无损流式传输，而不是设置内容上限或依赖
# 客户机环境相关的 ARG_MAX 余量。）
_INLINE_PROGRAM_B64_LIMIT = 32 * 1024
# 内联 base64 传输的阈值：超过则切换为流式推文件（传输选择，并非大小上限）。


def _atomic_guest_part_path(destination: str, token: str) -> str:
    """Return a hidden staging path on the destination's own filesystem."""
    # 返回与目标同文件系统上的隐藏暂存路径（保证 os.replace 原子改名可行）。
    normalized = str(destination or "").rstrip("/")
    # 规整目标路径：去掉末尾斜杠。
    basename = posixpath.basename(normalized)
    # 取目标文件名。
    if not basename:
        return ""
        # 目标不指向具体文件（如根目录）则无法暂存，返回空串。
    directory = posixpath.dirname(normalized) or "."
    # 取目标所在目录（空则用当前目录）。
    return posixpath.join(directory, f".{basename}.rsiagent-part-{token}")
    # 生成形如 .<文件名>.rsiagent-part-<token> 的隐藏 part 文件路径。


def _decode_run_output_envelopes(output: str) -> tuple[str, str | None]:
    """Decode raw Program bytes carried safely through OSWorld's JSON endpoint.

    The guest execute server decodes subprocess output as strict UTF-8 before it
    constructs its response. A perfectly valid read-only probe such as ``cat`` on
    an executable can therefore become HTTP 500 and masquerade as a dead command
    channel. Program logs cross that boundary as ASCII base64. Valid UTF-8 remains
    byte-for-byte text and fully enters Agent context. Non-text bytes remain exactly
    reproducible as base64 in the archived trace, while Agent context receives a
    content-addressed notice instead of an arbitrarily large binary payload.

    Returns ``(archived_output, context_output)``. ``context_output`` is ``None``
    when the archived bytes are ordinary UTF-8 and need no externalization.
    """
    if _RUN_OUTPUT_B64_PREFIX not in output:
        # Compatibility with already-running workers and test doubles that return
        # the historical plain-text envelope.
        # （既有英文注释：兼容仍在运行的 worker 与返回纯文本信封的测试替身。）
        return output, None
        # 无信封前缀：原样返回，归档输出=输入，上下文输出=None。
    pattern = re.compile(
        re.escape(_RUN_OUTPUT_B64_PREFIX) + r"([A-Za-z0-9+/=]*)\n")
    # 匹配“前缀 + base64 载荷 + 换行”的信封模式。
    archived_parts = []
    context_parts = []
    # 归档片段与进入 Agent 上下文的片段分别累积。
    cursor = 0
    externalized = False
    # 扫描游标与“是否发生了二进制外置化”标记。
    for match in pattern.finditer(output):
        prefix = output[cursor:match.start()]
        archived_parts.append(prefix)
        context_parts.append(prefix)
        # 信封之间的普通文本两个通道都保留。
        try:
            raw = base64.b64decode(match.group(1), validate=True)
        except Exception as exc:  # noqa: BLE001 - malformed trusted envelope
            # base64 非法：信封本身损坏，两个通道都记为通道错误。
            error = ("[channel error: malformed Program output envelope — "
                     f"{type(exc).__name__}]")
            archived_parts.append(error)
            context_parts.append(error)
            cursor = match.end()
            continue
        try:
            decoded = raw.decode("utf-8")
            # 尝试把载荷按 UTF-8 解码。
        except UnicodeDecodeError:
            externalized = True
            encoded = base64.b64encode(raw).decode("ascii")
            digest = hashlib.sha256(raw).hexdigest()
            # 二进制数据：重新编码 base64 并计算内容摘要（sha256，内容寻址）。
            archived_parts.append(
                "[RSIAGENT NON-UTF8 PROGRAM OUTPUT — EXACT BASE64; "
                f"bytes={len(raw)} sha256={digest}]\n{encoded}\n"
                "[END RSIAGENT NON-UTF8 PROGRAM OUTPUT]")
            # 归档通道：无损保留精确字节（base64 + 大小 + 摘要）。
            context_notice = (
                "[PROGRAM OUTPUT EXTERNALIZED: non-UTF8/binary data; "
                f"bytes={len(raw)} sha256={digest}. The exact bytes are preserved "
                "as base64 in this turn's archived trace.txt. Do not infer binary "
                "contents from this notice; re-inspect deliberately with file, "
                "readelf, strings, or xxd as appropriate.]" )
            # 上下文通道：只给一条外置化通知，防止大块二进制灌入模型上下文。
            # Effect-isolated execution emits an unpredictable host-authored token
            # before model code begins. It is control-plane evidence, not binary
            # payload, and must remain visible to the trusted sandbox validator.
            # （既有英文注释：效果隔离执行在模型代码开始前会输出宿主随机令牌；
            # 它是控制面证据而非二进制负载，必须对受信沙箱校验器可见。）
            control_tokens = re.findall(
                rb"__RSIAGENT_[A-Za-z0-9_]+__", raw)
            # 从原始字节中提取 __RSIAGENT_xxx__ 形式的控制令牌。
            if control_tokens:
                context_notice += "\n" + "\n".join(
                    token.decode("ascii") for token in control_tokens)
                # 令牌追加到上下文通知之后，保证校验器能看到。
            context_parts.append(context_notice)
        else:
            archived_parts.append(decoded)
            context_parts.append(decoded)
            # 合法 UTF-8：两个通道都保留解码后的原文。
        cursor = match.end()
        # 游标推进到本信封末尾。
    if not archived_parts:
        error = ("[channel error: incomplete Program output envelope — the "
                 "machine response cannot be trusted]")
        return error, error
        # 一个片段都没有（例如整个输出就是残缺信封）：按不可信的通道错误处理。
    suffix = output[cursor:]
    archived_parts.append(suffix)
    context_parts.append(suffix)
    # 收尾：追加最后一个信封之后的剩余文本。
    archived = "".join(archived_parts)
    context = "".join(context_parts) if externalized else None
    # 拼接归档输出；只有发生外置化时才返回独立的上下文输出。
    return archived, context


@dataclass
class Trace:
    """What one program execution produced."""
    # 一次程序执行产出的完整记录（trace 是本系统的核心信号）。
    stdout: str                          # complete combined stdout+stderr, incl. "[exit N]" line
    # （既有英文注释）合并后的 stdout+stderr 全文，含结尾 "[exit N]" 行。
    exit_code: int = None                # parsed from the trailing [exit N]; None if unknown
    # （既有英文注释）从结尾 "[exit N]" 解析出的退出码；未知则为 None。
    secs: float = 0.0
    # 执行耗时（秒）。
    timed_out: bool = False
    # 是否在客户机内超时被杀（exit 124）。
    infra_fail: bool = False             # v17: the RUN WRAPPER itself failed (e.g. guest
    #                                      /tmp went read-only -> mktemp can't stage the
    #                                      program). No program actually ran; a streak of
    #                                      these = a dead VM, not a task failure.
    # （既有英文注释）v17：运行包装器本身失败（如客户机 /tmp 只读导致 mktemp 无法
    # 落盘程序），程序根本没跑；连续出现即代表 VM 已死，而非任务失败。
    context_stdout: str | None = None    # only set when non-text bytes are kept in the
    #                                      archive but intentionally externalized from
    #                                      the next model request.
    # （既有英文注释）仅当非文本字节保留在归档但被有意从下一次模型请求中外置时设置。


class VM:
    # Agent 作用的那台机器。对 v2 DesktopEnv 鸭子类型（绝不 import 它），
    # 只使用 env.controller.http_server；两个动词：run_command（单条命令）与
    # run_script（整段程序，代码即策略 code-as-policy 原语）。无 GUI、无截图——
    # 纯文本+代码 Agent。
    def __init__(self, de):
        self.env = de         # duck-typed handle; only .controller.http_server is used
        # （既有英文注释）鸭子类型句柄；仅使用 .controller.http_server。
        self._conda = ""      # persistent shell state: the controller runs each command in a
        #                       FRESH shell, so a `conda activate` would not survive to the next
        #                       command. We re-apply the last one (proven necessary in anchor).
        # （既有英文注释）持久 shell 状态：控制器每条命令都在全新 shell 中运行，
        # conda activate 不会留存到下一条命令，因此这里重放最近一次激活。
        self._staging_fallback_reported = False
        # 是否已报告过暂存回退故障（每 VM 只采集一次诊断）。

    # ------------------------------------------------------------------ commands
    def run_command(self, command: str, timeout: int = 30, cap: int = 4000) -> str:
        """One shell command in the VM; returns combined output (bounded to ``cap``)."""
        # 在 VM 内执行一条 shell 命令（探针/检查用），返回合并输出（按 cap 截断）。
        cmd = command
        if self._conda and "conda activate" not in command:
            cmd = (f"source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null; "
                   f"conda activate {self._conda} 2>/dev/null; {command}")
            # 重放最近一次 conda 激活（命令本身带 activate 时不重复）。
        try:
            # The OSWorld guest endpoint has its own 120-second default.  Merely
            # setting requests' timeout does not change that server-side limit:
            # the endpoint kills only its immediate ``shell=True`` wrapper when
            # the default expires, allowing grandchildren such as ffmpeg to keep
            # mutating the VM after RSIAgent has advanced to the next turn.  Forward
            # the action timeout explicitly.  The run_script wrapper's GNU
            # ``timeout`` still fires 30 seconds earlier, kills its process group,
            # and returns the partial trace before this transport ceiling.
            # （既有英文注释：客户机端点有 120 秒默认上限，仅设 requests 超时改不了
            # 服务端限制；必须显式转发动作超时，run_script 的 GNU timeout 仍会早
            # 30 秒触发并杀进程组。）
            r = requests.post(f"{self.env.controller.http_server}/execute",
                              json={"command": cmd, "shell": True,
                                    "timeout": max(1, int(timeout))},
                              timeout=timeout)
            # POST /execute：shell=True 执行命令，并把动作超时传给服务端。
            d = r.json()
            if r.status_code >= 400 or d.get("status") == "error":
                detail = str(d.get("message") or d.get("error")
                             or f"HTTP {r.status_code}")
                log.warning("guest execute failed: %s", detail)
                return (f"[channel error: guest execute HTTP {r.status_code} — "
                        "the machine did not complete the command; "
                        f"detail: {detail[:240]}]")
                # HTTP/状态错误：返回显式通道错误标记（绝不返回空串）。
            out = (d.get("output") or "").rstrip()
            # 取合并输出并去掉尾部空白。
            if d.get("error"):
                out = (out + "\n[stderr] " + d["error"]).strip()
                # 附加 stderr 字段（如有）。
        except requests.exceptions.Timeout:
            return f"[command timed out after {timeout}s]"
            # 传输层超时：返回超时标记（上层据此抢救部分输出）。
        except Exception as e:  # noqa: BLE001
            # v32.1: NEVER return "" on a transport failure — silent empties made a
            # whole inspection go blind (t063 iter_66: every probe incl. `echo`
            # "returned nothing" during a transient guest-server outage and the
            # inspector concluded from the absence). An explicit marker converts
            # silent blindness into a diagnosed outage the caller can reason about.
            # （既有英文注释：传输失败绝不能返回空串——静默的空让整场检查失明；
            # 显式标记把“静默失明”变成可推理的已诊断故障。）
            log.warning("run_command failed: %s", e)
            return (f"[channel error: {type(e).__name__} — the machine did not "
                    "answer; this is NOT command output and proves nothing]")
            # 返回带异常类型的通道错误标记。
        # The guest API reports shell signal termination as HTTP 200/success.
        # Its negative returncode is transport evidence, not model stdout. A
        # killed outer shell can lose the output envelope after code has run.
        # （既有英文注释：客户机 API 把信号终止报成 HTTP 200；负 returncode 是
        # 传输证据而非模型输出，外层 shell 被杀可能丢输出信封。）
        returncode = d.get("returncode")
        if isinstance(returncode, int) and returncode < 0:
            marker = (f"[channel error: guest execute terminated by signal {-returncode}; "
                      "output may be incomplete; program execution may have occurred]")
            log.warning("guest execute terminated by signal %s", -returncode)
            out = marker + ("\n" + out if out else "")
            # 被信号杀死：在输出前加通道错误标记并提示输出可能不完整。
        m = re.search(r"conda activate (\S+)", command)   # remember for subsequent commands
        # （既有英文注释）记住命令里的 conda activate 环境，供后续命令重放。
        if m:
            self._conda = m.group(1)
        return _bound(out, cap)
        # 按 cap 截断后返回（cap<=0 表示无损）。

    def wait_for_controller(self, timeout: float = 180,
                            probe_interval: float = 5.0,
                            stable_probes: int = 2):
        """Wait for the guest command channel to become stably responsive.

        This is recovery after a failed Actor action, not an action retry: only a
        non-mutating ``printf`` readiness probe is sent. Requiring consecutive
        healthy replies avoids returning control during a restart flap. Returns
        ``(recovered, report)`` for archival and for the Actor's next observation.
        """
        # （docstring 大意：这是 Actor 动作失败后的恢复，而非动作重试；只发送非变更的
        # printf 就绪探针；要求连续健康回复以避开重启抖动窗口；返回 (是否恢复, 报告)。）
        timeout = max(0.0, float(timeout))
        # 归一化总时限（非负）。
        probe_interval = max(0.0, float(probe_interval))
        # 归一化探测间隔。
        stable_needed = max(1, int(stable_probes))
        # 至少要求一次连续健康探针。
        started = time.monotonic()
        deadline = started + timeout
        # 记录开始时刻并计算截止时刻。
        attempts = 0
        stable = 0
        last = "no readiness probe was sent"
        # 尝试次数、连续健康计数、最近一次探针结果。

        while timeout > 0 and time.monotonic() < deadline:
            attempts += 1
            remaining = max(1.0, deadline - time.monotonic())
            probe_timeout = max(1, min(10, int(remaining)))
            # 单次探针超时：不超过 10 秒，也不超过剩余时限。
            last = self.run_command(
                "printf RSIAGENT_CONTROLLER_READY", timeout=probe_timeout, cap=1000)
            # 发送非变更 printf 探针，检查命令通道是否应答。
            if (last or "").strip() == "RSIAGENT_CONTROLLER_READY":
                stable += 1
                # 应答精确匹配：连续健康计数加一。
                if stable >= stable_needed:
                    elapsed = time.monotonic() - started
                    return True, (
                        f"controller recovered after {elapsed:.1f}s and {attempts} "
                        f"readiness probe(s); {stable_needed} consecutive probes passed")
                    # 连续达标：判定恢复，返回耗时与统计。
            else:
                stable = 0
                # 任何失败/噪声应答都把连续计数清零。
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(probe_interval, remaining))
                # 按间隔休眠，不超过剩余时限。

        elapsed = time.monotonic() - started
        compact = " ".join(str(last).split())[-240:]
        # 压缩最近一次探针输出，供诊断报告引用。
        return False, (
            f"controller did not recover within {elapsed:.1f}s after {attempts} "
            f"readiness probe(s); last probe: {compact}")
        # 超时未恢复：返回失败与最后一次探针的摘要。

    # ------------------------------------------------------------------ programs
    def run_script(self, lang: str, code: str, timeout: int = 600, cap: int = 0,
                   allow_staging_fallback: bool = False) -> Trace:
        """Run a whole program in the VM.

        Short programs use the historical base64-in-command transport. Large ones
        are streamed with exact size/SHA-256 verification to avoid ``execve``'s
        combined argv/environment limit; this is a transport choice, never a size
        cap. The program is killed in-VM 30s before the HTTP timeout (partial output
        survives in the log); output comes back with an ``[exit N]`` line (124 =
        in-VM timeout). ``cap=0`` is deliberately lossless: an Agent's observations
        must not be silently shortened before they enter its context. Each action
        owns a unique log, and ``timeout`` escalates to SIGKILL so an expired action
        cannot keep writing into later actions.
        """
        # （docstring 大意：短程序走历史 base64 内联传输；大程序经精确大小/SHA-256 校验
        # 流式上传，规避 execve 的 argv+环境总量上限——是传输选择而非大小上限；程序在
        # HTTP 超时前 30 秒于客户机内被杀，部分输出留在日志；输出带回 [exit N] 行
        # （124=客户机内超时）；cap=0 刻意无损——观察在进入上下文前不得被静默截短；
        # 每个动作独占唯一日志；超时升级 SIGKILL，过期动作不能污染后续动作。）
        if not code:
            return Trace(stdout="[empty program]", exit_code=None)
            # 空程序：直接返回占位 Trace，不发起任何传输。
        b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        # 程序源码 UTF-8 编码后转 base64，以便安全嵌入 shell 命令行。
        run_token = secrets.token_hex(12)
        run_log = f"{_RUN_LOG_PREFIX}{run_token}.log"
        shm_run_log = f"{_SHM_RUN_LOG_PREFIX}{run_token}.log"
        # 随机令牌保证本动作的日志路径全局唯一（/tmp 主路径 + /dev/shm 备用路径）。
        kill = max(30, timeout - 30)               # in-VM timeout fires first -> log survives
        # （既有英文注释）客户机内超时比 HTTP 超时早 30 秒触发，日志得以幸存。
        timeout_cmd = f"timeout --signal=TERM --kill-after=5s {kill}s"
        # GNU timeout 包装：先 TERM，5 秒宽限后 KILL，杜绝过期动作继续写入。
        python = (lang or "").startswith("py")
        extension = "py" if python else "sh"
        interpreter = "python3 -u" if python else "bash"
        # 按语言选择扩展名与解释器（python3 -u 关闭输出缓冲，trace 更完整）。
        streamed_script = ""
        if len(b64) > _INLINE_PROGRAM_B64_LIMIT:
            # base64 长度超过内联阈值：改走流式传输路径。
            streamed_script = (
                f"/dev/shm/rsiagent_program_{run_token}.{extension}")
            # 客户机侧程序落盘路径（/dev/shm 独立 tmpfs，根只读时仍可写）。
            local = tempfile.NamedTemporaryFile(
                prefix="rsiagent-program-", suffix=f".{extension}", delete=False)
            # 宿主侧临时文件承载程序源码。
            try:
                local.write(code.encode("utf-8"))
                local.flush()
                os.fsync(local.fileno())
                local.close()
                # 写入、flush 并 fsync，确保字节完整落盘后再推送。
                ok, reason = self.push_file(
                    local.name, streamed_script,
                    timeout=max(30, int(timeout)))
                # 无损推送（带精确大小与 SHA-256 校验）。
            finally:
                try:
                    local.close()
                except Exception:  # noqa: BLE001 - best-effort host cleanup
                    pass
                try:
                    os.unlink(local.name)
                except FileNotFoundError:
                    pass
                # 宿主清理尽力而为：关句柄、删临时文件（不存在则忽略）。
            if not ok:
                return Trace(
                    stdout=("[channel error: lossless Program staging failed; "
                            "the model-authored program was not executed; "
                            f"detail: {str(reason)[-240:]}]"),
                    exit_code=None, infra_fail=True)
                # 暂存失败：模型程序未执行，按基础设施故障 fail-closed 返回。
        # Never stream arbitrary Program bytes directly through OSWorld's strict
        # UTF-8 /execute response. The host decodes this uncapped ASCII envelope
        # immediately below, before parsing the transport trailer.
        # （既有英文注释：绝不把任意程序字节直接流过 OSWorld 严格 UTF-8 的 /execute
        # 响应；宿主在解析传输尾行之前就先解码这个不设上限的 ASCII 信封。）
        emit_log = (
            f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
            'base64 -w0 "$run_log"; printf "\\n"')
        # 输出回传片段：打印信封前缀 + 整份运行日志的 base64（-w0 不折行）。
        if streamed_script:
            # The verified stream already lives on the guest's independent tmpfs,
            # so the execution request contains only a small fixed-size wrapper.
            # This also remains usable for Verifier postmortems if ext4 is read-only.
            # （既有英文注释：已校验的流已在客户机独立 tmpfs 上，执行请求只含很小的
            # 固定尺寸包装；ext4 只读时仍可用于 Verifier 事后分析。）
            cmd = (
                f'f={shlex.quote(streamed_script)}; run_log={shm_run_log}; '
                f'{timeout_cmd} {interpreter} "$f" > "$run_log" 2>&1; e=$?; '
                f'{emit_log}; echo "[exit $e]"; rm -f "$f" "$run_log"')
            # 流式路径包装：执行已推送的程序 → 日志写入 /dev/shm → 回传 base64 信封
            # 与 [exit N] → 清理程序与日志。
        elif allow_staging_fallback:
            # The OSWorld guest occasionally remounts its ext4 root read-only after
            # an I/O/filesystem fault. /tmp then cannot stage even a diagnostic
            # Verifier Program, although /dev/shm remains a separate writable tmpfs.
            # Select the location before decoding or executing model code, so the
            # fallback never replays an action. Actor execution deliberately retains
            # the normal fail-fast path: it must not pretend it can repair a candidate
            # whose filesystem is read-only.
            # （既有英文注释：OSWorld 客户机在 I/O/文件系统故障后偶发把 ext4 根重挂为
            # 只读；此时 /tmp 连诊断用 Verifier 程序都暂存不了，而 /dev/shm 仍是独立
            # 可写 tmpfs。在解码/执行模型代码之前先选好落盘位置，回退路径绝不重放动作；
            # Actor 执行刻意保持 fail-fast：不能假装能修复一个根文件系统只读的候选。）
            cmd = (
                f'tmp_candidate=$(mktemp /tmp/rsiagent_XXXXXX.{extension} 2>&1); '
                'tmp_status=$?; '
                f'if [ "$tmp_status" -eq 0 ]; then f="$tmp_candidate"; run_log={run_log}; '
                'else tmp_error="$tmp_candidate"; '
                f'shm_candidate=$(mktemp /dev/shm/rsiagent_XXXXXX.{extension} 2>&1); '
                'shm_status=$?; '
                'if [ "$shm_status" -ne 0 ]; then '
                f'printf "%s\\n/tmp: %s\\n/dev/shm: %s\\n" '
                f'"{_STAGING_UNAVAILABLE}" "$tmp_error" "$shm_candidate" >&2; exit 125; '
                f'fi; f="$shm_candidate"; run_log={shm_run_log}; '
                f'printf "%s\\n%s\\n" "{_STAGING_FALLBACK}" "$tmp_error"; fi; '
                f'printf %s {b64} | base64 -d > "$f"; '
                f'{timeout_cmd} {interpreter} "$f" > "$run_log" 2>&1; e=$?; '
                f'{emit_log}; echo "[exit $e]"; rm -f "$f" "$run_log"')
        else:
            cmd = (f'f=$(mktemp /tmp/rsiagent_XXXXXX.{extension}); '
                   f'printf %s {b64} | base64 -d > "$f"; '
                   f'{timeout_cmd} {interpreter} "$f" > {run_log} 2>&1; e=$?; '
                   f'run_log={run_log}; {emit_log}; echo "[exit $e]"; '
                   f'rm -f "$f" {run_log}')
            # 内联路径包装：mktemp 落盘 → base64 解码写文件 → timeout 执行并合并日志
            # → 回传 base64 信封与 [exit N] → 清理程序与日志。
        t0 = time.time()
        out = self.run_command(cmd, timeout=timeout, cap=cap)
        secs = time.time() - t0
        # 经命令通道执行包装命令，并记录耗时。
        if out.startswith("[command timed out"):   # HTTP timeout: recover the partial trace
            # （既有英文注释）HTTP 超时：立刻抢救客户机内残留的部分输出 trace。
            if streamed_script:
                partial = self.run_command(
                    f'if test -f {shlex.quote(shm_run_log)}; then '
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
                    f'base64 -w0 {shlex.quote(shm_run_log)}; printf "\\n"; fi',
                    timeout=30, cap=cap)
                # 流式路径：从 /dev/shm 日志回传 base64 信封。
                self.run_command(
                    f"rm -f {shlex.quote(shm_run_log)} "
                    f"{shlex.quote(streamed_script)}", timeout=30, cap=1000)
                # 清理日志与已推送的程序文件。
            elif allow_staging_fallback:
                candidates = f"{run_log} {shm_run_log}"
                partial = self.run_command(
                    f'for p in {candidates}; do if test -f "$p"; then '
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; base64 -w0 "$p"; '
                    f'printf "\\n"; fi; done',
                    timeout=30, cap=cap)
                # 回退路径：/tmp 与 /dev/shm 两个候选日志都尝试回传。
                self.run_command(f"rm -f {candidates}", timeout=30, cap=1000)
                # 清理两处候选日志。
            else:
                partial = self.run_command(
                    f'printf %s {_RUN_OUTPUT_B64_PREFIX!r}; '
                    f'base64 -w0 {run_log}; printf "\\n"',
                    timeout=30, cap=cap)
                # 普通路径：从 /tmp 日志回传 base64 信封。
                self.run_command(f"rm -f {run_log}", timeout=30, cap=1000)
                # 清理日志文件。
            partial, partial_context = _decode_run_output_envelopes(partial)
            prefix = "[program timed out — partial output:]\n"
            return Trace(
                stdout=prefix + partial, exit_code=None, secs=secs,
                timed_out=True,
                context_stdout=(prefix + partial_context
                                if partial_context is not None else None))
            # 返回带“部分输出”前缀的 Trace：退出码未知、timed_out=True、
            # 上下文输出仅在外置化发生时提供。
        # Infrastructure sentinels are emitted outside the base64 Program-output
        # envelope. The guest API appends wrapper stderr after stdout, so inspect
        # both the prefix and suffix. In a full-observation Verifier the
        # model can legitimately inspect the wrapper process itself, whose command
        # line contains those same literal sentinel strings. Never classify bytes
        # decoded from model Program output as transport state.
        # （既有英文注释：基础设施哨兵输出在 base64 信封之外；包装器 stderr 会附加在
        # stdout 之后，因此前缀和后缀都要检查；全观察 Verifier 可以合法查看包装器
        # 进程自身（其命令行含相同哨兵字面量）；绝不把从模型程序输出解码的字节
        # 当成传输状态。）
        transport_preamble = re.sub(
            re.escape(_RUN_OUTPUT_B64_PREFIX) + r"[A-Za-z0-9+/=]*\n", "", out)
        # 先剥掉 base64 信封，得到纯传输层前言（用于哨兵判定，防模型伪造/混入）。
        out, context_out = _decode_run_output_envelopes(out)
        # 再按信封协议解码输出（归档输出 + 可选的上下文输出）。
        fallback_used = _STAGING_FALLBACK in transport_preamble
        # 判断是否动用了 /dev/shm 暂存回退。
        if (fallback_used or "Read-only file system" in transport_preamble) \
                and not self._staging_fallback_reported:
            # run_command does not stage through /tmp, so it remains usable for a
            # read-only postmortem. Capture this once before the VM is torn down.
            # （既有英文注释：run_command 不经 /tmp 暂存，只读事后分析仍可用；
            # 在 VM 被销毁前采集一次诊断。）
            diagnostic = self.run_command(
                "echo 'mount state:'; "
                "findmnt -T / -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /tmp -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /home/user -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "findmnt -T /dev/shm -no TARGET,SOURCE,FSTYPE,OPTIONS 2>&1; "
                "echo 'space:'; df -h / /tmp /home/user /dev/shm 2>&1; "
                "echo 'kernel warnings:'; dmesg --level=err,warn 2>&1 | tail -40",
                timeout=30, cap=8000)
            # 采集挂载状态、磁盘空间与内核告警等文件系统诊断信息。
            self._staging_fallback_reported = True
            log.warning("guest filesystem staging fault; fallback used=%s", fallback_used)
            # 每个 VM 只报告一次，避免诊断风暴。
            marker = re.search(r"\[exit \d+\]\s*$", out)
            report = "\n[HARNESS FILESYSTEM DIAGNOSTIC]\n" + diagnostic + "\n"
            if marker:
                out = out[:marker.start()] + report + out[marker.start():]
            else:
                out += report
            # 把诊断报告插入归档输出的 [exit N] 行之前（无尾行则追加）。
            if context_out is not None:
                context_marker = re.search(r"\[exit \d+\]\s*$", context_out)
                if context_marker:
                    context_out = (context_out[:context_marker.start()] + report
                                   + context_out[context_marker.start():])
                else:
                    context_out += report
                # 上下文输出同样插入诊断报告。
        exit_code = None
        m = re.search(r"\[exit (\d+)\]\s*$", out)
        if m:
            exit_code = int(m.group(1))
            # 从结尾 [exit N] 行解析真实退出码（124 表示客户机内超时）。
        # v17: the wrapper stages the program via `mktemp /tmp/...`; if the guest fs
        # went read-only (disk-full / I/O fault on the shared box) mktemp fails and NO
        # program ran. This signature = broken machine, not a bad program.
        # P2-era addition, same class: a [channel error] marker means the guest never
        # answered at all (container/server death — ep003 burned 30 iters against a
        # dead channel because only the RO signature counted as infra).
        # （既有英文注释：v17——包装器用 mktemp /tmp 暂存程序，客户机文件系统只读时
        # mktemp 失败、程序根本没跑，该签名代表机器坏了而非程序差；P2 补充——
        # [channel error] 标记表示客户机完全没应答（容器/服务死亡），同样算基础设施。）
        infra = ((_STAGING_UNAVAILABLE in transport_preamble)
                 or (not fallback_used
                     and "Read-only file system" in transport_preamble
                     and ("mktemp" in transport_preamble
                          or run_log in transport_preamble))
                 or transport_preamble.startswith("[channel error:"))
        # 综合判定基础设施故障：双暂存都失败；或非回退路径出现只读签名（伴随
        # mktemp/日志路径痕迹）；或前言以通道错误开头（通道死亡）。
        return Trace(stdout=out, exit_code=exit_code, secs=secs,
                     timed_out=(exit_code == 124), infra_fail=infra,
                     context_stdout=context_out)
        # 返回完整 Trace：归档输出、退出码、耗时、是否超时、基础设施标记与上下文输出。


    # ------------------------------------------------------------------ files
    def fetch_file(self, path: str, max_bytes: int | None = None):
        """Pull a file's raw bytes out of the VM.

        Transport is lossless by default. ``max_bytes`` remains available only
        as an explicit caller policy; a project snapshot, memory bank, image, or
        agent-authored handoff must not be rejected by an implicit harness ceiling.
        Returns (bytes, "") on success or (None, reason) on failure.
        """
        # （docstring 大意：从 VM 取出文件原始字节；传输默认无损；max_bytes 只是调用方
        # 的显式策略——项目快照/记忆库/图像/交接文件不得被隐式上限拒绝；成功返回
        # (bytes, "")，失败返回 (None, 原因)。）
        try:
            data = self.env.controller.get_file(path)
            # 走 OSWorld 控制器读取文件字节。
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_file(%s) failed: %s", path, e)
            return None, f"error reading the file ({type(e).__name__})"
            # 读取异常：返回失败与异常类型。
        if not data:
            return None, "file not found or empty"
            # 空数据视为文件不存在或为空。
        if max_bytes is not None and len(data) > max_bytes:
            return None, (f"file is {len(data)} bytes (limit {max_bytes}) — "
                          "downscale or crop it with a program first")
            # 显式上限超限：返回失败并提示先用程序缩放/裁剪。
        return data, ""
        # 成功：返回字节与空原因。

    def push_file(self, local_path: str, guest_path: str,
                  attempts: int = 3, timeout: int = 3600):
        """Stream one host artifact into the guest without a size ceiling.

        On the Docker provider, a one-shot harness-owned receiver writes the
        raw TCP stream directly to a temporary guest file and atomically
        publishes it only after exact size and SHA-256 verification. This
        avoids both shell-argument base64 expansion and OSWorld's WSGI adapter,
        which buffers request bodies in memory. Other providers retain the
        normal OSWorld task-setup upload endpoint as a compatibility path.
        Total artifact size is never a policy limit.

        Returns ``(True, "")`` on success or ``(False, reason)`` after all
        attempts fail.  The source is reopened for every attempt so a retry
        always starts from byte zero.
        """
        # （docstring 大意：把宿主工件无损流式推入客户机，不设大小上限；Docker provider
        # 上由一次性受信接收器把 TCP 流直写临时文件，精确大小+SHA-256 校验通过后才
        # 原子发布——避开 shell 参数 base64 膨胀与 OSWorld WSGI 适配器的内存缓冲；
        # 其他 provider 退回 OSWorld 上传端点；每次尝试都从字节 0 重开源文件。）
        if (not isinstance(local_path, str) or not local_path
                or os.path.islink(local_path) or not os.path.isfile(local_path)):
            return False, "host artifact is not an ordinary file"
            # 宿主侧必须是普通文件（拒绝符号链接/目录/空路径）。
        if not isinstance(guest_path, str) or not guest_path:
            return False, "guest artifact path is empty"
            # 客户机路径不能为空。

        docker_ip = self._docker_container_ip()
        # 尝试解析 Docker 容器的内网 IP（决定走直连流还是兼容上传）。
        if docker_ip:
            last_reason = "direct stream did not run"
            for attempt in range(max(1, attempts)):
                ok, last_reason = self._push_file_direct_docker(
                    local_path, guest_path, docker_ip, timeout)
                if ok:
                    return True, ""
                    # 直连流成功：立即返回。
                log.warning(
                    "push_file direct stream (%s -> %s) attempt %d/%d "
                    "failed: %s", local_path, guest_path, attempt + 1,
                    max(1, attempts), last_reason)
                # 失败记日志并重试（指数退避，封顶 4 秒）。
                if attempt + 1 < max(1, attempts):
                    time.sleep(min(4, 2 ** attempt))
            return False, last_reason
            # 全部尝试失败：返回最后一次原因。

        return self._push_file_multipart(
            local_path, guest_path, attempts=attempts, timeout=timeout)
        # 无 Docker 路由：退回 multipart 兼容上传。

    def _docker_container_ip(self) -> str:
        """Return this environment's outer Docker IP, or empty if inapplicable."""

        # 返回环境外层 Docker 容器的内网 IPv4；不适用/失败则返回空串。
        if getattr(self.env, "provider_name", "") != "docker":
            return ""
            # 非 Docker provider：无直连路由。
        provider = getattr(self.env, "provider", None)
        container = getattr(provider, "container", None)
        if container is None:
            return ""
            # 容器对象缺失：无直连路由。
        try:
            reload_container = getattr(container, "reload", None)
            if callable(reload_container):
                reload_container()
            # 刷新容器属性，拿到最新网络配置。
            network = container.attrs.get("NetworkSettings", {})
            candidates = [network.get("IPAddress", "")]
            candidates.extend(
                value.get("IPAddress", "")
                for value in network.get("Networks", {}).values()
                if isinstance(value, dict))
            # 汇总主 IP 与各网络下的 IP 候选。
            for candidate in candidates:
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                    # 非法地址跳过。
                if address.version == 4 and address.is_private:
                    return str(address)
                    # 返回第一个私有 IPv4。
        except Exception as exc:  # noqa: BLE001
            log.warning("could not resolve Docker artifact route: %s", exc)
            # 解析失败仅告警，退回兼容路径。
        return ""

    def _push_file_direct_docker(self, local_path: str, guest_path: str,
                                 docker_ip: str, timeout: int):
        """Push one verified stream through the Docker outer/guest NAT."""

        # 经 Docker 外层/客户机 NAT 推送一条经校验的 TCP 流（核心无损传输）。
        source_size = os.path.getsize(local_path)
        source_digest = hashlib.sha256()
        with open(local_path, "rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                source_digest.update(block)
        source_sha256 = source_digest.hexdigest()
        # 预先计算源文件的大小与 SHA-256，作为传输完整性验收标准。
        token = secrets.token_hex(12)
        prefix = f"/tmp/rsiagent_push_{token}"
        # 随机令牌隔离本次传输的状态/端口/日志文件。
        # ``os.replace`` is atomic only within one filesystem. Program payloads
        # intentionally target /dev/shm when the root filesystem is impaired, so
        # staging their part file in /tmp would fail with EXDEV/OSError. Keep the
        # private part beside its destination; status/control files remain in /tmp.
        # （既有英文注释：os.replace 只在同一文件系统内原子；根文件系统受损时程序
        # 载荷会故意指向 /dev/shm，把 part 文件放 /tmp 会 EXDEV 失败；因此 part 与
        # 目标同目录，状态/控制文件留在 /tmp。）
        part_path = _atomic_guest_part_path(guest_path, token)
        if not part_path:
            return False, "guest artifact path does not name a file"
            # 无法构造同文件系统的隐藏 part 路径则失败。
        status_path = prefix + ".status"
        port_path = prefix + ".port"
        log_path = prefix + ".log"
        # 接收器的状态、端口、日志三个控制文件路径。
        receiver = r'''import hashlib
import os
import socket
import sys

destination, part, status, port_file, size_text, expected, timeout_text = sys.argv[1:]
size = int(size_text)
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.settimeout(int(timeout_text))
listener.bind(("0.0.0.0", 0))
listener.listen(1)
with open(port_file, "w", encoding="ascii") as handle:
    handle.write(str(listener.getsockname()[1]))
    handle.flush()
    os.fsync(handle.fileno())
try:
    connection, _peer = listener.accept()
    connection.settimeout(int(timeout_text))
    digest = hashlib.sha256()
    received = 0
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    with connection, open(part, "wb") as output:
        while received < size:
            chunk = connection.recv(min(1 << 20, size - received))
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
            received += len(chunk)
        extra = connection.recv(1)
        output.flush()
        os.fsync(output.fileno())
    actual = digest.hexdigest()
    if received != size or extra or actual != expected:
        raise RuntimeError("stream size or digest mismatch")
    os.replace(part, destination)
    result = f"OK {received} {actual}"
except Exception as exc:
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    result = f"ERROR {type(exc).__name__}"
finally:
    listener.close()
with open(status, "w", encoding="ascii") as handle:
    handle.write(result)
    handle.flush()
    os.fsync(handle.fileno())
'''
        # 接收器脚本语义（宿主注入客户机的一次性 Python 程序）：监听随机端口并把
        # 端口写入 port 文件；接受连接后按期望大小接收字节流并同步计算 SHA-256；
        # 校验大小/摘要/无多余字节全部通过后，原子改名 part→目标文件；任何失败都
        # 删除 part 并把 ERROR 写入 status 文件，成功则写 "OK <size> <sha256>"。
        quoted_paths = " ".join(shlex.quote(value) for value in (
            guest_path, part_path, status_path, port_path,
            str(source_size), source_sha256, str(max(1, timeout))))
        # 以 shlex.quote 安全引用全部参数（目标、part、status、port、大小、摘要、超时）。
        launch = self.run_command(
            f"rm -f {shlex.quote(part_path)} {shlex.quote(status_path)} "
            f"{shlex.quote(port_path)} {shlex.quote(log_path)}; "
            f"nohup python3 -c {shlex.quote(receiver)} {quoted_paths} "
            f">{shlex.quote(log_path)} 2>&1 </dev/null & "
            "echo RSIAGENT_PUSH_PID=$!", timeout=30)
        # 清掉旧控制文件后，nohup 后台启动接收器并回显其 PID。
        pid_match = re.search(r"RSIAGENT_PUSH_PID=(\d+)", launch or "")
        pid = pid_match.group(1) if pid_match else ""
        # 解析接收器 PID（失败则为空，稍后判定）。

        def cleanup(*, terminate: bool) -> None:
            # 清理函数：可选先杀接收器进程，再删除全部控制文件。
            kill = f"kill {pid} 2>/dev/null || true; " if terminate and pid else ""
            self.run_command(
                kill + f"rm -f {shlex.quote(part_path)} "
                f"{shlex.quote(status_path)} {shlex.quote(port_path)} "
                f"{shlex.quote(log_path)}", timeout=30)

        if not pid:
            cleanup(terminate=False)
            return False, "guest receiver did not publish a process id"
            # 没拿到 PID：接收器没起来，清理后失败返回。
        port_out = self.run_command(
            f"for i in $(seq 1 100); do test -s {shlex.quote(port_path)} "
            f"&& cat {shlex.quote(port_path)} && exit 0; sleep 0.1; done; "
            "echo RSIAGENT_PUSH_PORT_TIMEOUT", timeout=20)
        # 轮询等待接收器发布监听端口（最多约 10 秒）。
        port_match = re.search(r"(?m)^([0-9]{2,5})$", port_out or "")
        if not port_match or not 1 <= int(port_match.group(1)) <= 65535:
            cleanup(terminate=True)
            return False, "guest receiver did not publish a valid port"
            # 端口缺失/非法：杀进程并失败返回。

        try:
            with socket.create_connection(
                    (docker_ip, int(port_match.group(1))), timeout=10) as stream:
                stream.settimeout(max(1, timeout))
                with open(local_path, "rb") as source:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        stream.sendall(block)
                stream.shutdown(socket.SHUT_WR)
                # 直连容器 NAT 内的接收器端口，按 1MB 块推送全部字节后关闭写端。
            after_size = os.path.getsize(local_path)
            if after_size != source_size:
                cleanup(terminate=True)
                return False, "host artifact changed size during streaming"
                # 传输期间源文件大小变了：判失败（避免错位流）。
        except Exception as exc:  # noqa: BLE001
            cleanup(terminate=True)
            return False, f"{type(exc).__name__}: {exc}"
            # 连接/发送异常：杀进程并失败返回。

        status_out = self.run_command(
            f"for i in $(seq 1 600); do test -s {shlex.quote(status_path)} "
            f"&& cat {shlex.quote(status_path)} && exit 0; sleep 0.1; done; "
            "echo RSIAGENT_PUSH_STATUS_TIMEOUT", timeout=90)
        # 轮询等待接收器写入最终状态（最多约 60 秒）。
        expected_status = f"OK {source_size} {source_sha256}"
        if expected_status not in (status_out or "").splitlines():
            cleanup(terminate=True)
            return False, (status_out or "guest receiver returned no status")[-240:]
            # 状态行与期望的 "OK 大小 摘要" 不符：判失败（校验未通过）。
        cleanup(terminate=False)
        return True, ""
        # 验收通过：清理控制文件并返回成功。

    def _push_file_multipart(self, local_path: str, guest_path: str,
                             attempts: int, timeout: int):
        """Compatibility transport for providers without the Docker route."""

        # 无 Docker 直连路由时的兼容传输：走 OSWorld 的 multipart 上传端点。
        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder
        except Exception as exc:  # noqa: BLE001
            return False, ("streaming multipart transport is unavailable "
                           f"({type(exc).__name__})")
            # 依赖缺失：报告不可用（fail-closed，不静默）。

        url = f"{self.env.controller.http_server}/setup/upload"
        # OSWorld 的任务装配上传端点。
        last_reason = "upload did not run"
        for attempt in range(max(1, attempts)):
            # 最多 attempts 次，指数退避重试。
            try:
                with open(local_path, "rb") as source:
                    form = MultipartEncoder({
                        "file_path": guest_path,
                        "file_data": (
                            os.path.basename(guest_path) or "artifact",
                            source, "application/octet-stream"),
                    })
                    # 组装 multipart 表单：目标路径 + 二进制文件流。
                    response = requests.post(
                        url, headers={"Content-Type": form.content_type},
                        data=form, timeout=(10, timeout))
                    # 流式 POST（连接 10 秒 / 读超时 timeout）。
                if response.status_code == 200:
                    return True, ""
                    # HTTP 200 即成功。
                last_reason = (f"upload returned HTTP {response.status_code}: "
                               f"{response.text[:160]}")
                # 记录 HTTP 错误与响应摘要。
            except Exception as exc:  # noqa: BLE001
                last_reason = f"{type(exc).__name__}: {exc}"
                # 记录异常。
            log.warning("push_file(%s -> %s) attempt %d/%d failed: %s",
                        local_path, guest_path, attempt + 1,
                        max(1, attempts), last_reason)
            # 每次失败都记日志。
            if attempt + 1 < max(1, attempts):
                time.sleep(min(4, 2 ** attempt))
                # 指数退避（封顶 4 秒）后重试。
        return False, last_reason
        # 全部失败：返回最后一次原因。


def _bound(text: str, cap: int) -> str:
    """Optionally bound ``text``; a non-positive cap means lossless transport."""
    # 可选截断工具：cap 非正表示无损传输（原样返回）。
    if cap is None or cap <= 0:
        return text
        # 无损模式。
    if len(text) <= cap:
        return text
        # 未超限原样返回。
    head, tail = cap // 3, cap - cap // 3
    return (text[:head] + f"\n...[{len(text) - cap} chars omitted]...\n" + text[-tail:])
    # 超限时保留头部 1/3 与尾部 2/3，中间插入省略标记（保住结尾的 [exit N] 行）。
