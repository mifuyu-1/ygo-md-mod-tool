# -*- coding: utf-8 -*-
"""
MD 卡图 Mod 工具 主界面
Tab1: Mod 整包替换   Tab2: 卡图浏览换图   Tab3: 还原管理
"""
import io
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cards
import core
import UnityPy
from PIL import Image, ImageTk
from UnityPy.enums import ClassIDType


def fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


class BundlePreviewDialog(tk.Toplevel):
    """预览 Bundle 文件内容：左侧资源对象列表，右侧贴图预览，支持导出 PNG"""

    def __init__(self, parent, bundle_path: Path, sources: list | None = None):
        super().__init__(parent)
        self._photo = None          # 防 GC
        self._tex_cache = {}        # name -> PIL.Image
        self._objects = {}          # iid -> obj
        # sources: [(显示名, 路径)]；多源时顶部提供切换（如 游戏当前版 / 原版备份）
        self._sources = sources or [(bundle_path.name, bundle_path)]
        self.title(f"资源预览 - {self._sources[0][1].name}")
        self.geometry("900x560")

        if len(self._sources) > 1:
            bar = ttk.Frame(self)
            bar.pack(fill=tk.X, padx=6, pady=(6, 0))
            ttk.Label(bar, text="查看:").pack(side=tk.LEFT)
            self._src_var = tk.StringVar(value=self._sources[0][0])
            cb = ttk.Combobox(bar, textvariable=self._src_var, state="readonly",
                              values=[s[0] for s in self._sources], width=28)
            cb.pack(side=tk.LEFT, padx=4)
            cb.bind("<<ComboboxSelected>>", lambda _e: self._load_current())

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # 左：对象列表
        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        cols = ("name", "type", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings")
        for c, t, w in (("name", "资源名", 200), ("type", "类型", 90), ("size", "尺寸", 90)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=tk.W if c == "name" else tk.CENTER)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 右：图片预览
        right = ttk.Frame(paned)
        paned.add(right, weight=1)
        self.img_label = ttk.Label(right, text="（选择左侧贴图查看）", anchor=tk.CENTER)
        self.img_label.pack(fill=tk.BOTH, expand=True)
        self.info_var = tk.StringVar()
        ttk.Label(right, textvariable=self.info_var).pack()
        self.export_btn = ttk.Button(right, text="导出为 PNG...",
                                     command=self.export_sel, state=tk.DISABLED)
        self.export_btn.pack(pady=4)

        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self._load(self._sources[0][1])

    def _load_current(self):
        """切换对比源"""
        label = self._src_var.get()
        path = next(p for lb, p in self._sources if lb == label)
        self.title(f"资源预览 - {path.name}")
        self._load(path)

    def _load(self, path: Path):
        """加载 Bundle 并填充对象列表"""
        self.tree.delete(*self.tree.get_children())
        self._objects.clear()
        self._tex_cache.clear()
        self._photo = None
        self.export_btn.config(state=tk.DISABLED)
        self.info_var.set("")
        try:
            # 读入内存再解析：立即释放文件句柄，避免预览窗口占用文件
            # （否则开着预览时还原/替换会因 WinError 32 删不掉该文件）
            env = UnityPy.load(io.BytesIO(Path(path).read_bytes()))
        except Exception as e:
            self.img_label.config(image="", text=f"无法解析该文件：{e}")
            return
        n_tex = 0
        for obj in env.objects:
            tname = obj.type.name
            name, size_text = "", ""
            try:
                if obj.type == ClassIDType.Texture2D:
                    tex = obj.read()
                    name, size_text = tex.m_Name, f"{tex.m_Width}×{tex.m_Height}"
                    n_tex += 1
                elif obj.type == ClassIDType.AudioClip:
                    name = obj.read().m_Name
                elif obj.type in (ClassIDType.Mesh, ClassIDType.Material,
                                  ClassIDType.AnimationClip, ClassIDType.Font):
                    name = getattr(obj.read(), "m_Name", "")
            except Exception:
                pass
            iid = self.tree.insert("", tk.END, values=(name or f"PathID={obj.path_id}",
                                                       tname, size_text))
            self._objects[iid] = obj
        if n_tex == 0:
            self.img_label.config(image="", text="该 Bundle 内没有贴图资源")
        else:
            self.img_label.config(image="", text="（选择左侧贴图查看）")

    def on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        obj = self._objects.get(sel[0])
        if obj is None or obj.type != ClassIDType.Texture2D:
            self.img_label.config(image="", text="（该类型不支持预览）")
            self.info_var.set("")
            self.export_btn.config(state=tk.DISABLED)
            self._photo = None
            return
        try:
            tex = obj.read()
            img = self._tex_cache.get(tex.m_Name)
            if img is None:
                img = tex.image
                self._tex_cache[tex.m_Name] = img
        except Exception as e:
            self.img_label.config(image="", text=f"贴图解码失败: {e}")
            self.export_btn.config(state=tk.DISABLED)
            return
        # 缩放适应预览区
        self.update_idletasks()
        max_w = max(self.img_label.winfo_width() - 10, 100)
        max_h = max(self.img_label.winfo_height() - 10, 100)
        scale = min(max_w / img.width, max_h / img.height, 1.0)
        disp = img.resize((max(1, int(img.width * scale)),
                           max(1, int(img.height * scale))), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(disp)
        self.img_label.config(image=self._photo, text="")
        self.info_var.set(f"{tex.m_Name}  {tex.m_Width}×{tex.m_Height}  {tex.m_TextureFormat}")
        self.export_btn.config(state=tk.NORMAL)

    def export_sel(self):
        sel = self.tree.selection()
        if not sel:
            return
        obj = self._objects.get(sel[0])
        if obj is None or obj.type != ClassIDType.Texture2D:
            return
        tex = obj.read()
        out = filedialog.asksaveasfilename(
            parent=self, defaultextension=".png", initialfile=f"{tex.m_Name}.png",
            filetypes=[("PNG 图片", "*.png")])
        if out:
            self._tex_cache.get(tex.m_Name, tex.image).save(out)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MD 卡图 Mod 工具")
        self.geometry("980x640")
        self.minsize(900, 560)

        self.cfg = core.load_config()
        root = self.cfg.get("game_root")
        if root:
            self.game_root = Path(root)
        else:
            self.game_root = core.detect_game_root()
        # 首次运行：未配置且自动检测失败，弹窗引导
        if not self.game_root or not (self.game_root / "masterduel.exe").exists():
            self.game_root = self._first_run_prompt()
        self.paths = core.Paths(self.game_root) if self.game_root else None
        self.manifest = None
        self.index_running = False  # 建索引进行中（与整包替换互斥，避免争抢索引库）
        self.pack_running = False   # 整包替换进行中
        self.ui_queue = queue.Queue()

        self._build_topbar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        self.pack_tab = PackTab(self.notebook, self)
        self.card_tab = CardTab(self.notebook, self)
        self.restore_tab = RestoreTab(self.notebook, self)
        self.notebook.add(self.pack_tab, text="  Mod 整包替换  ")
        self.notebook.add(self.card_tab, text="  卡图浏览换图  ")
        self.notebook.add(self.restore_tab, text="  还原管理  ")

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, anchor=tk.W,
                  relief=tk.SUNKEN).pack(fill=tk.X, side=tk.BOTTOM)
        self.after(100, self._poll_queue)
        self.refresh_status()

    def _first_run_prompt(self) -> Path | None:
        """首次运行引导：弹窗提示选择游戏目录"""
        from tkinter import simpledialog
        messagebox.showinfo(
            "首次使用",
            "欢迎使用 MD 卡图 Mod 工具！\n\n"
            "未能自动检测到游戏安装路径。\n"
            "请选择「Yu-Gi-Oh!  Master Duel」游戏根目录\n"
            "（包含 masterduel.exe 的文件夹）。",
        )
        d = filedialog.askdirectory(title="选择 Yu-Gi-Oh!  Master Duel 游戏根目录")
        if d:
            p = Path(d)
            if (p / "masterduel.exe").exists():
                self.cfg["game_root"] = str(p)
                core.save_config(self.cfg)
                return p
            if messagebox.askyesno("确认", "该目录下没有 masterduel.exe，仍然使用吗？"):
                self.cfg["game_root"] = str(p)
                core.save_config(self.cfg)
                return p
        return None

    # ---------- 顶部游戏目录栏 ----------
    def _build_topbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, padx=6, pady=(6, 0))
        ttk.Label(bar, text="游戏目录:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=str(self.game_root or "未检测到，请手动选择"))
        self.dir_label = ttk.Label(bar, textvariable=self.dir_var, foreground="#005a9e")
        self.dir_label.pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="浏览...", command=self.pick_game_dir).pack(side=tk.RIGHT)
        ttk.Button(bar, text="自动检测", command=self.auto_detect).pack(side=tk.RIGHT, padx=4)
        self.game_state = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.game_state, foreground="#c0392b").pack(side=tk.RIGHT, padx=8)
        self._update_game_state()

    def _update_game_state(self):
        if self.paths and self.paths.data_dir:
            state = "数据目录正常"
            if core.is_game_running():
                state += " | ⚠ 游戏正在运行，替换前请先关闭"
            self.game_state.set(state)
        else:
            self.game_state.set("未找到游戏数据目录")

    def pick_game_dir(self):
        init = str(self.game_root.parent) if self.game_root else None
        d = filedialog.askdirectory(title="选择 Yu-Gi-Oh! Master Duel 游戏根目录",
                                    initialdir=init)
        if d:
            self.set_game_root(Path(d))

    def auto_detect(self):
        p = core.detect_game_root()
        if p:
            self.set_game_root(p)
        else:
            messagebox.showwarning("自动检测", "未能在默认路径检测到游戏，请手动浏览选择。")

    def set_game_root(self, root: Path):
        if not (root / "masterduel.exe").exists():
            if not messagebox.askyesno("确认", "该目录下没有 masterduel.exe，确定使用吗？"):
                return
        self.game_root = root
        self.paths = core.Paths(root)
        self.cfg["game_root"] = str(root)
        core.save_config(self.cfg)
        self.manifest = None
        self.dir_var.set(str(root))
        self._update_game_state()
        self.refresh_status()
        self.restore_tab.refresh()

    def get_manifest(self) -> core.Manifest:
        if self.manifest is None:
            self.manifest = core.Manifest(self.paths.manifest_file)
        return self.manifest

    # ---------- 线程与 UI 通信 ----------
    def run_bg(self, fn, on_done=None):
        """后台线程执行 fn，结果通过队列回主线程"""
        def worker():
            try:
                result = fn()
                self.ui_queue.put(("done", on_done, result, None))
            except Exception as e:
                self.ui_queue.put(("done", on_done, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                tag, cb, result, err = self.ui_queue.get_nowait()
                try:
                    if tag == "done" and cb:
                        cb(result, err)
                    elif tag == "prog" and cb:
                        cb(result)
                except Exception:
                    pass  # 单个回调异常不阻断轮询
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def refresh_status(self):
        if not self.paths or not self.paths.data_dir:
            self.status_var.set("请先设置游戏目录")
            return
        m = self.get_manifest().entries()
        bak_count = len(list(self.paths.backup_dir.rglob("*"))) if self.paths.backup_dir.exists() else 0
        bak_files = sum(1 for f in self.paths.backup_dir.rglob("*") if f.is_file()) if self.paths.backup_dir.exists() else 0
        self.status_var.set(f"已备份 {bak_files} 个原版文件 | 当前 {len(m)} 个文件处于 mod 状态")

    def check_game_closed(self) -> bool:
        """替换/还原前检查游戏进程"""
        if core.is_game_running():
            messagebox.showerror("游戏运行中", "检测到 masterduel.exe 正在运行！\n请先关闭游戏再执行替换/还原。")
            return False
        if not self.paths or not self.paths.data_dir:
            messagebox.showerror("错误", "未设置有效的游戏数据目录。")
            return False
        return True


# ================= Tab1: Mod 整包替换 =================

class PackTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.items = []

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(top, text="Mod 源目录:").pack(side=tk.LEFT)
        self.src_var = tk.StringVar()
        src_entry = ttk.Entry(top, textvariable=self.src_var, width=60)
        src_entry.pack(side=tk.LEFT, padx=4)
        _attach_entry_menu(src_entry)
        ttk.Button(top, text="浏览...", command=self.pick_src).pack(side=tk.LEFT)
        ttk.Button(top, text="扫描预览", command=self.scan).pack(side=tk.LEFT, padx=8)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, padx=8)
        cols = ("rel", "size", "ver", "kind")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        self.tree.heading("rel", text="相对路径")
        self.tree.heading("size", text="大小")
        self.tree.heading("ver", text="来源版本")
        self.tree.heading("kind", text="类型")
        self.tree.column("rel", width=200)
        self.tree.column("size", width=90, anchor=tk.E)
        self.tree.column("ver", width=140, anchor=tk.W)
        self.tree.column("kind", width=60, anchor=tk.CENTER)
        sb = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", self.preview_selected)
        _attach_tree_preview_menu(self.tree, self.preview_selected)

        bot = ttk.Frame(self)
        bot.pack(fill=tk.X, padx=8, pady=6)
        self.summary_var = tk.StringVar(value="尚未扫描")
        ttk.Label(bot, textvariable=self.summary_var).pack(side=tk.LEFT)
        self.exec_btn = ttk.Button(bot, text="执行替换", command=self.execute, state=tk.DISABLED)
        self.exec_btn.pack(side=tk.RIGHT)
        self.progress = ttk.Progressbar(bot, length=220, mode="determinate")
        self.progress.pack(side=tk.RIGHT, padx=10)

    def pick_src(self):
        # 初始目录：上次填的路径优先，否则默认 mod下载文件 目录
        init = self.src_var.get().strip()
        if not init or not Path(init).exists():
            fallback = core.TOOL_DIR / "mod下载文件"
            init = str(fallback) if fallback.exists() else str(core.TOOL_DIR)
        d = filedialog.askdirectory(title="选择 Mod 源目录（结构与游戏 0000 相同）",
                                    initialdir=init)
        if d:
            self.src_var.set(d)

    def preview_selected(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.items):
            BundlePreviewDialog(self, Path(self.items[idx]["path"]))

    def scan(self):
        src = Path(self.src_var.get())
        if not src.is_dir():
            messagebox.showwarning("提示", "请先选择有效的 Mod 源目录")
            return
        if not self.app.paths or not self.app.paths.data_dir:
            messagebox.showwarning("提示", "请先设置游戏目录")
            return
        self.summary_var.set("扫描中...")
        self.update_idletasks()
        result = core.scan_pack_source(src)
        scanned, skipped = result["items"], result["skipped"]
        if not scanned:
            self.items = []
            self.tree.delete(*self.tree.get_children())
            self.summary_var.set("未找到 mod 文件")
            messagebox.showwarning(
                "提示",
                "源目录里没有识别到游戏 Bundle 文件（8位hex文件名）。\n"
                "请选择包含 mod Bundle 文件的目录。")
            self.exec_btn.config(state=tk.DISABLED)
            return
        # 同名文件（同一 rel 存在多个变体）时，弹窗让用户逐个选择安装哪个版本
        multi = [it for it in scanned if len(it["variants"]) > 1]
        if multi:
            picks = self._pick_variants(multi)
            if picks is None:
                self.summary_var.set("已取消选择")
                return
        else:
            picks = {}
        self.items = []
        for it in scanned:
            v = picks.get(it["rel"], it["variants"][0])
            self.items.append({
                "rel": it["rel"], "path": v["path"], "size": v["size"],
                "variant": v["label"],
                "added": not (self.app.paths.data_dir / it["rel"]).exists()})
        self.tree.delete(*self.tree.get_children())
        for it in self.items:
            self.tree.insert("", tk.END, values=(
                it["rel"], fmt_size(it["size"]), it["variant"],
                "新增" if it["added"] else "覆盖"))
        total_size = sum(i["size"] for i in self.items)
        added = sum(1 for i in self.items if i["added"])
        msg = f"共 {len(self.items)} 个文件，其中新增 {added} 个，总大小 {fmt_size(total_size)}"
        if multi:
            msg += f"，{len(multi)} 个同名文件已手动选择版本"
        if skipped:
            msg += f"，已忽略 {skipped} 个非游戏文件（预览图/作者备份等）"
        self.summary_var.set(msg)
        self.exec_btn.config(
            state=tk.NORMAL if (self.items and not self.app.index_running) else tk.DISABLED)

    def _pick_variants(self, multi: list):
        """同名 Bundle 存在多个版本时，逐个弹窗选择；返回 {rel: 选中的 variant} 或 None"""
        dlg = tk.Toplevel(self)
        dlg.title("选择同名文件的版本")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        ttk.Label(dlg, text="以下文件在不同文件夹里存在同名版本，请为每个选择要安装的来源：",
                  padding=(8, 8, 8, 0)).pack(anchor=tk.W)

        labels = sorted({v["label"] for it in multi for v in it["variants"]})
        rows = []  # (StringVar, {label: variant})
        for it in multi:
            var = tk.StringVar(value=it["variants"][0]["label"])
            rows.append((var, {v["label"]: v for v in it["variants"]}))

        # 一键全选某个版本
        if len(labels) > 1:
            bar = ttk.Frame(dlg, padding=(8, 4))
            bar.pack(anchor=tk.W)
            ttk.Label(bar, text="一键全选:").pack(side=tk.LEFT)

            def select_all(x):
                for var, by_label in rows:
                    if x in by_label:
                        var.set(x)

            for lb in labels:
                ttk.Button(bar, text=lb or "(根目录)",
                           command=lambda x=lb: select_all(x)
                           ).pack(side=tk.LEFT, padx=4)

        # 可滚动列表
        outer = ttk.Frame(dlg, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, width=700, height=min(420, 40 + 30 * len(multi)),
                           highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for (var, by_label), it in zip(rows, multi):
            f = ttk.Frame(inner, padding=(4, 2))
            f.pack(fill=tk.X)
            ttk.Label(f, text=it["rel"], width=18).pack(side=tk.LEFT)
            for v in it["variants"]:
                ttk.Radiobutton(
                    f, text=f"{v['label'] or '(根目录)'}（{fmt_size(v['size'])}）",
                    variable=var, value=v["label"]).pack(side=tk.LEFT, padx=6)

        chosen = {"picks": None}

        def close():
            canvas.unbind_all("<MouseWheel>")
            dlg.destroy()

        def ok():
            chosen["picks"] = {it["rel"]: by_label[var.get()]
                               for (var, by_label), it in zip(rows, multi)}
            close()

        dlg.protocol("WM_DELETE_WINDOW", close)
        btns = ttk.Frame(dlg, padding=8)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="确定", command=ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="取消", command=close).pack(side=tk.RIGHT)
        dlg.wait_window()
        return chosen["picks"]

    def execute(self):
        if not self.items:
            return
        if self.app.index_running:
            messagebox.showinfo("提示", "正在建立卡图索引，请等索引完成后再执行替换。")
            return
        if not cards.index_ready():
            if not messagebox.askyesno(
                    "建议先建立索引",
                    "卡图索引尚未建立。\n"
                    "若包内含卡图，其镜像 Bundle（同卡的其他格式版本）将无法同步，"
                    "游戏里可能新旧图混杂（非卡图 mod 不受影响）。\n\n"
                    "建议先到「卡图浏览换图」页建立索引（只需一次）。\n"
                    "仍要继续替换吗？"):
                return
        if not self.app.check_game_closed():
            return
        if not messagebox.askyesno(
                "确认替换",
                f"将把 {len(self.items)} 个 mod 文件覆盖到游戏目录。\n"
                "原版文件会先自动备份（只备份一次），确定执行？"):
            return
        self.app.pack_running = True
        self.exec_btn.config(state=tk.DISABLED)
        self.app.card_tab.scan_btn.config(state=tk.DISABLED)
        items = list(self.items)
        paths, manifest = self.app.paths, self.app.get_manifest()

        def work():
            # 1. 整包替换
            for i, it in enumerate(items):
                core.replace_file(paths, manifest, it["path"], it["rel"], "pack")
                self.app.ui_queue.put(("prog", self._set_progress, i + 1, None))
            replaced = len(items)
            # 2. 同步镜像 Bundle（同一卡号的其他格式 Bundle）
            self.app.ui_queue.put(("prog", lambda n=None: self.progress.config(
                maximum=1, value=0, text="正在同步镜像 Bundle..."), None, None))
            mirrors = cards.sync_mirror_bundles(paths, manifest, items)
            return replaced, mirrors

        def done(result, err):
            # 不论成功/失败，UI 状态统一复原
            self.app.pack_running = False
            self.exec_btn.config(state=tk.NORMAL)
            self.app.card_tab.scan_btn.config(state=tk.NORMAL)
            self.progress.config(value=0, text="")
            if err:
                messagebox.showerror("替换失败", str(err))
            else:
                replaced, mirrors = result
                cards.invalidate_replaced_cache()
                msg = f"已替换 {replaced} 个文件。"
                if mirrors:
                    msg += f"\n额外同步了 {mirrors} 个镜像 Bundle（同一卡图的其他格式版本）。"
                messagebox.showinfo("完成", msg)
            self.app.refresh_status()
            self.app.restore_tab.refresh()
        # 进度回调借用队列机制
        self._set_progress = lambda n: self.progress.config(
            maximum=len(items), value=n)
        self.app.run_bg(work, done)

    _set_progress = None  # 运行时注入


# ================= 卡图裁剪对话框 =================

class CropDialog(tk.Toplevel):
    """按目标宽高比手动框选裁剪区域（比例锁定，拖动移动、拖四角缩放）。

    结果在 self.result：
    - “裁剪并替换”   → 裁剪后的 PIL Image
    - “不裁剪用原图” → 原始 PIL Image
    - 取消/关闭      → None
    """

    HANDLE_R = 5   # 角手柄半径（画布像素）
    MIN_SEL = 32   # 选区最小宽度（原图像素）

    def __init__(self, parent, img: Image.Image, target_w: int, target_h: int):
        super().__init__(parent)
        self.title(f"裁剪卡图（目标 {target_w}×{target_h}）")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)

        self.result = None
        self._src = img
        self._tw, self._th = target_w, target_h
        self._ar = target_w / target_h
        iw, ih = img.size
        self._iw, self._ih = iw, ih

        # 整张图缩放显示（完整可见，便于整体构图）
        self._scale = min(720 / iw, 520 / ih, 1.0)
        dw, dh = max(1, round(iw * self._scale)), max(1, round(ih * self._scale))
        self._tk_img = ImageTk.PhotoImage(
            img.convert("RGB").resize((dw, dh), Image.LANCZOS))

        self.canvas = tk.Canvas(self, width=dw, height=dh,
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=8, pady=(8, 4))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_img)

        # 初始选区：居中、尽量大、符合目标比例
        if iw / ih > self._ar:
            sh, sw = ih, ih * self._ar
        else:
            sw, sh = iw, iw / self._ar
        self._sel = [(iw - sw) / 2, (ih - sh) / 2, sw, sh]  # x, y, w, h（原图坐标）

        info = ttk.Frame(self)
        info.pack(fill=tk.X, padx=8)
        self._info_var = tk.StringVar()
        ttk.Label(info, textvariable=self._info_var).pack(side=tk.LEFT)
        ttk.Label(info, text="拖动选区移动，拖四角调整大小（比例已锁定）",
                  foreground="#666").pack(side=tk.RIGHT)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(btns, text="取消", command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="不裁剪用原图", command=self._skip).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="裁剪并替换", command=self._ok).pack(side=tk.RIGHT, padx=4)

        self._mode = None    # move / nw / ne / sw / se / new
        self._press = None   # 按下时的画布坐标
        self._anchor = None  # 缩放时的固定角（原图坐标）

        cv = self.canvas
        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_drag)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Motion>", self._on_hover)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._redraw()
        self.grab_set()

    # ---------- 坐标与选区 ----------

    def _to_img(self, cx, cy):
        return cx / self._scale, cy / self._scale

    def _sel_canvas(self):
        x, y, w, h = self._sel
        s = self._scale
        return x * s, y * s, (x + w) * s, (y + h) * s

    def _clamp(self, x, y, w, h):
        """保持比例，把选区收进图片范围"""
        w = max(self.MIN_SEL, min(w, self._iw))
        h = w / self._ar
        if h > self._ih:
            h = self._ih
            w = h * self._ar
        x = min(max(0.0, x), self._iw - w)
        y = min(max(0.0, y), self._ih - h)
        return [x, y, w, h]

    def _box_from_anchor(self, ax, ay, ix, iy):
        """以 (ax,ay) 为固定角、(ix,iy) 为对角，生成符合比例的选区"""
        dw, dh = abs(ix - ax), abs(iy - ay)
        w = dw if dw / self._ar >= dh else dh * self._ar
        h = w / self._ar
        x = ax - w if ix < ax else ax
        y = ay - h if iy < ay else ay
        return self._clamp(x, y, w, h)

    def _hit(self, cx, cy):
        x1, y1, x2, y2 = self._sel_canvas()
        r = self.HANDLE_R + 4
        corners = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in corners.items():
            if abs(cx - hx) <= r and abs(cy - hy) <= r:
                return name
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return "move"
        return "new"

    # ---------- 鼠标事件 ----------

    def _on_press(self, e):
        self._mode = self._hit(e.x, e.y)
        self._press = (e.x, e.y)
        if self._mode == "new":
            self._anchor = self._to_img(e.x, e.y)
        elif self._mode != "move":
            x, y, w, h = self._sel
            fixed = {"nw": (x + w, y + h), "ne": (x, y + h),
                     "sw": (x + w, y), "se": (x, y)}
            self._anchor = fixed[self._mode]

    def _on_drag(self, e):
        if self._mode is None:
            return
        if self._mode == "move":
            px, py = self._press
            dx, dy = self._to_img(e.x - px, e.y - py)
            x, y, w, h = self._sel
            self._sel = self._clamp(x + dx, y + dy, w, h)
            self._press = (e.x, e.y)
        else:  # new 或拖角
            ix, iy = self._to_img(e.x, e.y)
            self._sel = self._box_from_anchor(*self._anchor, ix, iy)
        self._redraw()

    def _on_release(self, _e):
        self._mode = None

    def _on_hover(self, e):
        cur = {"nw": "size_nw_se", "se": "size_nw_se",
               "ne": "size_ne_sw", "sw": "size_ne_sw",
               "move": "fleur"}.get(self._hit(e.x, e.y), "crosshair")
        self.canvas.config(cursor=cur)

    # ---------- 绘制与结果 ----------

    def _redraw(self):
        cv = self.canvas
        cv.delete("ovl")
        x1, y1, x2, y2 = self._sel_canvas()
        dw, dh = self._iw * self._scale, self._ih * self._scale
        # 选区外半透明遮罩
        for box in ((0, 0, dw, y1), (0, y2, dw, dh),
                    (0, y1, x1, y2), (x2, y1, dw, y2)):
            cv.create_rectangle(*box, fill="black", stipple="gray50",
                                outline="", tags="ovl")
        cv.create_rectangle(x1, y1, x2, y2, outline="white", width=2, tags="ovl")
        r = self.HANDLE_R
        for hx, hy in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
            cv.create_rectangle(hx - r, hy - r, hx + r, hy + r,
                                fill="white", outline="black", tags="ovl")
        w, h = self._sel[2], self._sel[3]
        self._info_var.set(f"选区 {round(w)}×{round(h)}  →  输出 {self._tw}×{self._th}")

    def _ok(self):
        x, y, w, h = self._sel
        rx, ry, rw, rh = round(x), round(y), round(w), round(h)
        self.result = self._src.crop((rx, ry, rx + rw, ry + rh))
        self.destroy()

    def _skip(self):
        self.result = self._src
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ================= Tab2: 卡图浏览换图 =================

def _attach_entry_menu(entry: tk.Entry, readonly: bool = False):
    """给输入框挂右键菜单：剪切/复制/粘贴/全选（readonly 时只有复制/全选）"""
    menu = tk.Menu(entry, tearoff=0)
    if not readonly:
        menu.add_command(label="剪切", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="复制", command=lambda: entry.event_generate("<<Copy>>"))
    if not readonly:
        menu.add_command(label="粘贴", command=lambda: entry.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="全选", command=lambda: (
        entry.select_range(0, tk.END), entry.icursor(tk.END)))
    entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))


def _attach_tree_preview_menu(tree: ttk.Treeview, preview_cmd):
    """给树列表挂右键菜单：预览（先选中右键所指的行，与双击等效）"""
    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="预览 Bundle 内容", command=preview_cmd)

    def popup(e):
        row = tree.identify_row(e.y)
        if not row:
            return
        tree.selection_set(row)
        menu.tk_popup(e.x_root, e.y_root)

    tree.bind("<Button-3>", popup)


class CardTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.conn = None
        self.names = cards.load_card_names()
        # 官方卡名（游戏 data.unity3d 提取）+ 旧第三方库回退
        self.names_full, self.ordered_ids = cards.load_game_names()
        self.current_row = None   # 首选卡图资源 {root, rel, path_id,...}
        self.current_card = None
        self._tk_img = None
        self._lang = tk.StringVar(value="cn")  # 默认中文
        self._search_idx = None  # [(card_id, pref_row, blob)] 全语言卡名+卡号，用于搜索
        self._modded_ids = set()

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=6)
        self.scan_btn = ttk.Button(top, text="建立/更新卡图索引", command=self.build_index)
        self.scan_btn.pack(side=tk.LEFT)
        self.index_var = tk.StringVar(value=self._index_info())
        ttk.Label(top, textvariable=self.index_var).pack(side=tk.LEFT, padx=8)
        self.progress = ttk.Progressbar(top, length=180, mode="determinate")
        self.progress.pack(side=tk.LEFT, padx=6)

        sear = ttk.Frame(self)
        sear.pack(fill=tk.X, padx=8, pady=(0, 4))
        ttk.Label(sear, text="搜索卡号/卡名:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        e = ttk.Entry(sear, textvariable=self.search_var, width=30)
        e.pack(side=tk.LEFT, padx=4)
        e.bind("<Return>", lambda _e: self.search())
        _attach_entry_menu(e)   # 右键：剪切/复制/粘贴/全选
        ttk.Button(sear, text="搜索", command=self.search).pack(side=tk.LEFT)
        ttk.Button(sear, text="显示全部", command=lambda: self.search(clear=True)).pack(side=tk.LEFT, padx=4)
        # 语言切换：列表与右侧卡名立即刷新
        ttk.Label(sear, text="语言:").pack(side=tk.LEFT, padx=(8, 2))
        for label, val in [("中文", "cn"), ("EN", "en"), ("日本語", "jp")]:
            ttk.Radiobutton(sear, text=label, variable=self._lang,
                            value=val, command=self._on_lang_change).pack(side=tk.LEFT, padx=2)
        # 筛选：仅显示已替换
        self.filter_modded = tk.BooleanVar(value=False)
        ttk.Checkbutton(sear, text="仅显示已替换", variable=self.filter_modded,
                        command=self.search).pack(side=tk.LEFT, padx=8)

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=8)

        # 分页导航（底部独立一行）
        self._all_rows = []
        self._page = 0
        self._page_size = 300
        pager = ttk.Frame(self)
        pager.pack(fill=tk.X, padx=8, pady=(2, 6))
        ttk.Button(pager, text="◀上一页", command=lambda: self._goto_page(self._page - 1)).pack(side=tk.LEFT)
        ttk.Button(pager, text="下一页▶", command=lambda: self._goto_page(self._page + 1)).pack(side=tk.LEFT, padx=4)
        ttk.Label(pager, text="跳至").pack(side=tk.LEFT, padx=(10, 2))
        self.page_entry = ttk.Entry(pager, width=5)
        self.page_entry.pack(side=tk.LEFT)
        self.page_entry.bind("<Return>", lambda _e: self._jump_page())
        _attach_entry_menu(self.page_entry)
        ttk.Button(pager, text="跳转", command=self._jump_page).pack(side=tk.LEFT, padx=2)
        self.page_var = tk.StringVar(value="")
        ttk.Label(pager, textvariable=self.page_var).pack(side=tk.LEFT, padx=10)

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cols = ("card_id", "name", "modded")
        self.tree = ttk.Treeview(left, columns=cols, show="headings")
        self.tree.heading("card_id", text="卡号")
        self.tree.heading("name", text="卡名")
        self.tree.heading("modded", text="状态")
        self.tree.column("card_id", width=80, anchor=tk.CENTER)
        self.tree.column("name", width=240)
        self.tree.column("modded", width=60, anchor=tk.CENTER)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        right = ttk.Frame(body, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        right.pack_propagate(False)
        ttk.Label(right, text="当前卡图预览:").pack(anchor=tk.W)
        self.preview = ttk.Label(right)
        self.preview.pack(pady=6)
        # 卡名：只读输入框，可用鼠标拖选、右键复制
        self.name_var = tk.StringVar()
        name_e = tk.Entry(right, textvariable=self.name_var, state="readonly",
                          relief="flat", highlightthickness=0, font="TkDefaultFont",
                          readonlybackground=ttk.Style().lookup("TFrame", "background"))
        name_e.pack(fill=tk.X, pady=(0, 4))
        _attach_entry_menu(name_e, readonly=True)
        self.info_var = tk.StringVar(value="未选择卡片")
        ttk.Label(right, textvariable=self.info_var, wraplength=280).pack(anchor=tk.W)
        ttk.Button(right, text="导出当前卡图为 PNG",
                   command=self.export_art).pack(fill=tk.X, pady=(12, 2))
        self.replace_btn = ttk.Button(right, text="选择本地图片替换此卡图",
                                      command=self.replace_art)
        self.replace_btn.pack(fill=tk.X, pady=2)

        self._try_open_db()

    def _index_info(self):
        p = core.INDEX_DB
        if p.exists():
            try:
                conn = cards.init_db()
                n = cards.get_index_count(conn)
                conn.close()
                if n == 0:
                    return "索引需重建，请点击「建立/更新卡图索引」"
                return f"索引: {n} 张卡"
            except Exception:
                pass
        return "索引未建立（约需几分钟，只需建一次）"

    def _try_open_db(self):
        if core.INDEX_DB.exists():
            try:
                self.conn = cards.init_db()
                self._search_idx = None  # 索引可能重建过，搜索索引失效
                self.search()
            except Exception:
                self.conn = None

    def _build_search_index(self):
        """构建内存搜索索引：[(card_id, pref_row, blob)]。
        只含游戏已实装（CARD_Prop）且有卡图的卡，blob 含中/英/日卡名+卡号（小写）。"""
        idx = []
        for cid in cards.official_card_ids(self.conn, self.ordered_ids):
            rows = cards.get_card_rows(self.conn, cid)
            if not rows:
                continue
            parts = [cid]
            entry = self.names_full.get(cid)
            if entry:
                for lang in ("cn", "en", "ja"):
                    n = entry.get(lang)
                    if n:
                        parts.append(n.lower())
            else:
                old = self.names.get(cid)
                if old:
                    parts.append(str(old).lower())
            idx.append((cid, rows[0], " ".join(parts)))
        self._search_idx = idx

    def build_index(self):
        if not self.app.paths or not self.app.paths.data_dir:
            messagebox.showwarning("提示", "请先设置游戏目录")
            return
        if self.app.pack_running:
            messagebox.showinfo("提示", "正在执行整包替换，请等替换完成后再建立索引。")
            return
        if not messagebox.askyesno(
                "建立索引",
                "将从游戏文件提取官方卡名，并扫描全部资源（含 StreamingAssets），\n"
                "约需几分钟。游戏更新后重新执行即可加入新卡。确定开始？"):
            return
        self.app.index_running = True
        self.scan_btn.config(state=tk.DISABLED)
        self.app.pack_tab.exec_btn.config(state=tk.DISABLED)
        paths = self.app.paths

        def progress(done, total):
            self.app.ui_queue.put(("prog", lambda n=None: self.progress.config(
                maximum=total, value=done), None, None))

        def work():
            r = cards.scan_index(paths, progress_cb=progress)
            # 从 data.unity3d 提取官方卡名（游戏更新后重跑即得到新卡名）
            import gamedata
            names_res = gamedata.save_game_names(paths.game_root, core.GAME_NAMES_JSON)
            r["names"] = names_res["count"]
            return r

        def done(result, err):
            self.app.index_running = False
            self.scan_btn.config(state=tk.NORMAL)
            self.app.pack_tab.exec_btn.config(
                state=tk.NORMAL if self.app.pack_tab.items else tk.DISABLED)
            self.progress["value"] = 0
            if err:
                messagebox.showerror("索引失败", str(err))
            else:
                self.names_full, self.ordered_ids = cards.load_game_names()
                self.index_var.set(f"索引: {result['ids']} 张卡")
                messagebox.showinfo(
                    "完成",
                    f"索引建立完成：{result['ids']} 张游戏实装卡（{result['rows']} 条卡图资源），"
                    f"官方卡名 {result['names']} 条。")
                self._try_open_db()
        self.app.run_bg(work, done)

    def search(self, clear=False):
        if self.conn is None:
            self.index_var.set("请先建立索引")
            return
        if clear:
            self.search_var.set("")
        kw = self.search_var.get().strip().lower()
        if self._search_idx is None:
            self._build_search_index()
        # 关键词匹配：卡号 / 中文名 / 英文名 / 日文名（全部小写后子串匹配）
        if kw:
            rows = [(cid, pref) for cid, pref, blob in self._search_idx if kw in blob]
        else:
            rows = [(cid, pref) for cid, pref, _ in self._search_idx]
        # 已替换卡号集合（清单文件经索引反查，含 local/stream 两根）
        modded = self.app.get_manifest().entries() if self.app.paths else {}
        self._modded_ids = cards.get_modded_card_ids(self.conn, modded)
        # 筛选：仅显示已替换
        if self.filter_modded.get():
            rows = [r for r in rows if r[0] in self._modded_ids]
        self._all_rows = rows
        self._page = 0
        self._render_page()

    def _goto_page(self, page):
        if page < 0 or page * self._page_size >= len(self._all_rows):
            return
        self._page = page
        self._render_page()

    def _jump_page(self):
        """跳转到输入的页码（1 起始，自动夹取到有效范围）"""
        s = self.page_entry.get().strip()
        if not s.isdigit():
            return
        total_pages = max(1, (len(self._all_rows) - 1) // self._page_size + 1)
        self._goto_page(min(max(1, int(s)), total_pages) - 1)

    def _get_name(self, cid):
        """按当前语言获取卡名（官方库语言键 cn/en/ja）"""
        lang = self._lang.get()
        entry = self.names_full.get(cid)
        if entry:
            key = "ja" if lang == "jp" else lang
            name = entry.get(key, "")
            if name:
                return name
            for l in ("cn", "en", "ja"):
                if entry.get(l):
                    return entry[l]
        return self.names.get(cid, "")  # 回退到旧卡名库

    def _on_lang_change(self):
        """切换语言：左侧列表卡名重渲染；右侧已选中卡的卡名同步刷新（卡图与语言无关，不重读）"""
        self.search()
        if self.current_card:
            self.name_var.set(self._get_name(self.current_card))

    def _render_page(self):
        rows = self._all_rows
        total = len(rows)
        start = self._page * self._page_size
        end = min(start + self._page_size, total)
        self.tree.delete(*self.tree.get_children())
        for cid, _pref in rows[start:end]:
            is_mod = cid in self._modded_ids
            self.tree.insert("", tk.END, values=(
                cid, self._get_name(cid), "●已改" if is_mod else ""))
        self.page_var.set(f"第 {start+1}-{end} / {total} 张  (第 {self._page+1}/{(total-1)//self._page_size+1} 页)" if total else "无结果")

    def on_select(self, _evt):
        sel = self.tree.selection()
        if not sel or self.conn is None:
            return
        vals = self.tree.item(sel[0], "values")
        cid = str(vals[0])
        card_rows = cards.get_card_rows(self.conn, cid)
        if not card_rows:
            return
        row = card_rows[0]
        self.current_card, self.current_row = cid, row
        loc = "基础资源" if row["root"] == "stream" else "下载数据"
        self.name_var.set("")
        self.info_var.set(f"卡号 {cid} | {loc} {row['rel']} | {row['w']}x{row['h']}\n读取中...")
        self.update_idletasks()
        try:
            img = cards.read_card_art(self.app.paths, row["root"], row["rel"],
                                      row["path_id"])
            if img:
                disp = img.copy()
                disp.thumbnail((280, 280))
                from PIL import ImageTk
                self._tk_img = ImageTk.PhotoImage(disp)
                self.preview.config(image=self._tk_img)
                self.name_var.set(self._get_name(cid))
                extra = f"，共 {len(card_rows)} 个资源" if len(card_rows) > 1 else ""
                self.info_var.set(
                    f"卡号 {cid}\n{loc} {row['rel']} ({row['w']}x{row['h']}){extra}")
            else:
                self.preview.config(image="")
                self.info_var.set("无法读取卡图")
        except Exception as e:
            self.info_var.set(f"读取失败: {e}")

    def export_art(self):
        if not self.current_row:
            messagebox.showinfo("提示", "请先在左侧选择一张卡片")
            return
        row = self.current_row
        p = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=f"{self.current_card}.png",
            filetypes=[("PNG 图片", "*.png")])
        if p:
            if cards.export_card_art(self.app.paths, row["root"], row["rel"],
                                     Path(p), row["path_id"]):
                messagebox.showinfo("完成", f"已导出到 {p}")
            else:
                messagebox.showerror("失败", "无法导出该卡图")

    def replace_art(self):
        if not self.current_row:
            messagebox.showinfo("提示", "请先在左侧选择一张卡片")
            return
        if not self.app.check_game_closed():
            return
        p = filedialog.askopenfilename(
            title="选择替换用图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not p:
            return
        try:
            img = Image.open(p)
            img.load()
        except Exception as e:
            messagebox.showerror("无法打开图片", str(e))
            return
        # 弹出裁剪窗口：按目标卡图比例锁定框选（该卡所有资源尺寸一致）
        row = self.current_row
        dlg = CropDialog(self, img, row["w"], row["h"])
        self.wait_window(dlg)
        if dlg.result is None:
            return
        img = dlg.result
        n_res = len(cards.get_card_rows(self.conn, self.current_card))
        if not messagebox.askyesno(
                "确认替换",
                f"将用所选图片替换卡号 {self.current_card}（{self._get_name(self.current_card)}）的卡图。\n"
                f"游戏中该卡的 {n_res} 个卡图资源会一并替换，原版文件自动备份（只备份一次），确定？"):
            return
        self.replace_btn.config(state=tk.DISABLED)
        self.info_var.set("替换中，请稍候...")

        cid = self.current_card

        def work():
            # 替换该卡的全部资源（local/stream、common/tcg/ocg），无需再单独同步镜像
            # sqlite 连接不能跨线程使用，后台线程内单独建立连接
            conn = cards.init_db()
            try:
                return cards.replace_card_art(
                    self.app.paths, self.app.get_manifest(), conn, cid, img)
            finally:
                conn.close()

        def done(count, err):
            self.replace_btn.config(state=tk.NORMAL)
            if err:
                self.info_var.set(f"替换失败: {err}")
                messagebox.showerror("替换失败", str(err))
                return
            cards.invalidate_replaced_cache()
            messagebox.showinfo("完成", f"卡图替换成功！共写入 {count} 个卡图资源。")
            self.app.refresh_status()
            self.app.restore_tab.refresh()
            self.on_select(None)  # 刷新预览
            self.search()         # 刷新已改标记
        self.app.run_bg(work, done)


# ================= Tab3: 还原管理 =================

class RestoreTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=6)
        ttk.Button(top, text="刷新", command=self.refresh).pack(side=tk.LEFT)
        ttk.Button(top, text="还原选中", command=self.restore_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="全部还原", command=self.restore_all).pack(side=tk.LEFT)
        ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Label(top, text="排序:").pack(side=tk.LEFT)
        sort_options = [("路径", "rel"), ("时间升序", "time_asc"), ("时间降序", "time_desc"),
                        ("方式", "source"), ("类型", "kind")]
        self._sort_var = tk.StringVar(value="rel")
        for label, val in sort_options:
            ttk.Radiobutton(top, text=label, variable=self._sort_var,
                            value=val, command=self._apply_sort).pack(side=tk.LEFT, padx=4)
        self.count_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.count_var).pack(side=tk.LEFT, padx=10)

        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        cols = ("rel", "time", "source", "kind", "bak")
        self._sort_col = "rel"      # 当前排序列
        self._sort_reverse = False   # 是否降序
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        for c, t, w in (("rel", "相对路径", 200), ("time", "替换时间", 150),
                        ("source", "方式", 70), ("kind", "类型", 60), ("bak", "原版备份", 80)):
            self.tree.heading(c, text=t,
                              command=lambda col=c: self._sort_by(col))
            self.tree.column(c, width=w, anchor=tk.CENTER if c != "rel" else tk.W)
        sb = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", self.preview_selected)
        _attach_tree_preview_menu(self.tree, self.preview_selected)
        self.refresh()

    def preview_selected(self, _evt=None):
        """双击预览：同时提供游戏当前文件与原版备份两个源对比"""
        sels = self.tree.selection()
        if not sels or not self.app.paths:
            return
        rel = sels[0]  # iid 即 rel
        paths = self.app.paths
        sources = []
        cur = paths.bundle_path(rel)
        if cur.exists():
            sources.append(("游戏当前(mod)", cur))
        bak = core._effective_backup(paths, rel)
        if bak.exists():
            sources.append(("原版备份", bak))
        if not sources:
            messagebox.showinfo("提示", "游戏内文件与备份都不存在，无法预览")
            return
        BundlePreviewDialog(self, sources[0][1], sources=sources)

    def _apply_sort(self):
        """单选按钮排序回调"""
        val = self._sort_var.get()
        if val == "time_asc":
            self._sort_col = "time"
            self._sort_reverse = False
        elif val == "time_desc":
            self._sort_col = "time"
            self._sort_reverse = True
        else:
            self._sort_col = val
            self._sort_reverse = False
        self.refresh()

    def _sort_by(self, col: str):
        """点击列标题排序：同列再点翻转升降序"""
        if col == self._sort_col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        # 同步单选按钮状态
        if col == "time":
            self._sort_var.set("time_desc" if self._sort_reverse else "time_asc")
        else:
            self._sort_var.set(col)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self.app.paths:
            return
        entries = self.app.get_manifest().entries()
        src_map = {"pack": "整包", "image": "单图"}
        # 排序键
        def sort_key(item):
            rel, e = item
            if self._sort_col == "time":
                return (e.get("time", ""), rel)
            if self._sort_col == "source":
                return (src_map.get(e.get("source"), e.get("source")), rel)
            if self._sort_col == "kind":
                return ("新增" if e.get("added") else "覆盖", rel)
            if self._sort_col == "bak":
                has = core._effective_backup(self.app.paths, rel).exists()
                return ("有" if has else "无", rel)
            return rel  # 默认按路径
        ordered = sorted(entries.items(), key=sort_key, reverse=self._sort_reverse)
        for rel, e in ordered:
            has_bak = core._effective_backup(self.app.paths, rel).exists()
            self.tree.insert("", tk.END, iid=rel, values=(
                rel, e.get("time", ""), src_map.get(e.get("source"), e.get("source")),
                "新增" if e.get("added") else "覆盖",
                "有" if has_bak else ("无(新增)" if e.get("added") else "无")))
        sort_label = {"rel": "路径", "time": "时间", "source": "方式", "kind": "类型", "bak": "备份"}
        self.count_var.set(f"共 {len(entries)} 个文件处于 mod 状态"
                           f"  |  排序: {sort_label.get(self._sort_col, self._sort_col)}"
                           f"{' 降序' if self._sort_reverse else ' 升序'}")

    def restore_selected(self):
        if not self.app.check_game_closed():
            return
        sels = self.tree.selection()
        if not sels:
            messagebox.showinfo("提示", "请先勾选要还原的文件")
            return
        if not messagebox.askyesno("确认", f"还原选中的 {len(sels)} 个文件？"):
            return
        manifest = self.app.get_manifest()
        ok, failed = 0, []
        for rel in sels:
            try:
                core.restore_one(self.app.paths, manifest, rel)
                ok += 1
            except OSError as e:
                failed.append(f"{rel}: {e}")
        cards.invalidate_replaced_cache()
        self.refresh()
        self.app.refresh_status()
        msg = f"已还原 {ok} 个文件。"
        if failed:
            msg += f"\n{len(failed)} 个失败：\n" + "\n".join(failed[:5])
        messagebox.showinfo("完成", msg)

    def restore_all(self):
        if not self.app.check_game_closed():
            return
        if not messagebox.askyesno("确认", "将全部还原为原版状态（含删除新增文件），确定？"):
            return
        restored, deleted, failed = core.restore_all(self.app.paths, self.app.get_manifest())
        cards.invalidate_replaced_cache()
        self.refresh()
        self.app.refresh_status()
        msg = f"已还原 {restored} 个原版文件，删除 {deleted} 个新增文件。"
        if failed:
            msg += f"\n{len(failed)} 个失败：\n" + "\n".join(failed[:5])
        messagebox.showinfo("完成", msg)


if __name__ == "__main__":
    App().mainloop()
