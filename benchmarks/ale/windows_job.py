"""Trusted Windows guest helper: exact output, process-tree timeout, restricted verifier.

Uses CreateRestrictedToken/CreateProcessAsUser and a kill-on-close Job Object.
Reference: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasusera
This file contains no task data, model credentials, or evaluator code.
"""
# 【中文注释·模块定位】Windows 客户机内的受信运行助手：为一条 Program（python/bash）
# 提供精确的输出捕获、进程树级超时终止，以及"受限令牌"的 Verifier 执行。
# 机制：CreateRestrictedToken/CreateProcessAsUser 创建受限进程 + kill-on-close 的
# Windows Job Object 兜底整棵进程树。本文件不含任务数据、模型凭据或评分器代码，
# 由 host 的 prepare() 下发到客户机（也被 smoke 用作 Windows 传输验证的一部分）。

# base64：输出日志的 base64 回传。
import base64
# json：结果载荷序列化。
import json
# os：句柄继承控制、devnull、环境变量。
import os
# subprocess：list2cmdline 组装命令行。
import subprocess
# sys：读取 argv。
import sys
# time：耗时统计（单调时钟）。
import time
# Path：输出文件路径。
from pathlib import Path


# 执行一条作业 spec：argv/timeout/output/restricted/cwd/env；打印单行 JSON 结果。
def run(spec):
    # 以下 Windows 专属依赖在函数内导入（非 Windows 平台不会触发）。
    import msvcrt

    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    import win32security
    import win32service

    # 受限令牌句柄（restricted 模式创建， finally 关闭）。
    token = None
    # 创建作业对象：用于把整棵子进程树纳入管控。
    job = win32job.CreateJobObject(None, "")
    # 查询作业的扩展限制信息结构。
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    # 设置"句柄关闭即杀死全部关联进程"标志（兜底清理）。
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    # 写回作业限制。
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info
    )
    # 输出日志文件路径。
    output_path = Path(spec["output"])
    # 进程/线程句柄先置 None（finally 需判空）。
    process = thread = None
    # 默认退出码 125（基础设施失败码，与共享运行时约定一致）。
    rc = 125
    # 是否超时。
    timed_out = False
    # 正常完成的 Program 允许留下持久子进程（不清树）。
    preserve_children = False
    # 记录开始时间。
    start = time.monotonic()
    try:
        # 打开输出文件（二进制写）与 devnull（作为子进程 stdin）。
        with output_path.open("wb") as output, open(os.devnull, "rb") as incoming:
            # 取输出文件的原生句柄。
            out_handle = msvcrt.get_osfhandle(output.fileno())
            # 取 devnull 的原生句柄。
            in_handle = msvcrt.get_osfhandle(incoming.fileno())
            # 允许两个句柄被子进程继承。
            os.set_handle_inheritable(out_handle, True)
            os.set_handle_inheritable(in_handle, True)
            # 构造 STARTUPINFO：重定向 std 三件套并隐藏窗口。
            si = win32process.STARTUPINFO()
            si.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
            si.wShowWindow = win32con.SW_HIDE
            si.hStdOutput = si.hStdError = out_handle
            si.hStdInput = in_handle
            si.lpDesktop = r"winsta0\default"
            # 复制当前环境并合并作业 spec 里的环境覆盖。
            environment = os.environ.copy()
            environment.update(spec.get("env") or {})
            # 强制 Python 子进程用 UTF-8 输出并无缓冲（结果可精确回传）。
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            # 创建标志：挂起启动（入 Job 后再恢复）、Unicode 环境、不弹窗。
            flags = (
                win32con.CREATE_SUSPENDED
                | win32con.CREATE_UNICODE_ENVIRONMENT
                | win32con.CREATE_NO_WINDOW
            )
            # 组装 CreateProcess 参数元组：应用名空、命令行、安全属性空、继承句柄、
            # 标志、环境、工作目录、STARTUPINFO。
            args = (
                None,
                subprocess.list2cmdline(spec["argv"]),
                None,
                None,
                True,
                flags,
                environment,
                spec.get("cwd"),
                si,
            )
            if spec.get("restricted"):
                # 受限模式（Verifier 专用）：打开当前进程的全部访问权令牌。
                current = win32security.OpenProcessToken(
                    win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS
                )
                try:
                    # 取当前用户 SID。
                    user_sid = win32security.GetTokenInformation(
                        current, win32security.TokenUser
                    )[0]
                    # 取令牌的组列表。
                    groups = win32security.GetTokenInformation(
                        current, win32security.TokenGroups
                    )
                    # 找登录 SID（同时带 SE_GROUP_ENABLED|LOGON 等标志位的组项）；找不到退回用户 SID。
                    logon_sid = next(
                        (
                            sid
                            for sid, attrs in groups
                            if attrs & 0xC0000000 == 0xC0000000
                        ),
                        user_sid,
                    )
                    # 取内置 Administrators 组的已知 SID。
                    admins = win32security.CreateWellKnownSid(
                        win32security.WinBuiltinAdministratorsSid, None
                    )
                    # 本机管理员组 SID（S-1-5-114，仅 Windows 专属语义）。
                    local_admins = win32security.ConvertStringSidToSid("S-1-5-114")
                    # 创建受限令牌：把 Administrators/本机管理员组降为仅拒绝（deny-only），
                    # 不删除任何特权、不限制额外 SID——管理员权限被禁用但用户身份保留。
                    token = win32security.CreateRestrictedToken(
                        current, 1, [(admins, 0), (local_admins, 0)], None, None
                    )
                finally:
                    # 关闭当前令牌句柄。
                    current.Close()
                # The image starts CUA under an elevated logon. Once the Admin
                # group is disabled, explicitly authorize that logon to the
                # interactive desktop (the documented CreateProcessAsUser flow).
                # 【中文注释】镜像以提升权限的登录运行 CUA。一旦 Admin 组被降为
                # deny-only，必须显式把该登录 SID 授权到交互桌面——这是文档化的
                # CreateProcessAsUser 流程，否则受限进程打不开窗口站。
                # 打开默认窗口站与桌面（读控制 + 写 DAC 权限）。
                handles = [
                    win32service.OpenWindowStation(
                        "winsta0", False, win32con.READ_CONTROL | win32con.WRITE_DAC
                    ),
                    win32service.OpenDesktop(
                        "default", 0, False, win32con.READ_CONTROL | win32con.WRITE_DAC
                    ),
                ]
                try:
                    # 逐个对象（站/桌面）修改 DACL。
                    for handle in handles:
                        # 读取当前 DACL 安全描述符。
                        sd = win32security.GetUserObjectSecurity(
                            handle, win32security.DACL_SECURITY_INFORMATION
                        )
                        # 取 DACL。
                        acl = sd.GetSecurityDescriptorDacl()
                        # 追加：允许登录 SID 完全访问。
                        acl.AddAccessAllowedAce(
                            win32security.ACL_REVISION, win32con.GENERIC_ALL, logon_sid
                        )
                        # 写回 DACL（不继承、不设默认）。
                        sd.SetSecurityDescriptorDacl(True, acl, False)
                        # 应用到窗口站/桌面对象。
                        win32security.SetUserObjectSecurity(
                            handle, win32security.DACL_SECURITY_INFORMATION, sd
                        )
                finally:
                    # 关闭桌面与窗口站句柄。
                    handles[1].CloseDesktop()
                    handles[0].CloseWindowStation()
                # 为受限令牌设置默认 DACL：允许用户 SID 与 SYSTEM 完全访问，
                # 使子进程创建的文件仍可被宿主侧（SYSTEM/用户）读写。
                acl = win32security.ACL()
                acl.AddAccessAllowedAce(
                    win32security.ACL_REVISION, win32con.GENERIC_ALL, user_sid
                )
                acl.AddAccessAllowedAce(
                    win32security.ACL_REVISION,
                    win32con.GENERIC_ALL,
                    win32security.CreateWellKnownSid(
                        win32security.WinLocalSystemSid, None
                    ),
                )
                # 写入令牌的默认 DACL。
                win32security.SetTokenInformation(
                    token, win32security.TokenDefaultDacl, acl
                )
                # 以受限令牌创建进程。
                process, thread, pid, tid = win32process.CreateProcessAsUser(
                    token, *args
                )
            else:
                # 非受限模式（Actor/普通命令）：直接创建进程。
                process, thread, pid, tid = win32process.CreateProcess(*args)
            # No model code executes until it belongs to the bounded process tree.
            # 【中文注释】先把（挂起的）进程加入作业对象，再恢复运行——保证任何模型代码
            # 都逃不出有界进程树。
            win32job.AssignProcessToJobObject(job, process)
            # 恢复主线程执行。
            win32process.ResumeThread(thread)
            # 等待进程结束（按 spec 的超时毫秒数）。
            waited = win32event.WaitForSingleObject(
                process, int(spec["timeout"] * 1000)
            )
            if waited == win32con.WAIT_TIMEOUT:
                # 超时：标记并置退出码 124（GNU timeout 约定）。
                timed_out = True
                rc = 124
            elif waited == win32con.WAIT_OBJECT_0:
                # 正常退出：读取进程退出码。
                rc = win32process.GetExitCodeProcess(process)
                # Normal Programs may launch persistent applications/services,
                # just as RSIAgent's Linux GNU timeout wrapper allows. Only an
                # expired/failed harness action kills the whole process tree.
                # 【中文注释】正常 Program 允许启动持久应用/服务（与 Linux 侧 GNU timeout
                # 包装的行为对齐）。只有超时/失败的作业才杀整棵进程树。
                # 因此这里撤销 kill-on-close 标志并记住 preserve_children。
                info["BasicLimitInformation"][
                    "LimitFlags"
                ] &= ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(
                    job, win32job.JobObjectExtendedLimitInformation, info
                )
                # 标记保留子进程。
                preserve_children = True
            else:
                # 其他等待结果：意外状态，抛错。
                raise RuntimeError(f"Unexpected process wait result: {waited}")
            if timed_out:
                # 超时：终止整棵作业进程树（含全部子进程）。
                win32job.TerminateJobObject(job, rc)
            # 再等最多 10 秒确认进程完全退出（回收句柄状态）。
            win32event.WaitForSingleObject(process, 10000)
    finally:
        # Includes the CREATE_SUSPENDED failure path; never leave a child running.
        # 【中文注释】涵盖 CREATE_SUSPENDED 后的失败路径：绝不留下仍在运行的子进程。
        if process is not None and not preserve_children:
            try:
                # 非保留场景：确保进程被终止。
                win32process.TerminateProcess(process, rc)
            except Exception:
                pass
        # 关闭作业对象（若仍为 kill-on-close 且未保留，则此时整树被杀）。
        job.Close()
        # 关闭全部句柄（线程/进程/令牌）。
        for handle in (thread, process, token):
            if handle is not None:
                handle.Close()
    # 组装结果：退出码、是否超时、耗时、完整输出的 base64。
    result = {
        "exit_code": rc,
        "timed_out": timed_out,
        "seconds": time.monotonic() - start,
        "output_b64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
    }
    # 以标记行打印单行 JSON 结果（宿主据此解析）。
    print("RSIAGENT_WINDOWS_RESULT:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    # 命令行入口：argv[1] 为作业 spec JSON 文件。
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
