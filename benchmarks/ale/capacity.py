"""Cooperative host-wide reservations, including VMs from the earlier baseline."""
# 【中文注释·模块定位】跨进程协作的主机级容量预留（容量池）。职责：让并行的多个
# RSIAgent 会话（以及早期 baseline 运行可能遗留的 VM 容器）在同一台物理主机上安全
# 共享 VM 槽位、vCPU、内存与磁盘配额，避免资源超卖导致实验互相踩踏。
# 机制：共享目录下用 flock 文件锁串行化"读租约→校验→写租约"临界区；每个持有进程
# 写一份 <token>.json 租约文件，文件中记录持有者 PID，进程死亡即租约自动失效。

# from __future__ import annotations：延迟求值类型注解（本模块注解极少，保持仓库统一写法）。
from __future__ import annotations

# asyncio：在异步事件循环里以轮询睡眠的方式等待容量释放，不阻塞其他任务协程。
import asyncio
# fcntl：POSIX 排他文件锁（flock），是多进程共享租约目录的核心同步原语。
import fcntl
# json：租约文件的序列化与解析。
import json
# os：读取本进程 PID、逻辑 CPU 核数，用 kill(pid,0) 探测租约持有者是否存活。
import os
# shutil：查询磁盘剩余空间（disk_usage）。
import shutil
# time：记录租约创建时间戳（审计用）。
import time
# uuid：生成全局唯一的租约 token，同时充当租约文件名。
import uuid
# Path：共享租约目录与 /proc/meminfo 的路径操作。
from pathlib import Path


# 【中文注释】容量协调器：一份租约 = 一个进程对 VM 槽位/CPU/内存/磁盘的集体预留。
class Capacity:
    # root=共享租约目录；disk=统计剩余空间的磁盘路径；limit=本机 VM 槽位上限（默认 16，对应 --vm-limit）。
    def __init__(self, root, disk, *, limit=16):
        # 保存租约根目录、受监控磁盘与槽位上限（root/disk 统一转 Path）。
        self.root, self.disk, self.limit = Path(root), Path(disk), limit
        # 确保共享租约目录存在（多会话共用，幂等创建）。
        self.root.mkdir(parents=True, exist_ok=True)
        # 本进程租约的唯一标识：租约文件名为 <token>.json。
        self.token = uuid.uuid4().hex

    # 申请资源：count 个 VM 槽位、reserve_gib GiB 磁盘预留、vcpus/memory_gb 为 vCPU/内存预留；
    # 资源不足则轮询等待，直到成功或 stop 事件被置位。
    async def acquire(self, count, reserve_gib, stop, *, vcpus=0, memory_gb=0):
        # 单个阶段需要的槽位超过主机总上限：永远等不到，直接失败而非死等。
        if count > self.limit:
            raise RuntimeError("One phase needs more VM slots than the host limit")
        # 申请的 vCPU 超过物理逻辑核数：同样不可能满足，提前失败。
        if vcpus > (os.cpu_count() or 1):
            raise RuntimeError(
                "This phase requests more vCPUs than this host has logical CPUs"
            )
        # 延迟导入 docker SDK：只有外层 host 进程装有 docker，密封 worker 无此依赖。
        import docker

        # 连接本机 Docker 守护进程（用于枚举容器）。
        client = docker.from_env()
        # 保证 finally 中关闭 docker 连接，不泄漏套接字。
        try:
            # 反复尝试获取资源；外部把 stop 置位即整体中断。
            while not stop.is_set():
                # 打开（不存在则创建）共享锁文件。
                with (self.root / "lock").open("a+") as handle:
                    # 排他锁：以下"读全部租约+判定+写新租约"是临界区。
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    # 收集仍然有效的租约。
                    leases = []
                    # 遍历共享目录中的每个租约文件。
                    for path in self.root.glob("*.json"):
                        # 解析租约内容。
                        data = json.loads(path.read_text())
                        try:
                            # 信号 0 探测：只检查持有者进程是否存活，不真的发信号。
                            os.kill(data["pid"], 0)
                        except ProcessLookupError:
                            # 持有者进程已消失：陈旧租约，直接删除以归还其占用额度（崩溃自愈的关键）。
                            path.unlink()
                            continue
                        except PermissionError:
                            pass  # Another process still owns this live process.
                        # 进程存活，记入有效租约集合。
                        leases.append(data)
                    # 一致性检查：所有共享该池的会话必须约定相同的 VM 上限，
                    # 否则各会话按不同总数瓜分同一主机，额度计算互相矛盾。
                    if any(
                        lease.get("vm_limit", self.limit) != self.limit
                        for lease in leases
                    ):
                        raise RuntimeError(
                            "All sessions sharing the host pool must use the same VM limit"
                        )
                    # 枚举本机当前运行中的全部容器。
                    containers = client.containers.list()
                    # ALE QEMU guests expose their monitor in this container.
                    # 【中文注释】识别"ALE 相关"的存活容器集合（三条启发式任一命中即算）：
                    # ①容器名以 ale-qemu- 开头；②暴露 7100/ 端口（QEMU monitor 端口段）；
                    # ③名字含 ale 且镜像名含 qemu。用于把不属于任何租约的遗留容器也计入占用。
                    live = {
                        c.id
                        for c in containers
                        if c.name.startswith("ale-qemu-")
                        or any(
                            str(p).startswith("7100/")
                            for p in (
                                c.attrs.get("Config", {}).get("ExposedPorts") or {}
                            )
                        )
                        or "ale" in c.name
                        and "qemu"
                        in str(c.attrs.get("Config", {}).get("Image", "")).lower()
                    }
                    # 所有有效租约登记过的容器 ID 并集（attach() 登记的"在管"容器）。
                    managed = {
                        cid for lease in leases for cid in lease.get("containers", [])
                    }
                    # 存活但无租约认领的 ALE 容器数（崩溃残留/早期 baseline 遗留）。
                    unmanaged = len(live - managed)
                    # 已用槽位 = 无主容器数 + 各租约申报数。
                    used = unmanaged + sum(lease["count"] for lease in leases)
                    # 已被各租约预留的磁盘 GiB 总量。
                    reserved = sum(lease["reserve_gib"] for lease in leases)
                    # 目标磁盘当前空闲字节数换算为 GiB。
                    free_gib = shutil.disk_usage(self.disk).free / 1024**3
                    # 从 /proc/meminfo 提取 MemAvailable 行，KiB 换算为 GiB（1024**2）。
                    available_ram = next(
                        int(line.split()[1]) / 1024**2
                        for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:")
                    )
                    # 已预留 vCPU 总数（老租约可能没有该字段，默认 0）。
                    cpu_reserved = sum(lease.get("vcpus", 0) for lease in leases)
                    # 已预留内存 GB 总数。
                    ram_reserved = sum(lease.get("memory_gb", 0) for lease in leases)
                    # 四项资源检查全部通过才签发新租约（任一不满足则等待）：
                    # VM 槽位：已用 + 本次申请不超过上限；磁盘：空闲 ≥ 基线 128GiB + 已预留 + 本次预留；
                    # vCPU：已预留 + 本次申请不超过逻辑核数；内存：已预留 + 本次申请 + 8GiB 系统余量不超过可用内存。
                    if (
                        used + count <= self.limit
                        and free_gib >= 128 + reserved + reserve_gib
                        and cpu_reserved + vcpus <= (os.cpu_count() or 1)
                        and ram_reserved + memory_gb + 8 <= available_ram
                    ):
                        # 构造本进程的租约记录：pid 供死亡检测；count/reserve_gib/vcpus/memory_gb
                        # 为四类占用额度；containers 待 attach() 登记；vm_limit 供跨会话一致性校验。
                        data = {
                            "pid": os.getpid(),
                            "count": count,
                            "reserve_gib": reserve_gib,
                            "containers": [],
                            "created": time.time(),
                            "vcpus": vcpus,
                            "memory_gb": memory_gb,
                            "vm_limit": self.limit,
                        }
                        # 在临界区内整文件覆写，签出租约。
                        (self.root / (self.token + ".json")).write_text(
                            json.dumps(data)
                        )
                        # 成功取得资源配额，返回（锁随 with 块退出自动释放）。
                        return
                # 资源不足：让出事件循环，5 秒后重试（期间可响应 stop）。
                await asyncio.sleep(5)
        finally:
            # 无论成功/中断/异常，都关闭 docker 连接。
            client.close()
        # 走到这里说明 stop 被置位退出了循环：以"中断"结束，让调用方优雅停机。
        raise InterruptedError("Stopped while waiting for host VM capacity")

    # 把实际启动的容器登记进本进程租约：其他会话统计时才能把它们当作"在管"容器。
    def attach(self, container_id):
        # 延迟导入 docker SDK。
        import docker

        # 连接 docker 守护进程。
        client = docker.from_env()
        try:
            # 用 docker 解析并规范化为完整容器 ID（兼容短 ID 输入）。
            container_id = client.containers.get(container_id).id
        finally:
            # 查询完立即关闭连接。
            client.close()
        # 打开共享锁文件。
        with (self.root / "lock").open("a+") as handle:
            # 排他锁保护租约的"读-改-写"。
            fcntl.flock(handle, fcntl.LOCK_EX)
            # 本进程的租约文件。
            path = self.root / (self.token + ".json")
            # 读取现有租约。
            data = json.loads(path.read_text())
            # 追加登记的容器 ID。
            data["containers"].append(container_id)
            # 写回租约文件。
            path.write_text(json.dumps(data))

    # 释放本进程预留的全部资源（阶段结束/异常清理时调用）。
    def release(self):
        # 打开共享锁文件。
        with (self.root / "lock").open("a+") as handle:
            # 排他锁：避免与其他会话的容量判定交错。
            fcntl.flock(handle, fcntl.LOCK_EX)
            # 删除本进程租约文件即释放额度；文件不存在也不报错（幂等）。
            (self.root / (self.token + ".json")).unlink(missing_ok=True)
