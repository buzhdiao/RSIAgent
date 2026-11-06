"""Host-only Verifier setup transport for guests whose desktop sudo is disabled.

The QEMU control socket is inside the provider container and bound to loopback;
the guest end is a root-only virtio port. No Actor API, sudo rule, task definition,
or grader is changed. Only trusted isolation wrappers use this transport. The
wrappers still drop to the desktop UID with no capabilities before model code.
"""
from __future__ import annotations

import base64
# base64：请求/响应载荷以 ASCII 安全穿越 virtio 串口与 JSON 通道。
import io
import json
import secrets
import tarfile
import threading
import time

from env.vm import Trace, _decode_run_output_envelopes

# 追加到 QEMU 命令行的设备参数：virtio-serial 总线上挂一个名为
# org.rsiagent.verifier.control 的 virtserialport；chardev 是 provider 容器内
# 127.0.0.1:7101 上的 socket 服务端（不等待客户端、不对外发布端口）。
QEMU_ARGUMENTS = (
    "-device virtio-serial-pci,id=rsiagent_control "
    "-chardev socket,id=rsiagent_control,host=127.0.0.1,port=7101,server=on,wait=off "
    "-device virtserialport,bus=rsiagent_control.0,chardev=rsiagent_control,"
    "name=org.rsiagent.verifier.control"
)
DEVICE = "/dev/virtio-ports/org.rsiagent.verifier.control"
# 客户机内该 virtio 串口端点对应的设备文件（仅 root 可访问）。

# This loopback belongs to the provider container, not the guest. Port 7101 is
# never published, nor bound to the guest's container-gateway interface.
# 客户机内的请求客户端脚本（原始字符串）：把请求文件内容写进 root-only 的
# virtio 串口（/dev/tcp 直连 provider 容器内回环端口 7101 的宿主端），然后带
# 截止时间地逐行读回响应，只接受 nonce 与本次请求一致的那一行（防串话/防陈旧
# 响应），超时以 124 退出。
_CLIENT = r'''set -eu
request=$1
budget=$2
nonce=$3
trap 'rm -f -- "$request"' EXIT
exec 3<>/dev/tcp/127.0.0.1/7101
cat "$request" >&3
deadline=$((SECONDS + budget))
while IFS= read -r -t "$((deadline - SECONDS))" response <&3; do
  case "$response" in
    *\"nonce\":\""$nonce"\"*) printf '%s\n' "$response"; exit 0 ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || exit 124
done
exit 124
'''

# 以 root 常驻客户机的守护进程（Python 源码，原始字符串）：监听 virtio 串口，
# 逐行解析 JSON 请求（ping / run 两类操作）；run 会把 base64 的受信包装脚本落盘
# 到 /run 下的私有目录并以受限环境执行，超时则杀整组进程；结果（退出码、是否
# 超时、base64 输出）带同一 nonce 回写串口。
_DAEMON = r'''import base64, json, os, pathlib, signal, subprocess, tempfile, time
device = "/dev/virtio-ports/org.rsiagent.verifier.control"
os.chown(device, 0, 0)
os.chmod(device, 0o600)
os.umask(0o077)
with open(device, "r+b", buffering=0) as wire:
    pending = b""
    while True:
        try:
            chunk = os.read(wire.fileno(), 65536)
        except OSError:
            time.sleep(.1)
            continue
        if not chunk:
            time.sleep(.1)
            continue
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            req = {}
            try:
                req = json.loads(line)
                nonce = req["nonce"]
                if req.get("op") == "ping":
                    result = {"uid": os.geteuid(), "pid": os.getpid()}
                else:
                    assert req["op"] == "run"
                    code = base64.b64decode(req["code_b64"], validate=True)
                    budget = int(req["timeout"])
                    assert budget > 0
                    with tempfile.TemporaryDirectory(dir="/run/rsiagent-verifier-control") as td:
                        script = pathlib.Path(td) / "wrapper.sh"
                        script.write_bytes(code)
                        script.chmod(0o600)
                        with open(pathlib.Path(td) / "output", "w+b") as output:
                            child = subprocess.Popen(["bash", str(script)], stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                env={"PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                     "HOME":"/root", "LANG":"C.UTF-8"})
                            timed_out = False
                            try:
                                child.wait(timeout=budget)
                            except subprocess.TimeoutExpired:
                                timed_out = True
                                os.killpg(child.pid, signal.SIGKILL)
                                child.wait()
                            output.seek(0)
                            result = {"exit_code":124 if timed_out else child.returncode,
                                      "timed_out":timed_out,
                                      "output_b64":base64.b64encode(output.read()).decode("ascii")}
                answer = {"nonce":nonce, "result":result}
            except Exception as exc:
                answer = {"nonce":req.get("nonce", "") if isinstance(req, dict) else "",
                          "error":type(exc).__name__ + ": " + str(exc)}
            data = (json.dumps(answer, separators=(",", ":")) + "\n").encode("ascii")
            try:
                while data:
                    data = data[os.write(wire.fileno(), data):]
            except OSError:
                pass
'''

# This function is defined only inside a root-owned, host-supplied wrapper. It
# implements the existing wrapper's sudo spelling without consulting desktop
# sudoers. It is not exported; model programs start under setpriv and env -i.
# （既有英文注释见上：该函数只存在于 root 属主、宿主下发的包装脚本内部，按既有
# 包装脚本的 sudo 语法做同形替换而不查询桌面 sudoers；模型程序在 setpriv + env -i
# 之下启动，不受此影响。）
_ROOT_WRAPPER = r'''set -eu
[ "$(id -u)" = 0 ] || exit 125
sudo() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -S|-k) shift ;;
      -p) shift 2 ;;
      -K) return 0 ;;
      --) shift; break ;;
      *) return 125 ;;
    esac
  done
  [ "$#" -gt 0 ] || return 125
  command "$@" </dev/null
}
'''


class VerifierControl:
    # 面向“宿主→root-only 通道”的 Verifier 专用传输：绕过被禁用的桌面 sudo，
    # 经 Docker 容器 API 把请求投递到客户机内的 root 守护进程。仅受信隔离包装
    # 允许使用；包装在运行模型代码前仍会降权到桌面 UID 且不带任何能力。
    def __init__(self, desktop):
        self.desktop = desktop
        # 保存桌面环境句柄（provider 容器对象从中取得）。
        self._lock = threading.Lock()
        # 串行化“投递归档 + exec_run”的临界区，避免并发请求互相读到对方的响应。

    def _request(self, payload, timeout):
        # 单次请求-响应：打 nonce → tar 投递请求文件 → exec_run 运行 _CLIENT →
        # 校验唯一匹配的 nonce 应答 → 返回其 result 字段。
        container = self.desktop.provider.container
        # 取得 provider 容器对象（Docker API 入口）。
        nonce = secrets.token_hex(16)
        # 生成 32 位十六进制随机 nonce，用于请求/响应配对，防止陈旧响应冒充本次结果。
        payload = {**payload, "nonce": nonce}
        # 把 nonce 合入请求载荷。
        data = (json.dumps(payload) + "\n").encode("ascii")
        # 序列化成一行 ASCII JSON（换行作为串口上的消息定界符）。
        name = "rsiagent-control-request-" + nonce
        # 投递到容器 /tmp 的请求文件名（含 nonce，保证唯一）。
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
        # 把请求文件打成内存 tar（权限 0600），供 put_archive 投递。
        with self._lock:
            container.client.api.timeout = max(
                container.client.api.timeout, int(timeout) + 60)
            # 临时抬高 Docker API 的 HTTP 超时，覆盖本次请求预算并留 60 秒余量。
            container.put_archive("/tmp", stream.getvalue())
            # 把 tar 投递到容器 /tmp（即客户机可见的请求文件）。
            result = container.exec_run(
                ["bash", "-c", _CLIENT, "rsiagent-verifier-control", "/tmp/" + name,
                 str(int(timeout) + 15), nonce])
            # 在容器内运行 _CLIENT：传入请求路径、读回超时预算（+15s 余量）与 nonce。
        if result.exit_code != 0:
            raise RuntimeError(f"Verifier control transport failed: exit={result.exit_code}")
        # 传输层失败（如超时 124）直接抛错，不产出任何可疑结果。
        # Do not accept an old response buffered across a failed connection or
        # QEMU rollback as the result of a different trusted operation.
        # （既有英文注释：不得把跨失败连接/QEMU 回滚缓冲下来的旧应答当作另一次受信操作的结果。）
        answers = [json.loads(line) for line in result.output.splitlines() if line.strip()]
        # 把输出逐行解析为 JSON 应答列表。
        matching = [a for a in answers if a.get("nonce") == nonce]
        # 只保留 nonce 与本次请求一致的应答。
        if len(matching) != 1 or "result" not in matching[0]:
            raise RuntimeError("Verifier control response missing, stale, or failed")
        # 恰好一条且带 result 才可信；缺失/重复/陈旧一律判失败。
        return matching[0]["result"]
        # 返回验证过的 result 字段。

    def ping(self):
        # 健康检查：请求一次 ping，并验证对端确实以 root 运行。
        result = self._request({"op": "ping"}, 15)
        if result.get("uid") != 0:
            raise RuntimeError("Verifier control endpoint is not privileged")
            # 非 root 说明通道落地错误，拒绝使用。
        return result

    def run_script(self, lang, code, *, timeout=600, cap=0):
        # 在 root 通道内执行一段受信 Bash 包装脚本，返回与 VM.run_script 兼容的 Trace。
        if lang != "bash":
            raise ValueError("Verifier control accepts trusted Bash wrappers only")
        # 该通道只接受受信 Bash 包装，其他语言一律拒绝。
        started = time.monotonic()
        try:
            result = self._request({"op": "run", "timeout": int(timeout),
                "code_b64": base64.b64encode((_ROOT_WRAPPER + code).encode()).decode()}, timeout)
            # 把“root 包装前缀 + 用户包装”拼接后 base64，作为 run 操作发往守护进程。
            output, context = _decode_run_output_envelopes(
                "RSIAGENT_RUN_OUTPUT_BASE64:" + result["output_b64"] + "\n"
                + f"[exit {result['exit_code']}]")
            # 用与 VM 相同的信封协议解码输出（二进制外置化逻辑一致），并拼上 [exit N] 尾行。
            return Trace(output, result["exit_code"], time.monotonic()-started,
                         result["timed_out"], False, context)
            # 组装 Trace：输出、退出码、耗时、是否超时、非基础设施故障、上下文输出。
        except Exception as exc:
            # 通道任何异常都折叠成一条 infra_fail 的 Trace（fail-closed，不让上层误判为候选证据）。
            return Trace(f"[trusted Verifier control failure: {type(exc).__name__}: {exc}]",
                         125, time.monotonic()-started, infra_fail=True)


def provision_verifier_control(desktop):
    """Install before task setup, while the pristine guest permits host sudo."""
    # 一次性安装：趁原始客户机仍允许宿主持有的 sudo 时，把 root 守护进程装好并启动。
    from env.vm import VM

    payload = base64.b64encode(_DAEMON.encode()).decode()
    # 守护进程源码 base64 编码后作为脚本参数传入。
    password = base64.b64encode(desktop.client_password.encode()).decode()
    # 桌面 sudo 密码 base64 编码，经 stdin 提权。
    script = f'''set -eu
printf %s {password!r} | base64 -d | sudo -S -k -p '' -- bash -ceu '
  test -c {DEVICE}
  install -d -m 0700 -o root -g root /run/rsiagent-verifier-control
  printf %s "$1" | base64 -d > /run/rsiagent-verifier-control/server.py
  chmod 0400 /run/rsiagent-verifier-control/server.py
  chmod 0600 {DEVICE}
  nohup env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /usr/bin/python3 \
    /run/rsiagent-verifier-control/server.py \
    >/run/rsiagent-verifier-control/server.log 2>&1 </dev/null &
' rsiagent-control-bootstrap {payload!r}
sudo -K
'''
    # 提权脚本：确认 virtio 设备存在 → 建立 root-only 目录 → 落盘守护进程源码
    # （0400 只读）→ 把串口设备收紧到 0600 → 以最小环境 nohup 启动；最后清空凭据缓存。
    trace = VM(desktop).run_script("bash", script, timeout=60, cap=0)
    # 经普通命令通道执行安装脚本（此时桌面 sudo 仍可用）。
    if trace.exit_code != 0 or trace.infra_fail:
        raise RuntimeError("Verifier control bootstrap failed: " + trace.stdout)
        # 安装失败立即抛错（fail-closed）。
    control = VerifierControl(desktop)
    control.ping()
    # 用 ping 验证通道就绪且对端是 root。
    desktop._rsiagent_verifier_control = control
    # 挂到桌面对象上，供 AgenticVerifierExecutor 的 _run_harness_script 优先选用。
    return control
