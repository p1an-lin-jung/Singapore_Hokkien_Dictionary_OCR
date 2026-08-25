#!/usr/bin/env python3
"""词典正文查阅器（tkinter GUI）。

左栏显示《新加坡闽南话词典》PDF 正文页面，右栏以友好格式显示该页
对应的 YAML 词条（词典正文.yaml）。工具栏支持按 词条/发音/释义/例句
搜索；发音搜索为模糊匹配：忽略声调（上标数字），鼻化韵与非鼻化韵等价
（ĩ ã ẽ ɔ̃ ũ 等视同 i a e ɔ u）。点击搜索结果跳转到对应页面并高亮词条。

用法:
    python3 dict_viewer.py            # 启动 GUI（需图形界面或 X11 转发）
    python3 dict_viewer.py --selftest # 无界面自检：数据加载与搜索逻辑
"""

from __future__ import annotations

import io
import sys
import unicodedata
from pathlib import Path

import fitz  # PyMuPDF
import yaml

BASE = Path(__file__).parent
PDF_PATH = BASE / "src" / "新加坡闽南话词典(2002).pdf"
YAML_PATH = BASE / "词典正文.yaml"

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


# ────────────────────── 数据加载与搜索 ──────────────────────


def load_entries(path: Path = YAML_PATH) -> list[dict]:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, encoding="utf-8") as f:
        entries = yaml.load(f, Loader=loader)
    for e in entries:  # 兜底：字段缺失时给空列表，避免渲染报错
        e.setdefault("音标", [])
        e.setdefault("释义", [])
        e.setdefault("例句", [])
    return entries


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

    ttk.Label(bar, text="缩放").pack(side="left")
    ttk.Button(bar, text="－", width=3, command=lambda: zoom(-0.2)).pack(side="left", padx=(4, 0))
    ttk.Button(bar, text="＋", width=3, command=lambda: zoom(0.2)).pack(side="left")

    # ── 主体：左 PDF，右 结果+词条 ──
    paned = ttk.Panedwindow(root, orient="horizontal")
    paned.pack(expand=True, fill="both", padx=8, pady=(0, 4))

    left = ttk.Frame(paned)
    paned.add(left, weight=3)
    canvas = tk.Canvas(left, bg="#555555", highlightthickness=0)
    cv = ttk.Scrollbar(left, orient="vertical", command=canvas.yview)
    ch = ttk.Scrollbar(left, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=cv.set, xscrollcommand=ch.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    cv.grid(row=0, column=1, sticky="ns")
    ch.grid(row=1, column=0, sticky="ew")
    left.rowconfigure(0, weight=1)
    left.columnconfigure(0, weight=1)

    right = ttk.Frame(paned)
    paned.add(right, weight=2)
    ttk.Label(right, text="搜索结果（点击跳转）", font=f_label).pack(anchor="w")
    results_lb = tk.Listbox(right, height=7, exportselection=False, font=f_body,
                            activestyle="dotbox")
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
    text.tag_configure("sep", foreground="#bbbbbb")
    text.tag_configure("hl", background="#ffe9a8")

    photo_ref = {"img": None}  # 防止 PhotoImage 被 GC

    # ── 渲染 ──

    def render_pdf(pdf_page: int) -> None:
        page = doc[pdf_page - 1]
        pix = page.get_pixmap(dpi=int(BASE_DPI * state["zoom"]), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        photo_ref["img"] = ImageTk.PhotoImage(img)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo_ref["img"])
        canvas.configure(scrollregion=(0, 0, img.width, img.height))

    def render_entries(pdf_page: int) -> dict[int, tuple[str, str]]:
        text.configure(state="normal")
        text.delete("1.0", "end")
        page_entries = by_pdf_page.get(pdf_page, [])
        ranges: dict[int, tuple[str, str]] = {}
        if not page_entries:
            text.insert("end", "本页无词条（非词典正文页，或该页无 OCR 结果）。", "body")
        for e in page_entries:
            start = text.index("end-1c")
            text.insert("end", f"{e['词条']}", "head")
            if e["音标"]:
                text.insert("end", "  " + "　".join(e["音标"]) + "\n", "ipa")
            else:
                text.insert("end", "\n", "body")
            if e["释义"]:
                text.insert("end", "释义\n", "label")
                for i, d in enumerate(e["释义"], 1):
                    text.insert("end", f"  {i}. {d}\n", "body")
            if e["例句"]:
                text.insert("end", "例句\n", "label")
                for s in e["例句"]:
                    text.insert("end", f"  · {s}\n", "body")
            text.insert("end", "─" * 30 + "\n", "sep")
            ranges[e["id"]] = (start, text.index("end-1c"))
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

    def goto_page(pdf_page: int, hl_id=None) -> None:
        pdf_page = max(1, min(doc.page_count, pdf_page))
        state["page"] = pdf_page
        page_var.set(str(pdf_page))
        render_pdf(pdf_page)
        ranges = render_entries(pdf_page)
        highlight_entry(ranges, hl_id)
        update_status()

    def parse_page_input() -> int:
        try:
            return int(page_var.get())
        except ValueError:
            return state["page"]

    def zoom(delta: float) -> None:
        state["zoom"] = max(0.6, min(3.0, round(state["zoom"] + delta, 1)))
        render_pdf(state["page"])

    def do_search() -> None:
        results = search(entries, query_var.get(), mode_var.get())
        state["results"] = results
        results_lb.delete(0, "end")
        for e in results:
            ipa = " ".join(e["音标"])
            results_lb.insert("end", f"{e['词条']}　{ipa}　· 正文p.{e['正文页码']}")
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
    root.bind("<Left>", lambda e: goto_page(state["page"] - 1) if not isinstance(e.widget, (tk.Entry, ttk.Entry)) else None)
    root.bind("<Right>", lambda e: goto_page(state["page"] + 1) if not isinstance(e.widget, (tk.Entry, ttk.Entry)) else None)

    goto_page(FIRST_MAIN_PAGE)
    query.focus_set()
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
    by_page = {e["pdf全文页码"] for e in entries}
    assert min(by_page) == 67 and max(by_page) == 366

    hits = search(entries, "阿爸", "词条")
    assert any(e["正文页码"] == 62 for e in hits), "词条搜索未命中 阿爸"
    # 发音模糊：查 apa 应命中 a¹ pa⁶ / a¹⁻⁶ pa² 等
    assert any("阿爸" == e["词条"] for e in search(entries, "apa", "发音"))
    # 鼻化模糊：不带鼻化符号的查询应命中带 ĩ/ã 的音标
    nasal = [e for e in entries if any(COMBINING_TILDE in unicodedata.normalize("NFD", p) for p in e["音标"])]
    if nasal:
        e0 = nasal[0]
        plain = norm_pron(e0["音标"][0])
        assert e0 in search(entries, plain, "发音"), "鼻化模糊匹配失败"
    print(f"selftest OK: {n} entries, search/normalization passed")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    for p in (PDF_PATH, YAML_PATH):
        if not p.exists():
            print(f"[fatal] 找不到文件: {p}")
            return 1
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
