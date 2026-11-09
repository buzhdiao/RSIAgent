# 【中文注释·模块定位】目标隔离（target isolation）的出网密封契约测试
# （explore/target_isolation：HTTP/TLS 前导校验、单请求过滤网关、防火墙脚本、
# 出网策略清单、活体探测与 Chrome SPKI 预建身份）。被钉死的安全承诺：
# 1) HTTP Host 允许名单只认精确源站：大小写/端口变体放行，同住租户、前后缀
#    拼接、尾点、异端口、user@ 权威、重复 Host、CONNECT、绝对目标走私、缺
#    完整头、同缓冲流水线全部拒绝；
# 2) HTTPS 内层主机只在 443 + https 方案下放行匹配的权威；
# 3) 单请求代理拒绝隧道/歧义分帧（chunked/100-continue/websocket/trailers/
#    重复 Content-Length），强制规范化 Host 与 Connection: close；
# 4) 过滤网关读到首个请求后立即封口：后续 keep-alive 请求既不被读也不被
#    中继上游（同住租户的第二个请求绝不出网）；
# 5) TLS SNI 允许名单跨记录分片解析，只认精确且唯一的 SNI；
# 6) 防火墙脚本只重定向 80/443 到网关，其余全弃：IPv4/IPv6/ebtables L2/
#    QEMU 桥匹配（按入接口而非可伪造的 guest 源地址）、DNS 不放行、conntrack
#    规则先于 DROP；null 模式无网关放行且仍装 DROP 链；
# 7) 外层防火墙安装走 root exec（非特权模式），绑定脚本哈希；exec 失败/
#    标记缺失/未知 IPv6 模式一律 fail-closed；真实镜像缺 ip6 过滤时有窄
#    ebtables 回退；
# 8) 策略清单声明双 HTTPS 门（SNI 精确 + 内层主机精确）与 null 全拒、guest-dns
#    被封锁、策略哈希 64 位；
# 9) 活体探测要求"正向精确 PASS + 全部负向 BLOCKED"同时成立，任何旁路打开
#    都抛 RSIEgressError；探测命令里含同住租户主机名；
# 10) Chrome SPKI 允许名单的身份预创建并在真实绑定后复用与启动。
from __future__ import annotations

from dataclasses import dataclass
# 导入 dataclass：伪造 docker exec 结果容器。

import pytest
# 导入 pytest：参数化与异常断言。

from explore.target_isolation import (
    ALLOWED_STREAMVIEW_HOST,
    # 唯一允许的出网源站（streamview.web.hku.icu）。
    RSIEgressError,
    # 出网密封错误：安装/探测失败时抛出。
    FORWARD_CHAIN,
    FilteringTcpGateway,
    INPUT_CHAIN,
    NAT_CHAIN,
    # 防火墙三链名与过滤 TCP 网关（单请求代理）。
    GatewayPorts,
    # 网关端口对（http/https）。
    SealInstallation,
    # 密封安装记录（策略/规则/网关证书哈希等）。
    TargetIsolationSeal,
    # 目标隔离密封器（预建身份/绑定网关/活体探测）。
    PrefaceIncomplete,
    PrefaceRejected,
    # 前导不完整 / 前导被拒两种异常。
    _firewall_script,
    # 防火墙脚本生成器（被测纯函数）。
    egress_policy_manifest,
    egress_policy_sha256,
    # 出网策略清单与其哈希。
    install_outer_firewall,
    # 外层防火墙安装入口。
    parse_tls_client_hello_sni,
    # TLS ClientHello SNI 解析器（跨记录分片）。
    prepare_single_http_request,
    # 单请求代理的请求规范化。
    validate_http_preface,
    validate_tls_preface,
    # HTTP/TLS 前导校验。
)


def _http(host: str, *, target: str = "/watch/example",
          extra: bytes = b"") -> bytes:
    """构造一个最小 HTTP/1.1 GET 请求字节串（可自定义目标路径与附加字节）。"""
    return (f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            "User-Agent: test\r\n\r\n").encode("ascii") + extra
    # 标准请求行 + Host 头 + UA，后接额外字节。


def _client_hello(host: str | None, *, duplicate_sni: bool = False,
                  split_record: bool = False) -> bytes:
    """手工构造 TLS ClientHello：可选 SNI、重复 SNI、跨记录分片。"""
    extensions = bytearray()
    if host is not None:
        name = host.encode("ascii")
        entry = b"\x00" + len(name).to_bytes(2, "big") + name
        sni = len(entry).to_bytes(2, "big") + entry
        extension = b"\x00\x00" + len(sni).to_bytes(2, "big") + sni
        extensions.extend(extension)
        # 组装 SNI 扩展（server_name_list → host_name entry）。
        if duplicate_sni:
            extensions.extend(extension)
            # 注入重复的 SNI 扩展（非法形态）。
    body = (
        b"\x03\x03" + b"R" * 32
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + len(extensions).to_bytes(2, "big") + bytes(extensions)
    )
    # ClientHello 主体：TLS1.2 版本 + 32 字节随机数 + 会话 id 空 + 一个
    # 密码套件 + 压缩方法空 + 扩展区。
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    # handshake 消息：类型 1（ClientHello）+ 3 字节长度 + 主体。

    def record(payload: bytes) -> bytes:
        return b"\x16\x03\x01" + len(payload).to_bytes(2, "big") + payload
        # TLS 记录层封装（类型 22 handshake）。

    if split_record:
        cut = 17
        return record(handshake[:cut]) + record(handshake[cut:])
        # 分片形态：一个 handshake 拆进两条记录（SNI 跨记录）。
    return record(handshake)
    # 单记录形态。


@pytest.mark.parametrize("host", [
    ALLOWED_STREAMVIEW_HOST,
    ALLOWED_STREAMVIEW_HOST.upper(),
    f"{ALLOWED_STREAMVIEW_HOST}:80",
])
def test_http_host_allowlist_accepts_only_exact_origin(host):
    """Host 允许名单只认精确源站：原值/大写/默认端口变体都放行。"""
    assert validate_http_preface(_http(host)) == ALLOWED_STREAMVIEW_HOST
    # 校验通过并归一化为允许主机名。


def test_https_inner_host_accepts_matching_443_authority():
    """HTTPS 内层主机：443 端口 + https 方案 + 匹配的绝对 URL 才放行。"""
    request = _http(
        f"{ALLOWED_STREAMVIEW_HOST}:443",
        target=f"https://{ALLOWED_STREAMVIEW_HOST}/watch/example")
    # Host 带 443 端口，目标为同源 https 绝对 URL。
    assert validate_http_preface(
        request, expected_port=443, expected_scheme="https") \
        == ALLOWED_STREAMVIEW_HOST
    # 期望端口/方案与请求一致 → 放行并返回内层主机名。


@pytest.mark.parametrize("host", [
    "evil.web.hku.icu",
    f"{ALLOWED_STREAMVIEW_HOST}.evil.invalid",
    f"evil.{ALLOWED_STREAMVIEW_HOST}",
    f"{ALLOWED_STREAMVIEW_HOST}.",
    f"{ALLOWED_STREAMVIEW_HOST}:81",
    f"user@{ALLOWED_STREAMVIEW_HOST}",
])
def test_http_host_allowlist_rejects_cotenants_suffixes_and_odd_authority(host):
    """同住租户/前后缀拼接/尾点/异端口/user@ 权威一律拒绝。"""
    # 六种伪装形态：别的同住租户、后缀挂载、前缀拼接、尾点、非默认端口、
    # userinfo 注入。
    with pytest.raises(PrefaceRejected):
        validate_http_preface(_http(host))
        # 全部必须被拒。


def test_http_rejects_duplicate_host_connect_and_absolute_target_smuggling():
    """重复 Host 头、CONNECT 隧道、绝对目标走私三种走私形态全部拒绝。"""
    duplicate = (
        f"GET / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        "Host: evil.web.hku.icu\r\n\r\n").encode()
    # 请求走私：两个 Host 头（第二个指向恶意同住租户）。
    connect = (
        f"CONNECT {ALLOWED_STREAMVIEW_HOST}:443 HTTP/1.1\r\n"
        f"Host: {ALLOWED_STREAMVIEW_HOST}\r\n\r\n").encode()
    # 隧道走私：CONNECT 方法（会变成任意 TCP 通道）。
    smuggled = _http(
        ALLOWED_STREAMVIEW_HOST,
        target="http://evil.web.hku.icu/private")
    # 目标走私：Host 合法但请求行是 http:// 恶意源的绝对 URL。
    for request in (duplicate, connect, smuggled):
        with pytest.raises(PrefaceRejected):
            validate_http_preface(request)
            # 三种形态全部拒绝。


def test_http_requires_a_complete_header():
    """头区不完整（缺终止空行）必须报 PrefaceIncomplete（等更多字节）。"""
    with pytest.raises(PrefaceIncomplete):
        validate_http_preface(
            f"GET / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n".encode())
            # 只有请求行 + Host，没有 \r\n\r\n 终止。


def test_same_buffer_pipelined_cotenant_request_is_rejected():
    """同一缓冲里的流水线第二请求（同住租户）被拒——绝不代理多个请求。"""
    first = _http(ALLOWED_STREAMVIEW_HOST)
    second = _http("gitlab.web.hku.icu")
    # 第一个合法、第二个是同住租户。
    with pytest.raises(PrefaceRejected, match="beyond its sole"):
        prepare_single_http_request(first + second)
        # 超出"单请求"限额 → 拒绝（报文含 beyond its sole）。


@pytest.mark.parametrize("header", [
    "Transfer-Encoding: chunked",
    "Expect: 100-continue",
    "Upgrade: websocket",
    "TE: trailers",
])
def test_single_request_proxy_rejects_tunnel_and_ambiguous_framing(header):
    """隧道/歧义分帧头（chunked/100-continue/websocket/trailers）全部拒绝。"""
    request = (
        f"POST /api/state HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        f"{header}\r\n\r\n").encode()
    with pytest.raises(PrefaceRejected, match="forbidden framing"):
        prepare_single_http_request(request)
        # 四种危险分帧头逐一拒绝（报文含 forbidden framing）。
    duplicate = (
        f"POST / HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}\r\n"
        "Content-Length: 3\r\nContent-Length: 3\r\n\r\nabc").encode()
    with pytest.raises(PrefaceRejected, match="ambiguous Content-Length"):
        prepare_single_http_request(duplicate)
        # 重复 Content-Length（歧义分帧经典形态）也拒绝。


def test_single_request_proxy_forces_canonical_host_and_connection_close():
    """规范化 Host（剥端口）并强制 Connection: close（keep-alive 被替换）。"""
    request = (
        f"POST /api/state HTTP/1.1\r\nHost: {ALLOWED_STREAMVIEW_HOST}:80\r\n"
        "Connection: keep-alive\r\nContent-Length: 3\r\n\r\nab").encode()
    prepared = prepare_single_http_request(request)
    # 规范化请求。
    assert f"Host: {ALLOWED_STREAMVIEW_HOST}\r\n".encode() in prepared.header
    # Host 被规范化（剥掉 :80 端口）。
    assert b"Connection: close\r\n" in prepared.header
    # 强制 Connection: close。
    assert b"keep-alive" not in prepared.header
    # keep-alive 值被清除。
    assert prepared.content_length == 3
    # Content-Length 如实解析为 3。
    assert prepared.initial_body == b"ab"
    # 已随头到达的部分体（2 字节）原样保留（差额等下轮）。


class _DownstreamSocket:
    """伪下游（guest 侧）套接字：按脚本供给读取并记录发送。"""
    def __init__(self, reads):
        self.reads = list(reads)
        # 脚本化读取序列。
        self.recv_calls = 0
        # recv 调用计数。
        self.sent = bytearray()
        # 回写给 guest 的字节。

    def recv(self, _size):
        self.recv_calls += 1
        return self.reads.pop(0) if self.reads else b""
        # 弹出下一段；耗尽后返回 b""（对端关闭）。

    def sendall(self, data):
        self.sent.extend(data)
        # 记录发送。

    def settimeout(self, _timeout):
        pass
        # 忽略超时设置。


class _UpstreamSocket:
    """伪上游（真实服务侧）套接字：记录发送并按脚本回应一次 200。"""
    def __init__(self):
        self.sent = bytearray()
        # 上游收到的字节。
        self.responses = [
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
            b"Content-Length: 2\r\n\r\nOK",
            b"",
        ]
        # 一次完整 200 响应 + 一次关闭。

    def sendall(self, data):
        self.sent.extend(data)
        # 记录发送。

    def recv(self, _size):
        return self.responses.pop(0)
        # 弹出脚本化响应。

    def settimeout(self, _timeout):
        pass
        # 忽略超时设置。

    def close(self):
        pass
        # 关闭无操作。


def test_later_keepalive_request_is_never_read_or_relayed_upstream():
    """首个请求后网关封口：后续 keep-alive 请求不被读、不被中继上游。"""
    upstream = _UpstreamSocket()
    gateway = FilteringTcpGateway(
        "127.0.0.1", connect=lambda *_args: upstream,
        upstream_ips=("127.0.0.1",))
    # 网关绑定 127.0.0.1，上游固定为伪套接字。
    downstream = _DownstreamSocket([
        _http(ALLOWED_STREAMVIEW_HOST),
        _http("gitlab.web.hku.icu"),
    ])
    # 下游先发合法请求，随后紧跟一个同住租户请求。
    gateway._handle(downstream, "http")
    # 处理这条连接。
    assert downstream.recv_calls == 1
    # 关键：只读了一次——第二个请求根本没被读。
    assert b"gitlab.web.hku.icu" not in upstream.sent
    # 同住租户主机名绝不出现在上游发送里。
    assert upstream.sent.count(b"Host:") == 1
    # 上游只收到一个请求（一个 Host 头）。
    assert b"Connection: close" in upstream.sent
    # 中继时已强制 close。
    assert bytes(downstream.sent).endswith(b"OK")
    # guest 收到了 200 响应体。


@pytest.mark.parametrize("split_record", [False, True])
def test_tls_sni_allowlist_accepts_exact_sni_across_records(split_record):
    """TLS SNI 允许名单：单记录与跨记录分片两种形态都解析并放行精确 SNI。"""
    hello = _client_hello(ALLOWED_STREAMVIEW_HOST, split_record=split_record)
    # 按参数化构造 ClientHello。
    assert parse_tls_client_hello_sni(hello) == ALLOWED_STREAMVIEW_HOST
    # 解析器返回精确 SNI。
    assert validate_tls_preface(hello) == ALLOWED_STREAMVIEW_HOST
    # 校验通过（分片不影响判定）。


@pytest.mark.parametrize("host", [
    "evil.web.hku.icu",
    f"{ALLOWED_STREAMVIEW_HOST}.evil.invalid",
    f"{ALLOWED_STREAMVIEW_HOST}.",
])
def test_tls_sni_allowlist_rejects_wrong_or_noncanonical_sni(host):
    """错误 SNI / 后缀挂载 / 尾点 SNI 一律拒绝。"""
    with pytest.raises(PrefaceRejected):
        validate_tls_preface(_client_hello(host))
        # 三种非规范 SNI 全部被拒。


def test_tls_requires_exactly_one_sni_hostname():
    """TLS 前导要求恰好一个 SNI：缺失或重复都拒绝。"""
    for hello in (
            _client_hello(None),
            _client_hello(ALLOWED_STREAMVIEW_HOST, duplicate_sni=True)):
        with pytest.raises(PrefaceRejected):
            validate_tls_preface(hello)
            # 无 SNI 与重复 SNI 两种形态都被拒。


def test_target_firewall_redirects_only_http_https_then_drops_everything_else():
    """防火墙脚本：只重定向 80/443 到网关，其余全弃；按桥接口匹配防伪造。"""
    ports = GatewayPorts(http=19080, https=19443)
    script = _firewall_script(
        mode="target", gateway_ip="172.17.0.1", ports=ports)
    # 生成 target 模式脚本。
    assert f"-i dockerbridge -j {NAT_CHAIN}" in script
    # docker 桥流量进入 NAT 链。
    assert "--dport 80 -j DNAT --to-destination 172.17.0.1:19080" in script
    # 80 重定向到网关 http 端口。
    assert "--dport 443 -j DNAT --to-destination 172.17.0.1:19443" in script
    # 443 重定向到网关 https 端口。
    assert f"-i dockerbridge -j {FORWARD_CHAIN}" in script
    assert f"-i dockerbridge -j {INPUT_CHAIN}" in script
    # FORWARD/INPUT 链挂接。
    assert "--ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT" in \
        script.replace("\\\n", "")
        # 已建立连接的回包放行（脚本换行续行被拼接后匹配）。
    assert "ip6tables" in script
    # IPv6 侧同样有规则。
    assert "ebtables -A RSI_TARGET_L2 -p IPv6 -j DROP" in script
    # L2 层丢弃 IPv6（防 ip6tables 被绕过）。
    assert "qemu-system-x86_64" in script
    # QEMU 进程存在性守卫（脚本针对 qemu 桥）。
    assert "/sys/class/net/qemu/master" in script
    # qemu 桥的 master 归属检查。
    assert "iptables -w -I OUTPUT 1 -j RSI_TARGET_OUT" in script
    # 宿主 OUTPUT 链首位插入目标出网链。
    assert "--sport 68 --dport 67 -j ACCEPT" in script
    # DHCP（68→67）放行（地址获取所需）。
    assert "--dport 53" not in script
    # 关键：DNS（53）绝不放行（guest-dns 被封锁）。
    assert script.index(f"-A {FORWARD_CHAIN} -m conntrack") \
        < script.index(f"-A {FORWARD_CHAIN} -j DROP")
    # conntrack 回包规则先于兜底 DROP（顺序错误会断回包）。
    # Match every packet arriving from the QEMU bridge, not the spoofable
    # default guest source address.
    assert "-s 20.20.20.21" not in script
    # 关键：匹配条件是"来自 qemu 桥的每个包"，绝不按可伪造的 guest 源地址
    # 20.20.20.21 匹配。


def test_null_firewall_has_no_gateway_allow_and_still_installs_drop_chains():
    """null 模式：无任何网关重定向，仍安装全弃链并打印密封 OK 标记。"""
    script = _firewall_script(mode="null")
    assert "--to-destination" not in script
    # 无重定向（全部直弃）。
    assert f"-A {FORWARD_CHAIN} -j DROP" in script
    assert f"-A {INPUT_CHAIN} -j DROP" in script
    assert f"-A RSI_TARGET_OUT -j DROP" in script
    # FORWARD/INPUT/OUTPUT 三链兜底 DROP。
    assert "RSI_EGRESS_SEAL_OK=null" in script
    # 脚本末尾打印 null 密封成功标记（安装校验用）。


@dataclass
class _ExecResult:
    """docker exec 的最小结果形状。"""
    exit_code: int
    output: bytes
    # 退出码与输出字节。


class _Container:
    """伪 docker 容器：按开关返回密封标记/退出码/IPv6 模式并记录调用。"""
    def __init__(self, *, exit_code: int = 0, marker: bool = True,
                 ipv6_mode: str = "ip6tables-plus-ebtables"):
        self.calls = []
        # 记录 exec_run 调用。
        self.exit_code = exit_code
        # 注入退出码。
        self.marker = marker
        # 是否输出密封标记。
        self.ipv6_mode = ipv6_mode
        # 报告的 IPv6 执行模式。

    def exec_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        mode = "target" if "RSI_EGRESS_SEAL_OK=target" in argv[-1] else "null"
        # 按脚本内容回显对应的 OK 标记。
        output = (
            f"RSI_EGRESS_IPV6_MODE={self.ipv6_mode}\n"
            f"RSI_EGRESS_SEAL_OK={mode}\n").encode() if self.marker else b""
        # 标记缺失时输出为空（模拟旧镜像）。
        return _ExecResult(self.exit_code, output)


def test_outer_firewall_install_uses_root_exec_and_binds_script_hash():
    """外层防火墙安装：root exec、非特权、脚本含自检，返回 64 位脚本哈希。"""
    container = _Container()
    digest = install_outer_firewall(
        container, mode="target", gateway_ip="172.17.0.1",
        ports=GatewayPorts(19080, 19443))
    # 执行安装。
    assert len(digest) == 64
    # 返回的脚本哈希是 64 位（sha256 十六进制）。
    argv, kwargs = container.calls[0]
    assert argv[:2] == ["sh", "-ceu"]
    # 以 sh -ceu 执行（出错即停）。
    assert kwargs == {"user": "root", "privileged": False}
    # 以 root 用户执行且容器无需 privileged（最小权限）。
    assert "iptables -w -C" in argv[-1]
    # 脚本带 iptables 自检（-C 先查再装，幂等）。


def test_outer_firewall_install_fails_closed_on_exec_or_marker_failure():
    """安装 fail-closed：exec 失败/标记缺失/未知 IPv6 模式都抛 RSIEgressError。"""
    with pytest.raises(RSIEgressError):
        install_outer_firewall(_Container(exit_code=1), mode="null")
        # 脚本退出码非 0。
    with pytest.raises(RSIEgressError):
        install_outer_firewall(_Container(marker=False), mode="null")
        # 旧镜像没有密封标记输出。
    with pytest.raises(RSIEgressError):
        install_outer_firewall(
            _Container(ipv6_mode="unexpected-ip6-failure"), mode="null")
        # IPv6 执行模式不是已知值。


def test_real_image_missing_ip6_filter_has_narrow_ebtables_fallback():
    """真实镜像缺 ip6 过滤能力时的窄回退：仅当全局无 IPv6 时才放行降级。"""
    script = _firewall_script(mode="null")
    assert "practice_ip6_rc\" -eq 3" in script
    # ip6tables 探测退出码 3（表不存在）才允许回退。
    assert "Table does not exist" in script
    # 探测同时匹配 stderr 的"表不存在"。
    assert "ebtables-authoritative-no-outer-ipv6" in script
    # 回退时打"ebtables 权威、无外层 IPv6"标记（可审计）。
    assert 'ip -6 -o addr show scope global' in script
    assert 'ip -6 route show default' in script
    # 回退前置条件：宿主无全局 IPv6 地址也无默认 IPv6 路由。
    assert "exit 73" in script
    # 条件不满足时以 73 退出（fail-closed，不静默降级）。


def test_policy_provenance_declares_double_https_gate_and_null_deny_all():
    """策略清单：双 HTTPS 门声明、null 全拒、guest-dns 被封、哈希 64 位。"""
    policy = egress_policy_manifest()
    target = policy["target_public_egress"]
    assert target["host"] == ALLOWED_STREAMVIEW_HOST
    # target 出网只到允许主机。
    assert "https-terminated-sni-exact-and-inner-host-exact" \
        in target["protocols"]
    # 双门协议声明：SNI 精确 + 内层主机精确（两层都要过）。
    assert policy["null_project_public_egress"] == []
    # null 项目公开出网为空（全拒）。
    assert "guest-dns" in policy["blocked"]
    # guest-dns 在封锁清单里。
    assert len(egress_policy_sha256()) == 64
    # 策略哈希 64 位（可绑定到安装记录）。


class _ProbeVM:
    """伪探测 VM：记录命令并返回脚本化输出。"""
    def __init__(self, output):
        self.output = output
        # 脚本化输出。
        self.commands = []
        # 记录的命令。

    def run_command(self, command, **_kwargs):
        self.commands.append(command)
        return self.output
        # 每条命令都返回同一输出。


def _installation(mode="target"):
    """构造一份完整的密封安装记录（target/null 两种模式）。"""
    return SealInstallation(
        mode=mode, policy_sha256="a" * 64, rules_sha256="b" * 64,
        outer_container_id="container", gateway_ip="172.17.0.1",
        gateway_ports=GatewayPorts(19080, 19443),
        gateway_ca_sha256="c" * 64,
        gateway_leaf_spki_sha256="d" * 64,
        pinned_upstream_ips=("18.209.93.223",) if mode == "target" else (),
        ipv6_enforcement="ip6tables-plus-ebtables")
        # 策略/规则/CA/叶证书哈希、网关 IP 与端口、钉死的上游 IP（仅 target）、
        # IPv6 执行模式。


def test_live_probe_seam_requires_positive_exact_and_all_negative_markers():
    """活体探测：正向精确 PASS 与全部负向 BLOCKED 必须同时成立。"""
    output = "\n".join([
        "RSI_PROBE_EXACT=PASS",
        "RSI_PROBE_WRONG_SNI=BLOCKED",
        "RSI_PROBE_DIRECT_IP=BLOCKED",
        "RSI_PROBE_DNS=BLOCKED",
        "RSI_PROBE_OUTER_GATEWAY=BLOCKED",
        "RSI_PROBE_NON_HTTP=BLOCKED",
        "RSI_PROBE_IPV6=BLOCKED",
        "RSI_PROBE_DONE=1",
    ])
    # 七项探测全绿 + 完成标记。
    seal = object.__new__(TargetIsolationSeal)
    seal.allowed_host = ALLOWED_STREAMVIEW_HOST
    vm = _ProbeVM(output)
    probe = seal.probe(vm, _installation())
    # 执行探测。
    assert probe.exact_streamview_https == "PASS"
    # 精确 HTTPS 通道通过。
    assert probe.wrong_sni_cotenant == "BLOCKED"
    # 错误 SNI（同住租户）被阻。
    assert "gitlab.web.hku.icu" in vm.commands[0]
    # 探测命令里确实包含同住租户主机名（负向测试是真实发出的）。


def test_live_probe_seam_fails_on_any_open_bypass():
    """任何一项旁路打开（OPEN）探测都必须抛 RSIEgressError。"""
    output = "\n".join([
        "RSI_PROBE_EXACT=PASS",
        "RSI_PROBE_WRONG_SNI=OPEN",
        "RSI_PROBE_DIRECT_IP=BLOCKED",
        "RSI_PROBE_DNS=BLOCKED",
        "RSI_PROBE_OUTER_GATEWAY=BLOCKED",
        "RSI_PROBE_NON_HTTP=BLOCKED",
        "RSI_PROBE_IPV6=BLOCKED",
        "RSI_PROBE_DONE=1",
    ])
    # 只有错误 SNI 一项打开。
    seal = object.__new__(TargetIsolationSeal)
    seal.allowed_host = ALLOWED_STREAMVIEW_HOST
    with pytest.raises(RSIEgressError):
        seal.probe(_ProbeVM(output), _installation())
        # 单点旁路即整体失败（密封无效）。


class _FakeIdentity:
    """伪网关 TLS 身份：CA PEM + 叶证书 SPKI 哈希（含 base64 形态）。"""
    ca_pem = b"ca"
    leaf_spki_sha256 = "e" * 64
    leaf_spki_sha256_b64 = "spki-b64"
    # b64 形态即 Chrome SPKI 允许名单值。


class _FakeGateway:
    """伪网关工厂产品：记录绑定 IP/启动状态，start 返回固定端口。"""
    def __init__(self, bind_ip, _host, *, identity=None, upstream_ips=None):
        self.bind_ip = bind_ip
        self.identity = identity or _FakeIdentity()
        self.upstream_ips = upstream_ips or ("18.209.93.223",)
        self.started = False
        self.ports = GatewayPorts(19080, 19443)

    def start(self):
        self.started = True
        return self.ports
        # 启动并返回端口。

    def close(self):
        self.started = False
        # 关闭。


def test_precreated_chrome_identity_is_reused_and_started_after_real_rebind():
    """预创建的 Chrome 身份被复用：真实绑定 IP 后启动同一网关（不换证书）。"""
    seal = TargetIsolationSeal(gateway_factory=_FakeGateway)
    assert seal.chrome_spki_allowlist == "spki-b64"
    # 构造密封器时已预创建网关身份，SPKI 允许名单即伪身份的 b64 值。
    precreated_identity = seal._gateway.identity
    # 记录预创建身份。
    gateway = seal._ensure_gateway("172.17.0.1")
    # 真实绑定到网关 IP。
    assert gateway.bind_ip == "172.17.0.1"
    # 绑定 IP 正确。
    assert gateway.identity is precreated_identity
    # 关键：复用预创建身份（Chrome 扩展的允许名单与实际网关证书一致）。
    assert gateway.started is True
    # 绑定后网关已启动。
