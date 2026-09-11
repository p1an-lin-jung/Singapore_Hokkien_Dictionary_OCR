"""Prepare page images / build a deploy bundle for the web viewer.

Modes:
    # (1) develop mode -- symlink page images into dict_output/page_images/
    python3 prepare_web.py

    # (2) copy real files (needed for GitHub / Cloudflare Pages)
    python3 prepare_web.py --copy

    # (3) resize + convert to webp (recommended for Cloudflare Pages)
    python3 prepare_web.py --copy --resize 900 --webp

    # (4) build a clean deploy bundle into dict_output/_site/ (recommended)
    python3 prepare_web.py --deploy
    python3 prepare_web.py --deploy --resize 900 --webp   # smallest bundle

    # (5) clean up
    python3 prepare_web.py --clean

The deploy bundle contains ONLY the files needed by index.html:
    _site/index.html
    _site/_headers                 # Cloudflare Pages headers
    _site/dictionary_ocr/3_词典正文.txt
    _site/dictionary_ocr/3_词典正文.bbox.json
    _site/page_images/page_*.png|webp
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
SRC_DIR  = BASE / "dict_images" / "main"
DEST_DIR = BASE / "dict_output" / "page_images"
SITE_DIR = BASE / "dict_output" / "_site"

CF_HEADERS = """# Cloudflare Pages / static host headers.
# Long-cache the immutable data + page images; the HTML itself we keep fresh.

/*.txt
  Content-Type: text/plain; charset=utf-8
  Cache-Control: public, max-age=604800, immutable

/*.json
  Content-Type: application/json; charset=utf-8
  Cache-Control: public, max-age=604800, immutable

/page_images/*
  Cache-Control: public, max-age=2592000, immutable

/index.html
  Cache-Control: public, max-age=300

/
  Cache-Control: public, max-age=300
"""


def _emit_images(src_dir: Path, dest_dir: Path, args) -> tuple[int, int, int]:
    """Emit page images to dest_dir. Returns (n_files, src_bytes, out_bytes)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    do_resize = args.resize > 0
    do_convert = args.webp
    must_copy = args.copy or args.deploy or do_resize or do_convert

    n = 0; total_src = 0; total_out = 0
    for src in sorted(src_dir.glob("page_*.png")):
        n += 1
        total_src += src.stat().st_size

        if do_resize or do_convert:
            from PIL import Image
            out_name = src.stem + (".webp" if do_convert else ".png")
            dst = dest_dir / out_name
            if dst.exists(): dst.unlink()
            with Image.open(src) as im:
                if do_resize and im.width > args.resize:
                    ratio = args.resize / im.width
                    im = im.resize((args.resize, int(im.height * ratio)), Image.LANCZOS)
                if do_convert:
                    im.save(dst, "WEBP", quality=args.quality, method=6)
                else:
                    im.save(dst, "PNG", optimize=True)
            total_out += dst.stat().st_size
        elif must_copy:
            dst = dest_dir / src.name
            if dst.exists(): dst.unlink()
            shutil.copy2(src, dst)
            total_out += dst.stat().st_size
        else:
            dst = dest_dir / src.name
            if dst.exists() or dst.is_symlink(): dst.unlink()
            os.symlink(os.path.relpath(src, dest_dir), dst)
            total_out += src.stat().st_size
    return n, total_src, total_out


def _build_deploy_bundle(args) -> int:
    """Build dict_output/_site/ containing only files needed by index.html."""
    do_deploy = SITE_DIR
    if do_deploy.exists():
        shutil.rmtree(do_deploy)
    do_deploy.mkdir(parents=True)

    src_root = BASE / "dict_output"

    # 1. index.html
    shutil.copy2(src_root / "index.html", do_deploy / "index.html")

    # 2. data files (txt + bbox.json)
    ocr_out = do_deploy / "dictionary_ocr"
    ocr_out.mkdir()
    src_ocr = src_root / "dictionary_ocr"
    for name in ("3_词典正文.txt", "3_词典正文.bbox.json"):
        s = src_ocr / name
        if not s.exists():
            print(f"[fatal] missing {s}"); return 1
        shutil.copy2(s, ocr_out / name)

    # 3. page images (respect --resize / --webp)
    img_dst = do_deploy / "page_images"
    n, src_b, out_b = _emit_images(SRC_DIR, img_dst, args)

    # 4. _headers for Cloudflare Pages
    (do_deploy / "_headers").write_text(CF_HEADERS, encoding="utf-8")

    # 5. patch index.html if webp: rewrite the IMG_URL_TPL suffix
    if args.webp:
        idx = do_deploy / "index.html"
        html = idx.read_text(encoding="utf-8")
        html = html.replace(
            'const IMG_URL_TPL = "./page_images/page_{PAGE}.png";',
            'const IMG_URL_TPL = "./page_images/page_{PAGE}.webp";',
        )
        idx.write_text(html, encoding="utf-8")

    total = sum(f.stat().st_size for f in do_deploy.rglob("*") if f.is_file())
    print(f"deploy bundle ready at {do_deploy}")
    print(f"  index.html              1 file")
    print(f"  dictionary_ocr/         2 files")
    print(f"  page_images/            {n} files ({out_b/1024/1024:.1f} MB)")
    print(f"  _headers                1 file")
    print(f"  total bundle size:      {total/1024/1024:.1f} MB")
    print()
    print("Cloudflare Pages / any static host: point at this directory.")
    print("  cd dict_output/_site && python3 -m http.server 8000    # local preview")
    print("  npx wrangler pages deploy dict_output/_site            # cloudflare")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy", action="store_true", help="copy files instead of symlinking")
    ap.add_argument("--resize", type=int, default=0,
                    help="downscale to this width in px (implies --copy)")
    ap.add_argument("--quality", type=int, default=80,
                    help="webp/jpeg quality when --resize > 0 (default 80)")
    ap.add_argument("--webp", action="store_true",
                    help="convert to .webp (implies --copy; big size saving)")
    ap.add_argument("--deploy", action="store_true",
                    help="build a self-contained _site/ ready for Cloudflare Pages")
    ap.add_argument("--clean", action="store_true",
                    help="remove page_images/ (and _site/ if it exists) and exit")
    args = ap.parse_args()

    if args.clean:
        removed = False
        for d in (DEST_DIR, SITE_DIR):
            if d.exists():
                shutil.rmtree(d); print(f"removed {d}"); removed = True
        if not removed:
            print("nothing to clean")
        return 0

    if args.deploy:
        return _build_deploy_bundle(args)

    if not SRC_DIR.exists():
        print(f"[fatal] source dir missing: {SRC_DIR}")
        return 1

    if (args.resize or args.webp):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            print("[fatal] --resize/--webp 需要 Pillow: pip install pillow"); return 1

    n_files, total_src, total_out = _emit_images(SRC_DIR, DEST_DIR, args)
    do_resize = args.resize > 0
    do_convert = args.webp
    must_copy = args.copy or do_resize or do_convert

    mode = "symlink"
    if do_convert: mode = f"webp (q={args.quality}, resize={args.resize or 'orig'})"
    elif do_resize: mode = f"resized PNG (w={args.resize})"
    elif must_copy: mode = "copy"

    print(f"prepared {n_files} images → {DEST_DIR}  [{mode}]")
    print(f"  source total: {total_src/1024/1024:6.1f} MB")
    if must_copy:
        print(f"  output total: {total_out/1024/1024:6.1f} MB "
              f"({total_out*100/total_src:.0f}% of source)")

    if do_convert:
        print("\n提示：webp 输出，请把 index.html 中 IMG_URL_TPL 的 .png 改成 .webp")

    return 0


if __name__ == "__main__":
    sys.exit(main())
