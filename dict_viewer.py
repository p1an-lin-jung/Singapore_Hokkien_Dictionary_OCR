#!/usr/bin/env python3
"""词典正文查阅器（tkinter GUI）。

左栏显示《新加坡闽南话词典》PDF 正文页面，右栏以友好格式显示该页对应的词条
（读取 `dictionary_ocr/3_词典正文.txt`）。工具栏支持按 词条/发音/释义/例句
搜索；发音搜索为模糊匹配：忽略声调（上标数字），鼻化韵与非鼻化韵等价
（ĩ ã ẽ ɔ̃ ũ 等视同 i a e ɔ u）。点击搜索结果跳转到对应页面并高亮词条。

用法:
    python3 dict_viewer.py            # 启动 GUI（需图形界面或 X11 转发）
    python3 dict_viewer.py --selftest # 无界面自检：数据加载与搜索逻辑
"""

from __future__ import annotations

import io
import json
import re
import sys
import unicodedata
from pathlib import Path

import fitz  # PyMuPDF

BASE = Path(__file__).parent
PDF_PATH = BASE / "src" / "新加坡闽南话词典(2002).pdf"
TXT_PATH = BASE / "dictionary_ocr" / "3_词典正文.txt"
BBOX_PATH = BASE / "dictionary_ocr" / "3_词典正文.bbox.json"

FIRST_MAIN_PAGE = 67   # 正文起始 pdf 页
DEFAULT_ZOOM = 1.3     # 渲染倍率（基础 120 DPI 之上）
BASE_DPI = 120

# 声调记号：上标数字与连读符号
TONE_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻"
# 鼻化符号（组合用波浪号）；ĩ ã ẽ õ ũ 等预组合字符经 NFD 分解后也是它
COMBINING_TILDE = "̃"


# ────────────────────── 发音归一化（模糊匹配核心） ──────────────────────


def norm_pron(s: str) -> str:
    """归一化发音串：去声调、鼻化符号、空白与标点，转小写。

    "a¹⁻⁶ pa⁶" → "apa"；"tshĩ⁵⁵" → "tshi"；"kɔ̃²" → "kɔ"
    """
    s = unicodedata.normalize("NFD", s)
    out = []
    for ch in s:
        if ch == COMBINING_TILDE:
            continue  # 鼻化韵 ≈ 非鼻化韵
        if ch in TONE_CHARS or ch.isdigit():
            continue  # 忽略声调
        if ch.isspace() or unicodedata.category(ch).startswith("P"):
            continue  # 忽略空格与标点（括号、斜杠等）
        out.append(ch.lower())
    return "".join(out)


# ────────────────────── 文本解析 ──────────────────────

# 页码标记：`<!-- page 067 -->` 或 `<!-- page 067 (empty) -->`
_PAGE_MARK = re.compile(r"^<!--\s*page\s+(\d+)(?:\s+\([^)]*\))?\s*-->$")

# 词条行：`【词】  [ipa]`；IPA 允许一层嵌套 `[a[b]c]`
_HEAD_RE = re.compile(
    r"^【(?P<hw>[^】]+)】\s+\[(?P<ipa>(?:[^\[\]]|\[[^\[\]]*\])+)\]\s*(?P<tail>.*)$"
)


def parse_ipa(inside: str) -> list[str]:
    """`a¹⁻⁶ / b²` → ['a¹⁻⁶', 'b²']；无斜杠时视作单个读音。"""
    if " / " in inside:
        return [s.strip() for s in inside.split(" / ") if s.strip()]
    return [inside.strip()]


def load_entries(path: Path = TXT_PATH) -> list[dict]:
    """把 txt 词典解析成 entry 列表。

    entry 字段：
        id          自增序号
        pdf全文页码  从页码标记推断
        正文页码    = pdf全文页码 - 66
        section     该词条前最近一次出现的大类/小节标题（如 "天文地理 / （1）天文"）
        词条        中文词头
        音标        list[str]，多读音
        释义        list[str]，非「例：」开头的正文行
        例句        list[str]，去掉「例：」前缀的正文行
    """
    entries: list[dict] = []
    cur_page: int | None = None
    cur_h1: str | None = None
    cur_h2: str | None = None
    cur_entry: dict | None = None

    def flush() -> None:
        nonlocal cur_entry
        if cur_entry is not None:
            entries.append(cur_entry)
            cur_entry = None

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip()

            if not line.strip():
                flush()
                continue

            m = _PAGE_MARK.match(line)
            if m:
                flush()
                cur_page = int(m.group(1))
                continue

            if line.startswith("# ") and not line.startswith("## "):
                flush()
                cur_h1 = line[2:].strip()
                cur_h2 = None
                continue
            if line.startswith("## "):
                flush()
                cur_h2 = line[3:].strip()
                continue

            hm = _HEAD_RE.match(line)
            if hm:
                flush()
                section_parts = [x for x in (cur_h1, cur_h2) if x]
                cur_entry = {
                    "id": len(entries) + 1,
                    "pdf全文页码": cur_page or 0,
                    "正文页码": (cur_page - 66) if cur_page else 0,
                    "section": " / ".join(section_parts),
                    "词条": hm.group("hw").strip(),
                    "音标": parse_ipa(hm.group("ipa")),
                    "释义": [],
                    "例句": [],
                }
                tail = hm.group("tail").strip()
                if tail:  # 尾巴（罕见）如 `=【别名】`，作为释义
                    cur_entry["释义"].append(tail)
                continue

            # 普通正文行：归到当前词条
            if cur_entry is None:
                continue  # 跳过任何游离行
            if line.startswith("例：") or line.startswith("例:"):
                cur_entry["例句"].append(line[2:].strip())
            else:
                cur_entry["释义"].append(line)

    flush()
    return entries


# ────────────────────── 搜索 ──────────────────────


def entry_matches(e: dict, q: str, nq: str, mode: str) -> bool:
    """q 为小写原文查询，nq 为归一化后的发音查询。"""
    def in_pron() -> bool:
        return bool(nq) and any(nq in norm_pron(p) for p in e["音标"])

    def in_text(field: str) -> bool:
        return q in " ".join(e[field]).lower()

    if mode == "发音":
        return in_pron()
    if mode == "词条":
        return q in str(e["词条"]).lower()
    if mode == "释义":
        return in_text("释义")
    if mode == "例句":
        return in_text("例句")
    # 全部
    return (
        q in str(e["词条"]).lower()
        or in_pron()
        or in_text("释义")
        or in_text("例句")
    )


def search(entries: list[dict], query: str, mode: str) -> list[dict]:
    q = query.strip()
    if not q:
        return []
    return [e for e in entries if entry_matches(e, q.lower(), norm_pron(q), mode)]


# ────────────────────── GUI ──────────────────────


def run_gui() -> int:
    import tkinter as tk
    from tkinter import ttk
    from tkinter import font as tkfont

    from PIL import Image, ImageTk

    entries = load_entries()
    by_pdf_page: dict[int, list[dict]] = {}
    for e in entries:
        by_pdf_page.setdefault(e["pdf全文页码"], []).append(e)

    # 加载可选的 bbox 数据（可能缺失）：
    #   bbox_pages[page_num] = {"image": {w,h}, "entries": [{idx, hw, ipa, bbox}]}
    bbox_pages: dict[int, dict] = {}
    if BBOX_PATH.exists():
        try:
            bbox_data = json.loads(BBOX_PATH.read_text(encoding="utf-8"))
            for k, v in bbox_data.get("pages", {}).items():
                bbox_pages[int(k)] = v
        except Exception as ex:
            print(f"[warn] 无法加载 bbox：{ex}", file=sys.stderr)

    # 将 (页码, entry_id) 映射到 (页内 idx)：依靠同页中 entries 的顺序
    entry_to_bbox_idx: dict[tuple[int, int], int] = {}
    for page_num, es in by_pdf_page.items():
        for i, e in enumerate(es):
            entry_to_bbox_idx[(page_num, e["id"])] = i

    # 类目索引：以 H1 大类（如“天文地理”）为单位，映射到首次出现的 pdf 页码。
    # section 字段形如 “天文地理 / （1）天文”，取 " / " 前部分作为 H1。
    sections: list[tuple[str, int]] = []
    _seen: set[str] = set()
    for e in entries:
        sec = (e.get("section") or "").split(" / ")[0].strip()
        if not sec or sec in _seen:
            continue
        _seen.add(sec)
        sections.append((sec, e["pdf全文页码"]))

    doc = fitz.open(PDF_PATH)

    root = tk.Tk()
    root.title("新加坡闽南话词典 · 正文查阅器")
    root.geometry("1280x860")

    # 尽量选同时覆盖中文与 IPA 上标字符的字体
    fams = set(tkfont.families(root))

    def pick(cands, **kw):
        for f in cands:
            if f in fams:
                return tkfont.Font(family=f, **kw)
        return tkfont.Font(**kw)

    CJK = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei",
           "PingFang SC", "SimSun", "AR PL UMing CN", "DejaVu Sans"]
    f_head = pick(CJK, size=15, weight="bold")
    f_ipa = pick(["DejaVu Sans"] + CJK, size=11)
    f_label = pick(CJK, size=10, weight="bold")
    f_body = pick(CJK, size=11)
    f_section = pick(CJK, size=10)

    state = {"page": FIRST_MAIN_PAGE, "zoom": DEFAULT_ZOOM,
             "results": [], "hl_id": None}

    # ── 顶部工具栏 ──
    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=8, pady=6)

    ttk.Label(bar, text="搜索").pack(side="left")
    query_var = tk.StringVar()
    query = ttk.Entry(bar, textvariable=query_var, width=22)
    query.pack(side="left", padx=(4, 4))
    mode_var = tk.StringVar(value="全部")
    ttk.Combobox(bar, textvariable=mode_var, state="readonly", width=5,
                 values=["全部", "词条", "发音", "释义", "例句"]).pack(side="left")
    search_btn = ttk.Button(bar, text="搜索")
    search_btn.pack(side="left", padx=(4, 12))

    ttk.Button(bar, text="◀", width=3,
               command=lambda: goto_page(state["page"] - 1)).pack(side="left")
    page_var = tk.StringVar(value=str(FIRST_MAIN_PAGE))
    page_ent = ttk.Entry(bar, textvariable=page_var, width=6, justify="center")
    page_ent.pack(side="left", padx=4)
    ttk.Label(bar, text=f"/ {doc.page_count}").pack(side="left")
    ttk.Button(bar, text="跳转",
               command=lambda: goto_page(parse_page_input())).pack(side="left", padx=(4, 4))
    ttk.Button(bar, text="▶", width=3,
               command=lambda: goto_page(state["page"] + 1)).pack(side="left", padx=(0, 12))

    # 类目跳转（H1 大类）
    ttk.Label(bar, text="类目").pack(side="left")
    section_var = tk.StringVar(value="─ 选择大类 ─")
    section_cb = ttk.Combobox(
        bar, textvariable=section_var, state="readonly", width=14,
        values=[name for name, _ in sections],
    )
    section_cb.pack(side="left", padx=(4, 12))

    def on_section_pick(_evt=None):
        name = section_var.get()
        for n, p in sections:
            if n == name:
                goto_page(p)
                break
        # 选完后把焦点交回搜索框，方便键盘翻页
        root.focus_set()
    section_cb.bind("<<ComboboxSelected>>", on_section_pick)

    ttk.Label(bar, text="缩放").pack(side="left")
    ttk.Button(bar, text="－", width=3, command=lambda: zoom(-0.2)).pack(side="left", padx=(4, 0))
    ttk.Button(bar, text="＋", width=3, command=lambda: zoom(0.2)).pack(side="left")

    # 键位/操作提示条（只读、淡色，不占程序逻辑）
    hint = (
        "← / ↑  上一页　　→ / ↓  下一页　　"
        "鼠标滑轮 = 上下滚动　　Shift + 滑轮 = 左右滚动"
    )
    ttk.Label(root, text=hint, foreground="#888888").pack(
        fill="x", padx=8, pady=(0, 2)
    )

    # ── 主体：左 PDF，右 结果+词条 ──
    paned = ttk.Panedwindow(root, orient="horizontal")
    paned.pack(expand=True, fill="both", padx=8, pady=(0, 4))

    left = ttk.Frame(paned)
    paned.add(left, weight=4)
    canvas = tk.Canvas(left, bg="#555555", highlightthickness=0)
    cv = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
    ch = ttk.Scrollbar(left, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=cv.set, xscrollcommand=ch.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    cv.grid(row=0, column=1, sticky="ns")
    ch.grid(row=1, column=0, sticky="ew")
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)

    # 鼠标滑轮滚动（仅当鼠标位于词典 PDF 区域时生效）。
    # 普通滑轮 = 上下；Shift + 滑轮 = 左右。
    # 跨平台：Windows/macOS 用 <MouseWheel> + event.delta；
    # Linux/X11 用 <Button-4>/<Button-5>。
    WHEEL_STEP = 3

    def _wheel_dir(event) -> int:
        """返回 +1（下/右滚）或 -1（上/左滚）。"""
        num = getattr(event, "num", 0)
        if num == 4:
            return -1
        if num == 5:
            return 1
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return 0
        return -1 if delta > 0 else 1

    def _on_wheel_y(event):
        canvas.yview_scroll(_wheel_dir(event) * WHEEL_STEP, "units")
        return "break"

    def _on_wheel_x(event):
        canvas.xview_scroll(_wheel_dir(event) * WHEEL_STEP, "units")
        return "break"

    def _bind_wheel(_e=None):
        # bind_all 确保滑轮事件不会因 focus 在别处而掉给其它控件
        canvas.bind_all("<MouseWheel>", _on_wheel_y)          # Win/mac 垂直
        canvas.bind_all("<Shift-MouseWheel>", _on_wheel_x)    # Win/mac 水平
        canvas.bind_all("<Button-4>", _on_wheel_y)            # Linux up
        canvas.bind_all("<Button-5>", _on_wheel_y)            # Linux down
        canvas.bind_all("<Shift-Button-4>", _on_wheel_x)      # Linux left
        canvas.bind_all("<Shift-Button-5>", _on_wheel_x)      # Linux right

    def _unbind_wheel(_e=None):
        for seq in ("<MouseWheel>", "<Shift-MouseWheel>",
                    "<Button-4>", "<Button-5>",
                    "<Shift-Button-4>", "<Shift-Button-5>"):
            canvas.unbind_all(seq)

    canvas.bind("<Enter>", _bind_wheel)
    canvas.bind("<Leave>", _unbind_wheel)

    right = ttk.Frame(paned)
    paned.add(right, weight=1)
    ttk.Label(right, text="搜索结果（点击跳转）", font=f_label).pack(anchor="w")
    results_lb = tk.Listbox(
        right, height=8, exportselection=False, font=f_body,
        activestyle="none",
        borderwidth=1, relief="solid",
        highlightthickness=0,
        selectbackground="#4a90e2", selectforeground="#ffffff",
        bg="#ffffff",
    )
    results_lb.pack(fill="x", pady=(2, 8))
    ttk.Label(right, text="本页词条", font=f_label).pack(anchor="w")
    text = tk.Text(right, wrap="word", state="disabled", font=f_body,
                   bg="#fdfdf8", relief="solid", borderwidth=1)
    tv = ttk.Scrollbar(right, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=tv.set)
    text.pack(side="left", expand=True, fill="both")
    tv.pack(side="right", fill="y")

    status_var = tk.StringVar()
    ttk.Label(root, textvariable=status_var, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

    # Text 标签样式
    text.tag_configure("head", font=f_head, foreground="#8a1f1f", spacing3=4)
    text.tag_configure("ipa", font=f_ipa, foreground="#1a4f8a")
    text.tag_configure("label", font=f_label, foreground="#666666")
    text.tag_configure("body", font=f_body, spacing3=2)
    text.tag_configure("section", font=f_section, foreground="#888888", spacing3=6)
    text.tag_configure("sep", foreground="#bbbbbb")
    text.tag_configure("hl", background="#ffe9a8")

    photo_ref = {"img": None}  # 防止 PhotoImage 被 GC
    render_state = {"pdf_w": 0, "pdf_h": 0, "img_w": 0, "img_h": 0,
                    "hl_rect": None}

    # ── 渲染 ──

    def render_pdf(pdf_page: int) -> None:
        page = doc[pdf_page - 1]
        pix = page.get_pixmap(dpi=int(BASE_DPI * state["zoom"]), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        photo_ref["img"] = ImageTk.PhotoImage(img)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo_ref["img"])
        canvas.configure(scrollregion=(0, 0, img.width, img.height))
        render_state["img_w"] = img.width
        render_state["img_h"] = img.height
        render_state["hl_rect"] = None

    def render_entries(pdf_page: int) -> dict[int, tuple[str, str]]:
        text.configure(state="normal")
        text.delete("1.0", "end")
        # 清理上一页入 tag。tag_names 后面会一个个 delete
        for t in text.tag_names():
            if t.startswith("entry:"):
                text.tag_delete(t)
        page_entries = by_pdf_page.get(pdf_page, [])
        ranges: dict[int, tuple[str, str]] = {}
        if not page_entries:
            text.insert("end", "本页无词条（非词典正文页，或该页无 OCR 结果）。", "body")
        last_section = None
        for e in page_entries:
            if e["section"] and e["section"] != last_section:
                text.insert("end", f"【类目】{e['section']}\n", "section")
                last_section = e["section"]
            start = text.index("end-1c")
            tag_name = f"entry:{e['id']}"
            text.insert("end", f"{e['词条']}", ("head", tag_name))
            if e["音标"]:
                text.insert("end", "  " + "　".join(e["音标"]) + "\n", ("ipa", tag_name))
            else:
                text.insert("end", "\n", ("body", tag_name))
            if e["释义"]:
                text.insert("end", "释义\n", ("label", tag_name))
                for i, d in enumerate(e["释义"], 1):
                    text.insert("end", f"  {i}. {d}\n", ("body", tag_name))
            if e["例句"]:
                text.insert("end", "例句\n", ("label", tag_name))
                for s in e["例句"]:
                    text.insert("end", f"  · {s}\n", ("body", tag_name))
            text.insert("end", "─" * 30 + "\n", "sep")
            end = text.index("end-1c")
            ranges[e["id"]] = (start, end)
            # 点击本词条 → 高亮本词条 + 在左侧画 bbox
            text.tag_bind(
                tag_name, "<Button-1>",
                lambda _evt, eid=e["id"]: (
                    highlight_entry(ranges, eid),
                    draw_bbox_on_canvas(state["page"], eid),
                ),
            )
            # 鼠标悬停时变手型光标，提示可点
            text.tag_bind(tag_name, "<Enter>", lambda _e: text.configure(cursor="hand2"))
            text.tag_bind(tag_name, "<Leave>", lambda _e: text.configure(cursor=""))
        text.configure(state="disabled")
        return ranges

    def highlight_entry(ranges: dict[int, tuple[str, str]], entry_id) -> None:
        text.tag_remove("hl", "1.0", "end")
        if entry_id in ranges:
            s, t = ranges[entry_id]
            text.tag_add("hl", s, t)
            text.see(s)

    def update_status(n_results=None) -> None:
        pdf_page = state["page"]
        n = len(by_pdf_page.get(pdf_page, []))
        book = pdf_page - 66 if FIRST_MAIN_PAGE <= pdf_page <= 366 else None
        parts = [f"PDF 第 {pdf_page} 页"]
        if book:
            parts.append(f"正文第 {book} 页")
        parts.append(f"本页 {n} 条词条")
        if n_results is not None:
            parts.append(f"搜索命中 {n_results} 条")
        status_var.set("　|　".join(parts))

    # ── 行为 ──

    def draw_bbox_on_canvas(pdf_page: int, entry_id) -> None:
        """在左侧 canvas 上画当前选中词条的高亮框，并滚动至可见。无 bbox 时作空。"""
        # 清除旧高亮
        if render_state["hl_rect"] is not None:
            canvas.delete(render_state["hl_rect"])
            render_state["hl_rect"] = None
        if entry_id is None:
            return
        page_bbox = bbox_pages.get(pdf_page)
        if not page_bbox:
            return
        # 找到本页 entry_id 对应的页内 idx
        bbox_idx = entry_to_bbox_idx.get((pdf_page, entry_id))
        if bbox_idx is None:
            return
        entries_on_page = page_bbox.get("entries", [])
        # 优先按 idx 字段匹配（鲁棒），否则退回数组下标
        target = None
        for eb in entries_on_page:
            if eb.get("idx") == bbox_idx:
                target = eb
                break
        if target is None and 0 <= bbox_idx < len(entries_on_page):
            target = entries_on_page[bbox_idx]
        if target is None or not target.get("bbox"):
            return
        x1, y1, x2, y2 = target["bbox"]
        iw, ih = render_state["img_w"], render_state["img_h"]
        # 坐标已归一化到 0..1；乘回当前图像尺寸
        cx1, cy1 = int(x1 * iw), int(y1 * ih)
        cx2, cy2 = int(x2 * iw), int(y2 * ih)
        render_state["hl_rect"] = canvas.create_rectangle(
            cx1, cy1, cx2, cy2,
            outline="#e02020", width=3,
        )
        # 将高亮滚入视野，类似居中
        sw = canvas.winfo_width() or 1
        sh = canvas.winfo_height() or 1
        # 将 (cx1, cy1) 放到视口 约 20% 处
        target_x = max(0, cx1 - int(sw * 0.15))
        target_y = max(0, cy1 - int(sh * 0.20))
        if iw > 0:
            canvas.xview_moveto(target_x / iw)
        if ih > 0:
            canvas.yview_moveto(target_y / ih)

    def goto_page(pdf_page: int, hl_id=None) -> None:
        pdf_page = max(1, min(doc.page_count, pdf_page))
        state["page"] = pdf_page
        state["hl_id"] = hl_id
        page_var.set(str(pdf_page))
        render_pdf(pdf_page)
        ranges = render_entries(pdf_page)
        highlight_entry(ranges, hl_id)
        draw_bbox_on_canvas(pdf_page, hl_id)
        update_status()

    def parse_page_input() -> int:
        try:
            return int(page_var.get())
        except ValueError:
            return state["page"]

    def zoom(delta: float) -> None:
        state["zoom"] = max(0.6, min(3.0, round(state["zoom"] + delta, 1)))
        render_pdf(state["page"])
        # 重新画上当前高亮框（如果有）
        if state.get("hl_id") is not None:
            draw_bbox_on_canvas(state["page"], state["hl_id"])

    def do_search() -> None:
        results = search(entries, query_var.get(), mode_var.get())
        state["results"] = results
        results_lb.delete(0, "end")
        for i, e in enumerate(results):
            ipa = " ".join(e["音标"])
            results_lb.insert("end", f"  {e['词条']}　{ipa}　· 正文p.{e['正文页码']}")
            # 斛马条纹：奇偶行交替背景，形成可点击行的视觉分隔
            if i % 2 == 1:
                results_lb.itemconfigure(i, background="#f2f2f2")
        update_status(n_results=len(results))

    def on_result_select(_evt) -> None:
        sel = results_lb.curselection()
        if not sel:
            return
        e = state["results"][sel[0]]
        goto_page(e["pdf全文页码"], hl_id=e["id"])

    search_btn.configure(command=do_search)
    query.bind("<Return>", lambda _e: do_search())
    page_ent.bind("<Return>", lambda _e: goto_page(parse_page_input()))
    results_lb.bind("<<ListboxSelect>>", on_result_select)
    def _paginate(delta: int):
        """绑到方向键上；如果焦点在输入框里则不干预。"""
        def _handler(e):
            if isinstance(e.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
                return None
            goto_page(state["page"] + delta)
            return "break"
        return _handler

    root.bind("<Left>",  _paginate(-1))
    root.bind("<Up>",    _paginate(-1))
    root.bind("<Right>", _paginate(+1))
    root.bind("<Down>",  _paginate(+1))

    goto_page(FIRST_MAIN_PAGE)
    query.focus_set()
    # 开窗后强制将分隔条推到靠右的位置，让左侧 PDF 占大头
    def _set_initial_sash():
        root.update_idletasks()
        try:
            total = paned.winfo_width()
            if total > 200:
                paned.sashpos(0, int(total * 0.72))
        except tk.TclError:
            pass
    root.after(50, _set_initial_sash)
    root.mainloop()
    return 0


# ────────────────────── 自检（无界面） ──────────────────────


def selftest() -> int:
    assert norm_pron("a¹⁻⁶ pa⁶") == "apa", norm_pron("a¹⁻⁶ pa⁶")
    assert norm_pron("tshĩ⁵⁵") == "tshi"
    assert norm_pron("kɔ̃²") == "kɔ"
    assert norm_pron("ã") == norm_pron("a") == "a"
    assert norm_pron("ũ⁵³") == "u"

    entries = load_entries()
    assert entries, "no entries loaded"
    n = len(entries)
    pages = {e["pdf全文页码"] for e in entries}
    assert min(pages) >= 67 and max(pages) <= 366, (min(pages), max(pages))

    # 每条词条至少应有词头与一个音标
    assert all(e["词条"] and e["音标"] for e in entries), "存在空词条"

    # 词条搜索：随机拿一条验证
    sample = entries[0]
    hits = search(entries, sample["词条"], "词条")
    assert sample in hits

    # 发音搜索模糊：ĩ→i、去声调
    nasal = [e for e in entries
             if any(COMBINING_TILDE in unicodedata.normalize("NFD", p) for p in e["音标"])]
    if nasal:
        e0 = nasal[0]
        plain = norm_pron(e0["音标"][0])
        assert e0 in search(entries, plain, "发音"), "鼻化模糊匹配失败"

    # 释义/例句字段能拆分
    has_ex = [e for e in entries if e["例句"]]
    has_def = [e for e in entries if e["释义"]]
    assert has_def, "没有任何释义？"

    print(f"selftest OK: {n} 条词条 / 释义有 {len(has_def)} 条 / 例句有 {len(has_ex)} 条")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    for p in (PDF_PATH, TXT_PATH):
        if not p.exists():
            print(f"[fatal] 找不到文件: {p}")
            return 1
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
