# -*- coding: utf-8 -*-
"""
功能B：卡图索引扫描、查询、预览、单图替换

索引的权威来源是 Bundle 自带的 container 资源清单：
  assets/resources[/assetbundle]/card/images/illust/(common|tcg|ocg)/.../<id>.bmp
只收录该路径下的 512x512 / 512x1024 贴图，彻底排除图标、3D 材质、卡框、
活动资源等同名数字贴图。资源分布在两个根目录：
  local  = LocalData\\<玩家>\\0000（按需下载）
  stream = masterduel_Data\\StreamingAssets\\AssetBundle（随安装包下发，含老牌经典卡）
"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import UnityPy
from PIL import Image
from UnityPy.enums import ClassIDType

from core import (CARDS_JSON, GAME_NAMES_JSON, INDEX_DB, Manifest, Paths,
                  ROOT_LOCAL, ROOT_STREAM, write_bundle_bytes)

ILLUST_MARKER = "card/images/illust/"
KIND_RANK = {"common": 0, "tcg": 1, "ocg": 2}
ROOT_RANK = {ROOT_LOCAL: 0, ROOT_STREAM: 1}


# ---------------- 索引数据库 ----------------

def init_db(db_path: Path = INDEX_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    cols = conn.execute("PRAGMA table_info(cards)").fetchall()
    if cols:
        names = [c[1] for c in cols]
        # v1 旧结构（无 root/path_id，按贴图名猜测)必须重建
        if "root" not in names or "path_id" not in names:
            conn.execute("DROP TABLE cards")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cards(
            card_id TEXT NOT NULL,
            root TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            path_id INTEGER,
            width INT, height INT, format INT, mip INT, kind TEXT,
            PRIMARY KEY (card_id, root, rel_path, path_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_id ON cards(card_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def get_index_count(conn) -> int:
    """可浏览的不同卡号数量"""
    try:
        return conn.execute("SELECT COUNT(DISTINCT card_id) FROM cards").fetchone()[0]
    except Exception:
        return 0


def _illust_kind(key: str) -> str | None:
    for k in ("common", "tcg", "ocg"):
        if f"/{k}/" in key:
            return k
    return None


def _inspect_bundle(root: str, path: Path) -> list[tuple]:
    """检查单个 Bundle，依据 container 清单返回卡图记录"""
    rows = []
    rel = f"{path.parent.name}/{path.name}"
    try:
        env = UnityPy.load(str(path))
        for key in env.container.keys():
            if ILLUST_MARKER not in key or "_info" in key:
                continue
            kind = _illust_kind(key)
            if kind is None:
                continue
            stem = key.split("/")[-1].split(".")[0]
            try:
                ptr = env.container[key]
                if ptr.type.name != "Texture2D":
                    continue
                t = ptr.read()
            except Exception:
                continue
            # 卡图只有 512x512（常规）与 512x1024（竖版完整卡图）两种
            if t.m_Width != 512 or t.m_Height not in (512, 1024):
                continue
            cid = t.m_Name if getattr(t, "m_Name", "").isdigit() else stem
            if not cid.isdigit():
                continue
            rows.append((cid, root, rel, int(ptr.path_id),
                         t.m_Width, t.m_Height, int(t.m_TextureFormat),
                         getattr(t, "m_MipCount", 0), kind))
    except Exception:
        pass
    return rows


def scan_index(paths: Paths, progress_cb=None, workers: int = 8) -> dict:
    """
    多线程扫描 local + stream 两个根，重建索引。
    返回 {"rows": 记录数, "ids": 不同卡号数}。
    """
    jobs = [(ROOT_LOCAL, f) for f in paths.data_dir.rglob("*") if f.is_file()]
    if paths.stream_dir.exists():
        jobs += [(ROOT_STREAM, f) for f in paths.stream_dir.rglob("*") if f.is_file()]
    total = len(jobs)

    conn = init_db()
    conn.execute("DELETE FROM cards")
    batch, found, done = [], 0, 0

    def flush():
        nonlocal batch, found
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?)", batch)
            found += len(batch)
            batch.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_inspect_bundle, root, f) for root, f in jobs]
        for fut in as_completed(futs):
            batch.extend(fut.result())
            done += 1
            if len(batch) >= 500:
                flush()
            if progress_cb and done % 200 == 0:
                progress_cb(done, total)
    flush()
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('total_files', ?)", (str(total),))
    conn.commit()
    ids = conn.execute("SELECT COUNT(DISTINCT card_id) FROM cards").fetchone()[0]
    conn.close()
    if progress_cb:
        progress_cb(total, total)
    return {"rows": found, "ids": ids}


# ---------------- 查询 ----------------

def index_ready() -> bool:
    """索引库是否已有数据（整包替换前据此提示镜像同步可用性）"""
    try:
        conn = init_db()
        n = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        conn.close()
        return n > 0
    except Exception:
        return False


def get_card_rows(conn, card_id: str) -> list[dict]:
    """一张卡的全部卡图资源（可能跨根/多版本），按 根(local优先) 与 版本(common优先) 排序"""
    cur = conn.execute(
        "SELECT root, rel_path, path_id, width, height, format, mip, kind "
        "FROM cards WHERE card_id=?", (str(card_id),))
    rows = [dict(root=r[0], rel=r[1], path_id=r[2], w=r[3], h=r[4],
                 fmt=r[5], mip=r[6], kind=r[7]) for r in cur.fetchall()]
    rows.sort(key=lambda r: (ROOT_RANK.get(r["root"], 9), KIND_RANK.get(r["kind"], 9)))
    return rows


def pick_preferred(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None


def official_card_ids(conn, ordered_ids: list[str]) -> list[str]:
    """按 CARD_Prop 顺序，返回在索引里有卡图的卡号（即游戏已实装且有图的卡）"""
    if not ordered_ids:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT card_id FROM cards ORDER BY CAST(card_id AS INT)")]
    have = {r[0] for r in conn.execute("SELECT DISTINCT card_id FROM cards")}
    return [cid for cid in ordered_ids if cid in have]


# ---------------- 卡名库 ----------------

def load_card_names() -> dict:
    """加载可选的旧版卡号→卡名 JSON"""
    if CARDS_JSON.exists():
        try:
            return json.loads(CARDS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_game_names() -> tuple[dict, list[str]]:
    """加载官方卡名 game_card_names.json。
    返回 ({card_id: {cn,en,ja}}, [按 Prop 顺序的 card_id])；文件不存在时回退旧库。"""
    if GAME_NAMES_JSON.exists():
        try:
            data = json.loads(GAME_NAMES_JSON.read_text(encoding="utf-8"))
            ordered = data.get("__ids__", [])
            names = {k: v for k, v in data.items()
                     if k != "__ids__" and isinstance(v, dict)}
            return names, ordered
        except Exception:
            pass
    # 回退：旧的第三方完整卡名库（键名 jp）
    old_full = Path(CARDS_JSON).parent / "card_names_full.json"
    if old_full.exists():
        try:
            data = json.loads(old_full.read_text(encoding="utf-8"))
            out = {}
            for cid, v in data.items():
                out[cid] = {"cn": v.get("cn", ""), "en": v.get("en", ""),
                            "ja": v.get("ja", "") or v.get("jp", "")}
            return out, []
        except Exception:
            pass
    return {}, []


# ---------------- 已替换状态（走索引，免解析 Bundle） ----------------

_modded_cache: dict = {}


def get_modded_card_ids(conn, manifest_entries: dict) -> set[str]:
    """清单中的文件 -> 卡号集合（经索引反查，含 stream 根）"""
    key = tuple(sorted(manifest_entries.keys()))
    if _modded_cache.get("key") == key:
        return _modded_cache["ids"]
    ids = set()
    if key:
        rels = list(key)
        # 兼容旧版裸 rel（local）
        norm = ["local/" + r if r.split("/", 1)[0] not in (ROOT_LOCAL, ROOT_STREAM) else r
                for r in rels]
        for i in range(0, len(norm), 400):
            chunk = norm[i:i + 400]
            ph = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"SELECT DISTINCT card_id FROM cards WHERE root||'/'||rel_path IN ({ph})",
                chunk)
            ids.update(r[0] for r in cur.fetchall())
    _modded_cache.clear()
    _modded_cache.update(key=key, ids=ids)
    return ids


def invalidate_replaced_cache():
    """替换/还原后调用，使缓存失效"""
    _modded_cache.clear()


# ---------------- 卡图读取与替换 ----------------

def _find_texture(env, path_id: int):
    for obj in env.objects:
        if obj.type == ClassIDType.Texture2D and obj.path_id == path_id:
            return obj.read()
    # 兜底：path_id 对不上时取 bundle 内第一张纯数字贴图
    for obj in env.objects:
        if obj.type == ClassIDType.Texture2D:
            t = obj.read()
            if getattr(t, "m_Name", "").isdigit():
                return t
    return None


def read_card_art(paths: Paths, root: str, rel: str, path_id: int | None = None):
    """从指定根目录读取卡图（用于预览）"""
    base = paths.data_dir if root == ROOT_LOCAL else paths.stream_dir
    bundle = base / rel
    if not bundle.exists():
        return None
    env = UnityPy.load(str(bundle))
    if path_id is not None:
        tex = _find_texture(env, int(path_id))
        if tex is not None:
            return tex.image
    for obj in env.objects:
        if obj.type == ClassIDType.Texture2D:
            return obj.read().image
    return None


def _write_texture_rgba32(tex, img: Image.Image):
    """把 PIL 图片写入 Texture2D（RGBA32，保持原尺寸，含 512x1024 竖版图）"""
    img = img.convert("RGBA").resize((tex.m_Width, tex.m_Height), Image.LANCZOS)
    data = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()
    tex.image_data = data
    tex.m_CompleteImageSize = len(data)
    tex.m_TextureFormat = 4  # RGBA32
    if getattr(tex, "m_MipCount", None) is not None:
        tex.m_MipCount = 1
    if getattr(tex, "m_MipMap", None) is not None:
        tex.m_MipMap = False
    if getattr(tex, "m_StreamData", None) is not None:
        tex.m_StreamData.path = ""
        tex.m_StreamData.offset = 0
        tex.m_StreamData.size = 0
    tex.save()


def replace_card_art(paths: Paths, manifest: Manifest, conn, card_id: str,
                     img: Path | Image.Image) -> int:
    """
    用本地图片替换该卡的全部卡图资源（local/stream、common/tcg/ocg 一并替换，
    保证游戏无论走哪个根/地区版本都显示新图）。各 Bundle 只备份一次。
    img 可以是图片路径，也可以是已裁剪好的 PIL Image。
    返回替换的 Bundle 数。
    """
    rows = get_card_rows(conn, card_id)
    if not rows:
        raise RuntimeError("索引中找不到该卡的卡图资源")
    src = img if isinstance(img, Image.Image) else Image.open(img)
    done_keys = set()
    count = 0
    for row in rows:
        full_rel = f"{row['root']}/{row['rel']}"
        if full_rel in done_keys:
            continue
        done_keys.add(full_rel)
        base = paths.data_dir if row["root"] == ROOT_LOCAL else paths.stream_dir
        bundle = base / row["rel"]
        if not bundle.exists():
            continue
        env = UnityPy.load(str(bundle))
        tex = _find_texture(env, int(row["path_id"]))
        if tex is None:
            continue
        _write_texture_rgba32(tex, src)
        data = env.file.save()
        if isinstance(data, str):
            data = data.encode("latin-1")
        write_bundle_bytes(paths, manifest, full_rel, bytes(data))
        count += 1
    return count


def export_card_art(paths: Paths, root: str, rel: str, out_path: Path,
                    path_id: int | None = None) -> bool:
    """导出当前卡图为 PNG"""
    img = read_card_art(paths, root, rel, path_id)
    if img is None:
        return False
    img.save(out_path)
    return True


# ---------------- 整包替换后的镜像 Bundle 同步 ----------------

def sync_mirror_bundles(paths: Paths, manifest: Manifest, mod_files: list[dict],
                       conn=None, progress_cb=None) -> int:
    """
    Mod 整包覆盖后同步同一卡号的其他 Bundle（含 stream 根与 tcg/ocg 版本）。
    mod_files: [{rel: 裸rel(local), path: mod包源文件或已在游戏目录的路径}]
    """
    if conn is None:
        conn = init_db()
        own_conn = True
    else:
        own_conn = False

    # 1. 从 mod 包读出 {card_id: PIL 图}
    mod_images: dict[str, Image.Image] = {}
    replaced_rels = set()
    for mf in mod_files:
        rel = mf["rel"]
        full_rel = rel if rel.split("/", 1)[0] in (ROOT_LOCAL, ROOT_STREAM) \
            else f"{ROOT_LOCAL}/{rel}"
        replaced_rels.add(full_rel)
        fp = mf.get("path")
        try:
            env = UnityPy.load(str(fp))
            for obj in env.objects:
                if obj.type == ClassIDType.Texture2D:
                    t = obj.read()
                    if t.m_Name and t.m_Name.isdigit():
                        mod_images[t.m_Name] = t.image
        except Exception:
            pass

    if not mod_images:
        if own_conn:
            conn.close()
        return 0

    # 2. 索引反查这些卡号的全部其他 Bundle
    targets: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for cid, img in mod_images.items():
        for row in get_card_rows(conn, cid):
            full_rel = f"{row['root']}/{row['rel']}"
            if full_rel in replaced_rels:
                continue
            targets.setdefault((row["root"], row["rel"]), []).append((cid, int(row["path_id"])))

    total = len(targets)
    if total == 0:
        if own_conn:
            conn.close()
        return 0

    # 3. 逐个重打包
    count = 0
    for (root, rel), entries in targets.items():
        base = paths.data_dir if root == ROOT_LOCAL else paths.stream_dir
        bundle = base / rel
        if not bundle.exists():
            continue
        try:
            env = UnityPy.load(str(bundle))
            touched = False
            idmap = {pid: cid for cid, pid in entries}
            for obj in env.objects:
                if obj.type == ClassIDType.Texture2D and obj.path_id in idmap:
                    _write_texture_rgba32(obj.read(), mod_images[idmap[obj.path_id]])
                    touched = True
            if touched:
                data = env.file.save()
                if isinstance(data, str):
                    data = data.encode("latin-1")
                write_bundle_bytes(paths, manifest, f"{root}/{rel}", bytes(data))
                count += 1
        except Exception:
            pass
        if progress_cb:
            progress_cb(count, total)
    if own_conn:
        conn.close()
    return count
