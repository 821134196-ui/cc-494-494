"""最小中文 PDF 生成器（仅依赖标准库）。

- 内嵌 TrueType 字体：DejaVuSans（拉丁/数字/符号）与
  DroidSansFallback（中日韩字符），按字符自动切换；
- Identity-H + CIDToGIDMap/Identity，文本按字形 ID(GID) 编码，
  每个字体附带 ToUnicode 保证复制/检索；
- 支持：标题、段落（按字宽换行）、表格、色块徽记、页眉页脚。
PDF 写入本地目录（config.PDF_DIR），不做云端上传。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

from . import config

FONT_DIR = config.ROOT / "assets" / "fonts"
CJK_FONT = FONT_DIR / "DroidSansFallbackFull.ttf"
LATIN_FONT = FONT_DIR / "DejaVuSans.ttf"

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 52, 52, 60, 56
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R


def _hex_color(s: str) -> tuple[float, float, float]:
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore


class TTFont:
    def __init__(self, path: Path, base_name: str):
        self.path = path
        self.base_name = base_name
        self.data = path.read_bytes()
        d = self.data
        sfnt, num = struct.unpack(">IH", d[:6])
        if sfnt not in (0x00010000, 0x74727565):
            raise ValueError("仅支持 TrueType 字体")
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(num):
            tag, _cs, off, ln = struct.unpack(">4sIII", d[12 + i * 16:28 + i * 16])
            self.tables[tag.decode("latin1")] = (off, ln)
        head_off = self.tables["head"][0]
        self.upem = struct.unpack(">H", d[head_off + 18:head_off + 20])[0]
        self.num_glyphs = struct.unpack(
            ">H", d[self.tables["maxp"][0] + 4:self.tables["maxp"][0] + 6])[0]
        hhea_off = self.tables["hhea"][0]
        num_metrics = struct.unpack(">H", d[hhea_off + 34:hhea_off + 36])[0]
        hmtx_off = self.tables["hmtx"][0]
        self.adv: list[int] = []
        j = hmtx_off
        last_aw = 0
        for i in range(num_metrics):
            aw, _lsb = struct.unpack(">HH", d[j:j + 4]); j += 4
            self.adv.append(aw); last_aw = aw
        while len(self.adv) < self.num_glyphs:
            self.adv.append(last_aw)
        self.ucs2gid = self._parse_cmap()

    def gid(self, ch: str) -> int | None:
        return self.ucs2gid.get(ord(ch))

    def advance_pt(self, ch: str, size: float) -> float:
        g = self.gid(ch)
        aw = self.adv[g] if g is not None and g < len(self.adv) else self.upem
        return aw / self.upem * size

    def _parse_cmap(self) -> dict[int, int]:
        off, _ln = self.tables["cmap"]
        d = self.data
        n = struct.unpack(">H", d[off + 2:off + 4])[0]
        best = None
        for i in range(n):
            pid, eid, suboff = struct.unpack(">HHI", d[off + 4 + i * 8:off + 12 + i * 8])
            score = {(3, 10): 30, (0, 6): 20, (3, 1): 10, (0, 3): 9, (0, 4): 8}.get((pid, eid), 0)
            if best is None or score > best[0]:
                best = (score, off + suboff)
        table = best[1]
        fmt = struct.unpack(">H", d[table:table + 2])[0]
        if fmt == 4:
            return self._cmap4(table)
        if fmt == 12:
            return self._cmap12(table)
        raise ValueError(f"不支持的 cmap 格式 {fmt}")

    def _cmap4(self, table: int) -> dict[int, int]:
        d = self.data
        seg_x2 = struct.unpack(">H", d[table + 6:table + 8])[0]
        segc = seg_x2 // 2
        p = table + 14
        end = [struct.unpack(">H", d[p + 2 * i:p + 2 * i + 2])[0] for i in range(segc)]
        p += seg_x2 + 2
        start = [struct.unpack(">H", d[p + 2 * i:p + 2 * i + 2])[0] for i in range(segc)]
        p += seg_x2
        delta = [struct.unpack(">h", d[p + 2 * i:p + 2 * i + 2])[0] for i in range(segc)]
        p += seg_x2
        roff_pos = p
        roff = [struct.unpack(">H", d[p + 2 * i:p + 2 * i + 2])[0] for i in range(segc)]
        out: dict[int, int] = {}
        for i in range(segc):
            for c in range(start[i], end[i] + 1):
                if c == 0xFFFF:
                    continue
                if roff[i] == 0:
                    gid = (c + delta[i]) & 0xFFFF
                else:
                    gp = roff_pos + 2 * i + roff[i] + 2 * (c - start[i])
                    if gp + 2 > len(d):
                        continue
                    gid = struct.unpack(">H", d[gp:gp + 2])[0]
                    if gid:
                        gid = (gid + delta[i]) & 0xFFFF
                if gid:
                    out[c] = gid
        return out

    def _cmap12(self, table: int) -> dict[int, int]:
        d = self.data
        ngroups = struct.unpack(">I", d[table + 12:table + 16])[0]
        out: dict[int, int] = {}
        p = table + 16
        for _ in range(ngroups):
            sc, ec, sg = struct.unpack(">III", d[p:p + 12]); p += 12
            for c in range(sc, ec + 1):
                out[c] = sg + (c - sc)
        return out


class FontSet:
    """按字符回退选择字体。顺序：拉丁优先（标点/数字更协调），再 CJK。"""

    def __init__(self):
        self.latin = TTFont(LATIN_FONT, "DejaVuSans")
        self.cjk = TTFont(CJK_FONT, "DroidSansFallback")
        self.fonts = [self.latin, self.cjk]

    def pick(self, ch: str) -> TTFont:
        if self.latin.gid(ch) is not None:
            return self.latin
        return self.cjk  # .notdef 兜底

    def width(self, text: str, size: float) -> float:
        cur, total, w = None, 0.0, 0.0
        for ch in text:
            f = self.pick(ch)
            total += f.advance_pt(ch, size)
        return total


class _Page:
    def __init__(self):
        self.ops: list[str] = []


class PDFDoc:
    """轻量排版文档。坐标原点在左下角。"""

    def __init__(self, title: str, fonts: FontSet | None = None):
        self.title = title
        self.fs = fonts or get_fonts()
        self.pages: list[_Page] = [_Page()]
        self.y = PAGE_H - MARGIN_T
        # 每字体收集实际用到的字符（供 ToUnicode）
        self._used: dict[str, set[str]] = {f.base_name: set() for f in self.fs.fonts}

    @property
    def page(self) -> _Page:
        return self.pages[-1]

    # ---- 文本编码（按字体分段） ----
    def _runs(self, text: str):
        runs: list[tuple[TTFont, list[str]]] = []
        for ch in text:
            f = self.fs.pick(ch)
            if runs and runs[-1][0] is f:
                runs[-1][1].append(ch)
            else:
                runs.append((f, [ch]))
        return runs

    def _show(self, pg: _Page, x: float, y: float, text: str, size: float, color: str):
        r, g, b = _hex_color(color)
        names = {self.fs.latin.base_name: "F1", self.fs.cjk.base_name: "F2"}
        parts = [f"BT {r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} Td"]
        for f, chars in self._runs(text):
            s = "".join(chars)
            self._used[f.base_name].update(s)
            hexs = "".join(f"{(f.gid(c) or 0):04X}" for c in s)
            parts.append(f"/{names[f.base_name]} {size:.2f} Tf <{hexs}> Tj")
        parts.append("ET")
        pg.ops.append(" ".join(parts))

    def _wrap(self, text: str, size: float, width: float) -> list[str]:
        lines, cur, cur_w = [], "", 0.0
        for ch in text:
            if ch == "\n":
                lines.append(cur); cur, cur_w = "", 0.0
                continue
            w = self.fs.pick(ch).advance_pt(ch, size)
            if cur_w + w > width and cur:
                lines.append(cur); cur, cur_w = ch, w
            else:
                cur += ch; cur_w += w
        if cur:
            lines.append(cur)
        return lines or [""]

    def _ensure(self, need: float):
        if self.y - need < MARGIN_B:
            self.pages.append(_Page())
            self.y = PAGE_H - MARGIN_T

    # ---- 公共排版 API ----
    def gap(self, pt: float = 6):
        self._ensure(pt)
        self.y -= pt

    def heading(self, text: str, size: float = 17, color: str | None = None):
        self._ensure(size + 10)
        self.y -= size + 2
        self._show(self.page, MARGIN_L, self.y, text, size, color or config.COLORS["ink"])
        self.gap(5)

    def paragraph(self, text: str, size: float = 10.2, color: str | None = None, leading: float = 16):
        for line in self._wrap(text, size, CONTENT_W):
            self._ensure(leading)
            self.y -= leading
            self._show(self.page, MARGIN_L, self.y + 3, line, size, color or config.COLORS["ink"])
        self.gap(2)

    def chip(self, text: str, fg: str, bg: str):
        size = 9
        w = self.fs.width(text, size) + 14
        h = 16
        self._ensure(h + 4)
        self.y -= h
        r, g, b = _hex_color(bg)
        x0, y0 = MARGIN_L, self.y
        self.page.ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg {x0:.2f} {y0:.2f} {w:.2f} {h:.2f} re f"
        )
        self._show(self.page, x0 + 7, y0 + 4, text, size, fg)
        self.gap(5)

    def hr(self, color: str = "#dce2e4"):
        self._ensure(8)
        self.y -= 4
        r, g, b = _hex_color(color)
        self.page.ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} RG 0.6 w {MARGIN_L:.2f} {self.y:.2f} {CONTENT_W:.2f} 0 re S"
        )
        self.gap(4)

    def table(self, headers: list[str], rows: list[list[str]],
              col_w: list[float] | None = None, size: float = 9.2,
              row_colors: list[str | None] | None = None):
        n = len(headers)
        col_w = col_w or [CONTENT_W / n] * n
        line_h, pad = 13.5, 5

        def measure(cells):
            wrapped = [self._wrap(str(c), size, w - 2 * pad) for c, w in zip(cells, col_w)]
            rh = max(line_h * max(1, max(len(wl) for wl in wrapped)), 20)
            return wrapped, rh

        def draw(cells, top, fill, text_color):
            wrapped, rh = measure(cells)
            if fill:
                r, g, b = _hex_color(fill)
                self.page.ops.append(
                    f"{r:.3f} {g:.3f} {b:.3f} rg {MARGIN_L:.2f} {top - rh:.2f} "
                    f"{CONTENT_W:.2f} {rh:.2f} re f"
                )
            yy = top - pad - size
            for wl, x in zip(wrapped, self._col_x(col_w)):
                for ln in wl:
                    self._show(self.page, x + pad, yy + 2.5, ln, size, text_color)
                    yy -= line_h
            rr, gg, bb = _hex_color(config.COLORS["line"])
            self.page.ops.append(
                f"{rr:.3f} {gg:.3f} {bb:.3f} RG 0.4 w {MARGIN_L:.2f} {top - rh:.2f} "
                f"{CONTENT_W:.2f} {rh:.2f} re S"
            )
            return rh

        self._ensure(30)
        top = self.y
        top -= draw(headers, top, config.COLORS["teal"], "#ffffff")
        for i, row in enumerate(rows):
            _, need = measure(row)
            if self.y - need < MARGIN_B:
                self.pages.append(_Page())
                self.y = PAGE_H - MARGIN_T
                top = self.y
                top -= draw(headers, top, config.COLORS["teal"], "#ffffff")
            top -= draw(row, top, row_colors[i] if row_colors else None, config.COLORS["ink"])
        self.y = top
        self.gap(6)

    def _col_x(self, col_w):
        xs, x = [], MARGIN_L
        for w in col_w:
            xs.append(x); x += w
        return xs

    # ---- 页眉页脚 ----
    def _decorate(self, idx: int, label: str):
        pg = self.page
        r, g, b = _hex_color(config.COLORS["teal"])
        pg.ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} RG 0.8 w "
            f"{MARGIN_L:.2f} {PAGE_H - 40:.2f} {CONTENT_W:.2f} 0 re S"
        )
        head = "体检中心 · 报告解读平台（演示）"
        self._show(pg, MARGIN_L, PAGE_H - 32, head, 8, config.COLORS["ink_2"])
        if label:
            self._show(pg, PAGE_W - MARGIN_R - self.fs.width(label, 8),
                       PAGE_H - 32, label, 8, config.COLORS["ink_2"])
        foot = config.DEMO_DISCLAIMER
        lines = self._wrap(foot, 7.5, CONTENT_W - 60)[:2]
        for j, ln in enumerate(lines):
            self._show(pg, MARGIN_L, 30 - j * 10, ln, 7.5, config.COLORS["ink_2"])
        pn = f"第 {idx} / {len(self.pages)} 页"
        self._show(pg, PAGE_W - MARGIN_R - self.fs.width(pn, 8), 30, pn, 8, config.COLORS["ink_2"])

    # ---- PDF 输出 ----
    def save(self, path: Path, doc_label: str = "") -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        for idx in range(len(self.pages)):
            self._decorate(idx + 1, doc_label)

        objects: list[bytes] = []

        def add(obj: bytes) -> int:
            objects.append(obj)
            return len(objects)

        catalog_id = add(b"")
        pages_id = add(b"")
        font_ids = []
        for f in self.fs.fonts:
            ttf = add(b""); cid = add(b""); fd = add(b""); ff = add(b""); tu = add(b"")
            font_ids.append((f, ttf, cid, fd, ff, tu))
        page_ids, content_ids = [], []
        for pg in self.pages:
            content = "\n".join(pg.ops).encode("latin1")
            content_ids.append(add(
                b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                + content + b"\nendstream"))
            page_ids.append(add(b""))

        kids = " ".join(str(p) for p in page_ids)
        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        objects[pages_id - 1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>").encode()

        names = {self.fs.latin.base_name: "F1", self.fs.cjk.base_name: "F2"}
        for f, ttf, cid, fd, ff, tu in font_ids:
            widths = " ".join(str(round(aw / f.upem * 1000)) for aw in f.adv)
            comp = _compressed_font(f)
            objects[ttf - 1] = (
                f"<< /Type /Font /Subtype /Type0 /BaseFont /{f.base_name} "
                f"/Encoding /Identity-H /DescendantFonts [{cid} 0 R] /ToUnicode {tu} 0 R >>"
            ).encode()
            objects[cid - 1] = (
                f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{f.base_name} "
                f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                f"/FontDescriptor {fd} 0 R /CIDToGIDMap /Identity /DW 1000 /W [0 [{widths}]] >>"
            ).encode()
            objects[fd - 1] = (
                f"<< /Type /FontDescriptor /FontName /{f.base_name} /Flags 4 "
                f"/FontBBox [-1024 -410 1536 1280] /ItalicAngle 0 /Ascent 860 /Descent -240 "
                f"/CapHeight 700 /StemV 80 /FontFile2 {ff} 0 R >>"
            ).encode()
            objects[ff - 1] = (
                f"<< /Length {len(comp)} /Length1 {len(f.data)} /Filter /FlateDecode >>\n"
                f"stream\n".encode("latin1") + comp + b"\nendstream"
            )
            used = self._used[f.base_name]
            entries = []
            for ch in used:
                g = f.gid(ch)
                if g:
                    entries.append((g, ord(ch)))
            entries.sort()
            chunks = [
                "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            ]
            for i in range(0, len(entries), 100):
                block = entries[i:i + 100]
                chunks.append(f"{len(block)} beginbfchar\n")
                for g, u in block:
                    chunks.append(f"<{g:04X}> <{u:04X}>\n")
                chunks.append("endbfchar\n")
            chunks.append("endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
            cm = zlib.compress("".join(chunks).encode("latin1"), 9)
            objects[tu - 1] = (
                f"<< /Length {len(cm)} /Filter /FlateDecode >>\nstream\n".encode()
                + cm + b"\nendstream")

        for pid, cid in zip(page_ids, content_ids):
            res = " ".join(f"/{names[f.base_name]} {ttf} 0 R"
                           for f, ttf, *_ in font_ids)
            objects[pid - 1] = (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                f"/Resources << /Font << {res} >> >> /Contents {cid} 0 R >>"
            ).encode()

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for obj in objects:
            offsets.append(len(out))
            out += f"{len(offsets) - 1} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref_pos = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode()
        path.write_bytes(bytes(out))
        return path


_FONTS: FontSet | None = None
_FONT_COMPRESS_CACHE: dict[str, bytes] = {}


def _compressed_font(font: TTFont) -> bytes:
    """字体流对所有 PDF 相同，进程内只压缩一次。"""
    key = font.base_name
    comp = _FONT_COMPRESS_CACHE.get(key)
    if comp is None:
        comp = zlib.compress(font.data, 6)
        _FONT_COMPRESS_CACHE[key] = comp
    return comp


def get_fonts() -> FontSet:
    global _FONTS
    if _FONTS is None:
        _FONTS = FontSet()
    return _FONTS
