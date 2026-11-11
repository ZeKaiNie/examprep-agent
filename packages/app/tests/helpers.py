"""最小 DOCX / PPTX / PDF 构造器，测试不需要二进制夹具。移植自 Skill tests/helpers.py。"""

from __future__ import annotations

import zipfile
from pathlib import Path

_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>{}</Types>'
)
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'

DocItem = str | tuple[str, str] | tuple[str, list[list[str]]]


def make_docx(path: Path, paragraphs: list[DocItem]) -> None:
    """paragraphs：str | ("h1", 文本) | "PAGEBREAK" | ("table", [[单元格]])。"""
    body: list[str] = []
    for item in paragraphs:
        if item == "PAGEBREAK":
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        elif isinstance(item, tuple) and item[0] == "table":
            rows_data = item[1]
            assert isinstance(rows_data, list)
            rows = "".join(
                "<w:tr>{}</w:tr>".format(
                    "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
                )
                for row in rows_data
            )
            body.append(f"<w:tbl>{rows}</w:tbl>")
        elif isinstance(item, tuple):
            level, text = item[0][1:], item[1]
            body.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
            )
        else:
            body.append(f"<w:p><w:r><w:t>{item}</w:t></w:r></w:p>")
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}><w:body>{"".join(body)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            _CT.format(
                '<Override PartName="/word/document.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            ),
        )
        zf.writestr("word/document.xml", doc)


def make_pptx(path: Path, slides: list[list[str]], notes: dict[int, str] | None = None) -> None:
    notes = notes or {}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CT.format(""))
        for i, lines in enumerate(slides, 1):
            paras = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                f'<?xml version="1.0"?><p:sld {_P} {_A}><p:cSld><p:spTree><p:sp><p:txBody>{paras}'
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
            if i in notes:
                zf.writestr(
                    f"ppt/notesSlides/notesSlide{i}.xml",
                    f'<?xml version="1.0"?><p:notes {_P} {_A}><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>'
                    f"<a:t>{notes[i]}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>",
                )


def make_pdf(
    path: Path, pages: list[str], *, rects: dict[int, list[tuple[float, float, float, float]]] | None = None
) -> None:
    """最小未压缩 PDF，每页一行文本（仅 ASCII）。``rects``：{页序号(0 起): [(x, y, w, h)]} 画填充矩形（模拟图）。"""
    rects = rects or {}
    objs: list[str] = []
    n_pages = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>")
    font_id = 3 + 2 * n_pages
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        escaped = text.replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET"
        for x, y, w, h in rects.get(i, []):
            stream += f"\n0.5 g {x} {y} {w} {h} re f"
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = "%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))
