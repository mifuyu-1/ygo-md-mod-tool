# -*- coding: utf-8 -*-
"""超框卡图（over-frame）合成与写入。

超框 = 卡图主体跨出插图框线的那种卡面。游戏不是靠一张散落图片认超框，而是靠
一个 AssetBundle 里的 TextAsset ``of_card_asset``：little-endian uint16 对，
(触发卡号, 底图卡号)。只换纹理不改这张表，游戏会按普通卡渲染，超出的部分被裁掉。

素材在 ``tools``：
    border/*.png              边框叠加图，下拉栏列出全部可选

卡框不随包：型号 = 游戏 ``data.unity3d`` 里的 ``card_frameNN``，或界面里选的本地图片。
边框：下拉栏从 ``tools/border`` 读取全部 PNG，可选「不变边框」

合成顺序：
    卡框 → 背景 Cover 填满插图窗 → 边框叠加 → 主体放大跨出框线
    → 点亮箭头（连接怪兽）→ 效果框按透明度压顶

版式固定按原卡图（普通 / 灵摆），由 ``render(layout=...)`` 传入。
"""
import json
import os
import shutil
import struct
import zlib
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

import core

CARD_W = 704
CARD_H = 1024

TOOLS_DIR = core.RESOURCE_DIR / "tools"

GATE_NAME = "of_card_asset"
REC = 4
# 插图层选「空白」时的标记，表示插图窗不铺任何底图
BLANK_ART = "__blank__"
# 卡图工程文件（.zip）的格式版本
PROJECT_VERSION = 1
# 当前版本实测的 gate bundle id。游戏更新后会变，找不到时会动态扫描。
GATE_KNOWN_IDS = ("99f84cce",)

# 插图窗 / 灵摆插图窗
ART_WINDOW = (89, 191, 527, 528)
PENDULUM_ART_WINDOW = (50, 186, 604, 451)
# 效果框（含黑边）外接矩形
LORE_RECT = (41, 755, 623, 217)
PENDULUM_LORE_RECT = (41, 637, 623, 334)

# 主体相对插图窗的放大倍数（>1 才会跨出框线）
OVERFLOW_SCALE = 1.38

# 边框叠加：下拉栏列出 tools/border 下的所有 PNG，空字符串=不变边框
BORDER_DIR = TOOLS_DIR / "border"
BORDER_LABELS = {
    "border_rainbow.png": "虹彩（整圈艳彩虹）",
    "border_glory.png": "幻彩（柔和淡彩环带）",
    "border_official.png": "官方（还原官方超框）",
    "border_soft.png": "柔和（半透明软边）",
}
BORDER_NONE = "不变边框"

# 这些编号是素材文件名里的 NN：02=仪式，03=融合，07=魔法，08=陷阱，
# 10=同调，12=超量，18=连接；13~19 是灵摆组。
FRAME_NAMES = {
    0: "通常怪兽", 1: "效果怪兽", 2: "仪式怪兽", 3: "融合怪兽",
    7: "魔法卡", 8: "陷阱卡", 9: "衍生物", 10: "同调怪兽", 12: "超量怪兽",
    13: "灵摆通常", 14: "灵摆效果", 15: "灵摆超量", 16: "灵摆同调",
    17: "灵摆融合", 18: "连接怪兽", 19: "灵摆仪式",
}
PENDULUM = {13, 14, 15, 16, 17, 19}
LINK_FRAME = 18
# 版式固定按卡图原来来：普通卡用 1 号版式，灵摆卡（原始卡图 512×1024 且非超框）用 13 号；
# 超框贴图也是竖图，判定时必须排除，制作时会把版式存进编辑记录
LAYOUT_NORMAL = 1
LAYOUT_PENDULUM = 13

# 连接怪兽的 8 个箭头。按 Floowan 超框1 的做法：不抠掩码，用近黑芯拟合三角形，
# 再用有符号距离场画银边和填充。
LINK_ARROWS = (
    (42, 142, 120, 125),   # 左上
    (282, 132, 140, 72),   # 上
    (537, 141, 125, 125),  # 右上
    (30, 382, 72, 145),    # 左
    (598, 382, 74, 145),   # 右
    (26, 642, 135, 132),   # 左下
    (282, 700, 140, 74),   # 下
    (536, 642, 135, 132),  # 右下
)
LINK_CORE_LUM = 32
LINK_RIM_PX = 6.4
LINK_INSET_PX = 0.0
LINK_HALO_PX = 3.4
LINK_HALO_SOLID = 1.3
LINK_EDGE_AA = 0.85
LINK_EXPAND_PX = -0.6
LINK_INSET = (8, 8, 12)
LINK_RIM_INNER = (178, 156, 152)
LINK_RIM_OUTER = (218, 230, 244)
LINK_HALO_ALPHA = 215
LINK_LIT_HOT = (255, 242, 224)
LINK_LIT_MID = (248, 126, 58)
LINK_LIT_EDGE = (146, 18, 12)


class OverframeError(Exception):
    pass


# ------------------------------------------------------------------ 素材
def frame_name(n: int) -> str:
    return FRAME_NAMES.get(int(n), f"卡框 {int(n):02d}")


# ------------------------------------------------------------------ 游戏内卡框
# masterduel_Data/data.unity3d 里带 card_frameNN 贴图（704×1024，插图窗镂空），
# 就是游戏渲染卡面用的原生卡框，直接拿来当底比随包素材更贴合游戏。
GAME_DATA_REL = ("masterduel_Data", "data.unity3d")

_gframe_ids: dict = {}
_gframe_img: dict = {}


def _game_data_path(paths) -> Path:
    return Path(paths.game_root) / GAME_DATA_REL[0] / GAME_DATA_REL[1]


def _gframe_tag(paths) -> str | None:
    try:
        st = _game_data_path(paths).stat()
    except OSError:
        return None
    return f"{st.st_size}-{int(st.st_mtime)}"


def _gframe_index_file(paths) -> Path | None:
    tag = _gframe_tag(paths)
    if not tag:
        return None
    return _state_dir() / "gameframes" / f"{tag}_index.json"


def _gframe_disk(paths, cid: int) -> Path | None:
    """游戏卡框的磁盘缓存路径（data.unity3d 大小+时间做版本号）。"""
    tag = _gframe_tag(paths)
    if not tag:
        return None
    return _state_dir() / "gameframes" / f"{tag}_{int(cid):02d}.png"


def _load_gframe_index(paths):
    f = _gframe_index_file(paths)
    if not f or not f.is_file():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return {int(k): (str(v[0]), int(v[1])) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return None


def game_frame_ids(paths) -> list[int]:
    """游戏里可用的卡框编号（升序）。没有游戏数据时返回空表。"""
    p = _game_data_path(paths)
    if not p.is_file():
        return []
    try:
        st = p.stat()
        key = (str(p), st.st_size, int(st.st_mtime))
    except OSError:
        return []
    if _gframe_ids.get("key") != key:
        found = _load_gframe_index(paths)
        if found is None:
            import UnityPy
            found = {}
            try:
                for o in UnityPy.load(str(p)).objects:
                    if o.type.name != "Texture2D":
                        continue
                    d = o.read()
                    nm = str(getattr(d, "m_Name", ""))
                    if not nm.startswith("card_frame"):
                        continue
                    if (d.m_Width, d.m_Height) != (CARD_W, CARD_H):
                        continue
                    tail = nm[len("card_frame"):]
                    if tail.isdigit():
                        found[int(tail)] = (nm, int(o.path_id))
            except Exception:
                found = {}
            f = _gframe_index_file(paths)
            if f and found:
                try:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    payload = {str(k): list(v) for k, v in found.items()}
                    f.write_text(json.dumps(payload), encoding="utf-8")
                except OSError:
                    pass
        _gframe_ids.clear()
        _gframe_ids.update(key=key, ids=found)
    return sorted(_gframe_ids.get("ids", {}))


def game_frame_image(paths, n: int) -> Image.Image:
    """取游戏原生卡框（704×1024）。进程内缓存 + 磁盘缓存，切换卡/重开工具不再解包。"""
    cid = int(n)
    key = (str(_game_data_path(paths)), cid)
    if key in _gframe_img:
        return _gframe_img[key]

    # 先看磁盘缓存，命中就完全不用扫描 data.unity3d
    cached = _gframe_disk(paths, cid)
    if cached and cached.is_file():
        try:
            img = _open(cached)
            _gframe_img[key] = img
            return img
        except OverframeError:
            pass

    if not _game_data_path(paths).is_file():
        raise OverframeError(
            "找不到游戏数据 masterduel_Data/data.unity3d，请确认游戏目录是否正确")
    if cid not in _gframe_ids.get("ids", {}):
        game_frame_ids(paths)
    info = _gframe_ids.get("ids", {}).get(cid)
    if not info:
        raise OverframeError(f"游戏里没有卡框 {cid:02d}")

    import UnityPy
    img = None
    for o in UnityPy.load(str(_game_data_path(paths))).objects:
        if o.type.name == "Texture2D" and int(o.path_id) == info[1]:
            img = o.read().image
            break
    if img is None:
        raise OverframeError(f"读取游戏卡框 {cid:02d} 失败")
    img = img.convert("RGBA")
    if img.size != (CARD_W, CARD_H):
        img = img.resize((CARD_W, CARD_H), Image.LANCZOS)
    _gframe_img[key] = img
    if cached:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            # 游戏更新后旧版本的缓存没用了，顺手清掉（只删别的版本，保留同版本其它卡框）
            tag = _gframe_tag(paths)
            for old in cached.parent.glob("*.png"):
                if old.name != cached.name and not (tag and old.name.startswith(f"{tag}_")):
                    old.unlink(missing_ok=True)
            img.save(cached, "PNG", optimize=True)
        except OSError:
            pass
    return img


def game_frame_options(paths) -> list[tuple[str, int]]:
    return [(f"{n:02d} · {frame_name(n)}", n) for n in game_frame_ids(paths)]


_border_cache: dict = {}
_border_scan = {"key": None, "paths": {}}


def _border_dirs() -> list:
    """边框素材目录：随包 tools/border 优先，其次是工具目录下的历史边框。"""
    dirs = [BORDER_DIR]
    state = _state_dir() / "borders"
    if state != BORDER_DIR:
        dirs.append(state)
    return dirs


def _border_key() -> tuple:
    key = []
    for d in _border_dirs():
        try:
            key.append((str(d), d.stat().st_mtime_ns))
        except OSError:
            key.append((str(d), None))
    return tuple(key)


def border_paths() -> dict:
    """文件名 → 路径（tools/border 优先）。目录没变时复用上次扫描结果。"""
    key = _border_key()
    if _border_scan["key"] != key:
        found = {}
        for d in reversed(_border_dirs()):
            if d.is_dir():
                for p in sorted(d.glob("*.png")):
                    found[p.name] = p
        _border_scan["key"] = key
        _border_scan["paths"] = found
    return dict(_border_scan["paths"])


def borders() -> list[str]:
    """可用的边框叠加图文件名（随包 + 历史导入）。"""
    return sorted(border_paths())


def adopt_border(src) -> str:
    """把旧记录/旧工程里的边框图收进工具目录 borders/，返回文件名。"""
    p = Path(str(src or ""))
    if not p.is_file():
        return ""
    name = p.name
    if name in border_paths():
        return name
    dst = _state_dir() / "borders" / name
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    except OSError:
        return ""
    return name


def border_options() -> list[tuple[str, str]]:
    """界面下拉框用：[(显示名, 文件名)]，开头是「不变边框」。"""
    out = [(BORDER_NONE, "")]
    for name in borders():
        out.append((BORDER_LABELS.get(name, Path(name).stem), name))
    return out


def normalize_border(name) -> str:
    n = Path(str(name or "")).name
    return n if n and n in border_paths() else ""


def border_image(name) -> Image.Image | None:
    """取边框叠加层（704×1024）；没选或找不到返回 None。"""
    n = normalize_border(name)
    if not n:
        return None
    if n not in _border_cache:
        try:
            im = _open(border_paths()[n])
        except (OverframeError, KeyError):
            return None
        _border_cache[n] = _fit_cover(im, CARD_W, CARD_H, Image.LANCZOS)
    return _border_cache[n]


def art_box(n: int) -> tuple:
    return PENDULUM_ART_WINDOW if int(n) in PENDULUM else ART_WINDOW


def lore_rect(n: int) -> tuple:
    return PENDULUM_LORE_RECT if int(n) in PENDULUM else LORE_RECT


def lore_top(n: int) -> int:
    return lore_rect(n)[1]


def lore_bottom(n: int) -> int:
    x, y, w, h = lore_rect(n)
    return y + h


# ------------------------------------------------------------------ 连接箭头
def _link_core_mask(part: Image.Image) -> list:
    px = part.load()
    w, h = part.size
    out = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a >= 200 and r + g + b <= LINK_CORE_LUM * 3:
                out.append((x, y))
    return out


def _link_largest_blob(points: list, w: int, h: int) -> list:
    if not points:
        return []
    pts = set(points)
    seen = set()
    best = []
    for start in points:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        blob = []
        while stack:
            x, y = stack.pop()
            blob.append((x, y))
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h and (nx, ny) in pts and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    stack.append((nx, ny))
        if len(blob) > len(best):
            best = blob
    return best


def _link_convex_hull(points: list) -> list:
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _link_fit_triangle(blob: list):
    """把锯齿状的近黑字形拟合成一个三角形，返回 (p0, p1, p2, cx, cy)。"""
    if len(blob) < 8:
        return None
    hull = _link_convex_hull(blob)
    if len(hull) < 3:
        return None
    best = -1.0
    tri = (hull[0], hull[0], hull[0])
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        for j in range(i + 1, n):
            bx, by = hull[j]
            for k in range(j + 1, n):
                cx_, cy_ = hull[k]
                area = abs((bx - ax) * (cy_ - ay) - (cx_ - ax) * (by - ay))
                if area > best:
                    best = area
                    tri = (hull[i], hull[j], hull[k])
    if best < 4:
        return None
    cx = sum(p[0] for p in blob) / len(blob)
    cy = sum(p[1] for p in blob) / len(blob)
    return ((tri[0][0] + 0.5, tri[0][1] + 0.5),
            (tri[1][0] + 0.5, tri[1][1] + 0.5),
            (tri[2][0] + 0.5, tri[2][1] + 0.5), cx, cy)


def _link_grow(tri, e: float):
    a, b, c = tri
    la = ((b[0] - c[0]) ** 2 + (b[1] - c[1]) ** 2) ** 0.5
    lb = ((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2) ** 0.5
    lc = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    per = la + lb + lc
    if per <= 1e-6:
        return tri
    area = 0.5 * abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))
    r_in = 2.0 * area / per
    if r_in <= 1e-3:
        return tri
    icx = (a[0] * la + b[0] * lb + c[0] * lc) / per
    icy = (a[1] * la + b[1] * lb + c[1] * lc) / per
    s = (r_in + e) / r_in
    return tuple((icx + (p[0] - icx) * s, icy + (p[1] - icy) * s) for p in tri)


def _link_sd_triangle(px, py, a, b, c) -> float:
    def edge_dist(ex, ey, vx, vy):
        ee = ex * ex + ey * ey
        t = max(0.0, min(1.0, (vx * ex + vy * ey) / ee)) if ee > 1e-8 else 0.0
        qx, qy = vx - ex * t, vy - ey * t
        return qx * qx + qy * qy

    e0 = (b[0] - a[0], b[1] - a[1])
    e1 = (c[0] - b[0], c[1] - b[1])
    e2 = (a[0] - c[0], a[1] - c[1])
    v0 = (px - a[0], py - a[1])
    v1 = (px - b[0], py - b[1])
    v2 = (px - c[0], py - c[1])
    d = min(edge_dist(e0[0], e0[1], v0[0], v0[1]),
            edge_dist(e1[0], e1[1], v1[0], v1[1]),
            edge_dist(e2[0], e2[1], v2[0], v2[1]))
    s = 1.0 if (e0[0] * e2[1] - e0[1] * e2[0]) >= 0 else -1.0
    s0 = s * (v0[0] * e0[1] - v0[1] * e0[0])
    s1 = s * (v1[0] * e1[1] - v1[1] * e1[0])
    s2 = s * (v2[0] * e2[1] - v2[1] * e2[0])
    return -(d ** 0.5) * (1.0 if min(s0, s1, s2) >= 0 else -1.0)


def _link_band(d, inner, outer, aa) -> float:
    def inside(edge):
        if edge + aa <= d:
            return 0.0
        if edge - aa >= d:
            return 1.0
        t = (d - (edge + aa)) / ((edge - aa) - (edge + aa))
        return t * t * (3 - 2 * t)
    return max(0.0, min(1.0, inside(outer) - inside(inner)))


def _link_blit_arrow(part: Image.Image, seed) -> Image.Image:
    """把未点亮的箭头从卡框上原样搬运出来。"""
    from collections import deque

    w, h = part.size
    src = part.load()
    mask = bytearray(w * h)
    q = deque()
    for sx, sy in seed:
        if 0 <= sx < w and 0 <= sy < h and not mask[sy * w + sx]:
            mask[sy * w + sx] = 1
            q.append((sx, sy))
    if not q:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    sat_max = 70.0
    for _ in range(10):
        if not q:
            break
        for _ in range(len(q)):
            x, y = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h) or mask[ny * w + nx]:
                    continue
                r, g, b, a = src[nx, ny]
                if a < 20:
                    continue
                if max(r, g, b) - min(r, g, b) >= sat_max:
                    continue
                mask[ny * w + nx] = 1
                q.append((nx, ny))

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dst = out.load()
    for y in range(h):
        for x in range(w):
            if mask[y * w + x]:
                dst[x, y] = src[x, y]
    from PIL import ImageFilter
    alpha = out.getchannel("A").filter(ImageFilter.GaussianBlur(0.9))
    rgb = out.convert("RGB").filter(ImageFilter.MaxFilter(3))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def _link_paint_arrow(part: Image.Image, tri) -> Image.Image:
    """按 SDF 画一支点亮的箭头：投影 → 银白边 → 橙红渐变。"""
    tip, ba, bb = tri
    w, h = part.size
    rim_in, rim_out = LINK_RIM_INNER, LINK_RIM_OUTER
    halo_alpha = LINK_HALO_ALPHA

    def _lit_color(t: float):
        if t < 0.45:
            u = t / 0.45
            a_, b_ = LINK_LIT_HOT, LINK_LIT_MID
        else:
            u = (t - 0.45) / 0.55
            a_, b_ = LINK_LIT_MID, LINK_LIT_EDGE
        return tuple(a_[i] + (b_[i] - a_[i]) * u for i in range(3))

    outer = _link_grow((tip, ba, bb), LINK_EXPAND_PX + LINK_RIM_PX)
    fill_line = -LINK_RIM_PX - LINK_INSET_PX

    _e1 = ((ba[0] - tip[0]) ** 2 + (ba[1] - tip[1]) ** 2) ** 0.5
    _e2 = ((bb[0] - ba[0]) ** 2 + (bb[1] - ba[1]) ** 2) ** 0.5
    _e3 = ((tip[0] - bb[0]) ** 2 + (tip[1] - bb[1]) ** 2) ** 0.5
    _peri = _e1 + _e2 + _e3
    _area = 0.5 * abs((ba[0] - tip[0]) * (bb[1] - tip[1])
                      - (bb[0] - tip[0]) * (ba[1] - tip[1]))
    r_in = ((2.0 * _area / _peri) if _peri > 0 else 1.0) - LINK_INSET_PX
    if r_in < 1.0:
        r_in = 1.0

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    buf = out.load()

    for ly in range(h):
        for lx in range(w):
            px, py = lx + 0.5, ly + 0.5
            d = _link_sd_triangle(px, py, *outer)
            if d >= LINK_HALO_PX + LINK_EDGE_AA:
                continue
            ur = ug = ub = ua = 0

            def over(r, g, b, a):
                nonlocal ur, ug, ub, ua
                if a <= 0:
                    return
                oa = a / 255.0
                uu = ua / 255.0
                na = oa + uu * (1 - oa)
                if na <= 0:
                    ur = ug = ub = ua = 0
                    return
                ur = int(max(0, min(255, round((r * oa + ur * uu * (1 - oa)) / na))))
                ug = int(max(0, min(255, round((g * oa + ug * uu * (1 - oa)) / na))))
                ub = int(max(0, min(255, round((b * oa + ub * uu * (1 - oa)) / na))))
                ua = int(max(0, min(255, round(na * 255))))

            solid = _link_band(d, 0.0, LINK_HALO_SOLID, LINK_EDGE_AA)
            if solid > 0.004 and halo_alpha > 0:
                over(0, 0, 0, int(round(halo_alpha * solid)))
            sc = _link_band(d, LINK_HALO_SOLID, LINK_HALO_PX, LINK_EDGE_AA)
            if sc > 0.004 and halo_alpha > 0:
                t = max(0.0, min(1.0, (d - LINK_HALO_SOLID)
                                  / (LINK_HALO_PX - LINK_HALO_SOLID)))
                falloff = (1 - t) * (1 - 0.35 * t)
                a = int(round(halo_alpha * 0.55 * falloff * sc))
                if a > 0:
                    over(0, 0, 0, a)

            if d <= 0.0:
                if d <= fill_line - LINK_EDGE_AA:
                    fd = d - fill_line
                    t = max(0.0, min(1.0, 1.0 - (-fd) / r_in)) ** 1.5
                    col = _lit_color(t)
                elif d >= -LINK_RIM_PX + LINK_EDGE_AA:
                    ot = max(0.0, min(1.0, (d + LINK_RIM_PX) / LINK_RIM_PX))
                    col = tuple(int(round(rim_in[i] + (rim_out[i] - rim_in[i]) * ot))
                                for i in range(3))
                else:
                    span = (-LINK_RIM_PX + LINK_EDGE_AA) - (fill_line - LINK_EDGE_AA)
                    u = ((d - (fill_line - LINK_EDGE_AA)) / span) if span > 1e-6 else 0.0
                    u = max(0.0, min(1.0, u))
                    u = u * u * (3 - 2 * u)
                    fd = d - fill_line
                    t = max(0.0, min(1.0, 1.0 - (-fd) / r_in)) ** 1.5
                    fill_col = _lit_color(t)
                    ot = max(0.0, min(1.0, (d + LINK_RIM_PX) / LINK_RIM_PX))
                    rim_col = tuple(rim_in[i] + (rim_out[i] - rim_in[i]) * ot
                                    for i in range(3))
                    col = tuple(fill_col[i] + (rim_col[i] - fill_col[i]) * u
                                for i in range(3))
                a_out = 255
                if d > -LINK_EDGE_AA:
                    a_out = int(round(255 * (LINK_EDGE_AA - d) / (2 * LINK_EDGE_AA)))
                    a_out = max(0, min(255, a_out))
                over(int(round(col[0])), int(round(col[1])),
                     int(round(col[2])), a_out)

            if ua > 0:
                buf[lx, ly] = (ur, ug, ub, ua)

    return out


def link_arrow_layers(frame: Image.Image, markers: int = 0):
    """返回 (lit, dim) 两张透明层：点亮方向 / 未点亮方向。"""
    lit = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    dim = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    if not markers:
        return lit, dim

    for i, (x, y, w, h) in enumerate(LINK_ARROWS):
        x1, y1 = min(x + w, CARD_W), min(y + h, CARD_H)
        x, y = max(0, x), max(0, y)
        part = frame.crop((x, y, x1, y1))
        pw, ph = part.size
        if pw <= 0 or ph <= 0:
            continue
        blob = _link_largest_blob(_link_core_mask(part), pw, ph)
        if not blob:
            continue
        if markers & (1 << i):
            fit = _link_fit_triangle(blob)
            if not fit:
                continue
            lit.alpha_composite(_link_paint_arrow(part, fit[:3]), (x, y))
        else:
            dim.alpha_composite(_link_blit_arrow(part, blob), (x, y))
    return lit, dim


_link_split_cache: dict = {}


def link_arrow_split(frm: Image.Image, markers: int = 0, cache_key=None):
    """从实际使用的卡框图里拆出亮/暗箭头层。cache_key 为空时不缓存。"""
    if cache_key is None:
        return link_arrow_layers(frm, markers)
    key = (cache_key, int(markers or 0))
    if key not in _link_split_cache:
        # 每项是两张 704×1024 RGBA（约 5.8MB），只留最近几组，防止长时间编辑累积
        if len(_link_split_cache) >= 4:
            _link_split_cache.clear()
        _link_split_cache[key] = link_arrow_layers(frm, markers)
    return _link_split_cache[key]


_prop_cache: dict = {}
_marker_cache: dict = {}


def _card_prop_dec(paths) -> bytes:
    """解密后的 CARD_Prop（每条 8 字节）。按游戏根目录缓存。"""
    key = str(paths.game_root)
    if key in _prop_cache:
        return _prop_cache[key]
    import UnityPy
    import gamedata

    p = Path(paths.game_root) / "masterduel_Data" / "data.unity3d"
    if not p.is_file():
        _prop_cache[key] = b""
        return b""
    try:
        env = UnityPy.load(p.read_bytes())
        props, _, _ = gamedata._text_assets(env)
        if not props:
            dec = b""
        else:
            dec = zlib.decompress(gamedata._xor(props[0], gamedata._find_key(props[0])))
    except Exception:
        dec = b""
    _prop_cache[key] = dec
    return dec


def markers_of_card(paths, card_id: int) -> int:
    """连接怪兽实际拥有的方向掩码（bit0..7 = LINK_ARROWS 顺序）。读不到给 0。"""
    cid = int(card_id)
    if cid in _marker_cache:
        return _marker_cache[cid]
    if paths is None:
        return 0
    dec = _card_prop_dec(paths)
    mask = 0
    found = False
    for i in range(8, len(dec) - 7, 8):
        if (dec[i] | (dec[i + 1] << 8)) != cid:
            continue
        found = True
        tb = dec[i + 2]
        packed = (dec[i + 4] | (dec[i + 5] << 8)
                  | (dec[i + 6] << 16) | (dec[i + 7] << 24))
        # 只有连接怪兽的 DEF 槽才存方向掩码
        if (tb & 0x20) and (tb & 0x0F) in (0x0A, 0x0B):
            mask = (packed >> 9) & 0xFF
        break
    if found:
        _marker_cache[cid] = mask
    return mask


# ------------------------------------------------------------------ 卡图路径
def illust_logical(card_id: int) -> str:
    return f"Card/Images/Illust/tcg/{int(card_id)}"


def _crc(path: str) -> str:
    return format(zlib.crc32(path.encode("utf-8")) & 0xFFFFFFFF, "08x")


def _base_dir(paths, root: str) -> Path:
    return paths.data_dir if root == core.ROOT_LOCAL else paths.stream_dir


def _illust_rel(paths, card_id: int):
    """卡图 bundle 的 (root, rel)；local 优先，其次 stream。找不到 (None, None)。"""
    if paths is None:
        return None, None
    h = _crc(illust_logical(card_id))
    for root, base in ((core.ROOT_LOCAL, paths.data_dir),
                       (core.ROOT_STREAM, paths.stream_dir)):
        if base and (base / h[:2] / h).is_file():
            return root, f"{h[:2]}/{h}"
    return None, None


def _illust_rels(paths, card_id: int) -> list:
    """这张卡全部卡图 bundle 的 [(root, rel)]：tcg/ocg 双版本 + common 都试，local 优先。
    双版本卡（语言不同显示不同图）必须一起写，否则切换语言会露出未改的那版。"""
    out = []
    if paths is None:
        return out
    for kind in ("tcg", "ocg", "common"):
        h = _crc(f"Card/Images/Illust/{kind}/{int(card_id)}")
        for root, base in ((core.ROOT_LOCAL, paths.data_dir),
                           (core.ROOT_STREAM, paths.stream_dir)):
            if not base or not (base / h[:2] / h).is_file():
                continue
            item = (root, f"{h[:2]}/{h}")
            if item not in out:
                out.append(item)
    return out


def read_illust(paths, card_id: int):
    """读游戏里这张卡的当前卡图。返回 (Image, 信息 dict) 或 (None, {"error": ...})。"""
    import UnityPy

    root, rel = _illust_rel(paths, card_id)
    if not rel:
        return None, {"error": f"本地没有卡 {card_id} 的卡图资源"}
    p = _base_dir(paths, root) / rel
    try:
        env = UnityPy.load(p.read_bytes())
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}
    tex = None
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        d = o.read()
        if str(getattr(d, "m_Name", "")) == str(int(card_id)):
            tex = d
            break
        if tex is None:
            tex = d
    if tex is None:
        return None, {"error": f"{illust_logical(card_id)} 里没有贴图"}
    return tex.image.convert("RGBA"), {
        "root": root, "rel": rel, "path": str(p),
        "width": int(tex.m_Width), "height": int(tex.m_Height),
        "overframe": int(tex.m_Width) >= CARD_W,
    }


def base_art(paths, card_id: int, frame: int = 1) -> Path:
    """没选整幅画时的兜底背景：游戏当前插图；已是超框图就还原 704 版式再裁插图窗。"""
    img, meta = read_illust(paths, card_id)
    if img is None:
        raise OverframeError(meta.get("error") or f"卡 {card_id} 没有卡图")
    if img.height == CARD_H:
        # 官方超框贴图的 alpha 是游戏合成遮罩，不是透明度；且 512×1024 是
        # 横向压缩存放的 704 版式，先摊回 704 再裁插图窗
        img = img.convert("RGBA")
        img.putalpha(255)
        if img.size != (CARD_W, CARD_H):
            img = img.resize((CARD_W, CARD_H), Image.LANCZOS)
        ax, ay, aw, ah = art_box(frame)
        img = img.crop((ax, ay, ax + aw, ay + ah))
    out = _state_dir() / f"{int(card_id)}_base.png"
    img.save(out)
    return out


# ------------------------------------------------------------------ 编辑记录
def _state_dir() -> Path:
    d = core.TOOL_DIR / "overframe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _edits_file() -> Path:
    return _state_dir() / "edits.json"


def _edits_dir() -> Path:
    d = _state_dir() / "edits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_edits() -> dict:
    try:
        return json.loads(_edits_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_edits(data: dict) -> bool:
    """原子写 edits.json（先写临时文件再替换），避免中途崩溃把记录写坏。"""
    f = _edits_file()
    tmp = f.with_name(f.name + ".tmp")
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_edit(card_id) -> dict | None:
    return _load_edits().get(str(int(card_id)))


# ------------------------------------------------------------------ 缓存清理
def _dir_size(d: Path) -> int:
    total = 0
    if d.is_dir():
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def cache_usage() -> dict:
    """各类缓存占用（字节）：游戏卡框、素材副本、导入素材、游戏原图底图、边框。"""
    base = _state_dir()
    out = {
        "gameframes": _dir_size(base / "gameframes"),
        "edits": _dir_size(base / "edits"),
        "imported": _dir_size(base / "imported"),
        "borders": _dir_size(base / "borders"),
        "base": _base_png_size(base),
    }
    out["total"] = sum(out.values())
    return out


def _base_png_size(base: Path) -> int:
    total = 0
    for f in base.glob("*_base.png"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def clean_stale_gameframes(paths) -> int:
    """删掉和当前游戏版本对不上的卡框缓存，返回释放的字节数。"""
    d = _state_dir() / "gameframes"
    if not d.is_dir():
        return 0
    tag = _gframe_tag(paths)
    freed = 0
    for f in list(d.iterdir()):
        if not f.is_file():
            continue
        if tag and f.name.startswith(tag + "_"):
            continue
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return freed


def _referenced_paths() -> set[str]:
    """edits.json 里引用到的素材文件（绝对路径小写）。"""
    keys = ("art", "subject", "frame_file", "over_layer")
    refs = set()
    for edit in _load_edits().values():
        if not isinstance(edit, dict):
            continue
        for key in keys:
            val = edit.get(key)
            if not isinstance(val, str) or not val:
                continue
            try:
                refs.add(str(Path(val).resolve()).lower())
            except OSError:
                pass
    return refs


def clean_cache(paths, keep_card=None) -> dict:
    """清理缓存：过期游戏卡框 + edits/imported 里已不被引用的素材。
    keep_card 指定当前正在编辑的卡号，避免删掉它刚生成的底图。
    返回 {类别: 释放字节}，以及合计 freed。"""
    freed = {"gameframes": clean_stale_gameframes(paths),
             "edits": 0, "imported": 0, "base": 0}
    refs = _referenced_paths()
    base = _state_dir()
    # 当前卡导入工程带来的素材：还没应用时不在 edits.json 引用里，不能清掉
    keep_dir = None
    if keep_card:
        try:
            keep_dir = base / "imported" / str(int(keep_card))
        except (TypeError, ValueError):
            keep_dir = None
    for name, folder in (("edits", base / "edits"), ("imported", base / "imported")):
        if not folder.is_dir():
            continue
        for f in list(folder.rglob("*")):
            if not f.is_file():
                continue
            if keep_dir is not None and keep_dir in f.parents:
                continue
            try:
                key = str(f.resolve()).lower()
            except OSError:
                continue
            if key in refs:
                continue
            try:
                freed[name] += f.stat().st_size
                f.unlink()
            except OSError:
                pass
        # 清掉删空的目录
        for d in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
    for f in list(base.glob("*_base.png")):
        try:
            if keep_card and f.name.startswith(f"{int(keep_card)}_base"):
                continue
            key = str(f.resolve()).lower()
            if key in refs:
                continue
            freed["base"] += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    freed["freed"] = sum(v for k, v in freed.items() if k != "freed")
    return freed


def drop_base(card_id) -> int:
    """删掉本卡自动生成的 <卡号>_base.png（关闭对话框时调用），返回字节数。"""
    refs = _referenced_paths()
    freed = 0
    for f in _state_dir().glob(f"{int(card_id)}_base*.png"):
        try:
            if str(f.resolve()).lower() in refs:
                continue
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return freed


def _copy_in(src, card_id: int, suffix: str) -> str:
    if not src:
        return ""
    p = Path(src)
    if not p.is_file():
        return ""
    ext = p.suffix.lower() or ".png"
    dst = _edits_dir() / f"{int(card_id)}_{suffix}{ext}"
    try:
        if p.resolve() == dst.resolve():
            return str(dst)
        dst.write_bytes(p.read_bytes())
        return str(dst)
    except OSError:
        return str(p)


def export_project(card_id, params: dict, dest) -> Path:
    """把当前卡的制作参数与素材打包成一个 .zip 工程文件。"""
    import zipfile
    dest = Path(dest)
    meta = {"version": PROJECT_VERSION, "card_id": str(int(card_id)), "params": {}}
    media = {}
    for key, base in (("art", "art"), ("subject", "subject"),
                      ("frame_file", "frame"), ("over_layer", "over")):
        src = params.get(key)
        if src and src != BLANK_ART and Path(src).is_file():
            p = Path(src)
            name = f"{base}{p.suffix.lower() or '.png'}"
            media[name] = p
            meta["params"][key] = name
        else:
            meta["params"][key] = None
    for k in ("scale", "dx", "dy", "lore_alpha", "border", "frame",
              "frame_source", "blank_color"):
        meta["params"][k] = params.get(k)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("params.json", json.dumps(meta, ensure_ascii=False, indent=1))
        for name, path in media.items():
            z.write(path, name)
    return dest


def import_project(zip_path) -> tuple[str, dict]:
    """读取 .zip 工程：素材解到工具目录，返回 (卡号, 可直接用于界面的参数)。"""
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        meta = json.loads(z.read("params.json").decode("utf-8"))
        card_id = str(meta.get("card_id") or "")
        params = dict(meta.get("params") or {})
        out_dir = _state_dir() / "imported" / (card_id or "unknown")
        out_dir.mkdir(parents=True, exist_ok=True)
        for key in ("art", "subject", "frame_file", "over_layer"):
            name = params.get(key)
            if not name:
                continue
            target = out_dir / Path(name).name
            target.write_bytes(z.read(name))
            params[key] = str(target)
        # 旧工程里打包的边框图：解到工具目录 borders/ 并转成文件名参数
        legacy = Path(str(params.pop("border_file", "") or "")).name
        if legacy:
            try:
                data = z.read(legacy)
            except KeyError:
                data = None
            if data:
                dst = _state_dir() / "borders" / legacy
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                    params["border"] = legacy
                except OSError:
                    pass
    return card_id, params


def save_edit(card_id, art=None, art_name="", subject=None, subject_name="",
              scale=1.0, dx=0, dy=0, lore_alpha=1.0,
              border="", frame=1, art_rel="", art_rels=None,
              frame_source="game", frame_file=None, over_layer=None,
              over_name="", blank_color="", ids=None, layout=None) -> bool:
    """记录这张卡的编辑参数。返回是否写盘成功。"""
    data = _load_edits()
    entry = {}
    art_copy = _copy_in(art, card_id, "art")
    if art_copy:
        entry["art"] = art_copy
        entry["art_name"] = art_name or Path(art_copy).name
    sub_copy = _copy_in(subject, card_id, "sub")
    if sub_copy:
        entry["subject"] = sub_copy
        entry["subject_name"] = subject_name or Path(sub_copy).name
    frame_copy = _copy_in(frame_file, card_id, "frame") if frame_source == "file" else ""
    if frame_copy:
        entry["frame_file"] = frame_copy
    over_copy = _copy_in(over_layer, card_id, "over")
    if over_copy:
        entry["over_layer"] = over_copy
        entry["over_name"] = over_name or Path(over_copy).name
    entry["scale"] = float(scale or 1.0)
    entry["dx"] = int(dx or 0)
    entry["dy"] = int(dy or 0)
    entry["lore_alpha"] = float(lore_alpha if lore_alpha is not None else 1.0)
    entry["border"] = normalize_border(border)
    entry["frame"] = int(frame or 1)
    entry["frame_source"] = frame_source if frame_source in ("game", "file") else "game"
    entry["blank_color"] = str(blank_color or "")
    if int(layout or 0) in (LAYOUT_NORMAL, LAYOUT_PENDULUM):
        # 记下制作时的版式：之后索引里的贴图尺寸会变成超框尺寸，
        # 再靠尺寸猜灵摆会把普通卡误判成灵摆
        entry["layout"] = int(layout)
    if art_rel:
        entry["art_rel"] = art_rel
    if art_rels:
        entry["art_rels"] = list(art_rels)
    if ids:
        # 这次一并写入/登记的卡号（同名同图的区域双建档），还原时按它一起撤
        entry["ids"] = [int(i) for i in ids]
    data[str(int(card_id))] = entry
    return _write_edits(data)


def clear_edit(card_id):
    data = _load_edits()
    if str(int(card_id)) in data:
        data.pop(str(int(card_id)), None)
        _write_edits(data)


# ------------------------------------------------------------------ 登记表
_gate_cache: dict = {}
_gate_probe_cache: dict = {}


def _gate_object(env):
    for o in env.objects:
        if o.type.name != "TextAsset":
            continue
        d = o.read()
        if str(d.m_Name) == GATE_NAME:
            return o
    return None


def _script_bytes(d) -> bytes:
    return (bytes(d.m_Script.encode("utf-8", "surrogateescape"))
            if isinstance(d.m_Script, str) else bytes(d.m_Script))


def _parse_script(raw: bytes) -> list:
    return [struct.unpack("<HH", raw[i:i + REC])
            for i in range(0, len(raw) - len(raw) % REC, REC)]


def _gate_entries_at(path: Path):
    """这个 bundle 里的登记条目；不是 gate bundle 返回 None。"""
    import UnityPy

    try:
        st = path.stat()
        ck = (str(path), st.st_size, int(st.st_mtime))
        if ck in _gate_probe_cache:
            return _gate_probe_cache[ck]
        env = UnityPy.load(path.read_bytes())
    except Exception:
        return None
    o = _gate_object(env)
    if o is None:
        _gate_probe_cache[ck] = None
        return None
    try:
        ent = _parse_script(_script_bytes(o.read()))
    except Exception:
        ent = None
    _gate_probe_cache[ck] = ent
    return ent


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def gate_bundle_path(paths) -> Path:
    """找到当前账号下真正生效的 gate bundle（LocalData 里的松散覆盖文件）。

    只按“含 of_card_asset 且至少若干条映射”判定，不要求包含内置表的全部条目：
    本机下载到的这张表通常只登记了本地已下载的那部分超框美术，条目数比
    data.unity3d 里的内置表少，用内置表做超集校验会永远匹配不上，
    每次都要全盘扫描（上万文件）导致开窗卡住。
    """
    root_key = str(paths.game_root)

    def fits(p: Path) -> bool:
        ent = _gate_entries_at(p)
        return bool(ent) and len(ent) >= 4

    for gid in GATE_KNOWN_IDS:
        p = paths.data_dir / gid[:2] / gid if paths.data_dir else None
        if p and p.is_file() and fits(p):
            _gate_cache[root_key] = p
            return p

    cached = _gate_cache.get(root_key)
    if cached and Path(cached).is_file() and fits(Path(cached)):
        return Path(cached)

    cache_file = _state_dir() / "_gate_bundle.json"
    try:
        c = json.loads(cache_file.read_text(encoding="utf-8"))
        if c.get("root") == root_key and c.get("id") and paths.data_dir:
            p = paths.data_dir / c["id"][:2] / c["id"]
            if p.is_file() and fits(p):
                _gate_cache[root_key] = p
                return p
    except (OSError, ValueError, KeyError):
        pass

    if not paths.data_dir:
        raise OverframeError("没有找到游戏数据目录")
    cands = []
    try:
        for p in paths.data_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if 0 < sz <= 40000:
                cands.append((sz, p))
    except OSError:
        pass
    cands.sort(key=lambda x: x[0])
    hits = []
    probe = 0
    for _, p in cands:
        ent = _gate_entries_at(p)
        if ent is None:
            continue
        hits.append((ent, p))
        probe += 1
        if probe >= 60:
            break
    if not hits:
        raise OverframeError(f"在 {paths.data_dir} 下找不到超框登记表 bundle")
    _, p = max(hits, key=lambda h: (len(h[0]), _mtime(h[1])))
    try:
        cache_file.write_text(json.dumps({"root": root_key, "id": p.name}),
                              encoding="utf-8")
    except OSError:
        pass
    _gate_cache[root_key] = p
    return p


def gate_records(paths) -> list:
    import UnityPy

    p = gate_bundle_path(paths)
    env = UnityPy.load(p.read_bytes())
    o = _gate_object(env)
    if o is None:
        return []
    return _parse_script(_script_bytes(o.read()))


def _add_entry(records, card_id):
    cid = int(card_id)
    out = [(t, b) for t, b in records if int(t) != cid]
    out.append((cid, cid))
    return out


def _drop_entry(records, card_id):
    cid = int(card_id)
    return [(t, b) for t, b in records if int(t) != cid]


def _gate_bytes_with(path: Path, records) -> bytes:
    import UnityPy

    env = UnityPy.load(path.read_bytes())
    o = _gate_object(env)
    if o is None:
        raise OverframeError(f"登记表 bundle 里找不到 {GATE_NAME}")
    d = o.read()
    payload = b"".join(struct.pack("<HH", int(t), int(b)) for t, b in records)
    d.m_Script = (payload.decode("utf-8", "surrogateescape")
                  if isinstance(d.m_Script, str) else payload)
    d.save()
    out = env.file.save(packer="lz4")
    return out.encode("latin-1") if isinstance(out, str) else out


def _rel_of_path(paths, p: Path) -> str:
    for root, base in ((core.ROOT_LOCAL, paths.data_dir),
                       (core.ROOT_STREAM, paths.stream_dir)):
        if base:
            try:
                return f"{root}/{p.relative_to(base).as_posix()}"
            except ValueError:
                continue
    raise OverframeError(f"无法确定文件所属目录：{p}")


def _register(paths, manifest, card_id):
    """把关卡登记成超框 (cid, cid)。若这张卡本来就有官方登记（映射到别的底图），
    说明游戏本来就按超框布局渲染，保持原表不动，只换纹理即可。"""
    gp = gate_bundle_path(paths)
    recs = gate_records(paths)
    cid = int(card_id)
    if any(int(t) == cid for t, _ in recs):
        return False
    data = _gate_bytes_with(gp, _add_entry(recs, cid))
    core.write_bundle_bytes(paths, manifest, _rel_of_path(paths, gp), data)
    return True


def unregister(paths, manifest, card_id):
    """只把这张卡从登记表里去掉（普通换图后调用）。"""
    try:
        gp = gate_bundle_path(paths)
        recs = _drop_entry(gate_records(paths), card_id)
        data = _gate_bytes_with(gp, recs)
        core.write_bundle_bytes(paths, manifest, _rel_of_path(paths, gp), data)
    finally:
        clear_edit(card_id)


# ------------------------------------------------------------------ 合成
def _fit_cover(img: Image.Image, w: int, h: int,
               resample=Image.LANCZOS) -> Image.Image:
    r = max(w / img.width, h / img.height)
    img = img.resize((max(1, round(img.width * r)), max(1, round(img.height * r))),
                     resample)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


_open_cache: dict = {}


def _open(path) -> Image.Image:
    p = Path(path)
    if not p.is_file():
        raise OverframeError(f"找不到图片：{path}")
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    if key is not None:
        hit = _open_cache.get(key)
        if hit is not None:
            return hit
    try:
        with Image.open(p) as im:
            img = im.convert("RGBA")
            img.load()
    except OSError as e:
        raise OverframeError(f"{p.name}：不是有效图片（{e}）") from None
    if key is not None:
        if len(_open_cache) > 16:
            _open_cache.clear()
        _open_cache[key] = img
    return img


# 主体图去底：JPEG 没有透明通道，从四边往里漫填相近颜色当背景抠掉
BG_TOL = 34


def _has_alpha(img: Image.Image) -> bool:
    a = img.getchannel("A")
    lo, hi = a.getextrema()
    return lo < 240 and hi > 0


def _edge_color(img: Image.Image):
    """取四边中点/四角的中位色；四边颜色差别太大就认为不是纯色背景。"""
    w, h = img.size
    px = img.load()
    pts = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
           (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    cols = [px[x, y][:3] for x, y in pts]
    med = tuple(sorted(c[i] for c in cols)[len(cols) // 2] for i in range(3))
    if max(sum(abs(c[i] - med[i]) for i in range(3)) for c in cols) > BG_TOL:
        return None
    return med


def _strip_bg(img: Image.Image) -> Image.Image:
    """把与四边同色的连通区域变透明，边缘做 1px 羽化减轻锯齿。"""
    if _has_alpha(img):
        return img
    ref = _edge_color(img)
    if ref is None:
        return img
    w, h = img.size
    px = img.load()
    tol = BG_TOL

    def near(c):
        return sum(abs(c[i] - ref[i]) for i in range(3)) <= tol * 3

    seen = bytearray(w * h)
    stack = []
    for x in range(w):
        for y in (0, h - 1):
            if not seen[y * w + x] and near(px[x, y]):
                seen[y * w + x] = 1
                stack.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not seen[y * w + x] and near(px[x, y]):
                seen[y * w + x] = 1
                stack.append((x, y))

    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    while stack:
        x, y = stack.pop()
        mp[x, y] = 255
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                i = ny * w + nx
                if not seen[i] and near(px[nx, ny]):
                    seen[i] = 1
                    stack.append((nx, ny))

    mask = mask.filter(ImageFilter.GaussianBlur(1.0))
    a = img.getchannel("A").point(lambda v: v)
    a = Image.composite(Image.new("L", (w, h), 0), a, mask)
    out = img.copy()
    out.putalpha(a)
    return out


_subject_cache: dict = {}


def subject_image(path) -> Image.Image:
    """主体图（已去底，并裁掉四周全透明的留白）。同一文件按 路径+大小+时间
    缓存，拖动滑块时不重复抠图。"""
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_size, int(st.st_mtime))
    except OSError:
        key = (str(p), 0, 0)
    if key not in _subject_cache:
        img = _strip_bg(_open(p))
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
        _subject_cache[key] = img
    return _subject_cache[key]


def _dim(layer: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return layer
    a = layer.getchannel("A").point(lambda v: round(v * alpha))
    out = layer.copy()
    out.putalpha(a)
    return out


def render(paths, card_id: int, art=None, subject=None,
           scale: float = 1.0, dx: int = 0, dy: int = 0,
           lore_alpha: float = 1.0, border: str = "",
           frame: int = 1, layout: int | None = None,
           link_markers: int | None = None,
           frame_source: str = "game", frame_file=None,
           over_layer=None,
           blank_color: str = "", fast: bool = False) -> Image.Image:
    """合成一张 704×1024 的超框卡图，返回 PIL Image（不写盘）。
    图层顺序与游戏一致：卡框 → 插图 → 边框叠加 → 主体 → 效果框 → 超框层。
    frame_source：game=游戏 data.unity3d 的 card_frameNN，
    file=frame_file 指定的本地卡框图片。border：tools/border 下的边框
    叠加图文件名，空字符串=不变边框。layout 决定版式（插图窗/效果框
    位置），固定按原卡图来：普通卡=LAYOUT_NORMAL，灵摆卡=LAYOUT_PENDULUM；
    不传时沿用 frame。fast=True 用双线性插值，供实时预览。"""
    nn = int(frame)
    ln = int(layout) if layout is not None else nn
    ax, ay, aw, ah = art_box(ln)
    lx, ly, lw, lh = lore_rect(ln)
    rs = Image.BILINEAR if fast else Image.LANCZOS

    cv = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    if frame_source == "file" and frame_file:
        frm = _fit_cover(_open(frame_file), CARD_W, CARD_H, rs)
    else:
        # 卡框只来自游戏 data.unity3d（随包 solid 已移除）
        frm = game_frame_image(paths, nn)

    if art == BLANK_ART:
        art = None
    if not art and blank_color:
        # 空白时给插图窗铺一层不透明底色，避免游戏底层透出
        from PIL import ImageColor
        rgb = ImageColor.getrgb(blank_color)
        cv.paste(Image.new("RGBA", (aw, ah), rgb + (255,)), (ax, ay))
    if art:
        bg = _fit_cover(_open(art), aw, ah, rs)
        if not subject:
            # 没选主体时，缩放/偏移直接作用在背景卡图上（窗口内裁剪）
            s = float(scale or 1.0)
            ox, oy = int(dx or 0), int(dy or 0)
            if s != 1.0 or ox or oy:
                size = (max(1, round(aw * s)), max(1, round(ah * s)))
                zoomed = bg if s == 1.0 else bg.resize(size, rs)
                canvas = bg.copy()
                canvas.paste(zoomed, ((aw - zoomed.width) // 2 + ox,
                                      (ah - zoomed.height) // 2 + oy))
                bg = canvas
        cv.alpha_composite(bg, (ax, ay))

    cv.alpha_composite(frm)

    overlay = border_image(border)
    if overlay is not None:
        cv.alpha_composite(overlay)

    lit = None
    if nn == LINK_FRAME:
        if link_markers is None:
            link_markers = markers_of_card(paths, card_id)
        if link_markers:
            tag = None if frame_source == "file" else (
                "game", _gframe_tag(paths))
            lit, dim = link_arrow_split(frm, link_markers, tag)
            cv.alpha_composite(dim)

    if subject:
        sub = subject_image(subject)
        cover = max(aw / sub.width, ah / sub.height)
        r = cover * OVERFLOW_SCALE * float(scale or 1.0)
        size = (max(1, round(sub.width * r)), max(1, round(sub.height * r)))
        sub_scaled = sub.resize(size, rs)
        x = ax + (aw - size[0]) // 2 + int(dx or 0)
        y = ay + (ah - size[1]) // 2 + int(dy or 0)
        layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        layer.alpha_composite(sub_scaled, (x, y))
        cv.alpha_composite(layer)

    if lit is not None:
        cv.alpha_composite(lit)

    panel = frm.crop((lx, ly, lx + lw, ly + lh))
    cv.alpha_composite(_dim(panel, lore_alpha), (lx, ly))

    if over_layer:
        # 超框层：整卡叠加图，alpha 即合成遮罩（与游戏第二层渲染一致）
        over = _fit_cover(_open(over_layer), CARD_W, CARD_H, rs)
        cv.alpha_composite(over)
    return cv


def compose(paths, card_id: int, out=None, **kwargs) -> Path:
    """合成并保存 PNG，返回路径。kwargs 同 render。"""
    img = render(paths, card_id, **kwargs)
    dst = Path(out) if out else _state_dir() / f"{int(card_id)}.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


# ------------------------------------------------------------------ 落盘
def _apply_texture_settings(d):
    try:
        ts = d.m_TextureSettings
        for attr, val in (("m_FilterMode", 1), ("m_Aniso", 2), ("m_MipBias", 0.0),
                          ("m_WrapMode", 1), ("m_WrapU", 1), ("m_WrapV", 1),
                          ("m_WrapW", 1)):
            if hasattr(ts, attr):
                setattr(ts, attr, val)
    except AttributeError:
        pass


def _replace_illust(data: bytes, img: Image.Image) -> bytes:
    """按 704×1024 换掉 bundle 里全部卡图贴图（同卡可能有多份/多格式副本）。"""
    import UnityPy
    from UnityPy.enums import TextureFormat

    env = UnityPy.load(data)
    hit = 0
    for o in env.objects:
        if o.type.name != "Texture2D":
            continue
        d = o.read()
        try:
            d.set_image(img, target_format=TextureFormat.BC7, mipmap_count=1)
        except Exception:
            d.set_image(img, target_format=TextureFormat.RGBA32, mipmap_count=1)
        _apply_texture_settings(d)
        d.save()
        hit += 1
    if not hit:
        raise OverframeError("卡图 bundle 里没有贴图")
    out = env.file.save(packer="lz4")
    return out.encode("latin-1") if isinstance(out, str) else out


def apply(paths, manifest, card_id: int, image, register: bool = True) -> dict:
    """把合成好的超框卡图写进游戏，并登记成超框。
    同名同图的重复卡号（区域双建档）会一起写入并登记，避免游戏走另一套资源显示旧图。"""
    cid = int(card_id)
    if not _illust_rels(paths, cid):
        raise OverframeError(f"本地没有卡 {cid} 的卡图 bundle，请先在游戏里加载这张卡")
    ids = [cid]
    try:
        import cards as _cards
        conn = _cards.init_db()
        ids += [int(i) for i in _cards.same_art_card_ids(paths, conn, str(cid))
                if int(i) != cid]
    except Exception:
        pass  # 索引/卡名数据缺失时只处理当前卡

    img = image.convert("RGBA")
    if img.size != (CARD_W, CARD_H):
        img = img.resize((CARD_W, CARD_H), Image.LANCZOS)

    if register:
        # 先确认登记表能读能写，登记不了就别动纹理（避免写了一半留下超框残局）
        try:
            gate_records(paths)
        except (OverframeError, OSError) as e:
            raise OverframeError(f"超框登记表不可用，已取消写入（游戏文件未改动）：{e}")

    rels = []
    written = set()
    for i in ids:
        for root, rel in _illust_rels(paths, i):
            p = _base_dir(paths, root) / rel
            full_rel = f"{root}/{rel}"
            if full_rel in written or not p.is_file():
                continue
            written.add(full_rel)
            data = _replace_illust(p.read_bytes(), img)
            core.write_bundle_bytes(paths, manifest, full_rel, data)
            rels.append(full_rel)
    if not rels:
        raise OverframeError(f"卡 {cid} 的 bundle 文件不存在，请先在游戏里加载这张卡")
    main_rel = f"{_illust_rels(paths, cid)[0][0]}/{_illust_rels(paths, cid)[0][1]}"
    if main_rel in rels:
        rels.remove(main_rel)
        rels.insert(0, main_rel)

    registered = []
    if register:
        for i in ids:
            if _register(paths, manifest, i):
                registered.append(i)
    return {"rel": rels[0], "rels": rels, "ids": ids,
            "registered": registered}


def remove(paths, manifest, card_id: int, drop_register: bool = True) -> dict:
    """把这张卡的超框图撤掉：纹理与登记表都尽量回到原样（同名同图的卡一起还原）。"""
    cid = int(card_id)
    edit = load_edit(cid) or {}
    ids = [cid]
    if edit.get("ids"):
        ids += [int(i) for i in edit["ids"] if int(i) != cid]
    else:
        # 老记录没存 ids：按当前卡图重新找一遍同名同图的卡
        conn = None
        try:
            import cards as _cards
            conn = _cards.init_db()
            ids += [int(i) for i in _cards.same_art_card_ids(
                paths, conn, str(cid)) if int(i) != cid]
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    rels = list(edit.get("art_rels") or [])
    if not rels and edit.get("art_rel"):
        rels = [edit["art_rel"]]
    if not rels:
        rels = [f"{root}/{rel}" for root, rel in _illust_rels(paths, cid)]
    restored = False
    for full_rel in rels:
        msg = core.restore_one(paths, manifest, full_rel)
        if msg.startswith("已"):
            restored = True

    if drop_register:
        try:
            gp = gate_bundle_path(paths)
            grel = _rel_of_path(paths, gp)
            recs = gate_records(paths)
            mine = set(ids)
            hits = [(int(t), int(b)) for t, b in recs if int(t) in mine]
            ours = bool(hits) and all(t == b for t, b in hits)
            remaining = {int(k) for k in _load_edits()} - mine
            bak = core._effective_backup(paths, grel)
            if ours and not remaining and bak.exists():
                # 这是本工具做的最后一张超框：登记表整个还原
                core.restore_one(paths, manifest, grel)
            elif ours:
                left = [r for r in recs if int(r[0]) not in mine]
                core.write_bundle_bytes(paths, manifest, grel,
                                        _gate_bytes_with(gp, left))
            # 官方原本就登记的超框（映射到别的底图）不动
        except OverframeError:
            pass

    clear_edit(cid)
    return {"restored": restored}


def saved_cards() -> dict:
    """所有有编辑记录的卡号 → 参数。"""
    return dict(_load_edits())


def is_registered(paths, card_id: int) -> bool:
    """这张卡当前是否在 of_card_asset 里（官方超框或本工具登记）。"""
    try:
        return int(card_id) in {t for t, _ in gate_records(paths)}
    except Exception:
        return False


def forget(paths, manifest, card_id: int) -> bool:
    """撤销本工具对这张卡的超框登记并删掉编辑记录，但不动卡图纹理
    （用于「还原管理」已经还原过纹理的情况）。返回是否有改动。"""
    cid = int(card_id)
    edit = load_edit(cid) or {}
    ids = [cid] + [int(i) for i in (edit.get("ids") or []) if int(i) != cid]
    mine = set(ids)
    changed = False
    try:
        gp = gate_bundle_path(paths)
        grel = _rel_of_path(paths, gp)
        recs = gate_records(paths)
        hits = [(int(t), int(b)) for t, b in recs if int(t) in mine]
        ours = bool(hits) and all(t == b for t, b in hits)
        remaining = {int(k) for k in _load_edits()} - mine
        bak = core._effective_backup(paths, grel)
        if ours and not remaining and bak.exists():
            core.restore_one(paths, manifest, grel)
            changed = True
        elif ours:
            left = [r for r in recs if int(r[0]) not in mine]
            core.write_bundle_bytes(paths, manifest, grel,
                                    _gate_bytes_with(gp, left))
            changed = True
        # 官方原本就登记的超框（映射到别的底图）不动
    except (OverframeError, OSError):
        pass
    if str(cid) in _load_edits():
        clear_edit(cid)
        changed = True
    return changed


def status(paths, card_id: int) -> dict:
    img, meta = read_illust(paths, card_id)
    if img is None:
        return {"exists": False, "error": meta.get("error")}
    cid = int(card_id)
    try:
        registered = cid in {t for t, _ in gate_records(paths)}
    except Exception:
        registered = False
    edit = load_edit(cid) or {}
    return {
        "exists": True,
        "width": meta["width"], "height": meta["height"],
        "overframe": bool(meta["overframe"]),
        "registered": registered,
        "ours": bool(edit),
        "frame": int(edit.get("frame", 1)),
        "border": normalize_border(edit.get("border") or Path(
            str(edit.get("border_file") or "")).name),
    }
