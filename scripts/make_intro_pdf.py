"""把开源仓核心文档（README + INTRODUCTION）合并转成「项目介绍」HTML + PDF。

用途：给「看一眼」的人一份点开就能看的介绍，不用下载解压 ZIP。
文档转 PDF 不破坏格式（文字/表格/代码块都保留），代码本身仍靠 ZIP / 仓库地址给。

用法：
    python scripts/make_intro_pdf.py [输出目录]

输出：
    <输出目录>/项目介绍_stm32-forge.html  （浏览器打开）
    <输出目录>/项目介绍_stm32-forge.pdf   （任何设备看，需本机有 Edge/Chrome）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import markdown

CORE = Path(__file__).resolve().parent.parent
OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else CORE.parent
NAME = "项目介绍_stm32-forge"
OUT_HTML = OUT_DIR / f"{NAME}.html"
OUT_PDF = OUT_DIR / f"{NAME}.pdf"

_CSS = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
       max-width: 860px; margin: 0 auto; padding: 40px 48px; color: #24292f;
       line-height: 1.7; font-size: 15px; }
h1 { font-size: 26px; border-bottom: 2px solid #185FA5; padding-bottom: 8px; }
h2 { font-size: 20px; margin-top: 32px; border-bottom: 1px solid #e1e4e8; padding-bottom: 6px; }
h3 { font-size: 16px; }
blockquote { border-left: 4px solid #185FA5; background: #f6f8fa; padding: 12px 16px;
             margin: 16px 0; color: #444; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { border: 1px solid #d0d7de; padding: 8px 12px; text-align: left; }
th { background: #f0f6fc; }
code { background: #f6f8fa; padding: 2px 5px; border-radius: 4px;
       font-family: Consolas, "Courier New", monospace; font-size: 13px; }
pre { background: #f6f8fa; padding: 14px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; padding: 0; }
hr { border: none; border-top: 1px solid #e1e4e8; margin: 32px 0; }
img { max-width: 100%; }
"""


def _find_browser() -> str | None:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(p).exists():
            return p
    return None


def main() -> int:
    readme = (CORE / "README.md").read_text(encoding="utf-8")
    intro = (CORE / "docs/INTRODUCTION.md").read_text(encoding="utf-8")
    combined = f"# Agent-S Embedded Core · 项目介绍\n\n{readme}\n\n---\n\n# 仓库介绍 · 核心逻辑与成品展示\n\n{intro}"

    body = markdown.markdown(combined, extensions=["tables", "fenced_code", "sane_lists"])
    html = f"<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n<title>{NAME}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"✅ HTML: {OUT_HTML}")

    browser = _find_browser()
    if browser is None:
        print("⚠️ 未找到 Edge/Chrome，跳过 PDF（HTML 已可用浏览器打印）")
        return 0
    subprocess.run(
        [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()],
        check=False, capture_output=True,
    )
    if OUT_PDF.exists():
        print(f"✅ PDF:  {OUT_PDF}（{OUT_PDF.stat().st_size // 1024} KB）")
    else:
        print("⚠️ PDF 生成失败，HTML 可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
