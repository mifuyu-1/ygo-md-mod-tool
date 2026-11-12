# -*- coding: utf-8 -*-
"""超框卡图制作对话框：插图 / 卡框型号 / 边框效果 / 主体 / 效果框透明度。"""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import overframe

LOCAL_PICK = "本地图片（选择文件）..."
IMG_TYPES = [("图片", "*.png *.webp *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")]


class OverframeDialog(tk.Toplevel):
    def __init__(self, parent, app, card_id, card_name=""):
        super().__init__(parent)
        self.app = app
        self.paths = app.paths
        self.manifest = app.get_manifest()
        self.card_id = str(int(card_id))
        self.card_name = card_name or self.card_id
        self.title(f"超框卡图制作 - {self.card_id} {self.card_name}")
        self.geometry("1040x780")
        self.minsize(940, 700)
        self.transient(parent.winfo_toplevel())

        self._alive = True
        self._photo = None
        self._base_path = None
        self._markers = 0
        self._markers_ready = False
        self._preview_job = None
        self._last_sig = None
        self._status = {}

        edit = overframe.load_edit(self.card_id) or {}
        if edit and not overframe.is_registered(self.paths, self.card_id):
            # 卡图已被还原（或校验覆盖）但编辑记录还留着：按未编辑状态显示
            edit = {}
        self._from_edit = bool(edit)
        # 版式固定按原卡图：优先用编辑记录里保存的版式（超框写入后贴图尺寸会变，
        # 只靠尺寸会把普通卡误判成灵摆），没有记录时才按尺寸判断
        self._saved_layout = int(edit.get("layout") or 0) if edit.get("layout") else 0
        self._pendulum = False
        # 型号下拉只列游戏 data.unity3d 里的原生卡框，末尾附「本地图片」
        self._game_labels = []
        self._frame_no = int(edit.get("frame", 1))
        self.frame_file_var = tk.StringVar(
            value=edit.get("frame_file", "") if Path(edit.get("frame_file", "")).is_file() else "")
        self.frame_var = tk.StringVar()
        self._frame_labels = []
        self._frame_by_label = {}
        self._set_frame_labels([], self._frame_no)

        # 默认空白：不自动铺游戏原图，需要时手动点「用游戏原图」
        self.art_var = tk.StringVar(value=edit.get("art", "") if Path(edit.get("art", "")).is_file() else overframe.BLANK_ART)
        self.blank_color_var = tk.StringVar(value=edit.get("blank_color") or "#FFFFFF")
        self.subject_var = tk.StringVar(value=edit.get("subject", "") if Path(edit.get("subject", "")).is_file() else "")
        self.over_var = tk.StringVar(value=edit.get("over_layer", "") if Path(edit.get("over_layer", "")).is_file() else "")
        # 边框：每次打开窗口重新扫描（运行中新放进 tools/border 的图也能出现）
        legacy_border = edit.get("border_file")
        if legacy_border:
            overframe.adopt_border(legacy_border)
        self._refresh_border_options()
        saved_border = overframe.normalize_border(edit.get("border")) or Path(
            str(legacy_border or "")).name
        self.border_var = tk.StringVar(value=next(
            (lab for lab, name in self._border_options if name == saved_border),
            self._border_options[0][0]))
        self.scale_var = tk.DoubleVar(value=float(edit.get("scale", 1.0)) * 100)
        self.dx_var = tk.DoubleVar(value=int(edit.get("dx", 0)))
        self.dy_var = tk.DoubleVar(value=int(edit.get("dy", 0)))
        self.lore_var = tk.DoubleVar(value=float(edit.get("lore_alpha", 1.0)) * 100)
        self.status_var = tk.StringVar(value="正在读取游戏卡图...")
        self.art_text = tk.StringVar()
        self.subject_text = tk.StringVar()
        self.over_text = tk.StringVar()
        self.frame_file_text = tk.StringVar()
        self.preview_info = tk.StringVar()
        self._update_file_texts()

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        self._load_initial()
        # 游戏更新后旧版本的卡框缓存没人清，打开时顺手清一次
        self.after(1500, lambda: overframe.clean_stale_gameframes(self.paths))

    # ------------------------------------------------------------------ 界面
    def _build(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(outer, width=400)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        right = ttk.Frame(outer)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        # 按钮固定在左栏底部，参数区放滚动容器，避免窗口变矮时按钮被挤出可视范围
        btns = ttk.Frame(left)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        self.apply_btn = ttk.Button(btns, text="制作并写入游戏", command=self._apply)
        self.apply_btn.pack(fill=tk.X)
        self.export_btn = ttk.Button(btns, text="仅导出 PNG...", command=self._export)
        self.export_btn.pack(fill=tk.X, pady=4)
        self.restore_btn = ttk.Button(btns, text="还原这张卡的原版卡图", command=self._restore)
        self.restore_btn.pack(fill=tk.X)
        row = ttk.Frame(btns)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row, text="导出工程...", command=self._export_project).pack(
            side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(row, text="导入工程...", command=self._import_project).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0))
        ttk.Button(btns, text="清理素材缓存...", command=self._clean_cache).pack(
            fill=tk.X, pady=(4, 0))
        ttk.Button(btns, text="关闭", command=self._close).pack(fill=tk.X, pady=(4, 0))

        body = ttk.Frame(left)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(body, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left = ttk.Frame(self._canvas)
        holder = self._canvas.create_window((0, 0), window=left, anchor="nw")

        def _sync_scroll(_e=None):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        def _sync_width(e):
            self._canvas.itemconfigure(holder, width=e.width)

        left.bind("<Configure>", _sync_scroll)
        self._canvas.bind("<Configure>", _sync_width)
        for widget in (self._canvas, left):
            widget.bind("<MouseWheel>", self._on_wheel)

        ttk.Label(left, text=f"{self.card_id}  {self.card_name}",
                  font=("TkDefaultFont", 11, "bold"), wraplength=360).pack(anchor=tk.W)
        ttk.Label(left, textvariable=self.status_var, foreground="#555",
                  wraplength=360).pack(anchor=tk.W, pady=(2, 8))

        # ① 插图层
        g = ttk.LabelFrame(left, text="① 插图层（画在插图窗里的图）", padding=6)
        g.pack(fill=tk.X, pady=3)
        ttk.Label(g, textvariable=self.art_text, wraplength=360,
                  foreground="#333").pack(anchor=tk.W)
        row = ttk.Frame(g)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row, text="本地图片...", command=self._pick_art).pack(side=tk.LEFT)
        ttk.Button(row, text="用游戏原图", command=self._use_game_art).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="空白", command=self._blank_art).pack(side=tk.LEFT)
        row = ttk.Frame(g)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(row, text="空白时铺底色:").pack(side=tk.LEFT)
        self.swatch = tk.Label(row, text="  ", bg=self.blank_color_var.get(),
                               relief=tk.SUNKEN, width=3)
        self.swatch.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(row, textvariable=self.blank_color_var,
                  foreground="#555").pack(side=tk.LEFT)
        ttk.Button(row, text="换颜色...", command=self._pick_blank_color).pack(side=tk.LEFT, padx=6)

        # 主体抠图（插图附加）
        g = ttk.LabelFrame(left, text="插图附加：主体抠图（可选）", padding=6)
        g.pack(fill=tk.X, pady=3)
        ttk.Label(g, textvariable=self.subject_text, wraplength=360,
                  foreground="#333").pack(anchor=tk.W)
        row = ttk.Frame(g)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row, text="本地图片...", command=self._pick_subject).pack(side=tk.LEFT)
        ttk.Button(row, text="清除", command=self._clear_subject).pack(side=tk.LEFT, padx=4)

        # ② 卡框层（只有型号与边框两个下拉；非官方素材都走「本地图片」项）
        g = ttk.LabelFrame(left, text="② 卡框层（卡片边框与配色）", padding=6)
        g.pack(fill=tk.X, pady=3)
        ttk.Label(g, text="型号（卡型卡框）:").grid(row=0, column=0, sticky=tk.W)
        cb = ttk.Combobox(g, textvariable=self.frame_var, state="readonly",
                          values=self._frame_labels, width=24)
        cb.grid(row=0, column=1, sticky=tk.EW, padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_frame_combo())
        self._frame_cb = cb
        ttk.Label(g, textvariable=self.frame_file_text, foreground="#555",
                  wraplength=340).grid(row=1, column=0, columnspan=2,
                                       sticky=tk.W, pady=(2, 0))
        ttk.Label(g, text="边框:").grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        cb2 = ttk.Combobox(g, textvariable=self.border_var, state="readonly",
                           values=[lab for lab, _ in self._border_options], width=24)
        cb2.grid(row=2, column=1, sticky=tk.EW, padx=(6, 0), pady=(6, 0))
        cb2.bind("<<ComboboxSelected>>", lambda _e: self._schedule_preview())
        self._border_cb = cb2
        g.columnconfigure(1, weight=1)

        # ③ 超框层
        g = ttk.LabelFrame(left, text="③ 超框层（整卡叠加图，alpha 作遮罩）", padding=6)
        g.pack(fill=tk.X, pady=3)
        ttk.Label(g, textvariable=self.over_text, wraplength=360,
                  foreground="#333").pack(anchor=tk.W)
        row = ttk.Frame(g)
        row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(row, text="本地图片...", command=self._pick_over).pack(side=tk.LEFT)
        ttk.Button(row, text="清除", command=self._clear_over).pack(side=tk.LEFT, padx=4)

        # 主体位置与效果框
        g = ttk.LabelFrame(left, text="位置与效果框", padding=6)
        g.pack(fill=tk.X, pady=3)
        self._slider(g, 0, "主体/背景缩放", self.scale_var, 30, 180, "%")
        self._slider(g, 1, "水平偏移", self.dx_var, -300, 300, " px")
        self._slider(g, 2, "垂直偏移", self.dy_var, -300, 300, " px")
        self._slider(g, 3, "效果框透明度", self.lore_var, 0, 100, "%")
        ttk.Label(g, text="没选主体时，缩放与偏移作用在背景卡图上",
                  foreground="#777", wraplength=340).grid(
            row=4, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        g.columnconfigure(1, weight=1)

        # 预览
        ttk.Label(right, text="预览（最终写入的就是这张）").pack(anchor=tk.W)
        self.preview = ttk.Label(right, text="正在准备...", anchor=tk.CENTER,
                                 relief=tk.SUNKEN)
        self.preview.pack(fill=tk.BOTH, expand=True, pady=6)
        ttk.Label(right, textvariable=self.preview_info, foreground="#555").pack(anchor=tk.W)
        self._bind_wheel(left)

    def _on_wheel(self, event):
        first, last = self._canvas.yview()
        if first > 0.0 or last < 1.0:
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _wheel_combo(self, event):
        """下拉框上滚轮只滚左栏，不切换选项。"""
        self._on_wheel(event)
        return "break"

    def _bind_wheel(self, widget):
        if isinstance(widget, ttk.Combobox):
            widget.bind("<MouseWheel>", self._wheel_combo, add="+")
        else:
            widget.bind("<MouseWheel>", self._on_wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _slider(self, parent, row, label, var, lo, hi, suffix):
        ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky=tk.W)
        val = ttk.Label(parent, width=7, anchor=tk.E)
        val.grid(row=row, column=2, sticky=tk.E)

        def show(v):
            if suffix == "%":
                val.config(text=f"{float(v):.0f}%")
            else:
                val.config(text=f"{int(float(v))}{suffix}")

        sc = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=show)
        sc.grid(row=row, column=1, sticky=tk.EW, padx=6, pady=2)
        show(var.get())
        var.trace_add("write", lambda *_a: self._schedule_preview())
        return sc

    # ------------------------------------------------------------------ 数据
    def _set_frame_labels(self, labels, n=None):
        """换一份卡框列表，末尾附「本地图片」项；选了本地卡框时高亮该项。"""
        self._frame_labels = [lab for lab, _ in labels] + [LOCAL_PICK]
        self._frame_by_label = {lab: num for lab, num in labels}
        want = self._frame_no if n is None else int(n)
        if self.frame_file_var.get():
            self.frame_var.set(LOCAL_PICK)
        else:
            label = next((lab for lab, num in labels if num == want), None)
            if label is None and labels:
                label = labels[0][0]
                want = labels[0][1]
            if label is not None:
                self.frame_var.set(label)
        if not labels and not self.frame_file_var.get():
            # 还没读到游戏卡框、也没选本地图：留空，等后台读完再补上
            self.frame_var.set("")
        self._frame_no = want
        cb = getattr(self, "_frame_cb", None)
        if cb is not None:
            cb.config(values=self._frame_labels)

    def _on_frame_combo(self):
        """型号下拉：选游戏卡框直接换；选「本地图片」则弹选图（默认 tools）。"""
        if self.frame_var.get() != LOCAL_PICK:
            self._frame_no = int(self._frame_by_label.get(self.frame_var.get(), self._frame_no or 1))
            # 从本地卡框切回游戏型号时清掉本地图，否则渲染仍会优先用本地图
            self.frame_file_var.set("")
            self._update_file_texts()
            self._on_frame_change()
            return
        p = filedialog.askopenfilename(
            parent=self, title="选择卡框图片（建议 704×1024 透明 PNG）",
            initialdir=str(overframe.TOOLS_DIR), filetypes=IMG_TYPES)
        if not p:
            # 取消：回到原来的选择
            self._set_frame_labels(self._game_labels, self._frame_no)
            return
        self.frame_file_var.set(p)
        self._set_frame_labels(self._game_labels, self._frame_no)
        self._update_file_texts()
        self._schedule_preview()

    def _frame(self) -> int:
        return int(self._frame_by_label.get(self.frame_var.get(), self._frame_no or 1))

    def _layout(self) -> int:
        return (overframe.LAYOUT_PENDULUM if self._pendulum
                else overframe.LAYOUT_NORMAL)

    def _refresh_border_options(self):
        """重新扫描可用边框（不变边框 + tools/border + 历史导入）。"""
        self._border_options = overframe.border_options()
        self._border_by_label = {lab: name for lab, name in self._border_options}

    def _border(self) -> str:
        """当前选的边框文件名（空字符串=不变边框）。"""
        return self._border_by_label.get(self.border_var.get(), "")

    def _update_file_texts(self):
        art = self.art_var.get()
        if not art or art == overframe.BLANK_ART:
            self.art_text.set("（空白：插图窗只铺底色，需要时点「用游戏原图」）")
        else:
            p = Path(art)
            name = "游戏原图" if self._base_path and p.resolve() == Path(self._base_path).resolve() else p.name
            self.art_text.set(name)
        sub = self.subject_var.get()
        self.subject_text.set(Path(sub).name if sub else "（没有主体，只换插图与边框）")
        over = self.over_var.get()
        self.over_text.set(Path(over).name if over else "（没有超框层，只输出普通卡面版式）")
        ff = self.frame_file_var.get()
        if ff and not Path(ff).is_file():
            self.frame_file_text.set("（本地卡框文件已丢失，将用游戏内卡框）")
        else:
            self.frame_file_text.set(f"本地卡框：{Path(ff).name}" if ff else "（用游戏内卡框，外观由型号决定）")

    def _params(self) -> dict:
        # 选过的本地卡框可能已被删掉/移走：找不到文件就回落到游戏卡框，
        # 否则 render 会在打开图片时直接抛错，预览和写入都会失败
        frame_file = self.frame_file_var.get()
        if frame_file and not Path(frame_file).is_file():
            frame_file = ""
        return {
            "art": self.art_var.get() or overframe.BLANK_ART,
            "subject": self.subject_var.get() or None,
            "over_layer": self.over_var.get() or None,
            "scale": float(self.scale_var.get()) / 100.0,
            "dx": int(self.dx_var.get()),
            "dy": int(self.dy_var.get()),
            "lore_alpha": max(0.0, min(1.0, float(self.lore_var.get()) / 100.0)),
            "border": self._border(),
            "frame": self._frame(),
            "link_markers": self._markers,
            "frame_source": "file" if frame_file else "game",
            "frame_file": frame_file or None,
            "blank_color": self.blank_color_var.get(),
        }

    def _load_initial(self):
        paths = self.paths
        card_id = self.card_id
        frame = self._frame()
        need_base = not self.art_var.get()

        def work_art():
            base = None
            if need_base:
                try:
                    base = str(overframe.base_art(paths, card_id, frame))
                except Exception:
                    base = None
            markers = 0
            if frame == overframe.LINK_FRAME:
                try:
                    markers = overframe.markers_of_card(paths, card_id)
                except Exception:
                    markers = 0
            try:
                game_labels = overframe.game_frame_options(paths)
            except Exception:
                game_labels = []
            pendulum = False
            if self._saved_layout:
                pendulum = self._saved_layout == overframe.LAYOUT_PENDULUM
            else:
                conn = None
                try:
                    import cards as _cards
                    conn = _cards.init_db()
                    rows = _cards.get_card_rows(conn, card_id)
                    # 超框贴图也是竖图（512×1024 / 704×1024），不能当灵摆；
                    # 官方超框（over=1）全部排除后没有普通行，就按普通版式
                    pendulum = any(int(r.get("h") or 0) > int(r.get("w") or 0)
                                   and not r.get("over") for r in rows)
                except Exception:
                    pendulum = False
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return base, markers, game_labels, pendulum

        def done_art(result, err):
            if not self._alive:
                return
            base, markers, game_labels, pendulum = result if result else (None, 0, [], False)
            self._pendulum = pendulum
            if pendulum and not self._from_edit:
                self._frame_no = overframe.LAYOUT_PENDULUM
            if game_labels:
                self._game_labels = game_labels
                self._set_frame_labels(game_labels, self._frame_no)
            if base:
                self._base_path = base
            self._markers = markers
            self._markers_ready = frame == overframe.LINK_FRAME
            self._update_file_texts()
            if err:
                self.preview.config(image="", text=f"读取卡图失败：{err}")
            self._schedule_preview()

        def work_status():
            return overframe.status(paths, card_id)

        def done_status(result, err):
            if not self._alive:
                return
            self._status = result or {}
            if err:
                self.status_var.set(f"读取卡图状态失败：{err}")
                return
            self._update_status_text()

        self.app.run_bg(work_art, done_art)
        self.app.run_bg(work_status, done_status)

    def _update_status_text(self):
        s = self._status or {}
        if not s.get("exists"):
            self.status_var.set(s.get("error") or "游戏里没有这张卡的卡图")
            return
        size = f"{s.get('width')}×{s.get('height')}"
        kind = "超框图" if s.get("overframe") else "普通卡图"
        reg = " · 已登记超框" if s.get("registered") else ""
        ours = " · 本工具制作过" if s.get("ours") else ""
        self.status_var.set(f"当前卡图：{size}（{kind}）{reg}{ours}")

    # ------------------------------------------------------------------ 选择素材
    def _pick_art(self):
        p = filedialog.askopenfilename(
            parent=self, title="选择卡图背景",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if p:
            self.art_var.set(p)
            self._update_file_texts()
            self._schedule_preview()

    def _use_game_art(self):
        frame = self._frame()

        def work():
            return str(overframe.base_art(self.paths, self.card_id, frame))

        def done(result, err):
            if not self._alive:
                return
            if err:
                messagebox.showerror("失败", str(err), parent=self)
                return
            self._base_path = result
            self.art_var.set(result)
            self._update_file_texts()
            self._schedule_preview()

        self.app.run_bg(work, done)

    def _blank_art(self):
        self.art_var.set(overframe.BLANK_ART)
        self._update_file_texts()
        self._schedule_preview()

    def _pick_blank_color(self):
        from tkinter import colorchooser
        rgb, hexcode = colorchooser.askcolor(
            color=self.blank_color_var.get(), parent=self, title="选空白底色")
        if not hexcode:
            return
        self.blank_color_var.set(hexcode.upper())
        self.swatch.configure(bg=hexcode)
        self._schedule_preview()

    def _export_project(self):
        default = f"超框工程_{self.card_id}.zip"
        dest = filedialog.asksaveasfilename(
            parent=self, title="导出卡图工程", defaultextension=".zip",
            initialfile=default, filetypes=[("卡图工程", "*.zip")])
        if not dest:
            return
        try:
            overframe.export_project(self.card_id, self._params(), dest)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self)
            return
        messagebox.showinfo("导出完成", f"已保存：\n{dest}", parent=self)

    def _import_project(self):
        src = filedialog.askopenfilename(
            parent=self, title="导入卡图工程", filetypes=[("卡图工程", "*.zip")])
        if not src:
            return
        try:
            cid, params = overframe.import_project(src)
        except Exception as e:
            messagebox.showerror("导入失败", f"不是有效的卡图工程：{e}", parent=self)
            return
        if cid and cid != self.card_id:
            if not messagebox.askyesno(
                    "卡号不一致",
                    f"这个工程是卡 {cid} 的，仍要套用到当前卡 {self.card_id}？",
                    parent=self):
                return
        self._apply_params(params)
        messagebox.showinfo("导入完成", "参数已套用，预览已刷新。", parent=self)

    def _clean_cache(self):
        usage = overframe.cache_usage()
        mb = usage["total"] / 1024 / 1024
        detail = (f"游戏卡框 {usage['gameframes']/1024/1024:.1f} MB\n"
                  f"素材副本 {usage['edits']/1024/1024:.1f} MB\n"
                  f"导入素材 {usage['imported']/1024/1024:.1f} MB\n"
                  f"边框素材 {usage.get('borders', 0)/1024/1024:.1f} MB（不清理）\n"
                  f"游戏原图底图 {usage['base']/1024/1024:.1f} MB\n\n"
                  f"合计 {mb:.1f} MB")
        if not messagebox.askyesno(
                "清理素材缓存",
                f"当前缓存占用：\n{detail}\n\n"
                "将删除：过期游戏版本的卡框、没有任何编辑记录引用的素材副本。\n"
                "已保存的工程参数和仍在使用的素材不会被删除。\n\n继续清理？",
                parent=self):
            return
        freed = overframe.clean_cache(self.paths, keep_card=self.card_id)
        messagebox.showinfo(
            "清理完成",
            f"共释放 {freed['freed']/1024/1024:.1f} MB。\n"
            f"（卡框 {freed['gameframes']/1024/1024:.1f} MB，"
            f"素材 {freed['edits']/1024/1024:.1f} MB，"
            f"导入 {freed['imported']/1024/1024:.1f} MB，"
            f"底图 {freed['base']/1024/1024:.1f} MB）",
            parent=self)

    def _apply_params(self, params: dict):
        art = params.get("art")
        self.art_var.set(art if art else overframe.BLANK_ART)
        self.subject_var.set(params.get("subject") or "")
        self.over_var.set(params.get("over_layer") or "")
        ff = params.get("frame_file") or ""
        self.frame_file_var.set(ff if ff and Path(ff).is_file() else "")
        self.blank_color_var.set(params.get("blank_color") or "#FFFFFF")
        self.swatch.configure(bg=self.blank_color_var.get())
        self.scale_var.set(float(params.get("scale") or 1.0) * 100)
        self.dx_var.set(int(params.get("dx") or 0))
        self.dy_var.set(int(params.get("dy") or 0))
        self.lore_var.set(float(params.get("lore_alpha", 1.0)) * 100)
        legacy_border = params.get("border_file")
        if legacy_border:
            overframe.adopt_border(legacy_border)
            self._refresh_border_options()
            self._border_cb.configure(
                values=[lab for lab, _ in self._border_options])
        saved_border = overframe.normalize_border(params.get("border")) or Path(
            str(legacy_border or "")).name
        self.border_var.set(next(
            (lab for lab, name in self._border_options if name == saved_border),
            self._border_options[0][0]))
        self._set_frame_labels(self._game_labels, int(params.get("frame") or 1))

        self._update_file_texts()
        # 导入的工程如果是连接怪兽（18 号卡框），按型号重新补算箭头，
        # 否则 link_markers 还是初始的 0，预览/写入会缺箭头
        self._on_frame_change()

    def _pick_subject(self):
        p = filedialog.askopenfilename(
            parent=self, title="选择主体大图（透明 PNG，或纯色背景的 JPG）",
            filetypes=[("图片（PNG 透明 / JPG 纯色底）", "*.png *.webp *.jpg *.jpeg *.bmp"),
                       ("所有文件", "*.*")])
        if p:
            self.subject_var.set(p)
            self._update_file_texts()
            self._schedule_preview()

    def _clear_subject(self):
        self.subject_var.set("")
        self._update_file_texts()
        self._schedule_preview()

    def _pick_over(self):
        p = filedialog.askopenfilename(
            parent=self, title="选择超框层图片（整卡 704×1024，alpha 作遮罩）",
            filetypes=[("图片（PNG 透明）", "*.png *.webp *.jpg *.jpeg *.bmp"),
                       ("所有文件", "*.*")])
        if p:
            self.over_var.set(p)
            self._update_file_texts()
            self._schedule_preview()

    def _clear_over(self):
        self.over_var.set("")
        self._update_file_texts()
        self._schedule_preview()

    def _on_frame_change(self):
        if self._frame() == overframe.LINK_FRAME and not self._markers_ready:
            self._load_markers()
        self._schedule_preview()

    def _load_markers(self):
        self._markers_ready = True

        def work():
            return overframe.markers_of_card(self.paths, self.card_id)

        def done(result, err):
            if not self._alive:
                return
            self._markers = int(result or 0)
            self._schedule_preview()

        self.app.run_bg(work, done)

    # ------------------------------------------------------------------ 预览
    def _schedule_preview(self):
        if not self._alive:
            return
        if self._preview_job:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(50, self._render_preview)

    def _render_preview(self):
        self._preview_job = None
        if not self._alive:
            return
        params = self._params()
        if not params["art"]:
            self.preview.config(image="", text="正在读取游戏卡图...")
            return
        # 参数没变就不重复合成（滑块 trace 会连续触发多次）
        sig = tuple(sorted((k, str(v)) for k, v in params.items()))
        if sig == self._last_sig:
            return
        try:
            img = overframe.render(self.paths, self.card_id, fast=True,
                                   layout=self._layout(), **params)
        except Exception as e:
            self.preview.config(image="", text=f"预览失败：{e}")
            self.preview_info.set("")
            return
        self._last_sig = sig
        disp = img.copy()
        disp.thumbnail((420, 580), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(disp)
        self.preview.config(image=self._photo, text="")
        self.preview_info.set(
            f"输出 {overframe.CARD_W}×{overframe.CARD_H} · 预览 {disp.width}×{disp.height}"
            f" · 卡框 {self._frame():02d} {overframe.frame_name(self._frame())}")

    # ------------------------------------------------------------------ 动作
    def _disable(self, busy_text):
        self.apply_btn.config(state=tk.DISABLED)
        self.export_btn.config(state=tk.DISABLED)
        self.restore_btn.config(state=tk.DISABLED)
        self.status_var.set(busy_text)

    def _enable(self):
        self.apply_btn.config(state=tk.NORMAL)
        self.export_btn.config(state=tk.NORMAL)
        self.restore_btn.config(state=tk.NORMAL)

    def _apply(self):
        if not self.app.check_game_closed():
            return
        params = self._params()
        if not params["art"]:
            messagebox.showwarning("提示", "还没有卡图背景。请选择图片或使用游戏原图。", parent=self)
            return
        if not messagebox.askyesno(
                "确认制作",
                f"将为 {self.card_id}（{self.card_name}）合成超框卡图并写入游戏，\n"
                "同时把这张卡登记为超框。原版文件会自动备份。\n\n"
                "游戏正在运行的话请先关闭。确定继续？", parent=self):
            return
        self._disable("正在合成并写入游戏...")
        paths, manifest, card_id = self.paths, self.manifest, self.card_id
        if params["art"] == overframe.BLANK_ART:
            art_name = "空白"
        else:
            art_name = ("游戏原图"
                        if self._base_path
                        and Path(params["art"]).resolve() == Path(self._base_path).resolve()
                        else Path(params["art"]).name)
        sub_name = Path(params["subject"]).name if params["subject"] else ""
        over_name = Path(params["over_layer"]).name if params["over_layer"] else ""

        def work():
            img = overframe.render(paths, card_id, layout=self._layout(), **params)
            res = overframe.apply(paths, manifest, card_id, img)
            res["edit_saved"] = overframe.save_edit(
                card_id, art=params["art"], art_name=art_name,
                subject=params["subject"], subject_name=sub_name,
                scale=params["scale"], dx=params["dx"], dy=params["dy"],
                lore_alpha=params["lore_alpha"], border=params["border"],
                frame=params["frame"], art_rel=res.get("rel", ""),
                art_rels=res.get("rels"), frame_source=params["frame_source"],
                frame_file=params["frame_file"], over_layer=params["over_layer"],
                over_name=over_name,
                blank_color=params["blank_color"], ids=res.get("ids"),
                layout=self._layout())
            return res

        def done(result, err):
            if not self._alive:
                return
            self._enable()
            if err:
                self.status_var.set(f"写入失败：{err}")
                messagebox.showerror("写入失败", str(err), parent=self)
                return
            self.status_var.set("超框卡图已写入（完全退出游戏再重开才会加载）")
            messagebox.showinfo(
                "完成",
                "超框卡图已写入游戏。\n完全退出游戏后重新启动即可看到效果。",
                parent=self)
            if not result.get("edit_saved"):
                messagebox.showwarning(
                    "编辑记录未保存",
                    "卡图已经写入游戏，但编辑参数没能写进 edits.json。\n"
                    "下次打开这张卡时不会自动带出这次的参数，\n"
                    "仍可在「还原管理」里还原原版卡图。",
                    parent=self)
            # 记住这次制作使用的版式，后续刷新/重开都按它走
            self._saved_layout = self._layout()
            self.app.refresh_status()
            self.app.restore_tab.refresh()
            if self.app.card_tab.conn is not None:
                self.app.card_tab.on_select(None)
                self.app.card_tab.search()
            self._load_initial()

        self.app.run_bg(work, done)

    def _export(self):
        params = self._params()
        if not params["art"]:
            messagebox.showwarning("提示", "还没有卡图背景。请选择图片或使用游戏原图。", parent=self)
            return
        p = filedialog.asksaveasfilename(
            parent=self, defaultextension=".png",
            initialfile=f"{self.card_id}_overframe.png",
            filetypes=[("PNG 图片", "*.png")])
        if not p:
            return
        try:
            img = overframe.render(self.paths, self.card_id, layout=self._layout(), **params)
            img.save(p)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self)
            return
        messagebox.showinfo("完成", f"已导出到：\n{p}", parent=self)

    def _restore(self):
        if not self.app.check_game_closed():
            return
        if not messagebox.askyesno(
                "确认还原",
                f"将把 {self.card_id}（{self.card_name}）的卡图还原为原版，\n"
                "并取消这张卡的超框登记。确定继续？", parent=self):
            return
        self._disable("正在还原...")
        paths, manifest, card_id = self.paths, self.manifest, self.card_id

        def work():
            return overframe.remove(paths, manifest, card_id)

        def done(result, err):
            if not self._alive:
                return
            self._enable()
            if err:
                messagebox.showerror("还原失败", str(err), parent=self)
                return
            if result and not result.get("restored"):
                messagebox.showinfo(
                    "完成",
                    "没有找到这张卡对应的备份（可能已经是原版卡图），\n"
                    "已清理超框登记与编辑记录。", parent=self)
            else:
                messagebox.showinfo("完成", "已还原这张卡的卡图与超框登记。", parent=self)
            self.status_var.set("已还原为原版卡图")
            self.app.refresh_status()
            self.app.restore_tab.refresh()
            if self.app.card_tab.conn is not None:
                self.app.card_tab.on_select(None)
                self.app.card_tab.search()
            self._base_path = None
            self._saved_layout = 0
            self.art_var.set(overframe.BLANK_ART)
            self.subject_var.set("")
            self.over_var.set("")
            self._load_initial()

        self.app.run_bg(work, done)

    def _close(self):
        self._alive = False
        try:
            overframe.drop_base(self.card_id)
        except Exception:
            pass
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
