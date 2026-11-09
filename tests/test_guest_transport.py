# 【中文注释·模块定位】guest 文件与 run_script 尾标传输的无损性契约测试
# （env/vm.VM：fetch_file/run_script/run_command/push_file/wait_for_controller
# + core/actor.trace_message 的渲染 + env.vm._bound/_atomic_guest_part_path）。
# 被钉死的传输承诺：
# 1) fetch_file 支持显式无上限工件传输；超上限时返回 (None, "limit N")；
# 2) Actor 程序传输默认无损（cap=0 零截断，10k 字节记忆中段原样存活）；
# 3) 非 UTF-8 字节从上下文外置化：guest 侧 base64 编码回传，上下文只留
#    "bytes=N sha256=..." 指纹与重查指引，就绪令牌保留，渲染层不出现原始
#    base64（PROGRAM OUTPUT EXTERNALIZED）；
# 4) run_command 把 timeout 原样转发给 guest HTTP 服务；guest 执行失败以
#    [channel error: ...] 通道错误形式浮出；
# 5) 控制器恢复要求"稳定且非变异"的探测（只 printf 就绪令牌），连续通过
#    达标才算恢复；
# 6) Actor 程序使用唯一运行日志且 TERM→KILL 升级（timeout --kill-after=5s）；
# 7) 大程序走 /dev/shm 流式暂存而非 shell argv（程序与 base64 均不进命令行），
#    暂存失败是显式基础设施故障（绝不执行未暂存程序）；
# 8) /tmp 不可写时按开关回退 /dev/shm 并捕获文件系统诊断；两个暂存挂载都
#    失败才判 infra；程序输出里引用哨兵语句不能伪造传输失败；
# 9) 输出信封之后的 stderr 单独携带包装脚本诊断（每 VM 只诊断一次，不重放
#    动作）；HTTP 超时只恢复自己的唯一部分日志；
# 10) push_file 在通用 guest 上走 OSWorld setup 多部件上传端点，在 docker
#    provider 上优先校验过的直连流；直连流在目标文件系统上原子分段。
# 文件头部还保留四张样例卡（milestone/project/E6/E8）供人工对照。
"""Lossless guest-file and run_script trailer transport."""
# 模块 docstring：guest 文件与 run_script 尾标传输的无损性。

import base64
# 导入 base64：构造/解出 RUN_OUTPUT_BASE64 信封。
import re
# 导入 re：从命令里抽取唯一日志路径。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 controller/provider/响应等轻量对象。

import pytest
# 导入 pytest：异常断言（含 pytest.fail 哨兵）。

from core.actor import trace_message
# 导入轨迹消息渲染：验证外置化输出在模型可见层的表现。
from env.vm import VM as RSIAgentVM, _bound
# 导入被测 VM 与字节截断原语 _bound。
from tools import exam_fence
# 导入考试围栏模块（样例卡片的语义对应其审计域）。


_NIGHT = """MODE: MILESTONE
TARGET: terminal-transport
REP: 1
BUDGET: 60
RATIONALE: reconstruct a small artifact from clean fixtures
Create the requested output file with the specified contents.
SUCCESS:
1. test -f /home/user/output.txt && echo PASS || echo FAIL"""
# 样例：milestone 夜卡（目标/预算/成功命令）。

_PROJECT = f"""PROJECT: terminal-transport
NIGHTS: 2
Create a small output from the supplied generic fixture.
NIGHT: 1/2
{_NIGHT}
NIGHT: 2/2
{_NIGHT}
FINAL:
1. test -f /home/user/output.txt && echo PASS || echo FAIL"""
# 样例：项目卡（两夜练习 + 终局检查）。

_E6_CARD = """INSTANCE
CLASS: zotero
RUNG: 1
RATIONALE: exercise a small local collection operation
Create a collection containing one locally chosen item.
SUCCESS:
1. test -f /home/user/item.txt && echo PASS || echo FAIL"""
# 样例：E6 卡（zotero 类第 1 级实例）。

_E8_CARD = """INSTANCE
CLASS: reconstruction
MODE: DRILL
TARGET: local-output
REP: 1
BUDGET: 10
RATIONALE: verify a clean reconstruction from a pinned fixture
Create the requested local output.
SUCCESS:
1. test -f /home/user/output.txt && echo PASS || echo FAIL
2. test -f /home/user/raw/source.txt && echo PASS || echo FAIL # GUARD"""
# 样例：E8 卡（reconstruction 钻练，第 2 条成功命令带 GUARD 标记）。


def test_vm_fetch_file_supports_explicit_unbounded_artifact_transfer():
    """fetch_file 支持显式无上限传输；超上限返回 (None, "limit N")。"""
    payload = b"artifact" * 600_000
    # 4.2MB 级别的工件字节。
    controller = SimpleNamespace(get_file=lambda _path: payload)
    # 伪控制器：任何路径都返回同一载荷。
    vm = RSIAgentVM(SimpleNamespace(controller=controller))
    # 被测 VM。

    assert vm.fetch_file("/tmp/artifact") == (payload, "")
    # 默认调用即全量取回（无截断）。
    assert vm.fetch_file("/tmp/artifact", max_bytes=None) == (payload, "")
    # 显式 max_bytes=None 同样无上限。
    bounded, reason = vm.fetch_file("/tmp/artifact", max_bytes=8)
    assert bounded is None
    assert "limit 8" in reason
    # 超过 8 字节上限：返回 None 与带上限值的说明（绝不给部分载荷）。


def test_actor_program_transport_is_lossless_by_default(monkeypatch):
    """Actor 程序传输默认无损：cap=0，2 万多字节的记忆中段原样存活。"""
    payload = "A" * 12000 + "MEMORY-MIDDLE-MUST-SURVIVE" + "Z" * 12000
    # 24000+ 字节载荷，中段放哨兵串（首尾截断藏不住它）。
    vm = RSIAgentVM(SimpleNamespace())
    seen = {}

    def fake_run_command(_command, timeout, cap):
        seen.update(timeout=timeout, cap=cap)
        return payload + "\n[exit 0]"
        # 伪底层命令：记录 cap 并回吐完整载荷 + 退出尾标。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "printf memory")
    # 执行脚本。

    assert seen["cap"] == 0
    # 关键：底层 cap=0（零截断上限）。
    assert payload in trace.stdout
    assert "MEMORY-MIDDLE-MUST-SURVIVE" in trace.stdout
    # 载荷完整出现在轨迹 stdout（中段哨兵未丢）。
    assert _bound(payload, 0) == payload
    # _bound 在 cap=0 时恒等返回。


def test_program_transport_externalizes_non_utf8_bytes_from_context(
        monkeypatch):
    """非 UTF-8 字节外置化：guest 侧 base64 信封，上下文只留指纹与重查指引。"""
    vm = RSIAgentVM(SimpleNamespace())
    ready = b"__RSIAGENT_VERIFIER_SANDBOX_READY_a1b2c3__"
    # 可信就绪令牌（绝不能被外置化抹掉）。
    raw = ready + b"\ntext-before\n\xc8\xff\x00\ntext-after"
    # 混合输出：合法文本 + 非 UTF-8 字节（\xc8\xff\x00）。
    envelope = (
        "RSIAGENT_RUN_OUTPUT_BASE64:"
        + base64.b64encode(raw).decode("ascii")
        + "\n[exit 0]")
    # guest 侧把原始输出编码进 base64 信封（模拟底层的二进制防护）。
    seen = {}

    def fake_run_command(command, timeout, cap):
        seen["command"] = command
        return envelope
        # 伪底层命令：记录 guest 收到的命令。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "cat /usr/local/bin/tool")
    # 执行脚本。

    assert "base64 -w0" in seen["command"]
    # VM 在命令里指示 guest 用 base64 -w0 编码输出。
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    # 正常完成，非基础设施故障。
    digest = __import__("hashlib").sha256(raw).hexdigest()
    encoded = base64.b64encode(raw).decode("ascii")
    assert encoded in trace.stdout
    # stdout 保留完整 base64 载荷（无损）。
    assert f"bytes={len(raw)} sha256={digest}" in trace.stdout
    # stdout 附带字节指纹。
    assert trace.context_stdout is not None
    assert f"bytes={len(raw)} sha256={digest}" in trace.context_stdout
    # 模型可见的上下文 stdout 里有指纹。
    assert encoded not in trace.context_stdout
    # 关键：原始 base64 不进模型上下文（省预算且避免噪声）。
    assert "re-inspect deliberately" in trace.context_stdout
    # 上下文给出"可刻意重查"的指引。
    assert ready.decode("ascii") in trace.context_stdout
    # 可信就绪令牌保留在上下文里（不被外置化抹掉）。
    rendered = trace_message(trace)
    assert encoded not in rendered
    assert "PROGRAM OUTPUT EXTERNALIZED" in rendered
    # 渲染给模型的轨迹明示"程序输出已外置化"且不含 base64。


def test_run_command_forwards_timeout_to_guest_server(monkeypatch):
    """run_command 把 timeout 原样转发给 guest HTTP 服务（600 秒不被钳制）。"""
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "success", "output": "ok", "error": ""}
            # 伪 200 成功响应。

    def fake_post(url, *, json, timeout):
        seen.update(url=url, payload=json, client_timeout=timeout)
        return Response()
        # 记录请求 URL/载荷/客户端超时。

    monkeypatch.setattr("env.vm.requests.post", fake_post)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = RSIAgentVM(SimpleNamespace(controller=controller))

    assert vm.run_command("sleep 1", timeout=600) == "ok"
    # 命令输出如实返回。
    assert seen == {
        "url": "http://controller.invalid/execute",
        "payload": {"command": "sleep 1", "shell": True, "timeout": 600},
        "client_timeout": 600,
    }
    # 关键：payload 与客户端超时都是 600（转发不缩水）。


def test_run_command_surfaces_guest_execution_failure(monkeypatch):
    """guest 执行失败（HTTP 500）以 [channel error: ...] 浮出并携带消息。"""
    class Response:
        status_code = 500

        @staticmethod
        def json():
            return {"status": "error", "message": "command timed out"}
            # 伪 500 错误响应。

    monkeypatch.setattr(
        "env.vm.requests.post", lambda *_args, **_kwargs: Response())
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = RSIAgentVM(SimpleNamespace(controller=controller))

    output = vm.run_command("sleep 999", timeout=45)

    assert output.startswith("[channel error: guest execute HTTP 500")
    # 通道错误前缀。
    assert "command timed out" in output
    # guest 的错误消息包含在内。


def test_controller_recovery_requires_stable_nonmutating_probes(monkeypatch):
    """控制器恢复：探测只 printf 就绪令牌（非变异），连续达标才算恢复。"""
    vm = RSIAgentVM(SimpleNamespace())
    replies = [
        "[channel error: ConnectionError — unavailable]",
        "RSIAGENT_CONTROLLER_READY",
        "[channel error: ConnectionError — restart flap]",
        "RSIAGENT_CONTROLLER_READY",
        "RSIAGENT_CONTROLLER_READY",
    ]
    # 探测脚本：失败、成功、再失败、成功、成功（最后连续 2 次成功）。
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        return replies.pop(0)
        # 记录每次探测。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    monkeypatch.setattr("env.vm.time.sleep", lambda _seconds: None)
    # 去掉探测间隔等待。

    recovered, report = vm.wait_for_controller(
        timeout=30, probe_interval=1, stable_probes=2)
    # 要求连续 2 次稳定通过。

    assert recovered is True
    # 恢复成功。
    assert len(calls) == 5
    # 五次探测后达标。
    assert all(call[0] == "printf RSIAGENT_CONTROLLER_READY" for call in calls)
    # 关键：探测命令恒为 printf 就绪令牌——绝不执行任何变异操作。
    assert "2 consecutive probes passed" in report
    # 报告记录连续通过次数。


def test_actor_programs_use_unique_logs_and_force_kill_escalation(monkeypatch):
    """Actor 程序使用唯一运行日志，并以 TERM→KILL 升级强制终止。"""
    vm = RSIAgentVM(SimpleNamespace())
    commands = []

    def fake_run_command(command, timeout, cap):
        commands.append(command)
        return "done\n[exit 0]"
        # 记录每次包装命令。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    vm.run_script("bash", "true", timeout=120)
    vm.run_script("python", "print('ok')", timeout=120)
    # 连跑两个脚本（同超时）。

    assert all("timeout --signal=TERM --kill-after=5s 90s" in command
               for command in commands)
    # 关键：包装命令用 timeout TERM + 5 秒 kill-after（软终止升级为强杀），
    # 有效期 90 秒（timeout 的 75%，留缓冲）。
    logs = [re.search(r"/tmp/rsiagent_run_[0-9a-f]{24}\.log", command).group(0)
            for command in commands]
    assert logs[0] != logs[1]
    # 两次运行使用不同的唯一日志（互不覆盖）。
    assert all("/tmp/rsiagent_run.log" not in command for command in commands)
    # 旧的共享固定日志名已废除。


def test_large_program_streams_losslessly_instead_of_using_shell_argv(
        monkeypatch):
    """大程序流式暂存而非 shell argv：程序与 base64 都不进命令行。"""
    payload = "BEGIN-LARGE-PROGRAM\n" + ("x = 1\n" * 20_000) + "END-LARGE-PROGRAM\n"
    # 10 万+ 字节的程序。
    vm = RSIAgentVM(SimpleNamespace())
    staged = {}
    commands = []

    def fake_push_file(local_path, guest_path, attempts=3, timeout=3600):
        with open(local_path, "rb") as source:
            staged["bytes"] = source.read()
        staged.update(guest_path=guest_path, attempts=attempts, timeout=timeout)
        return True, ""
        # 伪推送：读取暂存文件字节并记录 guest 路径/超时。

    def fake_run_command(command, timeout, cap):
        commands.append(command)
        raw = b"large program completed"
        return ("RSIAGENT_RUN_OUTPUT_BASE64:"
                + base64.b64encode(raw).decode("ascii")
                + "\n[exit 0]")
        # 伪执行：记录命令并返回 base64 输出信封。

    monkeypatch.setattr(vm, "push_file", fake_push_file)
    monkeypatch.setattr(vm, "run_command", fake_run_command)

    trace = vm.run_script("python", payload, timeout=600)
    # 执行大程序。

    assert staged["bytes"] == payload.encode("utf-8")
    # 关键：程序字节逐字节流式落盘（无损）。
    assert re.fullmatch(
        r"/dev/shm/rsiagent_program_[0-9a-f]{24}\.py",
        staged["guest_path"])
    # guest 路径是 /dev/shm 下的唯一 .py 临时文件。
    assert staged["timeout"] == 600
    # 暂存超时继承脚本超时。
    assert len(commands) == 1
    # 只有一条执行命令。
    assert "BEGIN-LARGE-PROGRAM" not in commands[0]
    assert base64.b64encode(payload.encode("utf-8")).decode("ascii") not in commands[0]
    # 关键：程序原文与 base64 均不进 shell 命令行（只引用暂存路径）。
    assert staged["guest_path"] in commands[0]
    # 命令引用暂存路径。
    assert 'rm -f "$f" "$run_log"' in commands[0]
    # 命令自带清理（程序文件与运行日志）。
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    assert "large program completed" in trace.stdout
    # 执行结果无损回传。


def test_large_program_staging_failure_is_explicit_infrastructure(monkeypatch):
    """大程序暂存失败 = 显式基础设施故障：绝不执行未暂存的程序。"""
    payload = "x = 1\n" * 20_000
    vm = RSIAgentVM(SimpleNamespace())
    monkeypatch.setattr(
        vm, "push_file",
        lambda *_args, **_kwargs: (False, "verified stream unavailable"))
    # 注入：推送失败（校验流不可用）。
    monkeypatch.setattr(
        vm, "run_command",
        lambda *_args, **_kwargs: pytest.fail("unstaged program was executed"))
    # 哨兵：一旦尝试执行未暂存程序即测试失败。

    trace = vm.run_script("python", payload, timeout=600)

    assert trace.exit_code is None
    # 无退出码（程序从未执行）。
    assert trace.infra_fail is True
    # 判定为基础设施故障。
    assert "program was not executed" in trace.stdout
    assert "verified stream unavailable" in trace.stdout
    # 轨迹如实报告未执行与原因。


def test_actor_program_falls_back_to_dev_shm_and_captures_diagnostics(monkeypatch):
    """/tmp 不可写时按开关回退 /dev/shm，并捕获文件系统诊断（仍算成功）。"""
    vm = RSIAgentVM(SimpleNamespace())
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        if len(calls) == 1:
            return ("[RSIAGENT STAGING FALLBACK: /tmp unavailable; using /dev/shm]\n"
                    "mktemp: /tmp: Read-only file system\n"
                    "program completed\n[exit 0]")
            # 第一次：回退到 /dev/shm 后程序完成（exit 0）。
        return ("mount state:\n/dev/sda3 / ext4 ro,relatime\n"
                "/dev/shm tmpfs tmpfs rw,nosuid,nodev")
        # 第二次：诊断命令返回挂载表。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script(
        "bash", "printf completed", allow_staging_fallback=True)
    # 允许暂存回退。

    assert "/dev/shm/rsiagent_XXXXXX.sh" in calls[0][0]
    assert "/dev/shm/rsiagent_run_" in calls[0][0]
    # 关键：首条命令直接落在 /dev/shm（mktemp 模板与日志路径）。
    assert "findmnt -T /home/user" in calls[1][0]
    # 第二条是文件系统诊断命令。
    assert trace.exit_code == 0
    assert trace.infra_fail is False
    # 回退成功不算故障。
    assert "program completed" in trace.stdout
    # 程序输出原样保留。
    assert "HARNESS FILESYSTEM DIAGNOSTIC" in trace.stdout
    # 诊断块被并入轨迹（可审计）。
    assert trace.stdout.rstrip().endswith("[exit 0]")
    # 轨迹以退出尾标收尾（不缺尾标）。


def test_actor_program_marks_infra_only_when_both_staging_mounts_fail(monkeypatch):
    """只有 /tmp 与 /dev/shm 两个暂存挂载都失败才判 infra。"""
    vm = RSIAgentVM(SimpleNamespace())

    monkeypatch.setattr(
        vm, "run_command",
        lambda *_args, **_kwargs: (
            "[RSIAGENT STAGING UNAVAILABLE: /tmp and /dev/shm both failed]\n"
            "/tmp: Read-only file system\n/dev/shm: No space left on device"))
    # 两个挂载都失败。

    trace = vm.run_script(
        "python", "print('never executed')", allow_staging_fallback=True)

    assert trace.exit_code is None
    assert trace.infra_fail is True
    # 程序未执行且判定为基础设施故障。


def test_program_output_cannot_rsiagent_a_transport_failure(monkeypatch):
    """A live-process listing may quote wrapper sentinels as ordinary evidence."""
    # 程序输出里引用包装哨兵语句绝不能伪造传输失败（防注入）。
    vm = RSIAgentVM(SimpleNamespace())
    program_output = (
        "ps: wrapper contains "
        "[RSIAGENT STAGING UNAVAILABLE: /tmp and /dev/shm both failed] "
        "and mktemp: Read-only file system")
    # 程序（如 ps）的输出恰好引用了哨兵语句与失败短语。
    envelope = (
        "RSIAGENT_RUN_OUTPUT_BASE64:"
        + base64.b64encode(program_output.encode("utf-8")).decode("ascii")
        + "\n[exit 0]")
    # 走 base64 信封（真实协议：信封来自可信包装，非原始文本）。
    monkeypatch.setattr(
        vm, "run_command", lambda *_args, **_kwargs: envelope)

    trace = vm.run_script(
        "bash", "ps aux", allow_staging_fallback=True)

    assert trace.exit_code == 0
    assert trace.infra_fail is False
    # 关键：退出码 0 优先——信封内的哨兵文本只是普通输出。
    assert program_output in trace.stdout
    # 输出原样保留。


def test_staging_stderr_after_output_envelope_captures_diagnostics(monkeypatch):
    """The guest API returns wrapper stderr separately, after stdout's envelope."""
    # guest API 把包装 stderr 单独放在输出信封之后；每 VM 只诊断一次。
    vm = RSIAgentVM(SimpleNamespace(controller=SimpleNamespace(http_server="http://guest")))
    calls = []

    def fake_post(_url, *, json, timeout):
        calls.append(json["command"])
        if "findmnt -T /home/user" in json["command"]:
            data = {"output": "mount state:\n/ /dev/sda3 ext4 ro,relatime\n"}
            # 诊断命令返回挂载表（根分区只读）。
        else:
            data = {
                "output": "RSIAGENT_RUN_OUTPUT_BASE64:\n[exit 2]\n",
                "error": "mktemp: /tmp/rsiagent_XXXXXX.sh: Read-only file system\n",
            }
            # 执行命令：空信封 + exit 2，stderr 单独携带 mktemp 失败。
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr("env.vm.requests.post", fake_post)
    trace = vm.run_script("bash", "printf never-executed")

    assert trace.infra_fail is True
    # stderr（信封之后）的 mktemp 失败 → 基础设施故障。
    assert "HARNESS FILESYSTEM DIAGNOSTIC" in trace.stdout
    assert "/ /dev/sda3 ext4 ro,relatime" in trace.stdout
    # 诊断块（含挂载表）并入轨迹。
    assert len(calls) == 2
    # 执行 + 诊断共两次调用。
    repeated = vm.run_script("bash", "printf still-not-executed")
    assert repeated.infra_fail is True
    assert len(calls) == 3  # Diagnose once per VM; never replay the action.
    # 第二次失败不再重复诊断（每 VM 一次）；动作本身绝不重放。


def test_http_timeout_recovers_only_its_unique_partial_log(monkeypatch):
    """HTTP 超时后只恢复自己的唯一部分日志，然后清理它。"""
    vm = RSIAgentVM(SimpleNamespace())
    calls = []

    def fake_run_command(command, timeout, cap):
        calls.append((command, timeout, cap))
        if len(calls) == 1:
            return "[command timed out after 120s]"
            # 第一次：命令超时。
        if len(calls) == 2:
            return "isolated partial output"
            # 第二次：读取部分日志。
        return ""
        # 第三次：删除日志（无输出）。

    monkeypatch.setattr(vm, "run_command", fake_run_command)
    trace = vm.run_script("bash", "sleep 999", timeout=120)

    match = re.search(r"/tmp/rsiagent_run_[0-9a-f]{24}\.log", calls[0][0])
    assert match is not None
    run_log = match.group(0)
    # 从超时命令里抽出本次的唯一日志路径。
    assert calls[1][1:] == (30, 0)
    # 读取命令用短超时 30 秒、cap=0（无损）。
    assert f"base64 -w0 {run_log}" in calls[1][0]
    assert "RSIAGENT_RUN_OUTPUT_BASE64:" in calls[1][0]
    # 用 base64 信封读取部分日志。
    assert calls[2] == (f"rm -f {run_log}", 30, 1000)
    # 恢复后删除该日志（只删自己的）。
    assert trace.timed_out is True
    # 轨迹标记超时。
    assert "isolated partial output" in trace.stdout
    # 部分输出无损并入轨迹。


def test_vm_push_file_streams_through_osworld_setup_endpoint(
        tmp_path, monkeypatch):
    """push_file 在通用 guest 上走 OSWorld setup 多部件上传端点。"""
    payload = b"large-in-principle-artifact" * 64
    source = tmp_path / "candidate.tgz"
    source.write_bytes(payload)
    # 本地源文件。
    seen = {}

    def fake_post(url, *, headers, data, timeout):
        seen.update(url=url, headers=headers, timeout=timeout,
                    body=data.to_string())
        return SimpleNamespace(status_code=200, text="uploaded")
        # 记录上传请求的全部要素。

    monkeypatch.setattr("env.vm.requests.post", fake_post)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = RSIAgentVM(SimpleNamespace(controller=controller))

    assert vm.push_file(str(source), "/tmp/candidate.tgz") == (True, "")
    # 推送成功。
    assert seen["url"] == "http://controller.invalid/setup/upload"
    # 走 setup/upload 端点。
    assert seen["timeout"] == (10, 3600)
    # 连接/读取超时为 (10, 3600)。
    assert seen["headers"]["Content-Type"].startswith(
        "multipart/form-data; boundary=")
    # 多部件表单上传。
    assert payload in seen["body"]
    assert b'/tmp/candidate.tgz' in seen["body"]
    # 文件字节与 guest 路径都在请求体里。


def test_vm_push_file_prefers_verified_direct_stream_on_docker(
        tmp_path, monkeypatch):
    """docker provider 上优先走校验过的直连流（不用多部件回退）。"""
    source = tmp_path / "candidate.tgz"
    source.write_bytes(b"candidate bytes")
    container = SimpleNamespace(attrs={
        "NetworkSettings": {
            "IPAddress": "172.17.0.42",
            "Networks": {},
        }})
    provider = SimpleNamespace(container=container)
    controller = SimpleNamespace(http_server="http://controller.invalid")
    vm = RSIAgentVM(SimpleNamespace(
        provider_name="docker", provider=provider, controller=controller))
    # docker provider：容器 IP 可查。
    calls = []

    def fake_direct(local_path, guest_path, docker_ip, timeout):
        calls.append((local_path, guest_path, docker_ip, timeout))
        return True, ""
        # 伪直连流：记录参数。

    monkeypatch.setattr(vm, "_push_file_direct_docker", fake_direct)
    monkeypatch.setattr(
        "env.vm.requests.post",
        lambda *_args, **_kwargs: pytest.fail("multipart fallback was used"))
    # 哨兵：一旦退回多部件上传即失败。

    assert vm.push_file(str(source), "/tmp/candidate.tgz") == (True, "")
    assert calls == [(
        str(source), "/tmp/candidate.tgz", "172.17.0.42", 3600)]
    # 关键：直连流以容器 IP 与 3600 秒超时执行（multipart 未被触碰）。


def test_direct_stream_stages_atomic_part_on_destination_filesystem():
    """直连流在目标文件系统上原子分段（分段名与最终名同目录同盘）。"""
    from env.vm import _atomic_guest_part_path

    assert _atomic_guest_part_path(
        "/dev/shm/rsiagent_program_abc.sh", "123") == (
            "/dev/shm/.rsiagent_program_abc.sh.rsiagent-part-123")
    # /dev/shm 下的程序：分段名带唯一序号（同目录保证同文件系统，rename 原子）。
    assert _atomic_guest_part_path(
        "/tmp/candidate.tgz", "456") == (
            "/tmp/.candidate.tgz.rsiagent-part-456")
    # /tmp 下的工件同理。
    assert _atomic_guest_part_path("/", "789") == ""
    # 根路径（无文件名）返回空串（拒绝）。










class _GateVM:
    """围栏守卫探测用伪 VM：脚本含 fixture_guard 则回 PASS，否则 FAIL。"""
    def run_command(self, _command, **_kwargs):
        return ""
        # 命令无输出。

    def run_script(self, _lang, code, **_kwargs):
        output = "PASS" if "fixture_guard" in code else "FAIL"
        return SimpleNamespace(stdout=output + "[exit 0]")
        # 按脚本内容返回守卫判定 + 退出尾标。
