"""Keep OSWorld's command service alive when one guest program is OOM-killed."""
# 模块目标：当客户机（guest）内某个程序被 OOM 杀死时，保住 OSWorld 的命令服务
# （osworld.service）本身不被 systemd 连带停止，从而让后续命令通道仍然可用。
from __future__ import annotations

import base64
# base64：把 sudo 密码与根脚本编码成纯 ASCII，避免引号/特殊字符破坏 shell 拼接。
import logging
import re

from env.vm import VM

log = logging.getLogger(__name__)
# 模块级日志器，记录 OOM 策略的安装与验证结果。
POLICY_PATH = '/run/systemd/system/osworld.service.d/50-rsiagent-execute-oom.conf'
# systemd drop-in 配置路径常量（文档用途；下方根脚本内写入的正是同一路径）。
READY = 'RSIAGENT_CONTROLLER_OOM_POLICY_CONTINUE'
# 根脚本执行成功后输出的就绪哨兵（sentinel）标记，宿主侧用正则匹配它来确认策略生效。
# 以 root 身份在客户机内执行的幂等脚本（原始字符串，逐段语义）：
# 记录 osworld.service 当前的 MainPID 与 OOMPolicy；若策略尚非 continue，则
# 以“临时文件 + 原子改名”的方式写入 drop-in（[Service] OOMPolicy=continue）并
# daemon-reload；随后校验策略已生效、服务 PID 未变、服务仍活跃，最后打印
# READY 标记行（携带 pid 与 previous），供宿主侧做机械化验证。
_ROOT_SCRIPT = r'''set -eu
unit=osworld.service
before_pid=$(systemctl show "$unit" --value -p MainPID)
test "$before_pid" -gt 0
before_policy=$(systemctl show "$unit" --value -p OOMPolicy)
case "$before_policy" in stop|kill|continue) ;; *) exit 125 ;; esac
systemctl is-active --quiet "$unit"
if [ "$before_policy" != continue ]; then
  install -d -m 0755 /run/systemd/system/osworld.service.d
  destination=/run/systemd/system/osworld.service.d/50-rsiagent-execute-oom.conf
  temporary=$(mktemp /run/systemd/system/osworld.service.d/.rsiagent-oom-XXXXXX)
  trap 'rm -f -- "$temporary"' EXIT
  printf '[Service]\nOOMPolicy=continue\n' > "$temporary"
  chmod 0644 "$temporary"
  if [ -e "$destination" ]; then
    cmp -s "$temporary" "$destination" || exit 125
  else
    mv -T -- "$temporary" "$destination"
  fi
  systemctl daemon-reload
fi
test "$(systemctl show "$unit" --value -p OOMPolicy)" = continue
test "$(systemctl show "$unit" --value -p MainPID)" = "$before_pid"
systemctl is-active --quiet "$unit"
printf 'RSIAGENT_CONTROLLER_OOM_POLICY_CONTINUE pid=%s previous=%s\n' "$before_pid" "$before_policy"
'''


def ensure_controller_oom_policy(desktop):
    """Install before task setup; do not restart the service or change RAM/OOM limits."""
    # 对外入口：必须在任务环境搭建之前调用；只安装 OOM 策略，绝不重启服务，
    # 也绝不改动内存或 OOM 限制（这两点由回执字段显式声明，见下）。
    password = base64.b64encode(desktop.client_password.encode()).decode()
    # 把桌面用户的 sudo 密码 base64 编码，经 stdin 喂给 sudo -S，避免命令行泄露/转义问题。
    payload = base64.b64encode(_ROOT_SCRIPT.encode()).decode()
    # 把根脚本本体也 base64 编码，作为位置参数传入外层包装，避免多层引号嵌套出错。
    script = f'''set -eu
printf %s {password!r} | base64 -d | sudo -S -k -p '' -- bash -ceu '
  printf %s "$1" | base64 -d | bash
' rsiagent-controller-oom {payload!r}
sudo -K
'''
    # 外层脚本：密码解码后经 sudo -S 提权，以 root 运行 `bash -ceu '内层' 标签 参数`；
    # 内层再把 base64 的根脚本解码并交给 bash 执行；末尾 `sudo -K` 清空凭据缓存。
    trace = VM(desktop).run_script('bash', script, timeout=60, cap=0)
    # 通过 VM 通道在客户机内执行该脚本；cap=0 表示输出无损保留（便于审计）。
    match = re.search(r'^' + READY + r' pid=([1-9][0-9]*) previous=(stop|kill|continue)$',
                      trace.stdout, re.MULTILINE)
    # 在输出中查找 READY 哨兵行，并捕获服务 PID（必须非零）与先前的 OOMPolicy 取值。
    if trace.exit_code != 0 or trace.infra_fail or trace.timed_out or not match:
        # 任一失败信号（退出码非 0、基础设施故障、超时、或未匹配到哨兵行）都判为未验证。
        raise RuntimeError('OSWorld controller OOM policy setup was not verified: ' + trace.stdout)
        # fail-closed：抛错并附完整输出，绝不带着未验证的策略继续任务。
    receipt = {'unit': 'osworld.service', 'policy': 'continue',
               'main_pid': int(match[1]), 'previous_policy': match[2],
               'service_restarted': False, 'ram_or_oom_limits_changed': False}
    # 构造回执：单元名、新策略、服务 PID、原策略，并显式声明未重启服务、未改任何限制。
    log.info('OSWorld controller OOMPolicy=continue verified; service PID %s unchanged', match[1])
    # 记录验证成功日志：策略已为 continue 且服务 PID 保持不变。
    return receipt
    # 返回回执字典，供调用方存档/审计。
