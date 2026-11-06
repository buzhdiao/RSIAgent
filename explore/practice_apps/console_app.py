#!/usr/bin/env python3
"""Practice app A — "Ops Console" (generic order-approval service, port 5081).

Self-authored practice terrain (E3): session/identity discipline, hidden state
channels (sqlite + /api/state), form validation, and exact deliverable shape
(exports must contain ONLY approved rows). Generic content by design — no
benchmark app, host, or task is imitated.
"""
# 练习应用 A——"运维控制台"：通用的订单审批服务（端口 5081）。
# 自编练习地形（E3）：考察会话/身份纪律、隐藏状态通道（sqlite + /api/state）、
# 表单校验，以及"交付物形状必须精确"（导出必须且只能包含已批准行）。
# 内容刻意通用化——不模仿任何基准应用、宿主机或任务。
import random
# 随机数模块：生成确定性的初始订单数据。
import sqlite3
# SQLite 驱动：练习现场的"隐藏状态通道"之一。
import time
# 时间模块：/api/state 返回服务器时间戳。

from flask import Flask, jsonify, redirect, request, session
# Flask Web 框架：路由、会话、JSON 响应与重定向。

app = Flask(__name__)
# 创建 Flask 应用实例。
app.secret_key = "practice-console-key"
# 会话签名密钥：防止 session cookie 被客户端伪造（身份纪律的前提）。
DB = "/tmp/ops_console.db"
# 订单数据库路径：状态藏在 guest 的 sqlite 文件里，需要主动发现。
random.seed(81)
# 固定随机种子：每次初始化产生相同的练习数据（可复现）。

ITEMS = ["beam clamp", "hex stock", "flux pen", "din rail", "torx bit",
         "edge trim", "core sleeve", "gasket kit", "lug crimp", "vent cap"]
# 初始订单可用的商品名清单。


def db():
    # 打开数据库连接的便捷函数。
    c = sqlite3.connect(DB)
    # 连接（必要时自动创建）订单数据库。
    c.row_factory = sqlite3.Row
    # 查询结果行用 sqlite3.Row，可按列名取值。
    return c
    # 返回连接对象。


def init():
    # 初始化数据库表并播种初始订单数据。
    c = db()
    # 打开连接。
    c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, "
              "item TEXT, qty INTEGER, requester TEXT, status TEXT, "
              "note TEXT DEFAULT '')")
    # 建 orders 表（若不存在）：id/商品/数量/申请人/状态/备注。
    if c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
        # 仅当表为空时才播种，避免重复初始化。
        for i in range(14):
            # 播种 14 条待审批订单。
            c.execute("INSERT INTO orders (item, qty, requester, status) "
                      "VALUES (?,?,?,?)",
                      (random.choice(ITEMS), random.randint(1, 40),
                       f"desk-{random.randint(1, 6)}", "pending"))
            # 插入随机商品、1-40 的数量、desk-1..6 申请人，初始状态 pending。
    c.commit()
    # 提交事务。
    c.close()
    # 关闭连接。


def authed():
    # 判断当前会话是否已登录为 analyst。
    return session.get("user") == "analyst"
    # 会话中的 user 字段等于内置账号名才算已认证。


@app.route("/")
def home():
    # 首页：未登录显示登录表单（含预置账号提示），已登录显示订单审批表。
    if not authed():
        # 未登录：渲染极简登录页。
        return ('<h3>Ops Console</h3><form method=post action=/login>'
                '<input name=user placeholder=user> '
                '<input name=pw type=password placeholder=password> '
                '<button>sign in</button></form>'
                '<p style="color:#888">provisioned account: analyst / rail-vent-42</p>')
        # 登录表单；页面直接给出预置账号 analyst / rail-vent-42（考察先"看"再操作）。
    rows = db().execute("SELECT * FROM orders ORDER BY id").fetchall()
    # 已登录：取全部订单（按 id 排序）。
    body = "".join(
        f"<tr><td>{r['id']}</td><td>{r['item']}</td><td>{r['qty']}</td>"
        f"<td>{r['requester']}</td><td>{r['status']}</td>"
        f"<td><form method=post action=/decide style='display:inline'>"
        f"<input type=hidden name=id value={r['id']}>"
        f"<button name=verdict value=approved>approve</button>"
        f"<button name=verdict value=rejected>reject</button></form></td></tr>"
        for r in rows)
    # 把每条订单拼成一行表格，附 approve/reject 两个内联表单按钮。
    return (f"<h3>Orders ({session['user']})</h3><table border=1>"
            f"<tr><th>id</th><th>item</th><th>qty</th><th>from</th>"
            f"<th>status</th><th>act</th></tr>{body}</table>"
            f"<p><a href=/export/approved.json>export approved</a> · "
            f"<a href=/logout>sign out</a></p>")
    # 整页 HTML：订单表 + 导出已批准项 + 登出链接。


@app.route("/login", methods=["POST"])
def login():
    # 登录接口：校验用户名与密码。
    if request.form.get("user") == "analyst" and \
            request.form.get("pw") == "rail-vent-42":
        # 账号与密码都匹配预置凭证才放行。
        session["user"] = "analyst"
        # 写入会话，标记已登录身份。
        return redirect("/")
        # 重定向回首页。
    return "bad credentials", 403
    # 凭证错误：403。


@app.route("/logout")
def logout():
    # 登出接口：清空会话。
    session.clear()
    # 清除会话中的全部字段。
    return redirect("/")
    # 回到登录页。


@app.route("/decide", methods=["POST"])
def decide():
    # 审批接口：把指定订单置为 approved 或 rejected。
    if not authed():
        # 必须先登录。
        return "sign in first", 403
        # 未认证：403。
    oid, verdict = request.form.get("id"), request.form.get("verdict")
    # 取表单中的订单 id 与裁决值。
    if verdict not in ("approved", "rejected"):
        # 只接受两种合法裁决值（表单校验）。
        return "verdict must be approved|rejected", 400
        # 非法裁决：400。
    c = db()
    # 打开数据库。
    c.execute("UPDATE orders SET status=? WHERE id=?", (verdict, oid))
    # 用参数化查询更新该订单状态（规范写法，避免 SQL 注入）。
    c.commit()
    # 提交变更。
    return redirect("/")
    # 回到首页刷新视图。


@app.route("/export/approved.json")
def export():
    # THE SHAPE RULE: an export is valid only if it contains approved rows and
    # NOTHING else — extra rows make it wrong, whatever else is right.
    # （中文）交付物形状规则：导出"有效"当且仅当恰好等于已批准行集合——
    # 多带任何多余行都算错，其余做得再对也无济于事。
    if not authed():
        # 必须先登录。
        return "sign in first", 403
        # 未认证：403。
    rows = db().execute(
        "SELECT id, item, qty, requester FROM orders "
        "WHERE status='approved' ORDER BY id").fetchall()
    # 只取状态为 approved 的行，且只含四列、按 id 排序。
    return jsonify([dict(r) for r in rows])
    # 以 JSON 数组返回；导出形状完全由查询决定，保证交付精确。


@app.route("/api/state")
def state():
    # 隐藏状态通道：以 JSON 暴露完整服务器状态（含全部订单与时间）。
    if not authed():
        # 状态接口同样要求登录。
        return jsonify({"error": "authenticated session required"}), 401
        # 未认证：401 JSON 错误。
    rows = db().execute("SELECT * FROM orders ORDER BY id").fetchall()
    # 取全部订单。
    return jsonify({"user": session["user"], "server_time": int(time.time()),
                    "orders": [dict(r) for r in rows]})
    # 返回当前用户、服务器时间与订单列表（机器可读，适合轮询）。


if __name__ == "__main__":
    # 直接运行本模块时启动服务。
    init()
    # 先初始化数据库并播种数据。
    app.run(host="127.0.0.1", port=5081)
    # 仅监听本地回环地址的 5081 端口。
