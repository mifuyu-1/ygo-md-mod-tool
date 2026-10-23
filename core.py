# -*- coding: utf-8 -*-
"""
共用底座：路径配置、manifest 清单、智能备份、还原、游戏进程检测
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

# 打包成 exe 后，__file__ 指向临时解压目录，必须用 exe 所在目录持久化配置/索引
TOOL_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = TOOL_DIR / "config.json"
INDEX_DB = TOOL_DIR / "card_index.sqlite"
CARDS_JSON = TOOL_DIR / "cards.json"  # 可选：卡号→卡名 {"18448": "青眼白龙", ...}
# 从游戏 data.unity3d 提取的官方卡名（重建索引时自动生成）
GAME_NAMES_JSON = TOOL_DIR / "game_card_names.json"

# Bundle 所在的两个根：local=按需下载目录 LocalData\<玩家>\0000；stream=随安装包下发的基础资源
ROOT_LOCAL = "local"
ROOT_STREAM = "stream"


# ---------------- 配置 ----------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_game_root() -> Path | None:
    """自动检测游戏根目录：尝试常见 Steam 路径和注册表"""
    # 1. 尝试注册表查找 Steam 安装路径
    steam_paths = set()
    try:
        import winreg
        for key_path in (r"SOFTWARE\Valve\Steam", r"SOFTWARE\WOW6432Node\Valve\Steam"):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
                install_path, _ = winreg.QueryValueEx(key, "SteamPath")
                steam_paths.add(Path(install_path))
                winreg.CloseKey(key)
            except Exception:
                pass
        for key_path in (r"SOFTWARE\Valve\Steam", r"SOFTWARE\WOW6432Node\Valve\Steam"):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                steam_paths.add(Path(install_path))
                winreg.CloseKey(key)
            except Exception:
                pass
    except Exception:
        pass

    # 2. 尝试常见路径
    common_paths = [
        Path.home() / "Steam" / "steamapps" / "common" / "Yu-Gi-Oh!  Master Duel",
        Path("C:/Program Files (x86)/Steam/steamapps/common/Yu-Gi-Oh!  Master Duel"),
        Path("C:/Program Files/Steam/steamapps/common/Yu-Gi-Oh!  Master Duel"),
        Path("D:/Steam/steamapps/common/Yu-Gi-Oh!  Master Duel"),
        Path("D:/SteamLibrary/steamapps/common/Yu-Gi-Oh!  Master Duel"),
        Path("E:/Steam/steamapps/common/Yu-Gi-Oh!  Master Duel"),
        Path("E:/SteamLibrary/steamapps/common/Yu-Gi-Oh!  Master Duel"),
    ]

    # 3. 如果注册表找到 Steam，检查其库文件夹
    for sp in steam_paths:
        common_paths.append(sp / "steamapps" / "common" / "Yu-Gi-Oh!  Master Duel")
        # 尝试读取 libraryfolders.vdf 获取其他 Steam 库
        vdf = sp / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                import re
                for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                    p = Path(m.group(1).replace("\\\\", "\\"))
                    common_paths.append(p / "steamapps" / "common" / "Yu-Gi-Oh!  Master Duel")
            except Exception:
                pass

    # 4. 逐个检查
    for p in common_paths:
        if (p / "masterduel.exe").exists():
            return p

    return None


def find_data_dir(game_root: Path) -> Path | None:
    """在 LocalData 下找到 *\\0000 数据目录（选文件数最多的）"""
    local = game_root / "LocalData"
    if not local.exists():
        return None
    best, best_n = None, -1
    for sub in local.iterdir():
        cand = sub / "0000"
        if cand.is_dir():
            n = sum(1 for _ in cand.rglob("*") if _.is_file())
            if n > best_n:
                best, best_n = cand, n
    return best


class Paths:
    """由游戏根目录派生的所有路径"""

    def __init__(self, game_root: Path):
        self.game_root = game_root
        self.data_dir = find_data_dir(game_root)
        self.stream_dir = game_root / "masterduel_Data" / "StreamingAssets" / "AssetBundle"
        self.backup_root = TOOL_DIR / "_MD卡图备份"
        self.backup_dir = self.backup_root / "backup"        # 原版文件备份（镜像根目录结构）
        self.manifest_file = self.backup_root / "manifest.json"

    # rel 统一形式："local/xx/hash" 或 "stream/xx/hash"；旧清单里的裸 "xx/hash" 视为 local
    def root_of(self, rel: str) -> str:
        first = rel.split("/", 1)[0]
        return first if first in (ROOT_LOCAL, ROOT_STREAM) else ROOT_LOCAL

    def bundle_path(self, rel: str) -> Path:
        root, bare = self.root_of(rel), rel.split("/", 1)[-1]
        base = self.data_dir if root == ROOT_LOCAL else self.stream_dir
        return base / bare

    def backup_path(self, rel: str) -> Path:
        """新版备份路径（按根分目录）；旧版 local 备份直接放在 backup/ 下"""
        root, bare = self.root_of(rel), rel.split("/", 1)[-1]
        if root == ROOT_LOCAL and "/" not in rel:
            return self.backup_dir / bare                    # 兼容历史裸 rel
        return self.backup_dir / root / bare


# ---------------- Manifest ----------------

class Manifest:
    """替换清单：{ rel_path: {time, source, added} }"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"replaced": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def record(self, rel: str, source: str, added: bool):
        self.data["replaced"][rel] = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,   # pack=整包替换, image=单图替换
            "added": added,     # 是否为游戏中原本不存在的新增文件
        }
        self.save()

    def remove(self, rel: str):
        self.data["replaced"].pop(rel, None)
        self.save()

    def entries(self) -> dict:
        return dict(self.data["replaced"])


# ---------------- 备份 / 替换 / 还原 ----------------

def backup_original(paths: Paths, rel: str) -> str:
    """
    替换前备份原版文件（只存一次）。rel 支持 "local/xx/hash"、"stream/xx/hash"
    及旧版裸 "xx/hash"（按 local 处理）。
    返回: 'backed_up' 新备份 | 'exists' 已有备份跳过 | 'added' 游戏里本来就没有（新增文件）
    """
    dst = paths.backup_path(rel)
    if dst.exists():
        return "exists"
    # 兼容旧版本：local 备份曾直接放在 backup/xx/hash
    root = paths.root_of(rel)
    bare = rel.split("/", 1)[-1]
    if root == ROOT_LOCAL and paths.backup_dir.joinpath(bare).exists():
        return "exists"
    src = paths.bundle_path(rel)
    if not src.exists():
        return "added"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "backed_up"


def replace_file(paths: Paths, manifest: Manifest, src_file: Path, rel: str, source: str):
    """整文件替换（Mod 包，目标固定为 local 根）：先按需备份，再覆盖，记录清单。
    rel 为裸 "xx/hash"；清单统一记录为 "local/xx/hash"。"""
    full_rel = rel if rel.split("/", 1)[0] in (ROOT_LOCAL, ROOT_STREAM) else f"{ROOT_LOCAL}/{rel}"
    status = backup_original(paths, full_rel)
    dst = paths.bundle_path(full_rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dst)
    # copy2 会保留 mod 源文件的旧时间戳，统一改为安装时刻，避免预览按 mtime 排序时
    # 把刚装上的旧 mod 包误判为“旧版 Bundle”
    dst.touch()
    manifest.record(full_rel, source, added=(status == "added"))


def write_bundle_bytes(paths: Paths, manifest: Manifest, rel: str, data: bytes):
    """单图替换：重打包后的 Bundle 字节写回对应根目录（先按需备份）"""
    status = backup_original(paths, rel)
    dst = paths.bundle_path(rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    manifest.record(rel, "image", added=(status == "added"))


def _effective_backup(paths: Paths, rel: str) -> Path:
    """实际存在的备份路径（兼容旧版 local 裸备份）"""
    bak = paths.backup_path(rel)
    if bak.exists():
        return bak
    bare = rel.split("/", 1)[-1]
    if paths.root_of(rel) == ROOT_LOCAL:
        legacy = paths.backup_dir / bare
        if legacy.exists():
            return legacy
    return bak


def _remove_with_retry(path: Path, timeout: float = 10.0):
    """删除文件；被占用（杀软/索引短暂锁定）时退避重试直至超时。
    只对占用类错误重试，其余异常直接抛出，绝不静默。"""
    deadline = time.monotonic() + timeout
    delay = 0.2
    while True:
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)


def restore_one(paths: Paths, manifest: Manifest, rel: str) -> str:
    """还原单个文件。返回结果描述"""
    bak = _effective_backup(paths, rel)
    target = paths.bundle_path(rel)
    entry = manifest.entries().get(rel)
    if bak.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bak, target)
        _remove_with_retry(bak)
        result = "已从备份还原原版"
    elif entry and entry.get("added") and target.exists():
        _remove_with_retry(target)
        result = "已删除新增文件"
    elif target.exists():
        result = "无备份，文件保持现状（可能是原版）"
    else:
        result = "文件不存在，无需处理"
    manifest.remove(rel)
    return result


def restore_all(paths: Paths, manifest: Manifest) -> tuple[int, int, list[str]]:
    """全部还原。两阶段：先全部拷回，再统一删备份（写删分离避免占用竞争）。
    单个失败不中断，返回 (还原数, 删除新增数, 失败描述列表)"""
    restored, deleted, failed = 0, 0, []
    pending_unlink: list[tuple[str, Path]] = []
    # 阶段一：拷回 / 删除新增
    for rel, entry in list(manifest.entries().items()):
        try:
            bak = _effective_backup(paths, rel)
            target = paths.bundle_path(rel)
            if bak.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bak, target)
                pending_unlink.append((rel, bak))
                restored += 1
            elif entry.get("added") and target.exists():
                _remove_with_retry(target)
                deleted += 1
                manifest.remove(rel)
        except OSError as e:
            failed.append(f"{rel}: {e}")
    # 阶段二：统一删备份（带占用重试）；删除成功才移除清单记录，
    # 失败的保留记录——Tab3 仍显示该文件，再次还原即是重试
    for rel, bak in pending_unlink:
        try:
            _remove_with_retry(bak)
            manifest.remove(rel)
        except OSError as e:
            failed.append(f"删除备份 {bak.name}: {e}")
    # 清理空的备份子目录
    if paths.backup_dir.exists():
        for d in sorted(paths.backup_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
    return restored, deleted, failed


# ---------------- 游戏进程检测 ----------------

def is_game_running() -> bool:
    """用 Toolhelp32 快照枚举进程（毫秒级），替代原 tasklist 子进程（1~3 秒且阻塞 UI）"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    try:
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return False
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(pe)):
                return False
            while True:
                if pe.szExeFile.lower() == "masterduel.exe":
                    return True
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    return False
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return False


# ---------------- 整包替换：源目录扫描 ----------------

_HEX2 = re.compile(r"^[0-9a-fA-F]{2}$")
_HEX8 = re.compile(r"^[0-9a-fA-F]{8}$")


def scan_pack_source(src_dir: Path) -> dict:
    """
    递归扫描 mod 源目录，识别游戏 Bundle 文件（8位hex文件名）。
    标准结构为 hex子目录/8位hex文件名；扁平结构（文件直接在任意目录下，
    无hex子目录）也接受，目标子目录从文件名前两位推导。
    预览图、作者备份等其他文件一律忽略。
    不同文件夹里的同名 Bundle（如 ver1/ver2 多变体包）视为同一 rel 的多个变体，
    全部保留，由调用方让用户逐个选择。
    返回 {"items": [{rel, variants: [{path, size, label}]}], "skipped": 忽略文件数}
    variants 按路径排序；label 为可区分的来源子路径（取 0000 的父目录名，
    无 0000 时取 hex 目录之上的相对路径；同 rel 内 label 保证唯一）。
    """
    skipped = 0
    by_rel: dict[str, list] = {}
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file():
            continue
        if not _HEX8.match(f.name):
            skipped += 1
            continue
        # 标准结构 xx/hash 直接用父目录；扁平结构从文件名前两位推导子目录
        if _HEX2.match(f.parent.name):
            sub = f.parent.name
            rel = f"{sub}/{f.name}"
            parts = f.relative_to(src_dir).parts[:-2]
        else:
            sub = f.name[:2].lower()
            rel = f"{sub}/{f.name}"
            parts = f.relative_to(src_dir).parts[:-1]
        label = ""
        if "0000" in parts:
            i = len(parts) - 1 - parts[::-1].index("0000")
            if i > 0:
                label = parts[i - 1]
        elif parts:
            label = "/".join(parts)
        by_rel.setdefault(rel, []).append(
            {"path": f, "size": f.stat().st_size, "label": label})
    items = []
    for rel in sorted(by_rel):
        variants = by_rel[rel]
        # 同一 rel 的变体 label 必须可区分：撞名时回退到更完整的来源路径
        if len(variants) > 1 and len({v["label"] for v in variants}) < len(variants):
            for v in variants:
                parts = v["path"].relative_to(src_dir).parts[:-2]
                v["label"] = "/".join(parts) or v["label"]
            if len({v["label"] for v in variants}) < len(variants):
                for n, v in enumerate(variants, 1):
                    v["label"] = f"{v['label']} #{n}"
        items.append({"rel": rel, "variants": variants})
    return {"items": items, "skipped": skipped}
