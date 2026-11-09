#!/usr/bin/env python3
# 【中文注释·模块定位】Agent 化 Verifier 的效果隔离（effect isolation）行为契约
# 测试（core/verifier_runtime.AgenticVerifierExecutor + env/qemu_rollback +
# core.verifier.verify_agentic）。被钉死的安全承诺：
# 1) Verifier 生成的任意代码只在效果沙箱/回滚镜像里执行——模型源码一律作为
#    数据（base64）进入可信包装脚本，绝不成为 shell 源码；包装脚本带完整边界
#    （mount/pid/net/ipc/uts 命名空间、仅私有回环、no-new-privs、bounding set、
#    只读重挂载、独立 tmpfs scratch）；
# 2) 观察无损：模型 stdout 截断不能抹掉可信就绪令牌；观察传输零上限零截断；
# 3) fail-closed：命名空间预检/keeper 健康复查失败 → exit 125 + infra_fail，
#    程序绝不送到 guest；未知语言 exit 126 零执行；
# 4) Verifier 私有 scratch 的归档/恢复/跨会话搬运全部无损且不设大小上限，
#    传输失败只在回滚成功后恢复；报告只能发布到宿主 harness（专用语言）；
# 5) look 通道守卫：只允许本地绝对路径或数字屏幕引用；私有路径/内存目录被
#    机械掩蔽；文档渲染被路由进隔离 scratch。
# 全部用 _RecordingVM 模拟可信包装（从不执行其 shell 源码），用真实 bash -n
# 校验包装脚本语法，用真实子进程验证管道行为。
"""Behavioral contract for the agentic Verifier's effect isolation."""
# 模块 docstring：Agent 化 Verifier 效果隔离的行为契约。

import base64
# 导入 base64：构造/解码包装脚本里的 base64 程序载荷。
import re
# 导入 re：从脚本中抽取 payload 与就绪令牌。
import subprocess
# 导入 subprocess：真实执行生成的管道与 bash -n 语法校验。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 Trace/env/provider 等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from config.settings import Config
# 导入配置 dataclass：构造 Verifier 风格配置。
from core import loop as L
# 导入主循环：专用 Verifier 执行器的循环级行为。
from core import verifier as V
# 导入独立验证模块：verify_agentic 入口与 VerifierSession。


def _runtime_trace(stdout="", exit_code=0, *, infra_fail=False,
                   context_stdout=None):
    # 测试工具：伪造 Trace 形状的对象（与 env.vm.Trace 字段对齐）。
    return SimpleNamespace(
        stdout=stdout, exit_code=exit_code, secs=0.0,
        timed_out=False, infra_fail=infra_fail,
        context_stdout=context_stdout)


def _decoded_shell_payload(script, variable="payload"):
    # 测试工具：从包装脚本里抽取 `${variable}='base64'` 行并解码为 UTF-8 文本。
    match = re.search(
        rf"(?m)^{re.escape(variable)}='([A-Za-z0-9+/=]+)'$", script)
    assert match is not None, f"missing {variable} payload"
    # 载荷必须存在。
    return base64.b64decode(match.group(1)).decode("utf-8")


class _RecordingVM:
    """Emulate trusted wrappers without ever executing their shell source."""

    # 伪 VM：模拟可信包装协议（健康检查/命名空间预检/就绪令牌），
    # 但从不执行包装脚本的 shell 源码——只记录。

    def __init__(self, program_output="sandbox program output"):
        self.env = SimpleNamespace(client_password="harness-only-password")
        # env 上带一个 harness 专属密码（不应出现在任何包装里）。
        self.commands = []
        # 记录 run_command 调用。
        self.scripts = []
        # 记录 run_script 调用。
        self.program_output = program_output
        # 沙箱/镜像内程序输出的脚本化内容。
        self.fail_preflight = False
        # 注入开关：命名空间预检失败。
        self.fail_health = False
        # 注入开关：keeper 健康检查失败。

    def run_command(self, command, timeout=30, cap=4000):
        self.commands.append({
            "command": command, "timeout": timeout, "cap": cap})
        return ""
        # 命令只记录、返回空。

    def run_script(self, lang, code, timeout=600, cap=0,
                   allow_staging_fallback=False):
        self.scripts.append({
            "lang": lang, "code": code, "timeout": timeout, "cap": cap,
            "allow_staging_fallback": allow_staging_fallback})
        # 记录脚本调用的全部参数（含回退开关）。
        if "RSIAGENT_VERIFIER_NAMESPACE_HEALTHY" in code:
            if self.fail_health:
                return _runtime_trace(
                    "nsenter: cannot open /proc/43210/ns/mnt\n[exit 1]", 1)
            return _runtime_trace(
                "RSIAGENT_VERIFIER_NAMESPACE_HEALTHY\n[exit 0]", 0)
        # keeper 健康检查：按开关返回健康或 nsenter 失败。
        if "RSIAGENT_VERIFIER_NAMESPACE=" in code:
            if self.fail_preflight:
                return _runtime_trace(
                    "sudo: permission denied\n[exit 1]", 1,
                    infra_fail=False)
            return _runtime_trace(
                "RSIAGENT_VERIFIER_NAMESPACE=43210:1000:1000\n[exit 0]", 0)
        # 命名空间预检：按开关返回成功（uid:gid）或 sudo 拒绝。
        match = re.search(
            r"__RSIAGENT_VERIFIER_(?:SANDBOX|MIRROR)_READY_[0-9a-f]+__", code)
        if match:
            return _runtime_trace(
                match.group(0) + "\n" + self.program_output + "\n[exit 0]", 0)
            # 沙箱/镜像就绪令牌回显 + 脚本化程序输出。
        return _runtime_trace("direct program\n[exit 0]", 0)
        # 其他脚本：直接成功输出。


def _verifier_cfg():
    # 测试工具：构造"Verifier 视角"的默认配置。
    cfg = Config()
    cfg.model = "verifier-model"
    cfg.vision_model = ""
    cfg.agent_decided_stop = True
    cfg.practice_mode = True
    cfg.independent_verify = False
    cfg.history_keep_pairs = 0
    cfg.wall_clock_secs = 100
    return cfg


def test_agentic_executor_runs_arbitrary_code_only_inside_effect_sandbox():
    # 测试：Verifier 生成的任意代码只进效果沙箱——源码以 base64 数据进包装
    # 脚本（绝不内联）；包装带全部隔离边界；观察零截断；允许暂存回退。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    model_code = (
        "open('/home/user/candidate', 'w').write('changed')\n"
        "import requests; requests.post('http://127.0.0.1:8000')\n"
        "import subprocess; subprocess.run(['pkill', '-f', 'soffice'])\n")
    # 模型生成的"危险"代码：写候选文件、发请求、杀进程。

    trace = executor("python", model_code, timeout=75)
    # 通过执行器运行。

    assert trace.exit_code == 0
    assert "sandbox program output" in trace.stdout
    # 在沙箱内执行（伪 VM 返回沙箱输出）。
    assert len(vm.scripts) == 2
    preflight, wrapper = vm.scripts
    # 两个脚本：预检 + 包装。
    assert preflight["lang"] == "bash" and wrapper["lang"] == "bash"
    assert model_code not in wrapper["code"]
    # 模型源码绝不内联进包装脚本（不是 shell 源码）。
    assert base64.b64encode(model_code.encode()).decode() in wrapper["code"]
    # 源码以 base64 数据形式进入包装。
    for boundary in (
        "--mount", "--pid", "--net", "--ipc", "--uts",
        "ip link set dev lo up",
        "findmnt -Rrn --raw", "remount,bind,ro", "tmpfs /tmp", "tmpfs /run",
        "--clear-groups", "--no-new-privs", "--bounding-set=-all",
        "VERIFIER_SCRATCH",
    ):
        assert boundary in wrapper["code"]
        # 每一条隔离边界都必须在包装里（五类命名空间、私有回环、只读重挂载、
        # tmpfs、无新特权、清空辅助组、scratch 变量）。
    assert all(call["cap"] == 0 for call in vm.scripts)
    # 观察输出零截断（cap=0 表示无上限）。
    assert all(call["allow_staging_fallback"] for call in vm.scripts)
    # 允许暂存回退。


def test_effect_sandbox_accepts_ready_token_preserved_outside_binary_context():
    # 测试：就绪令牌允许出现在"二进制上下文之外"——程序输出被外置
    # （externalized）时，令牌仍从 context_stdout 提取，非 UTF-8 输出
    # 不破坏就绪判定。
    from core.verifier_runtime import AgenticVerifierExecutor

    class ExternalizedVM(_RecordingVM):
        # 扩展伪 VM：模拟 stdout 含非 UTF-8、输出被外置的场景。
        def run_script(self, lang, code, timeout=600, cap=0,
                       allow_staging_fallback=False):
            if "RSIAGENT_VERIFIER_NAMESPACE=" in code:
                return super().run_script(
                    lang, code, timeout=timeout, cap=cap,
                    allow_staging_fallback=allow_staging_fallback)
                # 预检走父类。
            match = re.search(
                r"__RSIAGENT_VERIFIER_SANDBOX_READY_[0-9a-f]+__", code)
            assert match is not None
            return _runtime_trace(
                "[RSIAGENT NON-UTF8 PROGRAM OUTPUT — EXACT BASE64]\n[exit 0]",
                0,
                context_stdout=(match.group(0)
                                + "\n[PROGRAM OUTPUT EXTERNALIZED]\n[exit 0]"))
            # stdout 是非 UTF-8 占位；令牌与真实输出在 context_stdout。

    trace = AgenticVerifierExecutor(ExternalizedVM())(
        "bash", "cat /usr/local/bin/shotcut")
    # 让模型"读"一个二进制文件。

    assert trace.exit_code == 0
    assert trace.infra_fail is False
    # 外置输出不触发基础设施失败。
    assert "SANDBOX_READY" not in trace.context_stdout
    # 令牌被剥出上下文（不混进观察文本）。
    assert "PROGRAM OUTPUT EXTERNALIZED" in trace.context_stdout
    # 外置说明保留。


@pytest.mark.parametrize("mode", ["effect", "rollback"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_model_stdout_truncation_cannot_erase_trusted_readiness(
        tmp_path, mode, exit_code):
    """Execute generated output plumbing against a real regular-file sink."""
    # 测试（2 模式 × 2 退出码参数化）：用真实子进程执行生成的管道——
    # 即使程序在关闭 stdout/stderr 后再写（模型侧截断/吞字节），就绪令牌
    # READY 仍先行落盘，程序输出顺序与字节（含 \x00\xff）原样保留，退出码
    # 如实传播。模型 stdout 截断无法抹掉可信就绪判定。
    import shlex
    import sys
    from core.verifier_runtime import AgenticVerifierExecutor
    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    executor("python", "pass")
    # 先跑一次效果沙箱拿包装。
    if mode == "rollback":
        executor._run_rollback_mirror("python3", "py", "pass", 60)
        # rollback 模式：再跑一次回滚镜像拿其包装。
    wrapper = vm.scripts[-1]["code"]
    # 取最后一个包装脚本。
    # Replace only the privilege drop/guest command; execute the actual
    # generated pipe and shell error behavior without requiring portable sudo.
    # 只替换降权/guest 命令为本地 python；其余生成的管道与 shell 行为原样执行。
    pipeline = re.search(r"    set -o pipefail\n(.*?)\n  ' rsiagent-verifier",
                         wrapper, re.S).group(0).split("\n  ' rsiagent-verifier")[0]
    # 从包装里抽出核心管道段。
    program = tmp_path / "truncate.py"
    program.write_text(
        "import os,sys\n"
        "os.write(1,b'before\\n')\n"
        "open('/dev/stdout','w').close()\n"
        "open('/dev/stderr','w').close()\n"
        "os.write(1,b'after\\x00\\xff\\n')\n"
        f"sys.exit({exit_code})\n")
    # 伪程序：先写 before，关闭 stdout/stderr，再写 after（含 \x00\xff），
    # 以参数化退出码退出。
    pipeline = re.sub(r'exec setpriv .*?"\$4" "\$6"',
        'exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(program)),
        pipeline, flags=re.S)
    # 把 setpriv 降权换成直接执行伪程序。
    sink = tmp_path / "output"
    with sink.open("wb") as output:
        result = subprocess.run(["bash", "-c",
            'set -- unused "$(id -u)" "$(id -g)" unused READY unused\n' + pipeline],
                                stdout=output, stderr=subprocess.STDOUT)
        # 预置位置参数（含 READY 令牌）后真实执行管道。
    assert result.returncode == exit_code
    # 退出码如实传播（含 7）。
    assert sink.read_bytes() == b"READY\nbefore\nafter\x00\xff\n"
    # 落盘顺序：READY 令牌 → before → after（含原始字节）：
    # 模型侧截断/关闭流都无法抹掉令牌或篡改顺序。


def test_agentic_executor_remains_compatible_with_already_loaded_legacy_vm():
    # 测试：与"已加载的旧签名 VM"兼容——run_script 不带 allow_staging_fallback
    # 形参时执行器照常工作，且不启用暂存回退。
    from core.verifier_runtime import AgenticVerifierExecutor

    class LegacyVM(_RecordingVM):
        def run_script(self, lang, code, timeout=600, cap=0):
            return super().run_script(lang, code, timeout=timeout, cap=cap)
            # 旧签名：没有 allow_staging_fallback 形参。

    vm = LegacyVM()
    trace = AgenticVerifierExecutor(vm)("bash", "printf inspect")

    assert trace.exit_code == 0
    assert "sandbox program output" in trace.stdout
    # 沙箱路径正常。
    assert len(vm.scripts) == 2
    assert all(not call["allow_staging_fallback"] for call in vm.scripts)
    # 旧 VM：回退默认关闭（执行器按 VM 能力适配）。


def test_agentic_executor_enables_only_private_loopback_before_privilege_drop():
    # 测试：网络隔离只开"私有回环"且在降权之前完成——无 veth/桥接/路由拷贝/
    # 宿主网络逃逸；宿主命名空间的挂载进入（nsenter）先于 unshare。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm)(
        "bash", "python3 -m http.server 8765 & curl http://127.0.0.1:8765")
    # 模型代码：起本地 HTTP 服务再访问回环。

    wrapper = vm.scripts[-1]["code"]
    assert "--net" in wrapper
    assert "ip link set dev lo up" in wrapper
    # 独立网络命名空间 + 只把回环拉起来。
    assert wrapper.index("ip link set dev lo up") < wrapper.index("exec setpriv")
    # 回环配置发生在降权（setpriv）之前。
    # No veth, bridge, copied route, or host-network escape is provisioned.
    # 不提供 veth/桥接/复制路由/宿主网络逃逸。
    assert "nsenter --target 43210 --mount" in wrapper
    assert wrapper.index("nsenter --target 43210 --mount") < \
        wrapper.index("unshare")
    # 宿主侧挂载命名空间进入先于沙箱 unshare（否则看不到宿主环境）。
    for forbidden in ("ip link add", "ip route add", "--net=/"):
        assert forbidden not in wrapper
        # 禁止创建链路/路由/以 --net=/ 逃逸宿主网络。


def test_rollback_mirror_sees_live_namespaces_and_rolls_back(monkeypatch):
    # 测试：回滚镜像模式——镜像看到"活的"命名空间（无 pid/net/ipc/uts 隔离、
    # 无只读重挂载），事务 begin→rollback 成对发生。
    from env import qemu_rollback
    from core.verifier_runtime import AgenticVerifierExecutor

    transactions = []
    # 收集创建的事务。

    class Transaction:
        # 伪 QEMU 回滚事务：begin/rollback 可观察。
        def __init__(self, vm):
            self.vm = vm
            self.begun = False
            self.rolled_back = False
            transactions.append(self)

        def begin(self):
            self.begun = True

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(qemu_rollback, "QemuRollbackTransaction", Transaction)
    # 替换真实事务类。
    vm = _RecordingVM(program_output=(
        "candidate=ORIGINAL\nhttp=200\nserver_processes=1\n"))
    # 镜像内输出：看到的是原始候选/活服务/活进程。
    executor = AgenticVerifierExecutor(
        vm, execution_mode="rollback_mirror")
    # 回滚镜像模式执行器。

    trace = executor(
        "bash", "cat /candidate; curl http://127.0.0.1:8765; ps aux")
    executor.close()
    # 执行观察并关闭。

    assert trace.exit_code == 0 and not trace.infra_fail
    # 观察成功。
    assert transactions[0].begun and transactions[0].rolled_back
    # 事务先开始后回滚（恢复检查点）。
    wrapper = vm.scripts[-2]["code"]
    # 倒数第二个脚本是镜像内的观察包装。
    assert "__RSIAGENT_VERIFIER_MIRROR_READY_" in wrapper
    # 镜像就绪令牌。
    assert "nsenter --target 43210 --mount" in wrapper
    # 经宿主挂载命名空间看到活环境。
    assert "DISPLAY=:0" in wrapper
    assert "DBUS_SESSION_BUS_ADDRESS" in wrapper
    # GUI/桌面环境变量可用（能看到活 GUI）。
    for absent in ("--pid", "--net", "--ipc", "--uts", "remount,bind,ro"):
        assert absent not in wrapper
        # 镜像不做进程/网络/IPC/主机名隔离，也不只读重挂载。


def test_rollback_mirror_prompt_describes_observation_and_rollback():
    # 测试：回滚镜像的系统提示如实描述"观察活事务 + 评分前回滚"——
    # 不含"进程/网络/GUI/IPC 不可用"的沙箱措辞。
    from core.verifier_runtime import agentic_verifier_system_for

    prompt = agentic_verifier_system_for("rollback_mirror")

    assert "transactional mirror of the exact live Actor" in prompt
    # 身份：与活的 Actor 完全一致的事务性镜像。
    assert "processes, localhost services, network, GUI" in prompt
    # 可观察范围：进程/本地服务/网络/GUI。
    assert "restores\nthat checkpoint before the candidate is graded" in prompt
    # 评分前恢复检查点（保留换行）。
    assert "processes, network, GUI, and IPC\nchannels are unavailable" not in prompt
    # 不含沙箱式的"不可用"措辞。


def test_qemu_transaction_saves_restores_and_deletes_exact_snapshot():
    # 测试：QEMU 回滚事务的 HMP 命令序列——savevm → info snapshots →
    # loadvm → delvm → info snapshots，标签精确匹配。
    from env.qemu_rollback import QemuRollbackTransaction

    class Container:
        # 伪容器：模拟 QEMU monitor 的命令-响应对话。
        def __init__(self):
            self.snapshot = ""
            self.commands = []

        def exec_run(self, argv):
            command = argv[4]
            self.commands.append(command)
            if command.startswith("savevm "):
                self.snapshot = command.split(" ", 1)[1]
                output = "(qemu)\n(qemu)\n"
                # savevm：记录快照标签。
            elif command == "info snapshots":
                row = (f"--      {self.snapshot} 3.66 GiB 2026-09-03 "
                       "17:00:00 0000:00:01 --\n" if self.snapshot else "")
                output = "(qemu)\nList of snapshots present on all disks:\n" + row + "(qemu)\n"
                # info snapshots：按当前标签回显快照行。
            elif command.startswith("loadvm "):
                assert command.endswith(self.snapshot)
                output = "(qemu)\n(qemu)\n"
                # loadvm：必须加载刚保存的精确标签。
            elif command.startswith("delvm "):
                assert command.endswith(self.snapshot)
                self.snapshot = ""
                output = "(qemu)\n(qemu)\n"
                # delvm：删除同一精确标签。
            else:
                raise AssertionError(command)
                # 其他命令一律拒绝。
            return SimpleNamespace(exit_code=0, output=output.encode())

    container = Container()
    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=container))

    class VM:
        env = environment

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"
            # 回滚后控制器健康。

    transaction = QemuRollbackTransaction(VM())
    transaction.begin()
    assert transaction.active
    # begin 后事务活跃。
    transaction.rollback()

    assert transaction.restored and not transaction.active
    # rollback 后已恢复且不再活跃。
    assert container.commands == [
        "savevm " + transaction.tag,
        "info snapshots",
        "loadvm " + transaction.tag,
        "delvm " + transaction.tag,
        "info snapshots",
    ]
    # 命令序列精确：保存→核对→恢复→删除→再核对（确认已清）。


def test_qemu_transaction_restores_browser_listeners_present_at_checkpoint(
        monkeypatch):
    # 测试：检查点时浏览器监听器在场 → 回滚后必须等它回来（容忍一次通道
    # 抖动 + 缺席探测，直到连续就绪）。
    from env import qemu_rollback

    responses = iter([
        "RSIAGENT_BROWSER_LISTENERS_READY",
        # rollback(): a channel flap, missing listeners, then two ready probes.
        # 回滚验证序列：通道抖动 → 监听器缺席 → 两次就绪。
        "[channel error: temporary guest controller outage]",
        "RSIAGENT_BROWSER_LISTENERS_ABSENT",
        "RSIAGENT_BROWSER_LISTENERS_READY",
        "RSIAGENT_BROWSER_LISTENERS_READY",
    ])
    monkeypatch.setattr(qemu_rollback.time, "sleep", lambda _seconds: None)
    # 去掉重试等待。

    commands = []
    # 记录 HMP 命令。

    class Transaction(qemu_rollback.QemuRollbackTransaction):
        def _hmp(self, command, **_kwargs):
            commands.append(command)
            if command == "info snapshots":
                return (f"--      {self.tag} 3.66 GiB now 00:00:01 --\n"
                        if not any(c.startswith("delvm ") for c in commands)
                        else "")
                # delvm 之前快照在场，之后消失。
            return "(qemu)\n(qemu)\n"

    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=object()),
        vm_ip="localhost",
        chromium_port=9222)
    # 带浏览器端口的环境。

    class VM:
        env = environment

        @staticmethod
        def run_command(*_args, **_kwargs):
            return next(responses)
            # 探测按脚本回。

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"

    transaction = Transaction(VM())
    transaction.begin()
    assert transaction._required_interfaces == ("chromium_listeners",)
    # 检查点时监听器在场 → 必需接口含 chromium_listeners。
    transaction.rollback()
    assert transaction.restored
    # 回滚成功（等到了监听器回来）。


def test_qemu_transaction_does_not_require_browser_absent_at_checkpoint():
    # 测试：检查点时浏览器监听器本就缺席 → 回滚后不要求它出现
    # （required_interfaces 为空）。
    from env import qemu_rollback

    class Transaction(qemu_rollback.QemuRollbackTransaction):
        def _hmp(self, command, **_kwargs):
            if command == "info snapshots":
                return (f"--      {self.tag} 3.66 GiB now 00:00:01 --\n"
                        if self.active else "")
                # 事务活跃时快照在场。
            if command.startswith("delvm "):
                self.active = False
            return "(qemu)\n(qemu)\n"

    environment = SimpleNamespace(
        provider_name="docker",
        provider=SimpleNamespace(container=object()),
        vm_ip="localhost",
        chromium_port=9222)

    class VM:
        env = environment

        @staticmethod
        def run_command(*_args, **_kwargs):
            return "RSIAGENT_BROWSER_LISTENERS_ABSENT"
            # 探测恒缺席。

        @staticmethod
        def wait_for_controller(**_kwargs):
            return True, "healthy"

    transaction = Transaction(VM())
    transaction.begin()
    assert transaction._required_interfaces == ()
    # 检查点缺席 → 零必需接口。
    transaction.rollback()
    assert transaction.restored
    # 回滚成功。


def test_agentic_executor_uses_separate_tmpfs_for_persistent_scratch():
    # 测试：Verifier 私有 scratch 用独立 tmpfs——nosuid/nodev 挂载、root 所有
    # 且 0000 权限的落点、防泄漏检查、EXDEV 隔离（独立 tmpfs 使硬链接回候选
    # 文件系统失败）。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    executor("bash", "printf evidence > \"$VERIFIER_SCRATCH/e.txt\"")
    # 模型往 scratch 写证据。

    preflight = vm.scripts[0]["code"]
    wrapper = vm.scripts[1]["code"]
    keeper = _decoded_shell_payload(preflight, "keeper_payload")
    # 解码预检里的 keeper 载荷。
    assert executor.workspace.startswith("/mnt/rsiagent_verifier_")
    # 工作区是随机命名的 /mnt 挂载点。
    assert "mount -t tmpfs" in keeper
    assert "nosuid,nodev" in keeper
    # keeper 里挂载独立 tmpfs（nosuid,nodev）。
    assert "install -d -m 0000 -o root -g root" in preflight
    # scratch 落点 root:root 0000（防 Actor 侧探测）。
    assert "Verifier scratch leaked into the Actor-visible" in preflight
    # 预检带泄漏诊断文案。
    assert "findmnt -T /home/user" in wrapper
    assert 'findmnt -T "$1"' in wrapper
    # 包装里核对挂载点（候选文件系统与 scratch 分离）。
    # A distinct tmpfs makes hard links back into the candidate filesystem fail
    # with EXDEV; a writable bind directory on the candidate filesystem would not.
    # 独立 tmpfs 使硬链接回候选文件系统报 EXDEV；候选文件系统上的可写 bind
    # 目录做不到这一点。
    assert "rsiagent-verifier-scratch" in keeper
    # keeper 挂载 rsiagent-verifier-scratch。


def test_unified_verifier_mechanically_masks_actor_memory():
    # 测试：统一 Verifier 机械掩蔽 Actor 记忆——/home/user/.memory 以 000 权限
    # tmpfs 覆盖挂载；look 路径守卫拒绝记忆树内路径（含 ../ 逃逸），放行普通交付物。
    from core.verifier_runtime import (
        AgenticVerifierExecutor,
        validate_verifier_look_path,
    )

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm, hide_actor_memory=True)(
        "bash", "find /home/user -maxdepth 2 -type f")
    # 开启记忆掩蔽并执行枚举。

    wrapper = vm.scripts[-1]["code"]
    assert "mount -t tmpfs -o mode=000" in wrapper
    # 记忆目录被 000 权限 tmpfs 覆盖。
    assert base64.b64encode(b"/home/user/.memory").decode() in wrapper
    # 掩蔽路径以 base64 数据进入包装。
    assert validate_verifier_look_path(
        "/home/user/.memory/skill.md", hide_actor_memory=True)[0] is False
    # look 不允许指向记忆树内。
    assert validate_verifier_look_path(
        "/home/user/Desktop/../.memory/skill.md",
        hide_actor_memory=True)[0] is False
    # ../ 逃逸同样被拒。
    assert validate_verifier_look_path(
        "/home/user/report.pptx", hide_actor_memory=True) == (True, "")
    # 普通交付物放行。


def test_unified_verifier_masks_additional_actor_private_paths():
    # 测试：除记忆外的额外私有路径（private_paths）同样被掩蔽与守卫。
    from core.verifier_runtime import (
        AgenticVerifierExecutor,
        validate_verifier_look_path,
    )

    vm = _RecordingVM()
    private = ("/home/user/work",)
    AgenticVerifierExecutor(vm, private_paths=private)(
        "bash", "find /home/user -type f")

    wrapper = vm.scripts[-1]["code"]
    assert base64.b64encode(b"/home/user/work").decode() in wrapper
    # 私有路径进入包装（base64 数据）。
    assert validate_verifier_look_path(
        "/home/user/work/actor_notes.txt",
        private_paths=private)[0] is False
    # look 拒绝私有路径下文件。
    assert validate_verifier_look_path(
        "/home/user/Desktop/candidate.odt",
        private_paths=private) == (True, "")
    # 公开交付物放行。


def test_verifier_private_scratch_can_archive_and_restore_without_size_cap():
    # 测试：私有 scratch 的跨会话搬运——export 以无上限 fetch 抓 tar 归档；
    # 新会话恢复同一归档（字节级无损），文件原样可用。
    from core.verifier_runtime import AgenticVerifierExecutor

    class TransferVM(_RecordingVM):
        # 支持真实文件搬运的伪 VM。
        def __init__(self):
            super().__init__()
            self.pushed = []

        def fetch_file(self, path, max_bytes=None):
            assert max_bytes is None
            # 抓取必须无大小上限。
            assert path.startswith("/tmp/rsiagent_verifier_export_")
            assert path.endswith(".tar")
            return b"lossless-private-scratch-archive", ""
            # 返回固定归档字节。

        def push_file(self, local_path, guest_path):
            with open(local_path, "rb") as handle:
                self.pushed.append((guest_path, handle.read()))
            return True, ""
            # 推送记录目标路径与真实字节。

    first_vm = TransferVM()
    first = AgenticVerifierExecutor(first_vm)
    first("bash", "printf evidence > \"$VERIFIER_SCRATCH/evidence.txt\"")
    # 第一会话写证据。
    archive = first.export_scratch()
    # 导出归档。
    export_wrapper = first_vm.scripts[-1]["code"]
    export_program = _decoded_shell_payload(export_wrapper)
    first.close()

    second_vm = TransferVM()
    second = AgenticVerifierExecutor(second_vm, scratch_archive=archive)
    # 第二会话带归档构造。
    trace = second("bash", "test -f \"$VERIFIER_SCRATCH/evidence.txt\"")
    # 第二会话检查证据文件在。

    assert trace.exit_code == 0
    # 恢复成功。
    assert second_vm.pushed[0][0].startswith(
        "/tmp/rsiagent_verifier_restore_")
    assert second_vm.pushed[0][0].endswith(".tar")
    # 推送到 restore 路径的 tar。
    assert second_vm.pushed[0][1] == b"lossless-private-scratch-archive"
    # 推送字节与导出归档一致（无损）。
    assert "/tmp/rsiagent_verifier_export_" in export_program
    # 导出程序写的是 guest 侧 /tmp 导出路径。
    assert first.workspace + "/.rsiagent_export.tar" not in export_program
    # 绝不写工作区内的导出文件（那个位置在恢复的挂载里不可见）。
    assert all(call["cap"] == 0 for call in second_vm.scripts)
    # 第二会话所有脚本零截断。


def test_verifier_export_falls_back_to_dev_shm_after_guest_root_is_read_only():
    # 测试：guest 根文件系统只读时，scratch 导出回退到 /dev/shm——
    # 归档照常取回（无损）。
    from core.verifier_runtime import AgenticVerifierExecutor

    class ReadOnlyRootVM(_RecordingVM):
        # 扩展伪 VM：/tmp 导出路径只读，/dev/shm 可用。
        def __init__(self):
            super().__init__()
            self.fetched = []

        def run_script(self, lang, code, timeout=600, cap=0,
                       allow_staging_fallback=False):
            if "RSIAGENT_VERIFIER_NAMESPACE=" not in code:
                try:
                    payload = _decoded_shell_payload(code)
                except AssertionError:
                    payload = ""
                if "/tmp/rsiagent_verifier_export_" in payload:
                    self.scripts.append({
                        "lang": lang, "code": code, "timeout": timeout,
                        "cap": cap,
                        "allow_staging_fallback": allow_staging_fallback,
                    })
                    return _runtime_trace(
                        "OSError: [Errno 30] Read-only file system: "
                        "'/tmp/rsiagent_verifier_export_test.tar'\n[exit 1]", 1)
                    # 写 /tmp 导出路径报只读错误。
            return super().run_script(
                lang, code, timeout=timeout, cap=cap,
                allow_staging_fallback=allow_staging_fallback)

        def fetch_file(self, path, max_bytes=None):
            self.fetched.append((path, max_bytes))
            assert path.startswith("/dev/shm/rsiagent_verifier_export_")
            assert max_bytes is None
            # 回退抓取自 /dev/shm 且无上限。
            return b"lossless-emergency-archive", ""
            # 紧急归档字节。

    vm = ReadOnlyRootVM()
    executor = AgenticVerifierExecutor(vm)
    executor("bash", "printf evidence > \"$VERIFIER_SCRATCH/e.txt\"")

    archive = executor.export_scratch()
    # 执行导出（应触发回退）。

    assert archive == b"lossless-emergency-archive"
    # 拿到的是 /dev/shm 回退归档。
    export_payloads = [
        _decoded_shell_payload(call["code"])
        for call in vm.scripts
        if re.search(r"(?m)^payload='[A-Za-z0-9+/=]+'$", call["code"])
        and "rsiagent_verifier_export_" in _decoded_shell_payload(call["code"])
    ]
    # 收集所有涉及导出的程序载荷。
    assert any("/tmp/rsiagent_verifier_export_" in p for p in export_payloads)
    # 第一次尝试确实写过 /tmp 导出路径。
    assert any("/dev/shm/rsiagent_verifier_export_" in p for p in export_payloads)
    # 回退确实写了 /dev/shm 导出路径。
    assert all(
        "/dev/shm/rsiagent_verifier_host_" in call["code"]
        for call in vm.scripts
        if "rsiagent_verifier_export_" in call["code"])
    # 回退脚本走 host_ 挂载桥（宿主可见的 /dev/shm 桥）。


def test_agentic_executor_transports_observations_losslessly():
    # 测试：观察传输无损——50 万字符 + 多字节 Unicode 原样回到 Verifier，
    # 无 bounded/truncat 字样，脚本调用零上限。
    from core.verifier_runtime import AgenticVerifierExecutor

    observation = "证据\n" + "x" * 500_000
    vm = _RecordingVM(program_output=observation)

    trace = AgenticVerifierExecutor(vm)("bash", "printf anything")

    assert observation in trace.stdout
    # 观察原文完整（含 Unicode 头）。
    assert "bounded" not in trace.stdout
    assert "truncat" not in trace.stdout.lower()
    # 无任何截断痕迹。
    assert vm.scripts[-1]["cap"] == 0
    # 观察抓取零上限。


def test_agentic_executor_treats_model_source_as_data_not_wrapper_source():
    # 测试：模型源码一律当作数据——含 shell 注入的代码也只以 base64 进入
    # 包装，绝不成为包装的 shell 源码。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    injected = "'; sudo mount -o remount,rw /; touch /home/user/escaped; #"
    # 典型的 shell 注入载荷。

    executor("bash", injected)

    wrapper = vm.scripts[-1]["code"]
    assert injected not in wrapper
    # 注入文本绝不出现在包装脚本里。
    assert base64.b64encode(injected.encode()).decode() in wrapper
    # 只以 base64 数据形式传输。


def test_agentic_executor_trusted_wrapper_is_valid_bash():
    # 测试：可信包装是语法合法的 bash 脚本——用真实 bash -n 校验。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    AgenticVerifierExecutor(vm)("bash", "printf inspected")

    checked = subprocess.run(
        ["bash", "-n"], input=vm.scripts[-1]["code"], text=True,
        capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr
    # bash -n 语法检查通过。


def test_agentic_executor_fails_closed_when_namespace_preflight_fails():
    # 测试：命名空间预检失败时 fail-closed——exit 125 + infra_fail，程序源码
    # 绝不送到 guest（预检失败后不再发第二个脚本）。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    vm.fail_preflight = True
    trace = AgenticVerifierExecutor(vm)("python", "print('must not run')")

    assert trace.exit_code == 125
    assert trace.infra_fail is True
    # 基础设施失败形状。
    assert "not executed" in trace.stdout
    # 输出写明"未执行"。
    assert len(vm.scripts) == 1
    # 只有预检脚本（程序没发）。
    assert "must not run" not in vm.scripts[0]["code"]
    # 程序源码不在任何脚本里。


def test_agentic_executor_revalidates_cached_keeper_after_idle_and_fails_closed():
    # 测试：空闲后 keeper 缓存失效——下次执行前强制复查健康；复查失败
    # fail-closed（exit 125 + infra_fail），程序不上送。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    assert executor("bash", "printf first").exit_code == 0
    # 第一次成功（keeper 进入缓存）。
    vm.fail_health = True
    executor._last_namespace_check = 0.0
    # 把最近检查时间戳清零：强制复查。

    trace = executor("bash", "printf must-not-run")

    assert trace.exit_code == 125 and trace.infra_fail is True
    # 复查失败：基础设施失败。
    assert "infrastructure, not candidate evidence" in trace.stdout
    # 输出写明这是基础设施问题而非候选证据。
    assert "must-not-run" not in vm.scripts[-1]["code"]
    # 程序源码未上送。


def test_specialized_verifier_executor_infra_aborts_on_first_failure(
        monkeypatch, tmp_path):
    # 测试：专用 Verifier 执行器报告基础设施失败时，循环在第一次就中止——
    # status=infra、恰一次模型调用（不把基础设施失败当回合重试）。
    from core.trace import ArtifactSink

    cfg = _verifier_cfg()
    cfg.max_iters = 20
    cfg.script_timeout = 30
    calls = []

    def fake_chat(*args, **kwargs):
        calls.append((args, kwargs))
        return '{"program":{"lang":"bash","code":"printf inspect"}}'
        # 记录模型调用并返回检查程序。

    monkeypatch.setattr(L, "chat", fake_chat)
    result, _ = L.run_attempt(
        "verifier task", _RecordingVM(), cfg,
        ArtifactSink(str(tmp_path / "infra")),
        program_executor=lambda *args, **kwargs: _runtime_trace(
            "trusted namespace missing", 125, infra_fail=True))
    # 注入专用执行器：恒基础设施失败。

    assert result.status == "infra"
    # 状态：基础设施失败。
    assert result.iters == 1
    # 一轮即止。
    assert len(calls) == 1
    # 恰一次模型调用。


def test_verify_agentic_propagates_infrastructure_instead_of_unverified(
        monkeypatch, tmp_path):
    # 测试：verify_agentic 把基础设施状态如实上抛为
    # AgenticVerifierInfrastructureError——绝不把"验证器自己坏了"降级成
    # 候选的 unverified。
    from core.verifier_runtime import AgenticVerifierInfrastructureError

    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    monkeypatch.setattr(
        L, "run_attempt",
        lambda *args, **kwargs: (SimpleNamespace(
            status="infra", infra_pause_secs=0, infra_pauses=0), []))
    # 伪循环：直接返回 infra 状态。

    with pytest.raises(
            AgenticVerifierInfrastructureError,
            match="emergency/infrastructure status infra"):
        V.verify_agentic(
            "task", _RecordingVM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=None, turn_no=1, session=V.VerifierSession(), wall_budget=50)
    # 必须上抛基础设施错误（而不是返回 unverified）。


def test_agentic_executor_rejects_unknown_language_without_guest_execution():
    # 测试：未知语言（powershell）直接拒绝——exit 126、零 guest 执行。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    trace = AgenticVerifierExecutor(vm)("powershell", "Remove-Item candidate")

    assert trace.exit_code == 126
    assert vm.scripts == []
    # 没有任何脚本发出。


def test_agentic_executor_publishes_report_only_to_host_harness():
    # 测试：verifier-report 语言把报告只发布到宿主 harness——零 guest 脚本
    # 与命令（报告不出 guest 边界）。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    report = "# Independent evidence\nVERDICT: PASS\n"

    trace = executor("verifier-report", report)

    assert trace.exit_code == 0
    assert executor.published_report == report
    # 报告原文发布到宿主。
    assert vm.scripts == [] and vm.commands == []
    # guest 零交互。


def test_agentic_executor_explains_rejected_report_without_losing_it():
    # 测试：报告校验器拒绝报告时——exit 2、说明拒绝原因，但报告原文仍被
    # 记录（不丢失）；合法报告 exit 0。
    from core.verifier_runtime import AgenticVerifierExecutor

    executor = AgenticVerifierExecutor(_RecordingVM())
    executor.set_report_validator(
        lambda report: "ready" if report == "READY" else None,
        "a standalone READY line",
    )
    # 注入校验器：只有"READY"合法，说明是"需要独立 READY 行"。

    rejected = executor("verifier-report", "almost ready")
    assert rejected.exit_code == 2
    assert "NOT accepted" in rejected.stdout
    # 拒绝形状。
    assert executor.published_report == "almost ready"
    # 被拒报告仍被记录（不丢）。

    accepted = executor("verifier-report", "READY")
    assert accepted.exit_code == 0
    # 合法报告接受。


def test_agentic_executor_cleanup_unmounts_only_its_random_workspace():
    # 测试：清理只针对自己的随机工作区——umount/kill -TERM/rmdir 齐备，
    # 关闭幂等（第二次 close 不再发脚本）。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    executor("python", "print('inspect')")
    workspace = executor.workspace
    # 记录随机工作区。

    executor.close()
    executor.close()
    # 关闭两次（幂等）。

    cleanup = vm.scripts[-1]["code"]
    assert cleanup.count(workspace) >= 1
    # 清理脚本针对自己的工作区。
    assert "kill -TERM" in cleanup and "rmdir" in cleanup
    # 杀残留进程 + 删挂载点。
    assert "umount" in _decoded_shell_payload(
        vm.scripts[0]["code"], "keeper_payload")
    # keeper（预检）里带 umount 逻辑。
    assert len(vm.scripts) == 3
    # 总计三个脚本：预检 + 包装 + 清理（第二次 close 不再发）。


@pytest.mark.parametrize("failed_step", ["archive_helper", "archive_fetch"])
@pytest.mark.parametrize("tolerate,rollback_fails", [
    (True, False), (False, False), (True, True),
])
def test_scratch_export_transport_failure_recovers_only_after_rollback(
        monkeypatch, failed_step, tolerate, rollback_fails):
    # 测试（2×3 参数化）：scratch 导出传输失败（helper 或 fetch）只在回滚
    # 成功后才可恢复——回滚失败或不容忍时上抛基础设施错误；无论哪条路径，
    # 会话的执行器与 scratch 归档都被卸下。
    from env import qemu_rollback
    from core.verifier_runtime import (
        AgenticVerifierExecutor, AgenticVerifierInfrastructureError,
    )

    transactions = []

    class Transaction:
        # 伪事务：rollback 可注入失败。
        def __init__(self, _vm):
            self.rolled_back = False
            transactions.append(self)

        def begin(self):
            pass

        def rollback(self):
            if rollback_fails:
                raise RuntimeError("rollback transport unavailable")
                # 注入回滚传输失败。
            self.rolled_back = True

    class VM(_RecordingVM):
        def fetch_file(self, *_args, **_kwargs):
            return None, "controller unavailable during archive fetch"
            # 抓取归档时控制器不可用。

    monkeypatch.setattr(qemu_rollback, "QemuRollbackTransaction", Transaction)
    vm = VM()
    executor = AgenticVerifierExecutor(vm, execution_mode="rollback_mirror")
    assert executor("bash", "printf inspect").exit_code == 0
    # 先正常建立镜像会话。
    monkeypatch.setattr(
        executor, "_run_in_private_mount_namespace",
        lambda *_a, **_kw: _runtime_trace(
            "controller unavailable during archive helper",
            125 if failed_step == "archive_helper" else 0,
            infra_fail=failed_step == "archive_helper"))
    # 注入归档 helper 失败（或仅 fetch 失败）。
    session = V.VerifierSession()
    session._program_executor = executor
    # 会话挂上执行器。

    if rollback_fails or not tolerate:
        reason = "rollback failed" if rollback_fails else "could not archive"
        with pytest.raises(AgenticVerifierInfrastructureError, match=reason):
            session.detach_executor(vm, tolerate_archive_failure=tolerate)
        # 回滚失败或不容忍：必须抛基础设施错误。
    else:
        session.detach_executor(vm, tolerate_archive_failure=True)
        # 回滚成功且容忍归档失败：正常卸下。

    assert transactions[0].rolled_back is (not rollback_fails)
    # 回滚成功与否与注入一致。
    assert session._program_executor is None
    assert session._scratch_archive is None
    # 会话卸下执行器与归档（下一条路从干净边界继续）。


def test_verifier_document_look_routes_renderer_into_isolated_scratch(
        monkeypatch):
    # 测试：文档 look（pptx）把渲染器路由进 Verifier 隔离 scratch——
    # 渲染输出目录用工作区内的 look_render（绝不用共享 /tmp/rsiagent_render），
    # 包装仍带网络隔离与只读重挂载。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    executor = AgenticVerifierExecutor(vm)
    observed = {}

    def fake_fetch_look_image(render_vm, path):
        observed["path"] = path
        observed["output"] = render_vm.run_command(
            "rm -rf /tmp/rsiagent_render; mkdir -p /tmp/rsiagent_render; "
            "printf /tmp/rsiagent_render/page.png")
        return b"pixels", ""
        # 伪渲染：记录路径并返回像素。

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    data, reason = executor.fetch_look_image("/home/user/deck.pptx")

    assert data == b"pixels" and reason == ""
    assert observed["path"] == "/home/user/deck.pptx"
    # look 路径原样传入。
    wrapper = vm.scripts[-1]["code"]
    payload = re.search(r"^payload='([^']+)'$", wrapper, re.MULTILINE).group(1)
    render_program = base64.b64decode(payload).decode()
    assert "/tmp/rsiagent_render" not in render_program
    # 渲染程序不写共享渲染目录。
    assert executor.workspace + "/look_render" in render_program
    # 渲染输出被路由进工作区内的隔离目录。
    assert "--net" in wrapper and "remount,bind,ro" in wrapper
    # 渲染仍在沙箱边界内（网络隔离 + 只读重挂载）。


def test_verifier_private_scratch_look_uses_namespace_bridge_not_global_resolver(
        monkeypatch):
    # 测试：私有 scratch 内的图片 look 走命名空间桥读取（专用 fetch 通道），
    # 不经全局解析器——Actor 可见的挂载命名空间刻意无法解析 scratch 路径；
    # 桥程序在命名空间内自行 resolve 并校验根归属，读后清理临时桥文件。
    from core.verifier_runtime import AgenticVerifierExecutor

    class BridgeVM(_RecordingVM):
        def __init__(self):
            super().__init__()
            self.fetched = []

        def fetch_file(self, path, max_bytes=None):
            self.fetched.append((path, max_bytes))
            assert path.startswith("/tmp/rsiagent_verifier_fetch_")
            assert path.endswith(".bin")
            return b"private pixels", ""
            # 桥通道抓取临时文件。

    vm = BridgeVM()
    executor = AgenticVerifierExecutor(
        vm, private_paths=("/home/user/work",))
    private_image = executor.workspace + "/evidence/frame.png"
    # scratch 内的私有图片。

    def fake_fetch_look_image(render_vm, path):
        assert path == private_image
        return render_vm.fetch_file(path, max_bytes=None)
        # 渲染函数直接经 VM 抓取。

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    data, reason = executor.fetch_look_image(private_image)

    assert data == b"private pixels" and reason == ""
    # 桥读取成功。
    assert len(vm.fetched) == 1
    # 恰一次桥抓取。
    # The Actor-visible mount namespace deliberately cannot resolve scratch.
    # Actor 可见的挂载命名空间刻意无法解析 scratch。
    assert all("readlink -f" not in call["command"] for call in vm.commands)
    # 绝不用全局 readlink 解析（那会走错误命名空间）。
    bridge_program = _decoded_shell_payload(vm.scripts[-1]["code"])
    assert private_image in bridge_program
    # 桥程序处理目标图片路径。
    assert "resolved = source.resolve(strict=True)" in bridge_program
    assert "root not in resolved.parents" in bridge_program
    # 桥内自行 resolve 并校验路径归属根。
    assert any(
        "rm -f -- /tmp/rsiagent_verifier_fetch_" in call["command"]
        for call in vm.commands)
    # 读后清理桥文件。


def test_verifier_screen_look_uses_only_trusted_capture_and_scratch(monkeypatch):
    # 测试：屏幕 look 只用可信捕获与专用 scratch——捕获 VM 不是 Actor VM；
    # 屏幕输出落在一次性 scratch 目录并即时清理；全程无模型程序参与。
    from core.verifier_runtime import AgenticVerifierExecutor

    vm = _RecordingVM()
    observed = {}

    def fake_fetch_look_image(passed_vm, path):
        observed.update(vm=passed_vm, path=path)
        observed["output"] = passed_vm.run_command(
            "mkdir -p /tmp/rsiagent_render; printf /tmp/rsiagent_render/screen.png")
        return b"screen pixels", ""
        # 伪捕获：记录 VM 与路径。

    monkeypatch.setattr(
        "core.imagery.fetch_look_image", fake_fetch_look_image)
    executor = AgenticVerifierExecutor(vm)
    data, reason = executor.fetch_look_image("screen:")

    assert data == b"screen pixels" and reason == ""
    # 捕获成功。
    assert observed["path"] == "screen:"
    # 路径原样传入。
    assert observed["vm"] is not vm
    # 捕获发生在独立的可信 VM 上（不是 Actor VM）。
    assert "/tmp/rsiagent_verifier_screen_" in vm.commands[-1]["command"]
    # 捕获输出落在 Verifier 专用 scratch。
    assert "rm -rf --" in vm.commands[-1]["command"]
    # 用后即清理。
    assert executor.workspace not in vm.commands[-1]["command"]
    # 不写持久工作区。
    assert "/tmp/rsiagent_render" not in vm.commands[-1]["command"]
    # 不写共享渲染目录。
    # Workspace setup is trusted; no model-authored Program ran for the capture.
    # 工作区搭建是可信的；捕获没有运行任何模型编写的程序。
    assert len(vm.scripts) == 1
    # 只有预检一个脚本。


def test_verifier_look_guard_allows_only_local_absolute_or_numeric_screen():
    # 测试：look 路径守卫白名单——本地绝对路径与 screen:/screen:N 放行；
    # URL/file URI/选项注入/相对路径/shell 注入全部拒绝且给原因。
    from core.verifier_runtime import validate_verifier_look_path

    assert validate_verifier_look_path("/home/user/report.pptx") == (True, "")
    assert validate_verifier_look_path("screen:") == (True, "")
    assert validate_verifier_look_path("screen:1.0") == (True, "")
    # 三种合法形态。
    for path in (
        "http://127.0.0.1:8000/report.pptx",
        "file:///home/user/report.pptx",
        "--headless.pptx",
        "relative/report.pdf",
        "screen:0; touch /tmp/escaped",
    ):
        allowed, reason = validate_verifier_look_path(path)
        assert allowed is False and reason
        # 每种非法形态都拒绝且带原因。


def test_verify_agentic_injects_effect_isolated_executor_and_host_report(
        monkeypatch, tmp_path):
    # 测试：verify_agentic 注入效果隔离执行器与宿主报告通道——look 校验器
    # 拒绝 URL；程序在沙箱内执行；verifier-report 发布后交接即就绪；判定的
    # findings 来自宿主报告。
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _RecordingVM()
    observed = {}

    def fake_run_attempt(prompt, passed_vm, cfg, sink, **kwargs):
        observed["same_vm"] = passed_vm is vm
        # 记录是否同一个 VM。
        allowed, _ = kwargs["look_path_validator"](
            "http://127.0.0.1:8000/report.pptx")
        assert allowed is False
        # 注入的 look 校验器拒绝 URL。
        executor = kwargs["program_executor"]
        program = executor(
            "python", "open('/home/user/calendar.ics', 'w').write('bad')", 5)
        assert program.exit_code == 0
        # 注入的程序执行器在沙箱里跑通。
        executor(
            "verifier-report",
            "# Checked current candidate\nVERDICT: FAIL\n"
            "conflicting event remains\n",
            5,
        )
        # 发布宿主报告。
        assert kwargs["terminal_handoff_ready"]()
        # 报告发布后交接就绪。
        history = list(kwargs.get("initial_history") or [])
        history += [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content":
             '{"program":{"lang":"verifier-report","code":"published"}}'},
        ]
        return SimpleNamespace(
            status="done", infra_pause_secs=0, infra_pauses=0), history
        # 返回 done 与完整历史。

    monkeypatch.setattr(L, "run_attempt", fake_run_attempt)

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=None, turn_no=1, session=V.VerifierSession(), wall_budget=50)
    # 驱动 verify_agentic。

    assert observed["same_vm"] is True
    # 用的就是传入的 VM。
    assert verdict == "wrong"
    assert "conflicting event remains" in str(findings)
    # 判定与 findings 来自发布的宿主报告。
    assert any("--net" in call["code"] for call in vm.scripts)
    assert "kill -TERM" in vm.scripts[-1]["code"]
    # 沙箱边界与清理逻辑在包装里。


def test_verify_agentic_real_loop_accepts_python_then_host_report(monkeypatch):
    # 测试：真实循环端到端——先跑 python 检查程序再发宿主报告，pass 判定
    # 与 findings 原样返回；提示明确"可任意 Python/Bash"且无 verifier-read
    # 旧措辞；包装带只读重挂载。
    monkeypatch.setattr("config.settings.load", lambda _path: _verifier_cfg())
    vm = _RecordingVM(program_output="nine events\n")
    replies = iter((
        '{"program":{"lang":"python","code":"from pathlib import Path; '
        'print(Path(\'/home/user/calendar.txt\').read_text())"}}',
        '{"program":{"lang":"verifier-report","code":"# Evidence\\n'
        'Nine events were independently observed.\\nVERDICT: PASS\\n"}}',
    ))
    # 两步脚本：读文件 → 发布 PASS 报告。
    calls = []

    def fake_chat(model, system, user, **kwargs):
        calls.append({"system": system, "user": user})
        return next(replies)
        # 记录调用并按脚本回。

    monkeypatch.setattr(L, "chat", fake_chat)

    verdict, findings = V.verify_agentic(
        "task", vm, SimpleNamespace(agentic_verifier_config="verifier.yaml"),
        sink=None, turn_no=2, session=V.VerifierSession(), wall_budget=50)

    assert verdict == "pass"
    assert "Nine events" in str(findings)
    # 判定与证据来自报告。
    assert all("verifier-read" not in call["system"] for call in calls)
    # 系统提示无 verifier-read 旧措辞。
    assert all("arbitrary Python/Bash" in call["user"] for call in calls)
    # 每轮提示都声明可任意 Python/Bash。
    assert "remount,bind,ro" in vm.scripts[1]["code"]
    # 观察包装只读重挂载候选文件系统。


def test_real_verifier_loop_blocks_repeated_empty_provider_replies(monkeypatch, tmp_path):
    # 测试：供应商连续空回复被阻断——max_consec_dry=2 时每段 2 次空回复、
    # 3 段后抛 AgenticVerifierNoProgressError；每段转写落盘，不产生 verify2。
    from core.trace import ArtifactSink
    from core.verifier_runtime import AgenticVerifierNoProgressError

    cfg = _verifier_cfg()
    cfg.max_consec_dry = 2
    monkeypatch.setattr("config.settings.load", lambda _path: cfg)
    calls = []

    def empty_chat(*_args, **_kwargs):
        calls.append(1)
        assert len(calls) <= 6, "the real loop must stop after three empty segments"
        return ""
        # 恒空回复；超过 6 次即断言失败（上限护栏）。

    monkeypatch.setattr(L, "chat", empty_chat)
    root = tmp_path / "run"
    with pytest.raises(AgenticVerifierNoProgressError):
        V.verify_agentic(
            "task", _RecordingVM(),
            SimpleNamespace(agentic_verifier_config="verifier.yaml"),
            sink=ArtifactSink(str(root)), session=V.VerifierSession(), wall_budget=50)
    # 空回复循环必须以无进展错误终止。

    assert len(calls) == 6
    # 恰 6 次调用（3 段 × 每段 2 次空）。
    assert len(list(root.rglob("transcript.json"))) == 3
    # 三段的转写都落盘。
    assert not list(root.rglob("verify2.json"))
    # 没有重试产物 verify2。


def test_actor_default_program_path_remains_writable(monkeypatch, tmp_path):
    # 测试：Actor 的默认程序路径保持可写——Verifier 的沙箱化不波及 Actor：
    # Actor 的 python 程序直接执行（源码原样上送、lang 原样）。
    from core.trace import ArtifactSink

    vm = _RecordingVM()
    cfg = _verifier_cfg()
    cfg.practice_done_requires = ""
    cfg.max_iters = 2
    cfg.max_consec_done = 2
    cfg.strict_one_action = False
    actor_source = "open('/tmp/actor-ok', 'w').write('x')"
    # Actor 程序写 /tmp。
    replies = iter((
        '{"program":{"lang":"python","code":"' + actor_source + '"}}',
        '{"done":null}',
    ))
    monkeypatch.setattr(L, "chat", lambda *args, **kwargs: next(replies))

    result, _ = L.run_attempt(
        "actor task", vm, cfg, ArtifactSink(str(tmp_path / "actor")))
    # 走普通 Actor 循环（无 program_executor 注入）。

    assert result.status == "done"
    assert len(vm.scripts) == 1
    # 只有一次直接执行（无沙箱包装链）。
    assert vm.scripts[0]["lang"] == "python"
    assert vm.scripts[0]["code"] == actor_source
    # Actor 源码与语言原样直达 VM。


def test_verifier_program_artifacts_use_truthful_extensions(tmp_path):
    # 测试：程序工件按真实语言存扩展名——python→.py、bash→.sh、
    # verifier-report→.md（工件类型可辨识）。
    from core.trace import ArtifactSink

    sink = ArtifactSink(str(tmp_path / "run"))
    sink.save_program(1, "python", "print('inspect')")
    sink.save_program(2, "bash", "ffprobe candidate.mp4")
    sink.save_program(3, "verifier-report", "# Report\nVERDICT: PASS\n")
    # 保存三种语言的程序。

    assert (tmp_path / "run" / "iter_01" / "program.py").is_file()
    assert (tmp_path / "run" / "iter_02" / "program.sh").is_file()
    assert (tmp_path / "run" / "iter_03" / "program.md").is_file()
    # 扩展名与语言一一对应。
