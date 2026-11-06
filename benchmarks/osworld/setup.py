"""Disposable OSWorld practice guests and network isolation."""
# 一次性练习客户机（practice guest）的启动与网络出网封印（egress seal）。
from __future__ import annotations
import logging
from typing import Any
# 本模块专用日志器
log = logging.getLogger("rsiagent.practice")

def _boot_vm():
    # 延迟导入 OSWorld 的桌面环境实现
    from desktop_env.desktop_env import DesktopEnv

    # 延迟导入本仓库的 VM 包装层
    from env.vm import VM

    # 派生一个禁用官方评审的桌面环境类：TARGET（目标进化）阶段的硬性不变量
    class NoEvaluationDesktopEnv(DesktopEnv):
        def evaluate(self):  # pragma: no cover - the invariant is never call
            # 绝不允许在练习环境里触发官方 evaluation
            raise RuntimeError(
                "official evaluation is disabled inside TARGET evolution"
            )

    # 启动 Docker 提供的 Ubuntu 无头客户机：pyautogui 动作空间、1920x1080 屏幕、
    # 无需可达性树、60G 磁盘（练习任务需要安装应用）
    environment = NoEvaluationDesktopEnv(
        provider_name="docker",
        action_space="pyautogui",
        os_type="Ubuntu",
        screen_size=(1920, 1080),
        headless=True,
        require_a11y_tree=False,
        volume_size=60,
    )
    # 返回环境本体与其 VM 包装，调用方持有二者
    return environment, VM(environment)


def _egress_receipt(installation, probe) -> dict[str, Any]:
    """Return the security-relevant, JSON-safe seal receipt."""

    # 汇总出网封印（egress seal）安装与探测的安全相关事实，全部为 JSON 可序列化
    return {
        # 回执结构版本
        "schema_version": 1,
        # 封印模式（如 null：拒绝全部公网出网）
        "mode": installation.mode,
        # 安全策略与规则的 SHA-256 指纹（审计用）
        "policy_sha256": installation.policy_sha256,
        "rules_sha256": installation.rules_sha256,
        # 网关 CA 证书与网关叶证书 SPKI 的指纹
        "gateway_ca_sha256": installation.gateway_ca_sha256,
        "gateway_leaf_spki_sha256": installation.gateway_leaf_spki_sha256,
        # 被钉扎（pinned）的上游 IP 白名单
        "pinned_upstream_ips": list(installation.pinned_upstream_ips),
        # 是否强制 IPv6 出网封堵
        "ipv6_enforcement": installation.ipv6_enforcement,
        # 探测（probe）结果：逐项记录各类出网通道的封堵证据
        "probe": {
            # 探测模式
            "mode": probe.mode,
            # 仅允许访问指定站点 HTTPS（exact StreamView）
            "exact_streamview_https": probe.exact_streamview_https,
            # 错误 SNI 同租户探测（防止靠 SNI 混入其他站点）
            "wrong_sni_cotenant": probe.wrong_sni_cotenant,
            # 直连 IP 的 HTTPS 探测
            "direct_ip_https": probe.direct_ip_https,
            # 公网 DNS 解析探测
            "public_dns": probe.public_dns,
            # 外层网关服务探测
            "outer_gateway_service": probe.outer_gateway_service,
            # 公网非 HTTP TCP 探测
            "public_non_http_tcp": probe.public_non_http_tcp,
            # 公网 IPv6 探测
            "public_ipv6": probe.public_ipv6,
            # 探测记录本身的 SHA-256 指纹
            "transcript_sha256": probe.transcript_sha256,
        },
    }


def _reset_null_vm_sealed(
    vm, target_direction: str, egress_seal, prepare_unsealed=None
) -> dict:
    """Create a tool-complete null VM, then deny all guest public egress.

    ``prepare_unsealed`` is a trusted host-side application provisioner.  It
    runs after the ordinary null reset but before the egress seal, without any
    Agent context, target asset, evaluator, or grading surface.  This keeps a
    task-visible application available to Curriculum-authored practice while
    preserving the sealed learning boundary seen by every Agent.
    """

    # 复用练习循环里的“空 VM”重置逻辑（延迟导入避免循环依赖）
    from explore.practice_loop import _reset_null_vm

    # 先做普通空重置（工具齐全但无任务内容）
    result = _reset_null_vm(vm, target_direction)
    if not result.get("ok"):
        # 重置失败：原样返回失败结果
        return result
    try:
        if prepare_unsealed is not None:
            # 可信的宿主侧应用预装器必须是可调用对象
            if not callable(prepare_unsealed):
                raise RuntimeError("null tool provisioner is not callable")
            # 取底层桌面环境
            environment = getattr(vm, "env", None)
            # 取安装控制器（setup controller）
            setup_controller = getattr(environment, "setup_controller", None)
            if setup_controller is None:
                raise RuntimeError("null VM lacks a setup controller for trusted tools")
            # 在出网封印之前执行预装（此阶段无任何 Agent 上下文/资产/评审面）
            prepare_unsealed(setup_controller)
        # 安装出网封印（null 模式：拒绝所有公网出网）
        installation = egress_seal.install(vm, mode="null")
        # 立即探测，验证封印确实生效
        probe = egress_seal.probe(vm, installation)
    except Exception as exc:  # noqa: BLE001 - fail closed at reset boundary
        # 重置边界上失败即封闭（fail closed）：报错返回，不带洞继续
        return {"ok": False, "error": f"null pre-seal boundary failed: {exc}"}
    # 成功：连同封印回执一起返回
    return {"ok": True, "egress": _egress_receipt(installation, probe)}


def _close(environment) -> None:
    # 尽力关闭环境：拿不到 close 方法或不可调用就直接跳过
    close = getattr(environment, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - best-effort after durable state
            # 持久状态已落盘后关闭失败只记日志，不再上抛
            log.exception("environment close failed")
