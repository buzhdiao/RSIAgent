"""Prepare pinned local images and check host-specific transport readiness."""
# 【中文注释·模块定位】运行前准备：把固定版本（pinned）的 ALE 客户机镜像落到本地
# 缓存，并检查这台宿主机特有的就绪条件（KVM、docker、checkpoint 运行镜像）。
# 产物是 rsiagent-readiness/images.json 记录：后续 run/smoke 必须核对同一镜像指纹，
# 保证准备、冒烟与正式运行用的是同一套镜像（实验可复现的基础）。

# json：读写镜像就绪记录。
import json
# os：检查 /dev/kvm 的访问权限。
import os
# subprocess：调用 docker CLI 构建与检查镜像。
import subprocess
# Path：缓存与记录文件路径。
from pathlib import Path

# host：复用镜像版本号、ALE 路径安装与 provider 配置构造。
from .host import IMAGE_REVISION, install_ale_path, provider_config
# protocol：仓库根、协议校验、源码指纹与原子写盘。
from .protocol import RSIAGENT, protocol, source_fingerprint, write_json


# 准备动作：校验协议与硬件条件 → 构建 runner 镜像 → 下载/校验客户机磁盘 → 记录就绪信息。
async def prepare(args):
    # 把上游 ALE 仓库挂上 sys.path 并做隔离检查（禁止宿主进程先导入了 RSIAgent core）。
    install_ale_path(args.ale_root)
    # 校验固定协议：角色配置无漂移、67 任务队列完整、ALE 提交与任务卡一致。
    protocol(args.ale_root)
    # QEMU 硬件加速依赖 KVM：必须可读写 /dev/kvm，否则虚拟机性能不可接受。
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError(
            "Read/write access to /dev/kvm is required; grant this user the kvm group and start a new login"
        )
    # 延迟导入 docker SDK：仅准备/宿主环境需要。
    import docker

    # 连接 docker 守护进程。
    client = docker.from_env()
    # ping 一次确认守护进程可用。
    client.ping()
    # 用完即关。
    client.close()
    # 构建 checkpoint 运行镜像（worker 进程运行其上的可回滚容器镜像）；失败即中止。
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            args.runner_image,
            str(RSIAGENT / "benchmarks/ale/checkpoint_runner"),
        ],
        check=True,
    )
    # 延迟导入上游 QEMU provider（sys.path 已就位）。
    from ale_run.environments.providers.qemu import QemuProvider

    # 用固定 provider 配置实例化 QEMU 供给器。
    provider = QemuProvider(provider_config(args.cache, args.runner_image))
    # 选择本次准备的操作系统的快照（linux→Ubuntu 镜像，windows→Win10 镜像）。
    snapshot = provider.config.snapshots[
        "cpu-free-ubuntu" if args.os == "linux" else "cpu-free"
    ]
    # 解析（必要时下载并校验）该快照的客户机磁盘镜像，返回本地路径。
    disk = await provider._resolve_disk(snapshot)
    # 就绪记录目录：<cache>/rsiagent-readiness。
    ready = Path(args.cache) / "rsiagent-readiness"
    # 幂等创建。
    ready.mkdir(parents=True, exist_ok=True)
    # 读取刚构建镜像的完整 ID（sha256 摘要），作为后续一致性比对的指纹。
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
        text=True,
    ).strip()
    # 就绪记录文件路径。
    record_path = ready / "images.json"
    # 已落盘的各 OS 磁盘路径（默认空，随后合并）。
    disks = {}
    # 已有旧记录且镜像版本与 runner 镜像都未变：沿用其中的磁盘路径，避免重复下载。
    if record_path.is_file():
        previous = json.loads(record_path.read_text())
        if (
            previous.get("revision") == IMAGE_REVISION
            and previous.get("runner_image") == image_id
        ):
            disks.update(previous["disks"])
    # 记录本次准备的 OS 对应的磁盘路径。
    disks[args.os] = str(disk)
    # 写就绪记录：镜像版本、runner 镜像 ID、各 OS 磁盘路径、校验方式说明。
    write_json(
        ready / "images.json",
        {
            "revision": IMAGE_REVISION,
            "runner_image": image_id,
            "disks": disks,
            "verified_by": "pinned upstream QEMU provider checksum validation",
        },
    )
    # 打印就绪状态与记录路径。
    print(
        json.dumps(
            {"status": "images_ready", "record": str(ready / "images.json")}, indent=2
        )
    )


# 运行前就绪复核：worker 解释器、镜像记录、冒烟结果、KVM 权限逐项校验。
def validate_readiness(args, manifest):
    # --worker-python 必须指向已准备好的 RSIAgent 独立虚拟环境里的解释器（双环境隔离）。
    if not Path(args.worker_python).is_file():
        raise RuntimeError(
            "--worker-python does not point to the prepared RSIAgent environment"
        )
    # 就绪记录目录。
    root = Path(args.cache) / "rsiagent-readiness"
    # 读取镜像就绪记录。
    images = json.loads((root / "images.json").read_text())
    # 本次运行清单中状态为 ready 的任务涉及哪些操作系统。
    platforms = {t["os"] for t in manifest["tasks"] if t["status"] == "ready"}
    # 记录里的镜像版本必须一致，且每个涉及的 OS 都有磁盘文件在位。
    if images.get("revision") != IMAGE_REVISION or any(
        platform not in images["disks"] or not Path(images["disks"][platform]).is_file()
        for platform in platforms
    ):
        raise RuntimeError("Pinned image readiness record is invalid")
    # 重新读取当前 runner 镜像 ID，必须与准备阶段记录一致（镜像未被替换）。
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.runner_image],
        text=True,
    ).strip()
    if image_id != images["runner_image"]:
        raise RuntimeError("Checkpoint runner image changed since preparation")
    # 当前源码指纹：冒烟必须是在这份源码上通过的。
    fingerprint = source_fingerprint()
    # 每个涉及的 OS 的冒烟结果都必须通过且绑定同一源码指纹与同一 runner 镜像。
    for os_type in platforms:
        result = json.loads((root / f"smoke-{os_type}.json").read_text())
        if (
            result.get("status") != "passed"
            or result.get("source_sha256") != fingerprint
            or result.get("runner_image") != image_id
        ):
            raise RuntimeError(
                f"{os_type} smoke must pass on this exact release and runner image"
            )
    # 最后再查一次 /dev/kvm：防止准备之后权限被收回。
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("/dev/kvm access was lost")
