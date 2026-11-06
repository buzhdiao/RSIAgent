"""QEMU-backed rollback transactions for full-observation verification.

The scored state is a QEMU checkpoint, all Verifier effects occur after it, and
``rollback`` restores disk, RAM, processes, sockets, and GUI state. External
services outside the QEMU guest are not covered by this transaction. Provider
launch adaptation belongs to the host harness, not this generic VM boundary.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger("rsiagent.qemu_rollback")
# 模块级日志器，记录检查点保存/恢复的关键事件。

EFFECT_ISOLATED = "effect_isolated"
# 经典执行模式：Verifier 程序在 Linux 命名空间中运行，候选文件系统只读。
ROLLBACK_MIRROR = "rollback_mirror"
# 实验执行模式（回滚镜像）：先保存完整 QEMU 检查点（checkpoint），Verifier 直接
# 观察并测试活动状态，检查结束后恢复快照再评分。
VERIFIER_EXECUTION_MODES = (EFFECT_ISOLATED, ROLLBACK_MIRROR)
# 合法执行模式集合。

class QemuRollbackError(RuntimeError):
    """The trusted QEMU checkpoint or rollback boundary failed."""
    # 受信 QEMU 检查点/回滚边界失败——属基础设施错误，绝不能当作候选质量的证据。


def normalize_verifier_execution_mode(value: str | None) -> str:
    # 把外部传入的执行模式规整为合法枚举值；缺省归到 effect_isolated。
    mode = str(value or EFFECT_ISOLATED).strip().lower()
    # 空值回退到默认模式，并做去空白、小写归一。
    if mode not in VERIFIER_EXECUTION_MODES:
        raise ValueError(
            f"unsupported verifier execution mode {value!r}; expected one of "
            f"{VERIFIER_EXECUTION_MODES}")
        # 未知模式直接报错（fail-closed），绝不静默降级到无保护执行。
    return mode


# 与 QEMU 人机监控（HMP，human monitor protocol）交互的容器内脚本：连到容器
# 回环 7100 端口（QEMU monitor 的 socket），先等首个 `(qemu)` 提示符，写入命令，
# 再等第二个提示符（命令完成），随后输出捕获到的全部监控文本；两处超时都以 124
# 退出并打印对应哨兵。
_HMP_SCRIPT = r'''set -eu
command_text=$1
timeout_secs=$2
capture=$(mktemp /tmp/rsiagent_hmp_XXXXXX)
reader_pid=
cleanup() {
  if [ -n "$reader_pid" ]; then
    kill "$reader_pid" 2>/dev/null || true
    wait "$reader_pid" 2>/dev/null || true
  fi
  rm -f "$capture"
  exec 3>&- 3<&- || true
}
trap cleanup EXIT
exec 3<>/dev/tcp/127.0.0.1/7100
cat <&3 >"$capture" &
reader_pid=$!
deadline=$((SECONDS + timeout_secs))
prompt_count() { grep -ao '(qemu)' "$capture" 2>/dev/null | wc -l; }
while [ "$(prompt_count)" -lt 1 ]; do
  [ "$SECONDS" -lt "$deadline" ] || {
    echo 'RSIAGENT_HMP_INITIAL_PROMPT_TIMEOUT'
    exit 124
  }
  sleep 0.1
done
printf '%s\n' "$command_text" >&3
while [ "$(prompt_count)" -lt 2 ]; do
  [ "$SECONDS" -lt "$deadline" ] || {
    echo 'RSIAGENT_HMP_COMMAND_TIMEOUT'
    exit 124
  }
  sleep 0.1
done
kill "$reader_pid" 2>/dev/null || true
wait "$reader_pid" 2>/dev/null || true
reader_pid=
cat "$capture"
'''

# 匹配 ANSI 转义序列（CSI）的正则，用于清洗监控输出。
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# OSWorld Docker forwards its Chromium interface to guest port 9222. Inspect
# that listener and the browser's actual debug listener without connecting to
# either: even a GET /json/version can change a task's connection audit log.
# The browser's port comes from its process arguments, not from task state.
# （既有英文注释见上：OSWorld Docker 把 Chromium 接口转发到客户机 9222；只检查
# 监听器而不连接任何一端——即使一次 GET /json/version 也可能改写任务的连接审计
# 日志；浏览器端口来自进程参数而非任务状态。）
_BROWSER_LISTENER_PROBE = r'''/usr/bin/python3 - <<'RSIAGENT_BROWSER_LISTENERS'
from pathlib import Path
import shlex

proc = Path('/proc')
listening = set()
for table in ('tcp', 'tcp6'):
    path = proc / 'net' / table
    if table == 'tcp6' and not path.exists():
        continue
    for line in path.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[3] == '0A':
            listening.add(int(fields[1].rsplit(':', 1)[1], 16))

browser_ports = set()
for path in proc.glob('[0-9]*/cmdline'):
    try:
        args = [arg for arg in path.read_bytes().decode('utf-8', 'replace').split('\0') if arg]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        continue
    # Chrome may rewrite argv into one process-title string in /proc/cmdline.
    if len(args) == 1:
        try:
            args = shlex.split(args[0])
        except ValueError:
            continue
    if not args or Path(args[0]).name not in {
        'chrome', 'chromium', 'chromium-browser', 'google-chrome',
        'google-chrome-stable',
    } or any(arg.startswith('--type=') for arg in args):
        continue
    for index, arg in enumerate(args):
        value = arg.partition('=')[2] if arg.startswith('--remote-debugging-port=') else (
            args[index + 1] if arg == '--remote-debugging-port' and index + 1 < len(args) else '')
        if value.isdecimal() and 1 <= int(value) <= 65535:
            browser_ports.add(int(value))

ready = 9222 in listening and bool(browser_ports & listening)
print('RSIAGENT_BROWSER_LISTENERS_READY' if ready else 'RSIAGENT_BROWSER_LISTENERS_ABSENT')
RSIAGENT_BROWSER_LISTENERS'''
# 脚本语义：解析 /proc/net/tcp{,6} 得到处于 LISTEN（状态 0A）的端口集合；再扫描
# 各进程 cmdline 找出 Chrome/Chromium 主进程的 --remote-debugging-port；若 9222
# 与某个浏览器调试端口都在监听，则打印 READY，否则打印 ABSENT。


def _clean_hmp_output(raw: bytes | str) -> str:
    # 清洗 QEMU 监控输出：解码字节流、去掉 ANSI 转义、仅保留可打印 ASCII 与常用空白。
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
        # 字节输入按 UTF-8 宽松解码（非法字节替换为 U+FFFD）。
    else:
        text = str(raw)
        # 字符串输入原样转字符串。
    text = _ANSI.sub("", text)
    # 删除终端控制序列，避免污染后续正则解析。
    return "".join(
        char for char in text
        if char in "\n\r\t" or 32 <= ord(char) < 127)
        # 只保留换行/回车/制表与 32~126 的可打印字符，剔除其他控制字节。


@dataclass
class QemuRollbackTransaction:
    """One exact savevm/loadvm transaction around a Verifier inspection."""

    # 围绕一次 Verifier 检查的精确 savevm/loadvm 事务（论文术语：回滚镜像边界）。
    vm: object
    timeout: int = 300

    def __post_init__(self):
        # dataclass 初始化后的钩子：生成事务私有状态。
        self.tag = "rsiagent_verify_" + secrets.token_hex(12)
        # 本事务唯一的 QEMU 快照标签（随机化，避免与他人/历史快照冲突）。
        self.active = False
        # 是否已经 savevm（此后回滚成为强制义务）。
        self.restored = False
        # 是否已经完成 loadvm 恢复（保证事务一次性使用）。
        # Restore listeners that were present before the checkpoint. Never
        # contact browser endpoints: the probe itself can affect scored state.
        # The command controller can recover before the browser's listeners.
        # （既有英文注释：只恢复检查点前已存在的监听器；绝不主动连接浏览器端点，
        # 因为探测本身可能影响计分状态；命令控制器通常先于浏览器监听器恢复。）
        self._required_interfaces: tuple[str, ...] = ()
        # 检查点时刻健康、回滚后必须恢复的外部接口清单（如 chromium_listeners）。

    def _chromium_listeners_ready(self) -> bool:
        # 被动探测：Chromium 调试监听器（9222 转发与浏览器自身端口）是否就绪。
        environment = getattr(self.vm, "env", None)
        host = str(getattr(environment, "vm_ip", "") or "").strip()
        port = getattr(environment, "chromium_port", None)
        # 读取环境上的客户机 IP 与 Chromium 端口配置。
        if not host or not port:
            return False
            # 配置缺失视为未就绪（不视为错误）。
        try:
            port = int(port)
        except (TypeError, ValueError):
            return False
            # 端口不可转换同样视为未就绪。
        if not 1 <= port <= 65535:
            return False
            # 端口范围非法直接判未就绪。
        report = self.vm.run_command(_BROWSER_LISTENER_PROBE, timeout=10, cap=1000).strip()
        # 在客户机内运行被动探测脚本（只读 /proc，不建立连接）。
        if report == "RSIAGENT_BROWSER_LISTENERS_READY":
            return True
            # 哨兵 READY：监听器齐全。
        if report == "RSIAGENT_BROWSER_LISTENERS_ABSENT":
            return False
            # 哨兵 ABSENT：明确未就绪。
        raise QemuRollbackError(
            "passive browser listener probe did not complete: " + report[-400:])
        # 输出既非 READY 也非 ABSENT，说明探测本身失败——按边界错误处理。

    def _capture_required_interfaces(self) -> tuple[str, ...]:
        # 记录检查点时刻哪些外部接口是健康的，作为回滚后的恢复目标。
        required = []
        if self._chromium_listeners_ready():
            required.append("chromium_listeners")
            # 仅当检查点前 Chromium 监听器健康时才要求其恢复。
        return tuple(required)

    def _wait_for_required_interfaces(
            self, *, timeout: float = 180, probe_interval: float = 2.0,
            stable_probes: int = 2) -> tuple[bool, str]:
        # 回滚后轮询等待检查点时健康的外部接口恢复；返回 (是否全部恢复, 报告文本)。
        if not self._required_interfaces:
            return True, "no optional external interface was healthy at checkpoint"
            # 检查点时没有健康的外部接口，则无需等待。
        deadline = time.monotonic() + max(0.0, float(timeout))
        # 计算轮询截止时刻（单调时钟，避免系统时间跳变）。
        stable = 0
        attempts = 0
        current: dict[str, bool] = {}
        last_error = ""
        # 稳定计数、尝试次数、最近一次各接口状态与最近一次错误。
        while time.monotonic() < deadline:
            attempts += 1
            try:
                current = {"chromium_listeners": self._chromium_listeners_ready()}
                last_error = ""
                # 正常探测：记录各接口健康状态并清空错误。
            except QemuRollbackError as exc:
                # A command-channel flap after loadvm is not evidence that the
                # listeners disappeared. Retry within the original deadline.
                # （既有英文注释：loadvm 后命令通道抖动并不代表监听器消失，在原时限内重试。）
                current = {"chromium_listeners": False}
                last_error = str(exc)
                # 探测失败按“未知/不健康”处理并记录错误，继续轮询。
            healthy = all(current[name] for name in self._required_interfaces)
            # 汇总：要求的接口是否全部健康。
            stable = stable + 1 if healthy else 0
            # 连续健康计数：健康则累加，否则清零（防止单次假阳性）。
            if stable >= max(1, int(stable_probes)):
                return True, (
                    f"{', '.join(self._required_interfaces)} recovered after "
                    f"{attempts} probe(s)")
                # 连续足够次数健康，判定恢复完成。
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(max(0.0, probe_interval), remaining))
                # 按探测间隔休眠，但不超过剩余时限。
        detail = ", ".join(
            f"{name}={'healthy' if current.get(name) else 'unhealthy'}"
            for name in self._required_interfaces)
        # 汇总各接口最终状态，用于诊断报告。
        if last_error:
            detail += "; " + last_error
            # 附上最后一次探测错误。
        return False, (
            f"required checkpoint interfaces did not recover after {attempts} "
            f"probe(s): {detail}")
        # 超时仍未恢复：返回失败与细节。

    def _container(self):
        # 取得 QEMU 所在的 OSWorld Docker 容器对象（monitor 端口 7100 在其回环上）。
        environment = getattr(self.vm, "env", None)
        if getattr(environment, "provider_name", "") != "docker":
            raise QemuRollbackError(
                "rollback_mirror currently requires the OSWorld Docker provider")
            # 回滚镜像模式目前仅支持 Docker provider（QEMU 由它托管）。
        provider = getattr(environment, "provider", None)
        container = getattr(provider, "container", None)
        if container is None:
            raise QemuRollbackError("OSWorld Docker container is unavailable")
            # 容器对象缺失即边界不可用。
        return container

    def _hmp(self, command: str, *, timeout: int | None = None) -> str:
        # 向 QEMU monitor 发送一条 HMP 命令并返回清洗后的输出。
        if not re.fullmatch(r"[A-Za-z0-9_. -]+", command or ""):
            raise QemuRollbackError("unsafe QEMU monitor command")
            # 白名单校验命令文本（仅字母数字、下划线、点、空格、连字符），阻断注入。
        budget = max(1, int(timeout or self.timeout))
        # 计算超时预算（未指定则用事务默认值）。
        result = self._container().exec_run(
            ["bash", "-c", _HMP_SCRIPT, "rsiagent-hmp", command, str(budget)])
        # 在容器内执行 HMP 交互脚本。
        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", None)
        if exit_code is None and isinstance(result, tuple):
            exit_code, output = result
            # 兼容不同 docker-py 版本的返回形态（对象或二元组）。
        cleaned = _clean_hmp_output(output or b"")
        # 清洗监控输出。
        if exit_code != 0:
            raise QemuRollbackError(
                f"QEMU monitor command failed ({command!r}, exit={exit_code}):\n"
                + cleaned[-4000:])
            # 脚本退出码非 0（含 124 超时）：边界失败。
        if re.search(r"(?m)^Error:", cleaned):
            raise QemuRollbackError(
                f"QEMU rejected monitor command {command!r}:\n"
                + cleaned[-4000:])
            # QEMU 在输出中以行首 Error: 拒绝命令：同样按边界失败处理。
        return cleaned

    def _snapshot_present(self) -> bool:
        # 查询 QEMU 快照列表，确认本事务标签的检查点确实存在。
        report = self._hmp("info snapshots", timeout=60)
        return re.search(
            rf"(?m)^--\s+{re.escape(self.tag)}\s+", report) is not None
        # 在列表中按行首格式匹配本事务的标签。

    def begin(self) -> None:
        # 事务开始：保存 QEMU 检查点（savevm）。此后回滚成为强制义务。
        if self.active:
            return
            # 已激活则幂等返回。
        if self.restored:
            raise QemuRollbackError("a rollback transaction cannot be reused")
            # 事务一次性使用：恢复过的实例禁止再次开启。
        self._required_interfaces = self._capture_required_interfaces()
        # 先记录检查点时刻健康的外部接口（作为回滚后的恢复验收标准）。
        self._hmp("savevm " + self.tag)
        # 向 QEMU monitor 发出 savevm：保存磁盘+内存的完整检查点。
        # From this point onward rollback is mandatory.  Mark the transaction
        # active before the redundant inventory check so even a monitor/parsing
        # failure cannot make close() silently abandon a checkpoint that QEMU may
        # already have committed.
        # （既有英文注释：从此刻起回滚是强制的；先把事务标记为 active 再做冗余的
        # 快照清点，这样即便监控命令/解析失败，close() 也不会悄悄丢弃一个
        # QEMU 可能已经提交的检查点。）
        self.active = True
        # 标记激活（必须在清点之前，保证失败路径仍会尝试回滚）。
        if not self._snapshot_present():
            raise QemuRollbackError(
                "QEMU reported no saved verification checkpoint after savevm")
            # 冗余校验：快照列表中找不到标签则报错（但事务仍为 active，可回滚兜底）。
        log.info(
            "saved verifier rollback checkpoint %s (required interfaces: %s)",
            self.tag, ", ".join(self._required_interfaces) or "controller only")
        # 记录检查点保存成功及所需接口清单。

    def rollback(self) -> None:
        # 事务回滚：loadvm 恢复检查点，等待控制器与接口恢复，再清理快照。
        if self.restored:
            return
            # 已恢复则幂等返回。
        if not self.active:
            self.restored = True
            return
            # 从未 savevm（如 begin 前就 close）：无事可做，直接视为已收尾。
        self._hmp("loadvm " + self.tag)
        # loadvm：把磁盘、内存、进程、套接字与 GUI 状态整体还原到检查点时刻。
        recovered, detail = self.vm.wait_for_controller(
            timeout=180, probe_interval=2.0, stable_probes=2)
        # 等待客户机命令通道稳定恢复（只读 printf 探针）。
        if not recovered:
            raise QemuRollbackError(
                "QEMU loaded the verification checkpoint but the guest controller "
                "did not recover: " + detail)
            # 控制器未恢复：回滚边界失败。
        interfaces_ready, interface_detail = self._wait_for_required_interfaces()
        # 等待检查点时健康的外部接口（如 Chromium 监听器）恢复。
        if not interfaces_ready:
            raise QemuRollbackError(
                "QEMU loaded the verification checkpoint and the guest controller "
                "recovered, but an interface that was healthy at checkpoint did "
                "not recover: " + interface_detail)
            # 接口未恢复：同样视为回滚不完整。
        self._hmp("delvm " + self.tag, timeout=120)
        # 删除快照，释放检查点占用的存储。
        if self._snapshot_present():
            raise QemuRollbackError(
                "verification checkpoint remained after rollback cleanup")
            # 清理后快照仍在：报告异常（防止残留状态影响后续运行）。
        self.active = False
        self.restored = True
        # 事务完结：解除激活并标记已恢复。
        log.info(
            "restored verifier rollback checkpoint %s (%s)",
            self.tag, interface_detail)
        # 记录恢复成功与接口恢复详情。
