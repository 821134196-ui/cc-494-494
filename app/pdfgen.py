"""零依赖 PDF 生成器（仅标准库）。

中文渲染使用 PDF 标准 CJK CID 字体 STSong-Light + UniGB-UCS2-H 编码，
正文按 UTF-16BE 十六进制字符串输出，无需任何外部字体文件或第三方库；
主流阅读器（Acrobat / Chrome / Edge / WPS 等）会自动替换简体中文字体。

仅实现本项目报告所需的最小能力：多页、文本、直线、矩形、颜色、自动分页。
"""
from __future__ import annotations

import unicodedata
from datetime import datetime

# 主题色（与页面一致：医疗白 / 石墨灰 / 克制青 / 异常暗红）
GRAPHITE = (0.169, 0.184, 0.200)   # #2b2f33
TEAL = (0.055, 0.541, 0.549)       # #0e8a8c
TEAL_LIGHT = (0.92, 0.955, 0.955)  # 青色极浅底
RED = (0.702, 0.149, 0.118)        # #b3261e
GRAY_LINE = (0.85, 0.863, 0.878)   # #d9dce0
GRAY_SOFT = (0.965, 0.972, 0.973)  # 浅底
WHITE = (1, 1, 1)
MUTED = (0.43, 0.46, 0.49)

PAGE_W, PAGE_H = 595, 842          # A4
MARGIN = 44
BOTTOM = 56


def _ucs2_hex(s: str) -> str:
    """编码为 UCS-2（UniGB-UCS2-H 要求双字节）。BMP 外字符以？代替。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code > 0xFFFF:
            code = ord("？")
        out.append(f"{code:04X}")
    return "".join(out)


def char_width(ch: str, size: float) -> float:
    """粗略字宽：全角 1em，半角 0.5em。供换行与表格布局使用。"""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return size
    return size * 0.52


def text_width(s: str, size: float) -> float:
    return sum(char_width(c, size) for c in s)


def wrap_text(s: str, size: float, max_w: float) -> list[str]:
    lines: list[str] = []
    cur, cur_w = "", 0.0
    for ch in s:
        w = char_width(ch, size)
        if cur_w + w > max_w and cur:
            lines.append(cur)
            cur, cur_w = ch, w
        else:
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    return lines or [""]


class _Page:
    def __init__(self):
        self.ops: list[str] = []

    def rgb(self, c, stroke: bool):
        op = "RG" if stroke else "rg"
        self.ops.append(f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} {op}")

    def rect(self, x, y, w, h, fill=None, stroke=None, lw=0.8):
        self.ops.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re")
        if fill and stroke:
            self.rgb(fill, False)
            self.rgb(stroke, True)
            self.ops.append(f"{lw:.2f} w B")
        elif fill:
            self.rgb(fill, False)
            self.ops.append("f")
        elif stroke:
            self.rgb(stroke, True)
            self.ops.append(f"{lw:.2f} w S")

    def line(self, x1, y1, x2, y2, color=GRAY_LINE, lw=0.8):
        self.rgb(color, True)
        self.ops.append(f"{lw:.2f} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def text(self, s, x, y, size=10, color=GRAPHITE, bold=False):
        self.rgb(color, False)
        hexs = _ucs2_hex(s)
        self.ops.append(f"BT /F1 {size:.1f} Tf {x:.1f} {y:.1f} Td <{hexs}> Tj ET")
        if bold:  # 无粗体字库：错位重绘一次模拟加粗
            self.ops.append(f"BT /F1 {size:.1f} Tf {x + 0.3:.1f} {y:.1f} Td <{hexs}> Tj ET")

    def content_bytes(self) -> bytes:
        return "\n".join(self.ops).encode("latin-1")


class PDF:
    def __init__(self, title: str, footer: str):
        self.title = title
        self.footer = footer
        self.pages: list[_Page] = [_Page()]

    @property
    def page(self) -> _Page:
        return self.pages[-1]

    @property
    def y(self) -> float:
        return self._y

    def new_page(self):
        self.pages.append(_Page())
        self._y = PAGE_H - MARGIN

    def ensure(self, need: float):
        if self._y - need < BOTTOM:
            self.new_page()

    # 高层布局原语 ----------------------------------------------------------

    def begin(self):
        self._y = PAGE_H - MARGIN

    def gap(self, h: float):
        self._y -= h

    def paragraph(self, s, x=MARGIN, size=10, color=GRAPHITE, max_w=None,
                  leading=15, bold=False):
        max_w = max_w or (PAGE_W - 2 * MARGIN)
        for ln in wrap_text(s, size, max_w):
            self.ensure(leading)
            self._y -= leading
            self.page.text(ln, x, self._y, size=size, color=color, bold=bold)

    def labeled(self, label, value, x=MARGIN, label_w=72, size=10,
                color=GRAPHITE, max_w=None):
        self.ensure(size * 1.6)
        self._y -= size * 1.6
        self.page.text(label, x, self._y, size=size, color=MUTED)
        self.page.text(value, x + label_w, self._y, size=size, color=color)

    def table(self, x, widths, header, rows, *, size=9.5, row_h=21,
              header_fill=TEAL, flag_col=None):
        """简单表格。rows 单元格为 str 或 (str, color)。flag_col 为状态列下标，
        异常值文字自动用暗红色。"""
        total_w = sum(widths)
        # 表头
        self.ensure(row_h + 2)
        top = self._y
        self.page.rect(x, top - row_h, total_w, row_h, fill=header_fill)
        cx = x
        for w, htext in zip(widths, header):
            self.page.text(htext, cx + 6, top - row_h + 6.5, size=size,
                           color=WHITE, bold=True)
            cx += w
        self._y -= row_h
        # 行
        for i, row in enumerate(rows):
            n_lines = 1
            cells = []
            for cell in row:
                if isinstance(cell, tuple):
                    txt, col = cell
                else:
                    txt, col = cell, GRAPHITE
                lines = wrap_text(str(txt), size, widths[len(cells)] - 12)
                n_lines = max(n_lines, len(lines))
                cells.append((lines, col))
            rh = max(row_h, 12 * n_lines + 8)
            self.ensure(rh)
            if i % 2 == 1:
                self.page.rect(x, self._y - rh, total_w, rh, fill=GRAY_SOFT)
            cx = x
            for j, ((lines, col), w) in enumerate(zip(cells, widths)):
                ty = self._y - 14
                for k, ln in enumerate(lines):
                    self.page.text(ln, cx + 6, ty - k * 12, size=size, color=col)
                cx += w
            self._y -= rh
        self.page.line(x, self._y, x + total_w, self._y, color=GRAY_LINE, lw=0.8)

    # 输出 ------------------------------------------------------------------

    def save(self, path: str) -> None:
        objects: list[bytes] = []

        def add(b: bytes) -> int:
            objects.append(b)
            return len(objects)

        catalog = add(b"")   # 占位 1
        pages = add(b"")     # 占位 2
        font = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            b"/Encoding /UniGB-UCS2-H "
            b"/DescendantFonts [ << /Type /Font /Subtype /CIDFontType0 "
            b"/BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
            b"/FontDescriptor << /Type /FontDescriptor /FontName /STSong-Light "
            b"/Flags 6 /FontBBox [-250 -250 1000 1000] /ItalicAngle 0 "
            b"/Ascent 880 /Descent -120 /CapHeight 880 /StemV 80 >> >> ] >>"
        )
        font_id = add(font)

        page_ids = []
        content_ids = []
        for _ in self.pages:
            page_ids.append(add(b""))
            content_ids.append(add(b""))

        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages} 0 R >>".encode()
        objects[pages - 1] = (
            f"<< /Type /Pages /Kids [ {kids} ] /Count {len(page_ids)} "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()

        for idx, pg in enumerate(self.pages):
            # 页眉/页脚装饰
            pg.rect(0, PAGE_H - 30, PAGE_W, 30, fill=TEAL)
            pg.text(self.title, MARGIN, PAGE_H - 20, size=10.5, color=WHITE,
                    bold=True)
            pg.line(MARGIN, 44, PAGE_W - MARGIN, 44, color=GRAY_LINE)
            pg.text(self.footer, MARGIN, 33, size=8, color=MUTED)
            pg.text(f"第 {idx + 1} / {len(self.pages)} 页",
                    PAGE_W - MARGIN - 60, 33, size=8, color=MUTED)
            stream = pg.content_bytes()
            objects[page_ids[idx] - 1] = (
                f"<< /Type /Page /Parent {pages} 0 R "
                f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Contents {content_ids[idx]} 0 R >>"
            ).encode()
            objects[content_ids[idx] - 1] = (
                f"<< /Length {len(stream)} >>\nstream\n".encode() + stream +
                b"\nendstream"
            )

        # 组装 PDF，记录 xref 偏移
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref_pos = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (
            f"trailer << /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode()
        with open(path, "wb") as f:
            f.write(out)


# ---------------------------------------------------------------- 报告排版

def render_report_pdf(path: str, *, report_no: str, version: int, title: str,
                      examined_at: str, issued_at: str, patient_name: str,
                      doctor_name: str | None, correction_note: str | None,
                      metrics: list[dict], checksum_short: str,
                      demo_notice: str = "本报告为演示系统合成数据，受检人为虚构人员，不对应任何真实病历。") -> None:
    """生成一份体检报告 PDF。metrics: label/value/unit/ref_low/ref_high/status。"""
    is_corr = bool(correction_note)
    head = f"{title}（更正件 V{version}）" if is_corr else f"{title}（V{version}）"
    pdf = PDF(head, f"演示数据·无真实病历｜报告编号 {report_no}｜校验 {checksum_short}")
    pdf.begin()

    # 标题区
    pdf.gap(18)
    pdf.paragraph(head, size=17, color=GRAPHITE, bold=True, leading=24)
    if is_corr:
        pdf.paragraph("本件为更正报告，出具后原版本即被取代但仍保留可查；"
                      "已有预约与解读记录固定引用其当时版本，不会随更正自动改变。",
                      size=9, color=MUTED, leading=14)
    pdf.gap(6)
    pdf.page.line(MARGIN, pdf.y, PAGE_W - MARGIN, pdf.y, color=TEAL, lw=1.4)
    pdf.gap(10)

    pdf.labeled("报告编号", report_no)
    pdf.labeled("版本", f"V{version}" + ("（更正件）" if is_corr else "（初版）"))
    pdf.labeled("受检人", f"{patient_name}（虚构演示人员）")
    if doctor_name:
        pdf.labeled("解读医生", doctor_name)
    pdf.labeled("体检日期", examined_at)
    pdf.labeled("出具日期", issued_at)
    pdf.gap(6)

    if is_corr:
        note = f"更正说明：{correction_note}"
        lines = wrap_text(note, 9.5, PAGE_W - 2 * MARGIN - 20)
        h = 14 * len(lines) + 16
        pdf.ensure(h + 8)
        box_top = pdf.y
        pdf.page.rect(MARGIN, box_top - h, PAGE_W - 2 * MARGIN, h,
                      fill=TEAL_LIGHT, stroke=TEAL, lw=0.8)
        ty = box_top - 14
        for k, ln in enumerate(lines):
            pdf.page.text(ln, MARGIN + 10, ty - k * 14, size=9.5,
                          color=GRAPHITE)
        pdf.gap(h + 12)

    # 指标表
    pdf.paragraph("检验指标", size=12, color=TEAL, bold=True, leading=18)
    rows = []
    for m in metrics:
        ref = _ref_text(m)
        if m.get("status") == "high":
            flag = ("↑ 偏高", RED)
            val = (f"{m['value']:g}", RED)
        elif m.get("status") == "low":
            flag = ("↓ 偏低", RED)
            val = (f"{m['value']:g}", RED)
        else:
            flag = ("正常", (0.18, 0.5, 0.33))
            val = (f"{m['value']:g}", GRAPHITE)
        rows.append([m["label"], val, ref, m["unit"], flag])
    pdf.table(
        MARGIN, [128, 78, 172, 72, 57],
        ["指标", "结果", "参考范围", "单位", "提示"], rows)
    pdf.gap(14)

    pdf.paragraph("结果解读以医生在线下/线上解读中的签字结论为准；"
                  "本页数值异常仅表示超出本次报告所固化的参考范围。",
                  size=8.5, color=MUTED, leading=13)
    pdf.gap(18)
    pdf.paragraph(demo_notice, size=9, color=MUTED, leading=14)
    pdf.save(path)


def _ref_text(m: dict) -> str:
    lo, hi = m.get("ref_low"), m.get("ref_high")
    if lo is not None and hi is not None:
        return f"{lo:g} ~ {hi:g}"
    if hi is not None:
        return f"≤ {hi:g}"
    if lo is not None:
        return f"≥ {lo:g}"
    return "—"
