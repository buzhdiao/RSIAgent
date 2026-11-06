#!/usr/bin/env python3
"""Practice app B — "Ticket Stream" (live-window triage service, port 5082).

Self-authored practice terrain (E3): work arrives in TIMED BATCHES for ~35
minutes after first contact — one early sweep misses most of it. Organs:
long-window awareness, state polling, late-arrival handling. Generic content.
"""
# 练习应用 B——"工单流"：带时间窗的工单分诊服务（端口 5082）。
# 自编练习地形（E3）：首次接触后的约 35 分钟内，工单按时间"分批"陆续到达——
# 只做一次早期清扫会漏掉绝大多数。要练的"器官"：长时窗意识、状态轮询、
# 迟到数据处理。内容刻意通用化。
import random
# 随机数模块：生成确定性的工单内容与批量大小。
import threading
# 线程模块：后台按批投放工单。
import time
# 时间模块：记录到达时刻与窗口开合。

from flask import Flask, jsonify, request
# Flask Web 框架：路由、JSON 响应与请求解析。

app = Flask(__name__)
# 创建 Flask 应用实例。
random.seed(82)
# 固定随机种子：工单序列可复现。

# 工单表：id -> {文本, 严重度, 到达时间, 分诊动作}（保留原有行内英文注释）。
TICKETS = {}          # id -> {text, severity, arrived, triaged}
# 首次接触时间戳（用列表包装以便函数内赋值；保留原有行内英文注释）。
_started = [None]     # first-contact timestamp
_lock = threading.Lock()
# 互斥锁：保护 TICKETS/_started（后台线程与请求线程并发访问）。
KINDS = [("printer queue jam on floor %d", "low"),
         ("badge reader offline at gate %d", "medium"),
         ("cold-room sensor %d drifting", "high"),
         ("backup job %d skipped a volume", "high"),
         ("meeting room %d display flicker", "low")]
# 工单模板与对应严重度清单。


def _add_batch(n):
    # 向工单表追加 n 条新工单。
    with _lock:
        # 加锁，避免与请求线程并发读写冲突。
        for _ in range(n):
            # 逐条生成 n 条。
            tid = len(TICKETS) + 1
            # 新工单 id 取当前数量+1（单调递增）。
            tmpl, sev = random.choice(KINDS)
            # 随机选一个工单模板与严重度。
            TICKETS[tid] = {"id": tid, "text": tmpl % random.randint(1, 9),
                            "severity": sev, "arrived": int(time.time()),
                            "triaged": None}
            # 写入工单：文本填随机位置编号、记录到达时间，分诊状态留空。


def _feeder():
    # 5 batches, ~7 min apart, from first contact — a ~35-minute live window.
    # （中文）后台投放线程：从首次接触起每隔约 7 分钟投放一批，共 5 批——
    # 约 35 分钟的"活窗口"，迟到的工单必须被补收。
    for _ in range(5):
        # 共投放 5 批。
        time.sleep(7 * 60)
        # 每批间隔 7 分钟（模拟工作陆续到达）。
        _add_batch(random.randint(2, 4))
        # 每批随机投放 2-4 条。


@app.route("/api/state")
def state():
    # 状态接口：首次调用即"打开窗口"并启动后台投放；此后供轮询。
    if _started[0] is None:
        # 尚无首次接触记录：以本次调用为起点。
        _started[0] = int(time.time())
        # 记录首次接触时间戳。
        _add_batch(3)
        # 立即投放第一批 3 条。
        threading.Thread(target=_feeder, daemon=True).start()
        # 启动后台投放线程（守护线程，随进程退出）。
    with _lock:
        # 加锁对工单表做一致快照。
        items = sorted(TICKETS.values(), key=lambda t: t["id"])
        # 按 id 排序取出全部工单。
    open_n = sum(1 for t in items if not t["triaged"])
    # 统计尚未分诊（triaged 为空）的工单数。
    return jsonify({"window_open_secs": int(time.time()) - _started[0],
                    "feed_active": (int(time.time()) - _started[0]) < 36 * 60,
                    "open": open_n, "tickets": items})
    # 返回窗口已开秒数、投放是否仍活跃（<36 分钟）、未分诊数与工单列表。


@app.route("/triage", methods=["POST"])
def triage():
    # 分诊接口：对指定工单执行 ack/escalate/close。
    tid = request.json.get("id") if request.is_json else None
    # 从 JSON 请求体取工单 id（非 JSON 则为空）。
    action = request.json.get("action") if request.is_json else None
    # 同理取分诊动作。
    if action not in ("ack", "escalate", "close"):
        # 只接受三种合法动作（请求校验）。
        return jsonify({"error": "action must be ack|escalate|close"}), 400
        # 非法动作：400。
    with _lock:
        # 加锁读取并修改工单。
        t = TICKETS.get(tid)
        # 查找指定工单。
        if not t:
            # 工单不存在。
            return jsonify({"error": f"no ticket {tid}"}), 404
            # 返回 404。
        t["triaged"] = action
        # 记录该工单的分诊动作。
    return jsonify({"ok": True, "ticket": t})
    # 返回成功标志与该工单最新状态。


@app.route("/")
def home():
    # 首页：说明这是一个纯 JSON 服务的用法。
    return ("<h3>Ticket Stream</h3><p>JSON service. GET /api/state to open the "
            "window (new tickets keep arriving for a while); POST /triage "
            '{"id": N, "action": "ack|escalate|close"}.</p>')
    # 用法提示：GET /api/state 打开窗口（新工单会持续到达一段时间）；
    # POST /triage 以 JSON 提交 {id, action}。


if __name__ == "__main__":
    # 直接运行本模块时启动服务。
    app.run(host="127.0.0.1", port=5082)
    # 仅监听本地回环地址的 5082 端口。
