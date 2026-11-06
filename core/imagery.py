"""Look-attachment preparation (v13) — fix the EYES mechanically, not the prompt.

A full-page technical image (A4 drawing at 300 dpi ≈ 2480x3508) gets downsampled by
VLM preprocessing to a working resolution where small callout text and thin line-work
become physically illegible — the model is not careless, it literally cannot see.
The harness therefore attaches, alongside a bounded overview, NATIVE-RESOLUTION
overlapping tiles of any large image (and performs requested region crops
server-side), so a look sees what a human sees when they zoom. No behavioral demand
on the model; degrades gracefully to pass-through when PIL is unavailable.
"""
import io
import logging
import re

log = logging.getLogger("rsiagent.imagery")

# 概览/切片输出的长边像素上限
MAX_SIDE = 1400          # overview / tile output bound (px, long side)
# 长边超过该阈值的图片额外附原生分辨率切片
TILE_THRESHOLD = 1800    # images larger than this (long side) also get tiles
# 切片重叠比例（接缝上的特征保持完整）
OVERLAP = 0.10           # tile overlap fraction (features on seams stay whole)
# 一次视觉代理网格操作的单元数上限（刻意 <= 6）
MAX_GRID_ATTACH = 6      # one vision-agent grid operation is deliberately <= 6 cells


# v32.5: when set (loop wires it from cfg.archive_eyes + the run's sink root), every
# encoded image handed to the eyes is also written here — results/<task>/<seed>/eyes/
# NNN_<label>.png|jpg — so a human can inspect EXACTLY what the model saw, per look.
# Motivated by the alpha-flatten bug hiding for 3 runs: text about images is not
# evidence about images; now the pixels themselves are part of the run artifacts.
# （中文）眼睛归档目录：设置后（由 loop 根据 cfg.archive_eyes + 本次 sink 根目录
# 接线），每张交给眼睛的编码图像都会落盘到 results/<task>/<seed>/eyes/，让人类能
# 逐次 look 检查模型“到底看到了什么”。
ARCHIVE_DIR = None
# 归档序号（列表包装以便就地自增）
_ARCHIVE_SEQ = [0]


def _archive(data: bytes, label: str) -> None:
    # 未启用归档：直接返回
    if not ARCHIVE_DIR:
        return
    try:
        import os
        import re as _re
        # 确保目录存在
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        # 序号自增，保证文件名唯一有序
        _ARCHIVE_SEQ[0] += 1
        # 按魔数判扩展名（PNG 文件头 / 其余按 jpg）
        ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        # 标签清洗成安全文件名片段（限 60 字符）
        slug = _re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:60] or "img"
        with open(os.path.join(ARCHIVE_DIR,
                               f"{_ARCHIVE_SEQ[0]:03d}_{slug}.{ext}"), "wb") as f:
            f.write(data)
    except Exception:                                      # never break a look
        # 归档失败绝不影响 look 本身
        pass


def _flatten_alpha(img):
    """v32.4: flatten transparency onto a CONTRAST-AWARE background before the VLM
    sees it. Sending raw RGBA lets the provider flatten onto black, which DESTROYS
    dark-on-transparent content: t003's rain overlay (dark blue streaks on alpha)
    became near-invisible specks the eyes read as 'sparkle motifs on black' — a
    pipeline artifact we mistook for a semantic ceiling until a human referee viewed
    the image (2026-07-15). Background is chosen against the content's own
    luminance: dark content -> white, light content -> black. Returns
    (rgb_image, note); note is '' for opaque images."""
    from PIL import Image
    if img.mode == "P":
        # 调色板图：带透明信息转 RGBA，否则直接 RGB
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    if img.mode not in ("RGBA", "LA"):
        # 本就不透明：原样返回（note 为空）
        return img, ""
    rgba = img.convert("RGBA")
    # 分离 alpha 通道
    alpha = rgba.split()[-1]
    # 灰度图用于统计内容亮度
    lum = rgba.convert("L")
    # alpha 阈值掩码（>32 视为实际可见内容）
    mask = alpha.point(lambda a: 255 if a > 32 else 0)
    # 统计可见内容的亮度直方图
    stat = lum.histogram(mask)
    total = sum(stat) or 1
    # 可见内容的平均亮度
    mean_lum = sum(i * c for i, c in enumerate(stat)) / total
    # 对比感知底色：内容偏暗则垫白，偏亮则垫黑
    bg_val = 255 if mean_lum < 128 else 0
    bg = Image.new("RGB", rgba.size, (bg_val,) * 3)
    # 用 alpha 掩码把原图贴到底色上，完成压平
    bg.paste(rgba, mask=alpha)
    # 返回压平后的 RGB 图与说明备注
    return bg, ("(transparent image flattened onto "
                + ("white" if bg_val else "black") + " for viewing)")


def _encode(img) -> bytes:
    # 先压平透明通道
    img, _ = _flatten_alpha(img)
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        # 仍带透明/调色板模式：存 PNG（无损）
        img.save(buf, format="PNG", optimize=True)
    else:
        # 普通图转 RGB 后存 JPEG（质量 88，控制负载）
        img.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _bounded(img, max_side: int = MAX_SIDE):
    w, h = img.size
    if max(w, h) <= max_side:
        # 已在限制内：原样返回
        return img
    # 等比缩放到长边上限
    s = max_side / max(w, h)
    return img.resize((max(1, int(w * s)), max(1, int(h * s))))


def grid_cells(datas: list, rows: int, cols: int, cell=None):
    """v22.1 (vision-agent): split the FIRST image into a rows x cols grid and return
    native-resolution cell(s), bounded. ``cell=[r,c]`` -> that one cell (deep zoom for
    counting a section); ``cell=None`` -> all cells row-major (up to MAX_GRID_ATTACH), each
    labelled, so a VLM can count per-cell then sum. Returns (attachments, note)."""
    try:
        from PIL import Image
    except ImportError:
        # 无 PIL：优雅降级为原样透传
        return datas[:1], ""
    if not datas:
        # 没有输入图：空结果
        return [], ""
    try:
        img = Image.open(io.BytesIO(datas[0])); img.load()
    except Exception:                                      # noqa: BLE001
        # 打不开：原样返回第一张
        return datas[:1], ""
    # 行列数钳制在 1..6
    rows = max(1, min(int(rows), 6)); cols = max(1, min(int(cols), 6))
    w, h = img.size
    # 每个单元的宽高
    cw, ch = w / cols, h / rows
    # 指定单元则只取该格，否则全部行列（行优先）
    want = ([tuple(cell)] if (cell and len(cell) == 2)
            else [(r, c) for r in range(rows) for c in range(cols)])
    # 全格模式且超出上限：标记为被裁剪
    capped = cell is None and len(want) > MAX_GRID_ATTACH
    out, parts = [], []
    for (r, c) in want:
        if len(out) >= MAX_GRID_ATTACH:
            # 达到附加上限即停
            break
        # 行列号钳制到有效范围
        r = max(0, min(int(r), rows - 1)); c = max(0, min(int(c), cols - 1))
        # 计算单元包围盒并裁剪
        box = (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))
        out.append(_encode(_bounded(img.crop(box))))
        parts.append(f"row {r},col {c}")
    note = f"{rows}x{cols} grid, native-res cell(s): " + "; ".join(parts) + "."
    if capped:                                        # the agent prompt requires <= 6 grid cells
        # 被裁剪时在备注里提示改用更粗的网格
        note += (f" (ONLY {MAX_GRID_ATTACH} of {rows * cols} cells shown — use a "
                 f"COARSER grid of <= {MAX_GRID_ATTACH} cells to see them all at "
                 "once and sum reliably.)")
    for _d, _l in zip(out, parts):                         # v32.5: eyes archive
        # 逐单元归档
        _archive(_d, "grid_" + _l)
    return out, note


_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
# 客户机内渲染工作目录
_RENDER_DIR = "/tmp/rsiagent_render"
# 渲染超时：soffice 冷启动可达 20-40 秒，留足余量
_RENDER_TIMEOUT = 120          # soffice cold-start can be 20-40s; give headroom


def fetch_look_image(vm, path: str):
    """v26: fetch ``path`` from the VM as IMAGE bytes, rendering documents automatically.

    A `look` needs an image, but a deliverable is often a document (a .pptx is a zip of
    XML, not a picture). Before v26 the CALLER had to render it — the inspector authored
    a render pipeline across STATELESS probes (each run_command is a fresh shell, so cwd
    never persisted) and either fumbled it or spent its whole probe budget on it (t063
    inspections @80/@82). Here the harness does it: an image path is returned as-is; a
    document is rendered to a PNG in-VM with ONE known-good, absolute-path command and
    that PNG is returned. So a caller can `look` straight at a deliverable file. Returns
    (bytes, err) exactly like ``vm.fetch_file`` (err '' on success). General machinery —
    the only knowledge is 'documents render to images', no task/format specifics.

    v29: a target of ``screen:`` (optionally ``screen:<display>``, default :0) captures
    the LIVE screen — the running browser/app window — to a PNG and returns that. Stateful
    web/app tasks keep their deliverable in session memory, not a file; without this the
    inspector can only probe the filesystem and mis-judges genuinely-completed work as
    absent (t021 drove both web apps + read the card, scored 0 because the verifier could
    not SEE the session). The harness runs ONE known-good capture; the caller just
    ``look``s at ``screen:``. Same general-machinery contract; no task knowledge."""
    # 小写化便于扩展名/前缀判断
    low = path.lower()
    if low.startswith("screen:"):                          # v29: capture the LIVE screen
        # 取显示号（缺省 :0）
        disp = path.split(":", 1)[1].strip() or "0"
        if not re.fullmatch(r":?\d+(?:\.\d+)?", disp):
            # 显示号必须是数字形式的 X display
            return None, "invalid screen display; expected a numeric X display"
        disp = disp if disp.startswith(":") else f":{disp}"
        cap = (f"rm -rf {_RENDER_DIR} && mkdir -p {_RENDER_DIR} && "
               # try import(ImageMagick) then scrot then gnome-screenshot — first that exists
               f"(DISPLAY={disp} import -window root {_RENDER_DIR}/screen.png 2>/dev/null "
               f"|| DISPLAY={disp} scrot {_RENDER_DIR}/screen.png 2>/dev/null "
               f"|| DISPLAY={disp} gnome-screenshot -f {_RENDER_DIR}/screen.png 2>/dev/null); "
               f"ls {_RENDER_DIR}/screen.png 2>/dev/null")
        try:
            out = vm.run_command(cap, timeout=_RENDER_TIMEOUT)
        except Exception as e:                             # noqa: BLE001
            return None, f"screen capture failed ({type(e).__name__})"
        # 从输出中取实际生成的截图路径
        shot = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
        if not shot.endswith("screen.png"):
            # 没有可用截图工具/显示：明确失败
            return None, "could not capture the live screen (no display/tool)"
        return vm.fetch_file(shot)
    if low.endswith(_IMG_EXT):                             # already a picture
        # 本来就是图片：直接取回
        return vm.fetch_file(path)
    q = "'" + path.replace("'", "'\\''") + "'"            # single-quote for the shell
    if low.endswith(".pdf"):
        # PDF：pdftoppm 渲染第 1 页（120 dpi）
        conv = f"pdftoppm -png -f 1 -l 1 -r 120 {q} {_RENDER_DIR}/page"
    else:                                                 # office/vector formats -> soffice
        # Office/矢量文档：无头 soffice 转 PNG；用独立 profile 避免与运行中的实例抢锁
        conv = ("soffice --headless "
                f"-env:UserInstallation=file://{_RENDER_DIR}/profile "   # own profile: no
                f"--convert-to png --outdir {_RENDER_DIR} {q}")          # lock vs a running
    cmd = (f"rm -rf {_RENDER_DIR} && mkdir -p {_RENDER_DIR} && {conv} >/dev/null 2>&1; "
           f"ls {_RENDER_DIR}/*.png 2>/dev/null | head -1")             # echo the PNG path
    try:
        out = vm.run_command(cmd, timeout=_RENDER_TIMEOUT)
    except Exception as e:                                 # noqa: BLE001
        return None, f"render failed ({type(e).__name__})"
    # 从输出中找渲染出的 PNG 路径
    png = ""
    for line in (out or "").splitlines():
        line = line.strip()
        if line.endswith(".png"):
            png = line
            break
    if not png:
        # 没有产出：明确失败
        return None, f"could not render {path} to an image (no output produced)"
    # 取回渲染好的 PNG
    return vm.fetch_file(png)


def prepare_look_images(datas: list, region=None, max_side: int = None):
    """``datas``: list of raw image bytes. Returns (attachments, note) where
    ``attachments`` preserves every requested look item, in input order, and ``note``
    describes that order for the model. A single large image receives overview + zoom
    tiles. A multi-image comparison receives one bounded full view per item so tiles
    cannot change image numbering or crowd later requested paths out of the call.
    Pass-through (with empty note) when PIL is missing."""
    try:
        from PIL import Image
    except ImportError:                                    # graceful degrade
        # 无 PIL：原样透传
        return datas, ""
    out, parts = [], []
    # 多图对比模式：只给每张一个受限全览，不再切瓦片
    multi = len(datas) > 1
    for k, data in enumerate(datas):
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:                                  # noqa: BLE001 — not an
            out.append(data)                               # image PIL groks: attach
            parts.append(f"image {k + 1} as given")        # raw, let the API judge
            continue
        if region and len(region) == 4:
            try:
                # 请求了区域裁剪：按坐标裁剪（即“放大”操作本身）
                x0, y0, x1, y1 = (int(v) for v in region)
                img = img.crop((max(0, x0), max(0, y0),
                                min(img.width, x1), min(img.height, y1)))
                out.append(_encode(_bounded(img, max_side or MAX_SIDE)))
                parts.append(f"image {k + 1}: requested region ({x0},{y0})-({x1},{y1})")
                continue                                   # a crop IS the zoom —
            except Exception:                              # noqa: BLE001
                pass                                       # bad box -> fall through
        w, h = img.size
        if max(w, h) <= TILE_THRESHOLD or multi:
            # 不需要切片（图小或多图模式）：单张受限全览
            out.append(_encode(_bounded(img, max_side or MAX_SIDE)))
            suffix = " (downscaled)" if max(w, h) > (max_side or MAX_SIDE) else ""
            parts.append(f"image {k + 1}: full view{suffix}")
            continue
        out.append(_encode(_bounded(img, max_side or MAX_SIDE)))   # overview first
        names = ("top-left", "top-right", "bottom-left", "bottom-right")
        ow, oh = int(w * OVERLAP), int(h * OVERLAP)
        # 四象限重叠切片的原生分辨率包围盒
        boxes = [(0, 0, w // 2 + ow, h // 2 + oh),
                 (w // 2 - ow, 0, w, h // 2 + oh),
                 (0, h // 2 - oh, w // 2 + ow, h),
                 (w // 2 - ow, h // 2 - oh, w, h)]
        added = []
        for name, box in zip(names, boxes):
            out.append(_encode(_bounded(img.crop(box))))
            added.append(name)
        parts.append(f"image {k + 1}: full view (downscaled), then native-resolution "
                     f"tiles ({', '.join(added)}, {int(OVERLAP * 100)}% overlap)")
    note = "Attached: " + "; ".join(parts) + "." if parts else ""
    # v34: `parts` is one entry per LOOK ITEM (the tile path adds 5 attachments
    # under a single entry), so zip(out, parts) mislabeled and DROPPED archives.
    # Archive with a parallel per-attachment label list instead.
    # （中文）v34：parts 是按“look 条目”计数的（切片路径在一个条目下新增 5 张附件），
    # 直接 zip 会错标并漏归档；改为并行的逐附件标签列表。
    labels = list(parts)
    while len(labels) < len(out):
        labels.append(f"{labels[-1] if labels else 'img'}_tile{len(labels)}")
    for _d, _l in zip(out, labels):                        # v32.5/v34: eyes archive
        _archive(_d, _l)
    return out, note
