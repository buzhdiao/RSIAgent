"""Private host RPC for fresh guest resets; exposes no evaluator operation."""
# 【中文注释·模块定位】宿主进程私有的"全新客户机复位"RPC 服务。作用：worker 进程
# （密封智能体进程）通过本服务请求把练习用 VM 复位到全新边界（reset），但不暴露任何
# 评分器操作——密封评估（sealed evaluation）要求学习侧永远接触不到 grading 能力。
# 传输层：Unix domain socket（文件权限 0600，仅本机本用户可达），单行 JSON 请求/应答。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# asyncio：把"线程模型中的 RPC 处理"桥接回宿主的事件循环（run_coroutine_threadsafe）。
import asyncio
# json：请求与应答行的序列化/解析。
import json
# os：chmod 套接字文件权限。
import os
# socket：客户端侧的 AF_UNIX 套接字。
import socket
# socketserver：线程化的 Unix 流式服务器框架。
import socketserver
# tempfile：为套接字文件创建私有临时目录（自动清理）。
import tempfile
# threading：以守护线程运行服务器，不阻塞宿主事件循环。
import threading
# Path：套接字文件路径操作。
from pathlib import Path


# 【中文注释】复位 RPC 服务端：包一个 Unix socket 服务器，把请求转投给宿主协程 reset(sandbox_id)。
class ResetService:
    # reset=宿主侧异步复位函数（host.phase 内定义的 reset_sandbox）。
    def __init__(self, reset):
        # 记录宿主正在运行的事件循环：处理线程要把协程调度回这个循环执行。
        self.loop = asyncio.get_running_loop()
        # 创建私有临时目录，套接字文件放在里面（目录生命周期由 TemporaryDirectory 管理）。
        self.directory = tempfile.TemporaryDirectory(prefix="rsiagent-ale-rpc-")
        # 套接字文件路径（endpoint），将写进 worker-spec 交给 worker 进程。
        self.path = str(Path(self.directory.name) / "reset.sock")
        loop = self.loop
        # 局部捕获 loop，供下方 Handler 类闭包引用。

        # 逐行读取请求（每个请求一行 JSON），处理后回写一行 JSON。
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    # 解析请求行（限长 4096 字节，防止恶意超长行）。
                    request = json.loads(self.rfile.readline(4096))
                    # 白名单校验：字段必须恰好是 operation+sandbox_id，且 operation 只允许 "reset"；
                    # 任何评分/查询等操作一律拒绝（密封边界在此落地）。
                    if (
                        set(request) != {"operation", "sandbox_id"}
                        or request["operation"] != "reset"
                    ):
                        raise ValueError(
                            "Only fresh reset of an owned sandbox is allowed"
                        )
                    # 把复位协程投递回宿主事件循环执行，并同步等待其结果（阻塞当前处理线程）。
                    result = asyncio.run_coroutine_threadsafe(
                        reset(request["sandbox_id"]), loop
                    ).result()
                    # 成功应答：返回复位后的新沙箱描述。
                    reply = {"sandbox": result}
                except Exception as exc:
                    # 失败应答：把异常类型与消息回给客户端（客户端再转成 RuntimeError）。
                    reply = {"error": f"{type(exc).__name__}: {exc}"}
                # 无论成败都回写一行 JSON 应答。
                self.wfile.write(json.dumps(reply).encode() + b"\n")

        # 线程化 Unix 流服务器；daemon_threads=False：关停时等处理线程收尾，避免复位半途而废。
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = False

        # 绑定套接字并装配处理器。
        self.server = Server(self.path, Handler)
        # 套接字文件收紧为仅属主可读写（0600）：防止本机其他用户调用复位 RPC。
        os.chmod(self.path, 0o600)
        # 以守护线程启动服务循环：宿主退出时线程不阻止进程终止。
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        # 启动服务线程。
        self.thread.start()

    # 关停服务：先停接收循环，再关套接字、等线程退出、清理临时目录。
    async def close(self):
        # shutdown() 会阻塞到 serve_forever 循环退出，放进线程池避免卡住事件循环。
        await asyncio.to_thread(self.server.shutdown)
        # 关闭监听套接字并释放文件。
        await asyncio.to_thread(self.server.server_close)
        # 等服务线程真正结束（daemon_threads=False 时所有在途请求已收尾）。
        self.thread.join()
        # 删除临时目录与套接字文件。
        self.directory.cleanup()


# 客户端封装：worker 侧调用，向宿主请求把指定沙箱复位成全新 VM。
def request_reset(endpoint, sandbox_id):
    # 创建 Unix 流式套接字（用完自动关闭）。
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        # 超时 2400 秒：全新 QEMU 复位（销毁+重建+启动）可能非常慢，必须给足时间。
        client.settimeout(2400)
        # 连接宿主的复位 RPC 套接字。
        client.connect(endpoint)
        # 发送单行 JSON 请求并立即发送（sendall 保证全部写出）。
        client.sendall(
            json.dumps({"operation": "reset", "sandbox_id": sandbox_id}).encode()
            + b"\n"
        )
        # 读取一行应答（上限 1 MiB，沙箱描述不会超过）。
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline(1 << 20))
    # 应答带 error 字段：把服务端异常转为本侧 RuntimeError 抛出。
    if "error" in response:
        raise RuntimeError("Fresh ALE reset failed: " + response["error"])
    # 成功：返回复位后的全新沙箱描述（含新 endpoint 等）。
    return response["sandbox"]
