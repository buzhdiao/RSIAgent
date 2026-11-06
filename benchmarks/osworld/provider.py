"""Harness-only launch adapter for checkpointable OSWorld Docker/QEMU guests.

This module deliberately lives beside the benchmark entrypoints rather than in
the generic Agent packages.  It changes only the launch profile of subsequently
created Docker environments; no benchmark task, grader, or task-derived data is
read here.
"""
# 【中文注释·模块定位】仅供测试框架（harness）使用的启动适配器：让后续创建的
# OSWorld Docker/QEMU 客体变得"可检查点化"（checkpointable，支持回滚镜像隔离下的
# Verifier 候选保护）。它刻意放在基准入口旁边而非通用 Agent 包里：只改创建环境的
# 启动档案（QEMU 参数、端口绑定、生命周期钩子），不读任何基准任务、判分器或任务派生数据。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值。

import hashlib
# 导入 hashlib：为失败启动的证据容器日志计算 sha256。
import json
# 导入 json：序列化失败启动回执 receipt.json。
import os
# 导入 os：读取环境变量与宿主进程号。
import time
# 导入 time：启动槽超时与回执时间戳。
import uuid
# 导入 uuid：为每次容器启动生成唯一 launch_token。
from contextlib import contextmanager
# 导入 contextmanager：定义"启动槽"上下文管理器。
from pathlib import Path
# 导入 Path：定位诊断目录与脚本文件。

from filelock import FileLock, Timeout
# 导入文件锁：跨进程限制同时冷启动的客体数量，并捕获锁超时。

from env.qemu_rollback import (
    ROLLBACK_MIRROR,
    QemuRollbackError,
    normalize_verifier_execution_mode,
)
# 【中文注释】导入回滚镜像（rollback mirror）相关设施：执行模式常量、专用错误类型、
# 执行模式归一化函数。

_PATCH_MARKER = "_rsiagent_checkpointable_provider_v1"
# 补丁标记属性名：防止对同一提供者重复打补丁（幂等）。
_READ_ONLY_OVMF = (
    "-drive if=pflash,format=raw,unit=0,readonly=on,"
    "file=/usr/share/OVMF/edk2-x86_64-code.fd"
)
# 只读 OVMF 固件参数：把 UEFI 代码镜像以 pflash 只读挂载——快照恢复后固件状态可复现。


@contextmanager
def _boot_slot():
    """Bound simultaneous cold boots across processes, not running guests."""
    # 【中文注释】"启动槽"上下文管理器：跨进程限制同时进行的冷启动数量（2 个），
    # 约束的是启动动作本身而非正在运行的客体；拿不到槽就轮询等待，30 分钟超时。
    directory = Path(f"/tmp/rsiagent-vm-boots-{os.getuid()}")
    # 按用户隔离的锁目录，避免多用户互扰。
    directory.mkdir(mode=0o700, exist_ok=True)
    # 以 0700 权限创建锁目录（仅本用户可访问）。
    deadline = time.monotonic() + 1800
    # 计算等待死线：单调时钟 + 30 分钟。
    slots = [FileLock(str(directory / f"slot-{i}.lock")) for i in range(2)]
    # 两个槽位锁文件：最多允许 2 个进程同时冷启动。
    while True:
        # 轮询直到拿到任一空槽或超时。
        for lock in slots:
            # 逐个尝试非阻塞获取槽锁。
            try:
                lock.acquire(timeout=0)
                # 立即返回的获取尝试。
            except Timeout:
                # 该槽被占用，尝试下一个。
                continue
            try:
                yield
                # 持有槽位，放行被保护的启动流程。
            finally:
                lock.release()
                # 无论成败都释放槽位。
            return
            # 拿到并完成了一次槽位使用，结束。
        if time.monotonic() >= deadline:
            # 等待超过 30 分钟仍未拿到槽：放弃并报错。
            raise TimeoutError("timed out waiting for a shared VM boot slot")
        time.sleep(0.5)
        # 小睡 0.5 秒后重试。


def _prepare_boot_lifecycle(module):
    # 【中文注释】给 DockerProvider 的启动生命周期打补丁：缓存端口分配、守护冷启动
    # 失败（清理+证据+限次重试）、Ubuntu 客体就绪后安装控制器 OOM 策略、守护停止流程。
    provider = module.DockerProvider
    # 取出 OSWorld 的 Docker 提供者类。
    start = getattr(provider, "start_emulator", None)
    # 取原始 start_emulator（可能不存在）。
    if start is None or getattr(start, _PATCH_MARKER, False):
        # 没有该方法或已打过补丁：直接返回（幂等）。
        return
    module.LOCK_TIMEOUT = 300
    # 放宽提供者内部锁超时到 300 秒，容忍排队启动。
    wait_ready = provider._wait_for_vm_ready
    # 保存原始的"等待 VM 就绪"方法。
    used_ports = provider._get_used_ports
    # 保存原始的"获取已用端口"方法。

    def cached_ports(self):
        # 【中文注释】端口分配缓存：仅在本提供者正在分配端口期间（_rsiagent_allocating）
        # 缓存一次结果，避免启动过程中多次扫描得到不一致的端口集合。
        if getattr(self, "_rsiagent_allocating", False):
            # 处于本补丁标记的端口分配窗口内。
            if self._rsiagent_ports is None:
                # 尚未缓存：首次扫描并保存。
                self._rsiagent_ports = used_ports(self)
                # 记录当前已用端口集合。
            return self._rsiagent_ports
            # 返回缓存，保证同一启动过程端口视图一致。
        return used_ports(self)
        # 窗口外保持原始行为。

    def ready(self, timeout=600):
        # 【中文注释】就绪等待包装：冷启动超时且客体归属明确时，先落盘失败证据并
        # 连同匿名卷一起删除客体，再标记"可重试"，最后仍向上抛出原超时错误。
        try:
            return wait_ready(self, timeout=timeout)
            # 正常路径：直接委托原始等待逻辑。
        except TimeoutError as exc:
            # 捕获就绪等待超时。
            owner = os.environ.get("RSIAGENT_VM_OWNER", "")
            # 读取客体归属者环境变量（用于所有权校验）。
            if str(exc) == "VM failed to become ready within timeout period" and owner:
                # Upstream removes this guest before start_emulator propagates
                # the error. Capture cold-boot evidence first, then remove its
                # anonymous volume too. No task setup or Agent ran in this VM.
                # 【中文注释】上游在错误传播前就移除了客体；先抢救冷启动失败证据，
                # 再连同匿名卷一起清理。此 VM 上没有任何任务安装或智能体活动。
                try:
                    _archive_failed_boot(self, owner, timeout)
                    # 归档失败启动证据并清理容器。
                    self._rsiagent_retryable_boot_timeout = True
                    # 标记：本次超时属于可重试的冷启动失败。
                except Exception as diagnostic_error:
                    # 证据归档/清理本身失败只告警，不掩盖原始超时。
                    import logging

                    logging.getLogger(__name__).warning(
                        "cold-boot evidence/cleanup failed: %s", diagnostic_error
                    )
            raise
            # 始终重抛原始超时错误（语义不变）。

    def start_guarded(self, *args, **kwargs):
        # 【中文注释】带守卫的启动：在共享"启动槽"内执行原始启动；失败时强制清理
        # 本提供者自己的失败客体；仅当"首次尝试 + 超时 + 已标记可重试"才重试一次。
        for attempt in range(2):
            # 最多尝试两次（原始启动 + 一次冷启动重试）。
            with _boot_slot():
                # 持有跨进程启动槽（限制同时冷启动数）。
                self._rsiagent_allocating = True
                # 打开端口分配缓存窗口。
                self._rsiagent_ports = None
                # 重置端口缓存。
                self._rsiagent_retryable_boot_timeout = False
                # 重置可重试标记。
                try:
                    return start(self, *args, **kwargs)
                    # 调用原始启动逻辑。
                except BaseException as exc:
                    # 捕获一切异常/退出发出（即使 KeyboardInterrup 也要清理客体）。
                    # Force-remove only this provider's own failed guest.
                    # 【中文注释】只强制删除本提供者自己的失败客体。
                    container = self.container
                    # 取当前容器句柄。
                    if container is not None:
                        # 存在容器才需要清理。
                        try:
                            container.remove(force=True, v=True)
                            # 强制删除容器并连同匿名卷。
                        except Exception as cleanup_error:
                            # 清理失败只告警（后续 stop 流程还有机会重试）。
                            import logging

                            logging.getLogger(__name__).warning(
                                "failed-boot cleanup: %s", cleanup_error
                            )
                        self.container = None
                        # 清空容器引用，保持状态一致。
                    if not (
                        attempt == 0
                        and isinstance(exc, TimeoutError)
                        and self._rsiagent_retryable_boot_timeout
                    ):
                        # 非首次尝试、非超时、或未标记可重试——都不重试。
                        raise
                    # 落到此处的组合（首次+超时+可重试）将进行第二次循环。
                finally:
                    self._rsiagent_allocating = False
                    # 关闭端口分配缓存窗口。
                    self._rsiagent_ports = None
                    # 清空端口缓存。
            # Retry the pristine provider launch, never Actor/Verifier actions
            # or task setup. Release/reacquire the shared two-boot admission.
            # 【中文注释】重试的只是"全新提供者启动"：绝不重放 Actor/Verifier 动作或
            # 任务安装；槽位先释放再重新竞争，公平接入共享的双启动配额。

    setattr(start_guarded, _PATCH_MARKER, True)
    # 给新启动方法打补丁标记（幂等识别）。
    provider.start_emulator = start_guarded
    # 替换提供者的启动入口。
    provider._wait_for_vm_ready = ready
    # 替换就绪等待方法。
    provider._get_used_ports = cached_ports
    # 替换端口枚举方法。
    finalize = getattr(provider, "finalize_volume", None)
    # 取原始 finalize_volume（可能不存在）。
    if finalize is not None:
        # 存在该方法才打补丁。

        def finalize_with_controller_oom_policy(
            self,
            path_to_vm,
            volume_size,
            os_type,
            controller,
            setup_controller,
            client_password,
        ):
            # 【中文注释】finalize_volume 包装：先执行原始收尾，再对 Ubuntu 客体安装
            # 控制器 OOM（内存不足）防护策略。
            result = finalize(
                self,
                path_to_vm,
                volume_size,
                os_type,
                controller,
                setup_controller,
                client_password,
            )
            # 调用原始收尾逻辑（创建磁盘、返回路径等）。
            if os_type == "Ubuntu":
                # This runs after pristine guest readiness, before task setup
                # can restrict desktop sudo and before any Agent/QEMU snapshot.
                # 【中文注释】此步发生在全新客体就绪之后、任务安装收紧桌面 sudo 之前、
                # 任何智能体活动或 QEMU 快照之前——是安装防护策略的安全窗口。
                from types import SimpleNamespace

                from env.controller_oom import ensure_controller_oom_policy

                desktop = SimpleNamespace(
                    provider=self,
                    controller=controller,
                    client_password=client_password,
                )
                # 用轻量对象拼出策略安装所需的桌面句柄（提供者/控制器/口令）。
                self._rsiagent_controller_oom_policy = ensure_controller_oom_policy(
                    desktop
                )
                # 安装并保存控制器 OOM 策略句柄。
            return result
            # 原样返回原始收尾结果。

        provider.finalize_volume = finalize_with_controller_oom_policy
        # 替换 finalize_volume 入口。
    stop = getattr(provider, "stop_emulator", None)
    # 取原始 stop_emulator（可能不存在）。
    if stop is not None:
        # 存在该方法才打补丁。

        def stop_guarded(self, *args, **kwargs):
            # 【中文注释】带守卫的停止：优雅停止失败则告警；随后总是强制删除容器
            # （连同匿名卷），并清空端口等状态字段。
            owned = self.container
            # 取本提供者拥有的容器句柄。
            if owned is None:
                # 没有容器就无事可做。
                return
            try:
                owned.stop(timeout=10)
                # 先尝试 10 秒内优雅停止。
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "graceful guest stop failed; forcing owned cleanup: %s", exc
                )
                # 优雅停止失败：告警后转入强制清理。
            # Retain the handle if removal fails so a later cleanup can retry.
            # Disposable guests own their anonymous /storage volumes.
            # 【中文注释】若删除失败则保留句柄供后续清理重试；一次性客体自持有匿名
            # /storage 卷，因此删除时必须 -v 连卷清理。
            owned.remove(force=True, v=True)
            # 强制删除容器及其匿名卷。
            self.container = None
            # 清空容器引用。
            self.server_port = self.vnc_port = None
            # 清空服务与 VNC 端口。
            self.chromium_port = self.vlc_port = None
            # 清空 Chromium 与 VLC 端口。

        provider.stop_emulator = stop_guarded
        # 替换停止入口。


def _archive_failed_boot(provider, owner, timeout):
    """Host-only evidence and positive ownership before a bounded cold retry."""
    # 【中文注释】仅宿主侧的失败启动取证与正向归属确认（随后才允许有界冷重试）：
    # 校验容器标签归属→落盘容器日志与回执 JSON→连同匿名卷删除容器→更新回执。
    container = provider.container
    # 取失败客体容器。
    container.reload()
    # 重新加载容器属性，拿到最新状态与标签。
    labels = container.labels
    # 读取容器标签。
    if (
        labels.get("rsiagent.owner") != owner
        or labels.get("rsiagent.host_pid") != str(os.getpid())
        or not labels.get("rsiagent.launch_token")
    ):
        # 归属三重校验：owner、宿主 PID、launch_token 任一不符即拒绝操作
        # （绝不清理/取证别人的客体）。
        raise QemuRollbackError("failed boot ownership mismatch")
    root = Path(
        os.environ.get(
            "RSIAGENT_BOOT_DIAGNOSTICS_DIR",
            str(Path(__file__).resolve().parents[2] / "results/boot_failures"),
        )
    )
    # 诊断根目录：优先取环境变量，否则用仓库 results/boot_failures。
    root.mkdir(parents=True, exist_ok=True)
    # 确保诊断根目录存在。
    receipt = root / (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + "_"
        + labels["rsiagent.launch_token"]
    )
    # 回执目录名：UTC 时间戳 + 本次启动唯一令牌（唯一且可追溯）。
    receipt.mkdir(mode=0o700)
    # 以 0700 权限创建回执目录（仅本用户可读）。
    log = container.logs(stdout=True, stderr=True)
    # 抓取容器完整 stdout/stderr 日志字节。
    (receipt / "container.log").write_bytes(log)
    # 落盘容器日志。
    # 组装失败启动回执内容：owner=归属者；host_pid=宿主进程号；container_id=容器 ID；
    # labels=容器全部标签；timeout_seconds=触发超时的秒数；state=容器运行状态快照；
    # ports=端口映射快照；log_sha256=日志内容 sha256（防事后篡改的完整性指纹）；
    # stage=阶段声明（失败发生在提供者启动期，早于任务安装与智能体活动）；
    # removed_with_anonymous_volume=初始标记（尚未连卷删除）。
    details = {
        "owner": owner,
        "host_pid": os.getpid(),
        "container_id": container.id,
        "labels": labels,
        "timeout_seconds": timeout,
        "state": container.attrs.get("State", {}),
        "ports": container.attrs.get("NetworkSettings", {}).get("Ports"),
        "log_sha256": hashlib.sha256(log).hexdigest(),
        "stage": "provider_boot_before_task_setup_or_agent",
        "removed_with_anonymous_volume": False,
    }
    path = receipt / "receipt.json"
    # 回执文件路径。
    path.write_text(json.dumps(details, indent=2) + "\n")
    # 先写入"删除前"回执。
    container.remove(force=True, v=True)
    # 强制删除容器并连同匿名卷（消除失败客体的全部残留）。
    provider.container = None
    # 清空容器引用。
    provider.server_port = provider.vnc_port = None
    # 清空服务与 VNC 端口。
    provider.chromium_port = provider.vlc_port = None
    # 清空 Chromium 与 VLC 端口。
    details["removed_with_anonymous_volume"] = True
    # 更新标记：已连卷删除。
    path.write_text(json.dumps(details, indent=2) + "\n")
    # 覆盖写入"删除后"回执（两阶段回执保证删除事实可审计）。


class _LoopbackContainers:
    """Keep the provider's host ports local without editing the release tree."""
    # 【中文注释】容器集合代理：不改上游发布树，仅在容器创建时做三件事——
    # dnsmasq 启动重试包装（可选）、注入归属标签（可选）、把宿主端口绑定收紧到 127.0.0.1。

    def __init__(self, collection):
        # 构造时保存被代理的容器集合对象。
        self._collection = collection
        # 内部持有真实集合，其余属性全部转发。

    def __getattr__(self, name):
        # 除 run 外的所有属性访问都转发给真实集合。
        return getattr(self._collection, name)
        # 委托返回，保持行为透明。

    def run(self, *args, **kwargs):
        # 【中文注释】包装 containers.run：注入启动令牌/归属标签、回环端口绑定，
        # 启动异常时按唯一令牌清理"已创建但未返回句柄"的孤儿容器。
        owner = os.environ.get("RSIAGENT_VM_OWNER")
        # 读取归属者环境变量（可能为空）。
        if os.environ.get("RSIAGENT_DNSMASQ_START_RETRY") == "1":
            # A transient host inotify quota failure makes qemu-docker silently
            # fall back to networking without host port forwarding. Retry only
            # that dnsmasq startup error inside the new container.
            # 【中文注释】宿主 inotify 配额瞬时耗尽会让 qemu-docker 静默退化为无端口
            # 转发的网络；仅针对 dnsmasq 启动错误启用容器内重试包装。
            wrapper = (
                Path(__file__).resolve().parents[2] / "tools/dnsmasq_start_retry.sh"
            )
            # 定位重试包装脚本。
            environment = dict(kwargs.get("environment") or {})
            # 复制容器环境变量。
            if environment.get("DNSMASQ", "/usr/sbin/dnsmasq") != "/usr/sbin/dnsmasq":
                # 上游换了自定义 dnsmasq 启动器：超出本补丁预期，拒绝。
                raise QemuRollbackError("unexpected custom dnsmasq launcher")
            kwargs["environment"] = {
                **environment,
                "DNSMASQ": "/run/rsiagent-dnsmasq-start",
            }
            # 把 dnsmasq 启动器指向容器内的重试包装入口。
            kwargs["volumes"] = {
                **(kwargs.get("volumes") or {}),
                str(wrapper): {"bind": "/run/rsiagent-dnsmasq-start", "mode": "ro"},
            }
            # 以只读方式把包装脚本挂载进容器。
        launch_token = uuid.uuid4().hex if owner else None
        # 归属明确时生成本次启动的唯一令牌。
        if owner:
            kwargs["labels"] = {
                **(kwargs.get("labels") or {}),
                "rsiagent.owner": owner,
                "rsiagent.host_pid": str(os.getpid()),
                "rsiagent.launch_token": launch_token,
            }
            # 注入归属标签：owner、宿主 PID、启动令牌（正向归属，供失败取证/清理）。
        ports = kwargs.get("ports")
        # 取宿主端口映射请求。
        if ports:
            kwargs["ports"] = {
                port: ("127.0.0.1", binding) if isinstance(binding, int) else binding
                for port, binding in ports.items()
            }
            # 把所有整数绑定改为仅绑定 127.0.0.1：客体端口不出本机（回环化）。
        try:
            return self._collection.run(*args, **kwargs)
            # 调用真实集合创建并启动容器。
        except Exception:
            # Docker can create a container and then time out starting it before
            # returning its handle. The unique token identifies only this launch.
            # 【中文注释】Docker 可能已创建容器却在返回句柄前启动超时；唯一令牌
            # 只标识本次启动，用它找出并清理孤儿容器，不影响他人客体。
            if launch_token:
                # 有令牌才做定向清理。
                try:
                    for container in self._collection.list(
                        all=True,
                        filters={"label": f"rsiagent.launch_token={launch_token}"},
                    ):
                        container.remove(force=True, v=True)
                        # 按令牌过滤并强制删除（连匿名卷）。
                except Exception as cleanup_error:
                    import logging

                    logging.getLogger(__name__).warning(
                        "incomplete container launch cleanup: %s", cleanup_error
                    )
                    # 清理失败只告警，不吞掉原始异常。
            raise
            # 重抛原始异常。


class _LoopbackClient:
    # 【中文注释】Docker 客户端代理：仅把 containers 属性换成回环化代理，其余全部转发。
    def __init__(self, client):
        # 保存被代理的真实客户端。
        self._client = client
        # 内部持有原客户端。

    def __getattr__(self, name):
        # 其余属性一律转发（api、images、info 等）。
        return getattr(self._client, name)
        # 委托返回。

    @property
    def containers(self):
        # 覆写 containers 属性：返回回环化的容器集合代理。
        return _LoopbackContainers(self._client.containers)
        # 每次访问都包装当前集合对象。


def prepare_checkpointable_docker_provider(mode: str | None) -> None:
    """Make future Docker guests checkpointable when mirror mode is selected."""
    # 【中文注释】按执行模式装配"可检查点化"的 Docker 提供者：
    # 仅当选择了 rollback_mirror（回滚镜像）模式时打补丁；此后创建的 Docker 客体
    # 支持 QEMU 快照/恢复，供 Verifier 候选保护使用。
    if normalize_verifier_execution_mode(mode) != ROLLBACK_MIRROR:
        # 非 mirror 模式不需要检查点能力：原样返回。
        return

    from desktop_env.providers.docker import provider as provider_module
    # 延迟导入 OSWorld 的 Docker 提供者模块。

    DockerProvider = provider_module.DockerProvider
    # 取提供者类。

    current = DockerProvider.__init__
    # 保存原始构造函数。
    if getattr(current, _PATCH_MARKER, False):
        # 已打过补丁则幂等返回。
        return

    def checkpointable_init(self, region):
        # 【中文注释】可检查点化构造：先跑原始构造，再调整 Docker 客户端超时、
        # 回环化端口、注入 QEMU 参数（invtsc/migratable、只读 OVMF、可选 virtio 通道）。
        current(self, region)
        # 先执行原始构造逻辑。
        if hasattr(self.client, "api"):
            self.client.api.timeout = 300
            # 放宽 Docker API 超时到 300 秒（快照/恢复操作较慢）。
        self.client = _LoopbackClient(self.client)
        # 用回环化代理包装客户端（端口仅绑本机）。
        existing_flags = str(self.environment.get("CPU_FLAGS", "") or "")
        # 读取既有 CPU 标志配置。
        flags = [part for part in existing_flags.split(",") if part]
        # 拆分并去掉空段。
        flags.extend(["-invtsc", "migratable=yes"])
        # 追加 invariant TSC 与可迁移标志：保证计时器跨快照恢复一致。
        existing_arguments = str(self.environment.get("ARGUMENTS", "") or "").strip()
        # 读取既有 QEMU 附加参数。
        if "if=pflash" in existing_arguments:
            # 既有参数已定义 pflash 设备：与本补丁的只读 OVMF 冲突，拒绝。
            raise QemuRollbackError(
                "custom Docker QEMU arguments already define a pflash device"
            )
        arguments = " ".join(
            part for part in (existing_arguments, _READ_ONLY_OVMF) if part
        )
        # 合并既有参数与只读 OVMF 参数（各自非空才拼接）。
        if os.environ.get("RSIAGENT_TRUSTED_VERIFIER_TRANSPORT") == "virtio":
            # 启用可信 Verifier virtio 传输时追加专用 QEMU 参数。
            from env.verifier_control import QEMU_ARGUMENTS

            arguments += " " + QEMU_ARGUMENTS
            # 追加受控串口/virtio 通道参数。
        # 更新启动环境变量：CPU_MODEL=host（使用宿主 CPU 模型，快照恢复后指令集一致）、
        # CPU_FLAGS=合并后的 CPU 标志、BOOT_MODE=legacy（传统启动模式，配合只读 pflash）、
        # ARGUMENTS=合并后的 QEMU 附加参数。下方英文注释为原文件自带：
        # 压制 qemu-docker 自带的可写 pflash 挂载，同一 OVMF 代码镜像已在
        # 上面 ARGUMENTS 中以只读方式重新挂载。
        self.environment.update(
            {
                "CPU_MODEL": "host",
                "CPU_FLAGS": ",".join(flags),
                # Suppress qemu-docker's writable -pflash attachment. The same OVMF
                # code image is reattached read-only through ARGUMENTS above.
                "BOOT_MODE": "legacy",
                "ARGUMENTS": arguments,
            }
        )

    setattr(checkpointable_init, _PATCH_MARKER, True)
    # 给新构造打补丁标记。
    setattr(checkpointable_init, "_rsiagent_original", current)
    # 记录原始构造函数（便于审计/还原）。
    DockerProvider.__init__ = checkpointable_init
    # 替换构造入口。
    _prepare_boot_lifecycle(provider_module)
    # 继续给启动/停止生命周期打补丁。
    if os.environ.get("RSIAGENT_TRUSTED_VERIFIER_TRANSPORT") == "virtio":
        # virtio 传输模式下还需要接管任务安装入口。
        from desktop_env.desktop_env import DesktopEnv

        from env.verifier_control import provision_verifier_control

        original_setup = DesktopEnv._setup_task
        # 保存原始任务安装方法。

        def setup_with_verifier_control(self, task_config, use_proxy):
            # reset() can replace the VM. Provision each fresh container before
            # benchmark setup locks down the desktop user's privileges.
            # 【中文注释】reset() 可能更换 VM；必须在基准安装收紧桌面用户权限之前，
            # 为每个新容器预配 Verifier 控制通道。
            container_id = self.provider.container.id
            # 取当前容器 ID。
            if getattr(self, "_rsiagent_control_container_id", None) != container_id:
                # 该容器尚未预配控制通道。
                provision_verifier_control(self)
                # 预配 Verifier 控制设施（virtio 通道端点等）。
                self._rsiagent_control_container_id = container_id
                # 记录已预配的容器 ID，避免重复预配。
            result = original_setup(self, task_config, use_proxy)
            # 执行原始任务安装。
            return result
            # 原样返回结果。

        DesktopEnv._setup_task = setup_with_verifier_control
        # 替换任务安装入口。


__all__ = ["prepare_checkpointable_docker_provider"]
# 模块公开接口：仅导出补丁安装入口。
