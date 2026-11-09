# 【中文注释·模块定位】E7 供给（provisioning）捕获/重放回归（explore/provisioning.py）
# ——对应预注册 PREREG E7 §3 B7。核心保证：跨"检查点重启"的项目工件传输是
# 字节级保真的（byte-verbatim），绝不在宿主侧重新生成。
# 测试策略：_GuestVM 用真实的 tar/base64/sha256 数学在内存字典上模拟 guest，
# 并"捕获"实际推送的字节——往返测试因此能证明字节保真；再故意翻转一个字节
# 证明 guest 内校验能抓住基础设施腐化（在主循环层表现为 provision_failed，
# 绝不重新生成材料蒙混过关）。分五组：
# capture（捕获写 tgz+manifest、无上限传输、分块回退、空 guest 拒绝、目录可配、
# 死链捕获不跟踪）、round trip（重放=精确集合、流式上传优先、损坏拒绝、擦除
# 旧状态、fail-closed）、mismatch（损坏/缺失逐字节定位、长路径无损）、
# verify（只读校验接受精确集合、拒绝变异）、owned paths（结构与旧版回退）。
"""E7 provisioning capture/replay (explore/provisioning.py) — PREREG E7 §3 B7.

The mock VM emulates the guest with real tar/base64/sha256 semantics over an
in-memory file dict and CAPTURES the pushed bytes, so the round-trip proves
byte-verbatim transport, and a corrupted byte proves the in-guest verify
catches infra rot (-> provision_failed at the loop, never re-generation).
"""
# 模块 docstring：E7 供给捕获/重放。伪 VM 用真实归档数学模拟 guest 并捕获
# 推送字节，证明字节级保真；损坏字节证明 guest 内校验能抓住腐化。

import base64
# 导入 base64：分块传输/解码的编解码。
import hashlib
# 导入 hashlib：manifest 与校验的 sha256。
import io
# 导入 io：内存字节流（构造/读取 tgz）。
import os
# 导入 os：symlink/readlink/存在性检查。
import re
# 导入 re：解析伪 VM 收到的命令。
import shlex
# 导入 shlex：按 shell 规则拆分命令参数。
import shutil
# 导入 shutil：真实宿主侧文件复制/删除（LocalVM 用）。
import subprocess
# 导入 subprocess：LocalVM 用真实 bash 执行命令。
import sys
# 导入 sys：把仓库根插入 sys.path。
import tarfile
# 导入 tarfile：真实 tgz 打包/解包。
import pytest
# 导入 pytest：参数化与异常断言。

from pathlib import Path
# 导入 Path：路径对象处理。

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 把仓库根加入模块搜索路径（本文件可在任意 cwd 下运行）。
from explore import provisioning as P                    # noqa: E402
# 以 P 别名导入被测主体：供给捕获/重放/校验模块。
from env.vm import _bound
# 导入输出截断工具：伪 VM 模拟 env/vm.py 的 cap 行为。


_PRINTF = re.compile(r"printf %s '([^']*)' >> /tmp/proj_in\.b64")
# 匹配 base64 分块追加命令：捕获推送的每一块。
_CAPTURE = re.compile(r"tar czf /tmp/proj_materials\.tgz -C /home/user (.+?) "
                      r"2>/dev/null")
# 匹配 guest 内捕获命令：按目录打包。
_SHASUM = re.compile(r"cd /home/user && sha256sum (.+?) 2>&1")
# 匹配 guest 内逐文件 sha256 校验命令。


def _tgz_of(files: dict) -> bytes:
    # 测试工具：把 {相对路径: 字节} 打成真实 tgz 字节流。
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in sorted(files.items()):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        # 按排序逐个加入成员（确定性归档）。
    return buf.getvalue()


class _GuestVM:
    """Mock guest: {relpath under /home/user: bytes} + real archive math."""

    # 伪 guest VM：/home/user 下的相对路径到字节的字典 + 真实归档数学；
    # 捕获推送的 b64 分块与最后一次捕获的 tar。

    def __init__(self, files=None):
        self.files = dict(files or {})
        # guest 文件树。
        self.b64 = ""                      # accumulating pushed b64 chunks
        # 累积推送的 b64 分块（模拟 guest 侧文件内容）。
        self.tar_out = b""                 # last capture tar built in-guest
        # 最后一次 guest 内捕获构建的 tar 字节。
        self.pushed_tgz = b""              # decoded bytes replay extracted
        # 重放时解码出的 tgz 字节（即真正推送到达的内容）。
        self.cmds = []
        # 收到的全部命令（供断言）。

    def run_command(self, cmd, timeout=None, cap=4000):
        # 命令解释器：实现 provisioning 协议用到的每类命令。
        self.cmds.append(cmd)
        m = _CAPTURE.search(cmd)
        if m:
            dirs = shlex.split(m.group(1))
            sub = {rel: d for rel, d in self.files.items()
                   if rel.split("/", 1)[0] in dirs}
            if not sub:
                return "TAR_RC=2"
            self.tar_out = _tgz_of(sub)
            return "TAR_RC=0"
        # tar czf 捕获：按目录过滤文件树打包；空目录返回 RC=2。
        m = _PRINTF.search(cmd)
        if m:
            self.b64 += m.group(1)
            return ""
        # printf 追加：累积 base64 分块。
        if cmd.startswith("rm -rf"):
            for tgt in shlex.split(cmd)[2:]:
                if tgt.startswith("/home/user/"):
                    rel = tgt[len("/home/user/"):]
                    for k in [k for k in self.files
                              if k == rel or k.startswith(rel + "/")]:
                        del self.files[k]
            self.b64 = ""
            return ("PROJECT_WIPE_RC=0"
                    if "PROJECT_WIPE_RC" in cmd else "")
        # rm -rf：真删 guest 文件树（含子树）、清空 b64 累积；
        # 擦除命令带 PROJECT_WIPE_RC 探针时回显成功标记。
        if cmd.startswith("rm -f"):
            if P.GUEST_B64_IN in cmd:
                self.b64 = ""
            return ""
        # rm -f 单文件：清 b64 缓存文件即可。
        if "PROJECT_SET_RC" in cmd:
            expected = set(base64.b64decode(self.b64).decode().splitlines())
            actual = set(self.files)
            return ("PROJECT_SET_RC=0" if expected == actual
                    else "PROJECT_SET_RC=1")
        # 集合核对命令：guest 内比较期望文件清单与实际文件树，精确相等才 RC=0。
        if "base64 -d /tmp/proj_in.b64" in cmd:
            try:
                self.pushed_tgz = base64.b64decode(self.b64)
            except Exception:              # noqa: BLE001
                return "DECODE_RC=1"
            return "DECODE_RC=0"
        # b64 解码：把累积块解码为 tgz 字节（损坏时 RC=1）。
        if cmd.startswith(f"stat -c %s {P.GUEST_TGZ_IN}"):
            return str(len(self.pushed_tgz)) if self.pushed_tgz else ""
        # stat：返回推送 tgz 的字节数（无推送时空输出）。
        if cmd.startswith(f"sha256sum {P.GUEST_TGZ_IN}"):
            if not self.pushed_tgz:
                return ""
            return (f"{hashlib.sha256(self.pushed_tgz).hexdigest()}  "
                    f"{P.GUEST_TGZ_IN}")
        # 归档哈希校验：返回推送 tgz 的真实 sha256。
        if cmd.startswith(f"tar xzf {P.GUEST_TGZ_IN}"):
            try:
                with tarfile.open(fileobj=io.BytesIO(self.pushed_tgz),
                                  mode="r:gz") as tf:
                    for mem in tf.getmembers():
                        if mem.isfile():
                            self.files[mem.name] = tf.extractfile(mem).read()
            except Exception:              # noqa: BLE001
                return "SYNC_RC=1"
            return "SYNC_RC=0"
        # tgz 解包：真实解压进 guest 文件树（损坏归档 RC=1）。
        m = _SHASUM.search(cmd)
        if m:
            lines = []
            for rel in shlex.split(m.group(1)):
                if rel in self.files:
                    h = hashlib.sha256(self.files[rel]).hexdigest()
                    lines.append(f"{h}  {rel}")
                else:
                    lines.append(f"sha256sum: {rel}: No such file or directory")
            return _bound("\n".join(lines), cap)
        # 逐文件校验：真实 sha256sum 输出格式，缺失文件报标准错误行；
        # 输出按 cap 截断（模拟真实 VM 行为）。
        return ""

    def fetch_file(self, path):
        return (self.tar_out or None), ""  # (bytes|None, note) like env/vm.py
        # 抓取 guest 内捕获的 tar（无则 None），返回值形状与 env/vm.py 一致。


RAW = {"raw/clip0.mp4": b"\x00\x01video-zero\xff" * 40,
       "raw/clip1.mp4": b"\x00\x02video-one\xfe" * 55,
       "raw/notes.txt": b"generic provisioning note\n"}
# 标准工件集：两个二进制视频 + 一个文本（含非 ASCII 字节）。


# ----------------------------------------------------------------- capture --
def test_capture_writes_tgz_and_manifest(tmp_path):
    # 测试：捕获把 guest 工件打成 tgz 落盘，并生成与源字节逐文件对应的
    # sha256 manifest；根目录清单为 ["raw"]。
    vm = _GuestVM(RAW)
    # 带 RAW 工件的伪 guest。
    r = P.capture_project_materials(vm, "p01", str(tmp_path))
    # 执行捕获。
    assert r["ok"] and r["files"] == 3 and r["bytes"] > 0
    # 捕获成功：3 个文件、字节数为正。
    assert os.path.exists(tmp_path / "materials.tgz")
    # materials.tgz 落盘。
    man = P.read_manifest(str(tmp_path))
    # 读取 manifest。
    assert P.read_roots(str(tmp_path)) == ["raw"]
    # 捕获根目录恰为 raw。
    assert set(man) == set(RAW)
    # manifest 覆盖全部工件。
    for rel, data in RAW.items():          # manifest hashes = source bytes
        assert man[rel] == hashlib.sha256(data).hexdigest()
        # manifest 哈希 = 源字节哈希（逐文件）。


def test_capture_requests_unbounded_artifact_transport(tmp_path):
    # 测试：捕获必须以 max_bytes=None 抓取工件——工件传输永不设上限
    # （真实 VM 可能人为按上限拒绝，捕获不得接受这种限制）。
    class LimitAwareVM(_GuestVM):
        def __init__(self, files):
            super().__init__(files)
            self.seen_max_bytes = "not-called"
            # 记录 fetch_file 收到的 max_bytes。

        def fetch_file(self, path, max_bytes=4_000_000):
            self.seen_max_bytes = max_bytes
            if max_bytes is not None and len(self.tar_out) > max_bytes:
                return None, "artificial size rejection"
            # 有上限且超限时人为拒绝（真实 VM 的行为）。
            return (self.tar_out or None), ""

    vm = LimitAwareVM(RAW)

    result = P.capture_project_materials(vm, "unbounded", str(tmp_path))

    assert result["ok"]
    # 捕获成功。
    assert vm.seen_max_bytes is None
    # 关键：捕获调用用的是无上限（None）抓取。


def test_chunked_capture_fallback_decodes_each_chunk_independently(tmp_path,
                                                                   monkeypatch):
    # 测试：fetch_file 不可用时的分块回退——按 dd 的 bs/skip 逐块返回 base64，
    # 每块独立解码拼回完整 tgz，结果与原始归档逐字节一致。
    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)
    # 去掉重试间等待。
    payload = _tgz_of({"raw/large.bin": os.urandom(P._PULL_CHUNK + 17)})
    assert len(payload) > P._PULL_CHUNK
    # 构造超过单块大小（_PULL_CHUNK+17 字节）的载荷。

    class ChunkFallbackVM:
        # 只支持分块传输的伪 VM：fetch_file 强制失败。
        def run_command(self, cmd, timeout=None, cap=None):
            if "tar czf" in cmd:
                return "TAR_RC=0"
            # 捕获命令成功（内容不进伪 VM，直接用外部 payload）。
            if cmd.startswith("stat -c %s"):
                return str(len(payload))
            # stat 返回完整载荷大小。
            if cmd.startswith("dd if="):
                match = re.search(r"bs=(\d+) skip=(\d+)", cmd)
                size, index = map(int, match.groups())
                chunk = payload[index * size:(index + 1) * size]
                return base64.b64encode(chunk).decode("ascii")
                # 按 dd 的块大小与偏移切出对应分块，返回其 base64。
            return ""

        def fetch_file(self, _path, max_bytes=None):
            return None, "force fallback"
            # 强制走分块回退。

    result = P.capture_project_materials(
        ChunkFallbackVM(), "chunked", str(tmp_path), attempts=1)

    assert result["ok"]
    # 分块回退成功。
    assert (tmp_path / P.MATERIALS).read_bytes() == payload
    # 落盘归档与原始载荷逐字节一致（分块解码无错位）。


def test_capture_default_dirs_is_raw_only(tmp_path):
    # 测试：默认捕获目录只含 raw——guest 里其他位置的杂散文件（如坐席笔记）
    # 不进工件集。
    vm = _GuestVM({**RAW, "instance_next.md": b"stray seat file"})
    # 额外放一个杂散文件。
    r = P.capture_project_materials(vm, "p01", str(tmp_path))
    assert r["ok"] and set(P.read_manifest(str(tmp_path))) == set(RAW)
    # manifest 恰为 RAW 集合：杂散文件被排除。


def test_capture_configurable_dirs(tmp_path):
    # 测试：捕获目录可配置——guest_dirs=["raw","ref"] 时参考工件也被捕获。
    vm = _GuestVM({**RAW, "ref/target.mp4": b"reference artifact bytes"})
    # 额外的参考目录工件。
    r = P.capture_project_materials(vm, "p02", str(tmp_path),
                                    guest_dirs=["raw", "ref"])
    assert r["ok"] and r["files"] == 4
    # 4 个文件（3 RAW + 1 ref）。
    assert "ref/target.mp4" in P.read_manifest(str(tmp_path))
    # 参考工件进了 manifest。


@pytest.mark.parametrize('link_target', ['missing.txt', '/usr/bin/python3', '../../missing.txt'])
def test_broken_symlink_is_captured_replayed_and_verified_without_following(
        tmp_path, monkeypatch, link_target):
    """Adversarial filesystem fixtures may intentionally contain dead links."""
    # 测试（三种链接目标参数化）：对抗性文件系统里的死链/绝对链接/逃逸链接
    # 都被"按链接本身"捕获（记录 digest 与目标，不跟踪内容）、重放恢复、
    # 校验通过；链接目标被篡改时校验报出该文件的不匹配。

    guest_home = tmp_path / "guest-home"
    guest_tmp = tmp_path / "guest-tmp"
    project = guest_home / "evolution_project"
    project.mkdir(parents=True)
    guest_tmp.mkdir()
    (project / "source.txt").write_text("ground truth\n", encoding="utf-8")
    os.symlink(link_target, project / "broken-link.txt")
    # 在真实宿主目录构造夹具：一个正常文件 + 一个指向 link_target 的符号链接。

    monkeypatch.setattr(P, "GUEST_HOME", str(guest_home))
    monkeypatch.setattr(P, "GUEST_TGZ_OUT", str(guest_tmp / "out.tgz"))
    monkeypatch.setattr(P, "GUEST_TGZ_IN", str(guest_tmp / "in.tgz"))
    monkeypatch.setattr(P, "GUEST_B64_IN", str(guest_tmp / "in.b64"))
    monkeypatch.setattr(P, "GUEST_EXPECTED", str(guest_tmp / "expected"))
    monkeypatch.setattr(P, "GUEST_ACTUAL", str(guest_tmp / "actual"))
    # 把 provisioning 的全部 guest 路径重定向到临时目录（用真实 bash 执行）。

    class LocalVM:
        # 真实执行型伪 VM：命令交给 bash、文件经真实文件系统。
        def run_command(self, command, timeout=None, cap=None):
            completed = subprocess.run(
                ["bash", "-lc", command], capture_output=True, text=True,
                timeout=timeout, check=False)
            return completed.stdout
            # 真实 bash 执行命令，返回 stdout。

        def fetch_file(self, path, max_bytes=None):
            payload = open(path, "rb").read()
            if max_bytes is not None and len(payload) > max_bytes:
                return None, "too large"
            return payload, ""
            # 真实文件读取。

        def push_file(self, local_path, guest_path):
            os.makedirs(os.path.dirname(guest_path), exist_ok=True)
            shutil.copyfile(local_path, guest_path)
            return True, ""
            # 真实文件推送。

    vm = LocalVM()
    host_state = tmp_path / "host-state"
    captured = P.capture_project_materials(
        vm, "link-fixture", str(host_state),
        guest_dirs=["evolution_project"], attempts=1)
    # 捕获含链接的项目目录。

    assert captured["ok"] and captured["files"] == 2
    # 捕获成功：2 个条目（源文件 + 链接）。
    manifest = P.read_manifest(str(host_state))
    link_path = "evolution_project/broken-link.txt"
    assert manifest[link_path] == P._symlink_digest(link_target)
    # 链接条目的 manifest 值是符号链接摘要（不是目标内容哈希）。
    assert P.read_symlinks(str(host_state)) == {link_path: link_target}
    # 符号链接登记表记录链接→目标。

    shutil.rmtree(project)
    assert P.replay_project_materials(vm, str(host_state)) == {
        "ok": True, "mismatches": []}
    # 删光项目目录后重放：完全恢复。
    restored = project / "broken-link.txt"
    assert restored.is_symlink()
    assert os.readlink(restored) == link_target
    # 恢复的确实是符号链接且目标一致。
    if 'missing.txt' in link_target:
        assert not restored.exists()
        # 指向缺失目标的链接：恢复后 exists() 仍为 False（未被解引用填充）。
    assert P.verify_project_materials(vm, str(host_state)) == {
        "ok": True, "mismatches": []}
    # 只读校验通过。

    restored.unlink()
    os.symlink("different-missing.txt", restored)
    drift = P.verify_project_materials(vm, str(host_state))
    assert not drift["ok"]
    assert [item["file"] for item in drift["mismatches"]] == [link_path]
    # 换一个链接目标后：校验失败且不匹配恰为该链接。


def test_owned_paths_are_structured_and_legacy_falls_back_to_roots(tmp_path):
    # 测试：owned paths 优先读结构化清单；没有该清单时回退到 roots。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    assert P.read_owned_paths(str(tmp_path)) == ["raw"]
    # 捕获后（无 owned 清单）：回退为捕获根。

    P.write_owned_paths(str(tmp_path), ["raw", "result.mp4"])
    assert P.read_owned_paths(str(tmp_path)) == ["raw", "result.mp4"]
    # 写入结构化清单后：原样读回。


def test_capture_empty_guest_is_not_ok(tmp_path, monkeypatch):
    # 测试：guest 为空（供给器从未运行）时捕获必须失败，且不落盘空归档
    # ——空集不是合法工件集。
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    # 去掉重试等待。
    vm = _GuestVM({})                      # provisioner never ran
    # 空 guest。
    r = P.capture_project_materials(vm, "p03", str(tmp_path), attempts=1)
    assert not r["ok"]
    # 捕获失败。
    assert not os.path.exists(tmp_path / "materials.tgz")
    # 没有归档落盘。


# -------------------------------------------------------------- round trip --
def test_capture_replay_round_trip(tmp_path):
    # 测试：捕获→重放往返——新 guest 上恢复出同路径同字节的文件集，
    # 推送的 tgz 与宿主落盘的 tgz 逐字节一致（无重新生成）。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    # 第一次捕获。
    vm2 = _GuestVM({})                     # fresh project-night boot
    # 全新 guest。
    r = P.replay_project_materials(vm2, str(tmp_path))
    assert r == {"ok": True, "mismatches": []}
    # 重放成功零不匹配。
    assert vm2.files == RAW                # same paths, same bytes
    # guest 文件树与原始集合完全一致。
    host_tgz = (tmp_path / "materials.tgz").read_bytes()
    assert vm2.pushed_tgz == host_tgz      # byte-verbatim transport, no regen
    # 推送字节 = 宿主归档字节：传输逐字节保真。


def test_replay_prefers_streaming_upload_and_verifies_archive(tmp_path):
    # 测试：重放优先用 push_file 流式上传整个归档（不走 base64 分块解码），
    # 上传后仍做 guest 内哈希校验。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class StreamingGuestVM(_GuestVM):
        def push_file(self, local_path, guest_path):
            assert guest_path == P.GUEST_TGZ_IN
            self.pushed_tgz = open(local_path, "rb").read()
            return True, ""
            # 流式上传：整个本地文件直接进 pushed_tgz。

    vm = StreamingGuestVM({})
    result = P.replay_project_materials(vm, str(tmp_path))

    assert result == {"ok": True, "mismatches": []}
    assert vm.files == RAW
    # 恢复完整文件集。
    assert not any("DECODE_RC" in command for command in vm.cmds)
    # 没有走 base64 解码路径：流式上传优先。


def test_replay_rejects_corrupted_streaming_upload_before_extract(tmp_path):
    # 测试：流式上传被翻转一字节后，guest 内字节校验在解包前抓住——
    # 重放失败（错误含 "byte verification"），guest 文件树保持干净。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class CorruptStreamingGuestVM(_GuestVM):
        def push_file(self, local_path, _guest_path):
            payload = open(local_path, "rb").read()
            self.pushed_tgz = payload[:-1] + bytes([payload[-1] ^ 1])
            return True, ""
            # 上传前翻转最后一个字节（基础设施腐化模拟）。

    vm = CorruptStreamingGuestVM({})
    result = P.replay_project_materials(vm, str(tmp_path))

    assert not result["ok"]
    # 重放失败。
    assert "byte verification" in result["error"]
    # 错误指明是字节校验失败。
    assert vm.files == {}
    # 损坏归档未被解包：guest 保持干净。


def test_read_only_verify_accepts_exact_and_rejects_mutation(tmp_path):
    # 测试：只读校验（verify）在精确集合上通过；Verifier 侧文件被替换后
    # 校验失败并精确指出被改文件。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    vm = _GuestVM({})
    assert P.replay_project_materials(vm, str(tmp_path))["ok"]
    # 重放成功。
    assert P.verify_project_materials(vm, str(tmp_path)) == {
        "ok": True, "mismatches": []}
    # 校验通过。

    vm.files["raw/clip0.mp4"] = b"verifier-side replacement"
    # Verifier 侧替换一个文件。
    result = P.verify_project_materials(vm, str(tmp_path))
    assert not result["ok"]
    assert [item["file"] for item in result["mismatches"]] == [
        "raw/clip0.mp4"]
    # 校验失败且只报这一个文件。


def test_replay_wipes_stale_guest_state(tmp_path):
    # 测试：重放先擦除 guest 旧状态再恢复——死亡会话的残留文件不存活；
    # 恢复后的集合与捕获集合精确相等。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    vm2 = _GuestVM({"raw/leftover.mp4": b"stale from a dead session"})
    # 带残留的 guest。
    r = P.replay_project_materials(vm2, str(tmp_path))
    assert r["ok"] and vm2.files == RAW    # captured set EXACTLY
    # 残留被擦除：文件树精确等于捕获集合。


def test_replay_owns_and_wipes_an_initially_empty_root(tmp_path):
    # 测试：重放拥有全部 owned 根（含 guest 上本不存在的 output）——
    # 空根里的迟到文件同样被擦除。
    P.capture_project_materials(
        _GuestVM(RAW), "p01", str(tmp_path), guest_dirs=["raw", "output"])
    assert P.read_roots(str(tmp_path)) == ["output", "raw"]
    # 捕获根含 guest 上为空的 output（登记为拥有）。
    vm2 = _GuestVM({"output/late-research.bin": b"must not survive"})
    # guest 在空根里放了文件。

    r = P.replay_project_materials(vm2, str(tmp_path))

    assert r["ok"]
    assert vm2.files == RAW
    # 迟到文件被擦除：恢复后精确等于捕获集合。


def test_replay_fails_closed_when_wipe_transport_is_ambiguous(tmp_path):
    # 测试：擦除传输不明确（通道错误而非明确 RC=0）时重放 fail-closed——
    # 绝不在"不确定是否擦干净"的 guest 上恢复材料。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class WipeFailureVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_WIPE_RC" in cmd:
                return "[channel error: transport lost]"
                # 擦除命令返回通道错误（无 RC 标记）。
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(
        WipeFailureVM({"raw/stale.bin": b"stale"}), str(tmp_path))
    assert not r["ok"]
    # 重放失败。
    assert "wipe" in r["error"]
    # 错误指明擦除环节。


def test_replay_rejects_extra_file_even_when_expected_hashes_pass(tmp_path):
    # 测试：即使期望文件的哈希全部通过，多出的文件（迟到写入者）也必须
    # 被拒绝——集合精确性独立于哈希正确性。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class ExtraFileVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                self.files["raw/late-writer.bin"] = b"contamination"
                # 集合核对前偷偷多写一个文件。
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(ExtraFileVM({}), str(tmp_path))
    assert not r["ok"]
    assert "file set" in r["error"]
    # 重放失败，错误指明文件集合不一致。


def test_exact_set_command_rejects_any_non_directory_entry(tmp_path):
    # 测试：精确集合命令必须检查"非目录条目"（! -type d -print）——
    # guest 里出现命名管道等特殊条目时集合核对必须失败。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))

    class SpecialEntryVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                assert "! -type d -print" in cmd
                # 断言集合命令确实扫描非目录条目。
                return "PROJECT_SET_RC=1"
                # 模拟 guest 发现特殊条目。
            return super().run_command(cmd, timeout=timeout, cap=cap)

    result = P.replay_project_materials(SpecialEntryVM({}), str(tmp_path))
    assert not result["ok"]
    assert "file set" in result["error"]
    # 重放 fail-closed。


def test_replay_detects_late_writer_in_owned_empty_root(tmp_path):
    # 测试：拥有的空根（output）里的迟到写入者同样被集合核对抓住。
    P.capture_project_materials(
        _GuestVM(RAW), "p01", str(tmp_path), guest_dirs=["raw", "output"])

    class EmptyRootWriterVM(_GuestVM):
        def run_command(self, cmd, timeout=None, cap=4000):
            if "PROJECT_SET_RC" in cmd:
                self.files["output/late-writer.bin"] = b"contamination"
                # 空根里的迟到文件。
            return super().run_command(cmd, timeout=timeout, cap=cap)

    r = P.replay_project_materials(EmptyRootWriterVM({}), str(tmp_path))
    assert not r["ok"]
    assert "file set" in r["error"]


# ---------------------------------------------------------------- mismatch --
def test_long_path_checksums_are_lossless_and_still_detect_corruption(tmp_path):
    # 测试：长路径（多层嵌套）的校验输出无损——旧实现的输出截断会漏掉
    # 中段文件；现在 50 个深层文件全部校验，中段损坏仍被精确发现。
    files = {
        f"raw/{'nested-directory/' * 12}artifact_{i:03d}.bin": str(i).encode()
        for i in range(50)
    }
    # 50 个 12 层嵌套的长路径文件。
    captured = P.capture_project_materials(_GuestVM(files), "long", str(tmp_path))
    assert captured["ok"]
    vm = _GuestVM()
    assert P.replay_project_materials(vm, str(tmp_path))["ok"]
    # 长路径捕获/重放全通过。
    damaged = sorted(files)[12]  # in the middle that the old output cap omitted
    vm.files[damaged] = b"changed"
    result = P.verify_project_materials(vm, str(tmp_path))
    assert not result["ok"]
    assert [m["file"] for m in result["mismatches"]] == [damaged]
    # 精确找出中段那个被改文件。


def test_corrupted_byte_reported_as_mismatch(tmp_path):
    # 测试：宿主侧归档腐化一个字节 → 重放报告恰一个不匹配，且 expected/got
    # 分别是原始字节与腐化字节的 sha256（差异定位到具体文件）。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    rot = dict(RAW)                        # host-side rot: one byte flipped
    rot["raw/clip1.mp4"] = RAW["raw/clip1.mp4"][:-1] + b"\x00"
    # 翻转 clip1 的最后一个字节。
    (tmp_path / "materials.tgz").write_bytes(_tgz_of(rot))
    # 用腐化内容覆盖宿主归档。
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert r["ok"] is False and len(r["mismatches"]) == 1
    # 恰一个不匹配。
    mm = r["mismatches"][0]
    assert mm["file"] == "raw/clip1.mp4"
    assert mm["expected"] == hashlib.sha256(RAW["raw/clip1.mp4"]).hexdigest()
    assert mm["got"] == hashlib.sha256(rot["raw/clip1.mp4"]).hexdigest()
    # expected=原始哈希、got=腐化哈希：定位精确。


def test_missing_file_reported_as_mismatch(tmp_path):
    # 测试：宿主归档缺一个文件 → 重放报告该文件不匹配且 got 为 None
    # （缺失与损坏可区分）。
    P.capture_project_materials(_GuestVM(RAW), "p01", str(tmp_path))
    short = {k: v for k, v in RAW.items() if k != "raw/clip0.mp4"}
    # 归档里去掉 clip0。
    (tmp_path / "materials.tgz").write_bytes(_tgz_of(short))
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert not r["ok"]
    assert [m["file"] for m in r["mismatches"]] == ["raw/clip0.mp4"]
    assert r["mismatches"][0]["got"] is None
    # 缺失文件的 got 为 None。


def test_replay_without_host_state_is_infra_failure(tmp_path):
    # 测试：宿主根本没有捕获状态（空目录）时重放是基础设施失败——
    # ok=False、零不匹配清单、带 error 说明（不是"文件对不上"）。
    r = P.replay_project_materials(_GuestVM({}), str(tmp_path))
    assert not r["ok"] and r["mismatches"] == [] and "error" in r
    # 基础设施失败形状：错误信息在 error 字段。
