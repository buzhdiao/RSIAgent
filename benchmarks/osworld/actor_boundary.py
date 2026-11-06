"""OSWorld-specific pre-Actor boundary for removing evaluator source leaks.

OSWorld setup payloads may accidentally unpack task-class Python files beside the
authoritative user inputs. Those files are neither inputs nor candidate state: they
contain evaluator implementation details that no Agent should observe. This module
quarantines only files mechanically identified as OSWorld ``BaseTask`` sources (and
their compiled siblings), before S0 is captured and before Verifier orientation.
"""
# 【中文注释·模块定位】OSWorld 专用的"pre-Actor 边界"：在智能体（Actor）开始工作之前，
# 把 OSWorld 任务配置包可能误释放到客户机里的评测器（evaluator）源码文件隔离出去。
# 这些 .py/.pyc 文件属于评测实现细节，既不是任务输入也不是候选状态，任何智能体都不应观察到；
# 隔离必须发生在 S0（任务开始基线快照）捕获与 Verifier 定向（orientation）之前，
# 否则评测逻辑可能泄漏进智能体的可见世界。整个过程是"可信安装操作"，绝非智能体动作。

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：类型注解不在导入时求值，避免循环导入并兼容新语法。

import base64
# 导入 base64：对路径清单与 sudo 口令做 ASCII 安全编码，便于嵌入脚本传输。
import json
# 导入 json：解析扫描脚本的逐行 JSON 输出记录。
import posixpath
# 导入 posixpath：用 POSIX 规则规范化客户机（Linux）路径。
from typing import Any
# 导入 Any：为函数签名中的动态类型标注使用。

# 【中文注释】扫描脚本（以原始字符串内嵌，将在客户机内用 python 执行）：
# 遍历 /home/user 下所有 .py/.pyc 文件，通过"内容是否包含 desktop_env.task_base 与 BaseTask"
# 这一机械判定找出 OSWorld 任务基类源码及其编译产物（包括 __pycache__ 中的同名 .pyc），
# 并逐行输出 {path, bytes, sha256} 的 JSON 记录。判定只看文件内容特征，不看文件名，
# 因此不会误伤无关文件；脚本本身不含任何评测语义。
_SCAN_SCRIPT = r"""\
import glob
import hashlib
import json
import os

root = "/home/user"
found = set()
for directory, directories, names in os.walk(root, followlinks=False):
    directories[:] = [
        name for name in directories
        if name not in {"node_modules", ".git"}
        and not os.path.islink(os.path.join(directory, name))
    ]
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path) or not name.endswith((".py", ".pyc")):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if (b"desktop_env.task_base" not in data
                or b"BaseTask" not in data):
            continue
        found.add(path)
        if name.endswith(".py"):
            stem = name[:-3]
            found.update(glob.glob(os.path.join(directory, stem + ".pyc")))
            found.update(glob.glob(
                os.path.join(directory, "__pycache__", stem + ".*.pyc")))

for path in sorted(found):
    if (not path.startswith(root + os.sep) or os.path.islink(path)
            or not os.path.isfile(path)):
        continue
    with open(path, "rb") as handle:
        data = handle.read()
    print(json.dumps({
        "path": path,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }, sort_keys=True))
"""


def _scan_records(stdout: str) -> list[dict[str, Any]]:
    # 【中文注释】解析扫描脚本的标准输出：逐行还原 JSON 记录并做严格校验。
    records = []
    # 初始化记录列表：保存通过校验的 {path, bytes, sha256} 三元组。
    for raw in str(stdout or "").splitlines():
        # 遍历输出的每一行（容忍空输入）。
        line = raw.strip()
        # 去除首尾空白，便于判空与识别包装行。
        if not line or line.startswith("[exit "):
            # 跳过空行与运行包装器附加的 "[exit N]" 状态行，它们不是数据记录。
            continue
        try:
            record = json.loads(line)
            # 尝试把该行解析为 JSON 对象。
        except json.JSONDecodeError as exc:
            # 解析失败说明可信扫描输出被污染或格式损坏。
            raise RuntimeError(
                "trusted evaluator-source scan returned malformed output"
            ) from exc
        path = str(record.get("path") or "")
        # 取出 path 字段（缺省为空串），后续校验其形态。
        normalized = posixpath.normpath(path)
        # 规范化路径，用于检测 ".." 等逃逸写法。
        if (
            not normalized.startswith("/home/user/")
            or normalized != path
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            # 严格校验：路径必须位于 /home/user/ 下、不得含需规范化成分（防路径穿越）、
            # bytes 必须是整数、sha256 必须是 64 位十六进制字符串。
            raise RuntimeError(
                "trusted evaluator-source scan returned an invalid record"
            )
        # 校验通过后仅保留三个白名单字段，构造干净记录。
        records.append(
            {
                "path": path,
                "bytes": record["bytes"],
                "sha256": record["sha256"],
            }
        )
    return records
    # 返回全部通过校验的评测器源码记录。


def _encoded_paths_payload(records: list[dict[str, Any]]) -> str:
    """Encode one newline-terminated record per path for a POSIX read loop."""
    # 【中文注释】把每条路径单独 base64 编码并以换行结尾，便于 POSIX shell 的 read 循环
    # 逐条解码处理（避免空格/特殊字符破坏 shell 词法）。
    return "".join(
        base64.b64encode(record["path"].encode("utf-8")).decode("ascii") + "\n"
        for record in records
    )


def quarantine_actor_visible_evaluators(vm) -> list[dict[str, Any]]:
    """Move mechanically recognized evaluator files behind a root-only boundary.

    The scan and move are trusted setup operations, never Agent actions. No content
    or path is placed in either Agent's prompt. Failure is fail-closed: a target
    attempt must not begin if detected evaluator bytes remain Actor-visible.
    """
    # 【中文注释】把机械识别出的评测器文件移入仅 root 可读的隔离目录（信任边界操作）。
    # 扫描与搬移都是可信安装步骤，绝不是智能体动作；路径与内容不进入任何智能体提示词。
    # 失败即封闭（fail-closed）：只要被检出的评测器字节仍对 Actor 可见，目标尝试就不得开始。
    scan = vm.run_script("python", _SCAN_SCRIPT, timeout=180, cap=0)
    # 在客户机内执行扫描脚本（180 秒超时，cap=0 表示不截断输出）。
    if getattr(scan, "infra_fail", False) or getattr(scan, "exit_code", None) != 0:
        # 基础设施失败或退出码非 0 都视为无法建立边界。
        raise RuntimeError(
            "could not establish the pre-Actor evaluator-isolation boundary: "
            + str(getattr(scan, "stdout", "") or "scan failed")
        )
    records = _scan_records(getattr(scan, "stdout", ""))
    # 解析并校验扫描结果，得到待隔离文件清单。
    if not records:
        # 没有检出任何评测器源码：无需隔离，直接返回空列表。
        return []

    paths_payload = _encoded_paths_payload(records)
    # 把路径清单编码成可被 shell 逐行读取的 base64 载荷。
    password = str(getattr(getattr(vm, "env", None), "client_password", "") or "")
    # 读取客户机桌面用户的 sudo 口令（用于脚本内 sudo -S 提权）。
    password_b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    # 口令做 base64 编码，避免以明文出现在脚本文本中。
    payload_b64 = base64.b64encode(paths_payload.encode("ascii")).decode("ascii")
    # 路径载荷同样 base64 编码后整体嵌入脚本。
    script = f"""\
set -eu
sudo_password_b64={password_b64!r}
paths_payload_b64={payload_b64!r}
quarantine=/root/.rsiagent_actor_evaluator_quarantine
run_sudo() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- "$@"
}}
run_sudo install -d -m 0700 -- "$quarantine"
index=0
printf %s "$paths_payload_b64" | base64 -d | while IFS= read -r encoded; do
  test -n "$encoded" || continue
  path=$(printf %s "$encoded" | base64 -d)
  case "$path" in
    /home/user/*) ;;
    *) echo "invalid quarantine path"; exit 125 ;;
  esac
  test ! -L "$path" || {{ echo "refusing evaluator symlink"; exit 125; }}
  test -f "$path" || {{ echo "evaluator disappeared before quarantine"; exit 125; }}
  digest=$(printf %s "$path" | sha256sum | cut -d' ' -f1)
  run_sudo mv -- "$path" "$quarantine/$digest"
  run_sudo chmod 0600 -- "$quarantine/$digest"
  index=$((index + 1))
done
run_sudo find "$quarantine" -mindepth 1 -maxdepth 1 -type f -printf .
echo
echo __RSIAGENT_EVALUATOR_QUARANTINE_READY__
"""
    # 【中文注释】客户机内执行的 bash 隔离脚本：
    # 1) 用 sudo 创建仅 root 可访问（0700）的隔离目录 /root/.rsiagent_actor_evaluator_quarantine；
    # 2) 逐条解码路径，再次校验必须在 /home/user/ 之下、不是符号链接、确实是普通文件；
    # 3) 以"路径 sha256"为文件名 mv 进隔离目录并 chmod 0600，普通用户从此不可读；
    # 4) 列举隔离目录内容并以就绪标记行结尾，供宿主端校验输出完整性。
    moved = vm.run_script("bash", script, timeout=180, cap=0)
    # 在客户机内执行隔离脚本。
    output = str(getattr(moved, "stdout", "") or "")
    # 取出脚本完整输出用于校验。
    if (
        getattr(moved, "infra_fail", False)
        or getattr(moved, "exit_code", None) != 0
        or "__RSIAGENT_EVALUATOR_QUARANTINE_READY__" not in output
    ):
        # 基础设施失败、退出码非 0、或缺少就绪标记，都视为隔离未完成（fail-closed）。
        raise RuntimeError(
            "detected evaluator source but could not quarantine it: " + output
        )

    # Re-scan instead of trusting the move command's success text.
    # 【中文注释】不信任搬移命令的自我报告：用同一扫描脚本复扫验证隔离效果。
    verify = vm.run_script("python", _SCAN_SCRIPT, timeout=180, cap=0)
    # 重新执行扫描脚本，检查是否仍有评测器源码可见。
    if getattr(verify, "infra_fail", False) or getattr(verify, "exit_code", None) != 0:
        # 复扫本身失败即无法证明边界存在。
        raise RuntimeError("could not verify the evaluator-isolation boundary")
    remaining = _scan_records(getattr(verify, "stdout", ""))
    # 解析复扫结果，得到残余可见的评测器文件。
    if remaining:
        # 仍有残余说明隔离不彻底，必须中止（保持 fail-closed 语义）。
        raise RuntimeError("evaluator source remained Actor-visible after quarantine")
    return records
    # 返回本次隔离的文件记录清单（供事件审计使用）。


# 【中文注释】模块公开接口：仅导出隔离入口函数。
__all__ = ["quarantine_actor_visible_evaluators"]
# 声明模块公开接口：仅导出隔离入口函数。
