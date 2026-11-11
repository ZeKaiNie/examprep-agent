#!/usr/bin/env python
"""下载公开课样例（不入库，许可证 CC BY-NC-SA）。移植自 Exam Cram Coach Skill 的 samples/fetch.py。

  uv run python samples/fetch.py              # 两门都下载
  uv run python samples/fetch.py mit-6006     # MIT OCW 6.006（2020 春）：6 份讲义 PDF + Quiz 1 + 答案
  uv run python samples/fetch.py yale-psyc110 # Open Yale PSYC 110：4 讲文字稿（Markdown）

下载到 samples/<课程>/。
"""

from __future__ import annotations

import html
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OCW = "https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2020"
OCW_RESOURCES = ["lec1", "lec2", "lec3", "lec4", "lec5", "lec6", "q1", "q1_sol"]
YALE = "https://oyc.yale.edu/psychology/psyc-110/lecture-{}"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "examprep-agent-samples/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_mit() -> None:
    out = HERE / "mit-6006"
    out.mkdir(exist_ok=True)
    for res in OCW_RESOURCES:
        page = get(f"{OCW}/resources/mit6_006s20_{res}/").decode("utf-8", "ignore")
        m = re.search(rf'href="(/courses/6-006[^"]*MIT6_006S20_{res}\.pdf)"', page, re.I)
        if not m:
            print("  ! 找不到 PDF 链接：", res)
            continue
        dest = out / f"MIT6_006S20_{res}.pdf"
        dest.write_bytes(get("https://ocw.mit.edu" + m.group(1)))
        print("  ", dest.relative_to(HERE))
    (out / "SOURCE.md").write_text(
        "MIT OpenCourseWare, 6.006 Introduction to Algorithms (Spring 2020), Erik Demaine, Jason Ku, Justin Solomon. "
        f"License: CC BY-NC-SA 4.0. {OCW}\n",
        encoding="utf-8",
    )


def fetch_yale() -> None:
    out = HERE / "yale-psyc110"
    out.mkdir(exist_ok=True)
    for n in range(1, 5):
        page = get(YALE.format(n)).decode("utf-8", "ignore")
        m = re.search(r'<div id="inline_content">(.*?)<a href="#transcript-top">', page, re.S)
        if not m:
            print("  ! 找不到文字稿：lecture", n)
            continue
        body = m.group(1)
        body = re.sub(r"<h1[^>]*>.*?</h1>", "", body, flags=re.S)
        body = re.sub(r"<h2>", "# ", body)
        body = re.sub(r"<h3>", "\n\n## ", body)
        body = re.sub(r"</(p|h2|h3)>", "\n\n", body)
        text = html.unescape(re.sub(r"<[^>]+>", "", body))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip() + "\n"
        dest = out / f"psyc110_lecture{n:02d}.md"
        dest.write_text(text, encoding="utf-8")
        print("  ", dest.relative_to(HERE))
    (out / "SOURCE.md").write_text(
        "Open Yale Courses, PSYC 110 Introduction to Psychology, Paul Bloom. "
        "License: CC BY-NC-SA 3.0. https://oyc.yale.edu/introduction-psychology/psyc-110\n",
        encoding="utf-8",
    )


def main(argv: list[str]) -> None:
    jobs = {"mit-6006": fetch_mit, "yale-psyc110": fetch_yale}
    for which in argv or list(jobs):
        print("下载", which)
        jobs[which]()
    print("完成。试试：uv run examprep ingest samples/mit-6006")


if __name__ == "__main__":
    main(sys.argv[1:])
