"""解读页指标趋势的内联 SVG（无外部依赖）。

设计遵循：单指标单序列折线（2px、圆头），参考范围用浅琥珀水平带与
范围线表达；超出范围的点用警示色 + 2px 表面色环；只标注端点数值，
其余交给坐标与悬停 <title>；坐标网格用 1px 石墨灰细线。
颜色不单靠色觉：异常点同时放大、加形状区别并直接标注 ↑/↓。
"""
from __future__ import annotations

import html

W, H = 420, 150
ML, MR, MT, MB = 34, 30, 16, 26


def _nice_range(lo, hi, values):
    vmin, vmax = min(values), max(values)
    if lo is not None:
        vmin = min(vmin, lo)
    if hi is not None:
        vmax = max(vmax, hi)
    if vmin == vmax:
        vmin -= 1; vmax += 1
    pad = (vmax - vmin) * 0.18
    return vmin - pad, vmax + pad


def trend_svg(points, *, ref_low=None, ref_high=None, unit="") -> str:
    """points: [{'exam_date':'2023-09-12','value':2.8,'flag':'N'}, ...]（升序）。"""
    pts = [p for p in points if p.get("value") is not None]
    if len(pts) < 1:
        return '<div class="muted small">暂无数值趋势（文本型指标）</div>'
    vals = [p["value"] for p in pts]
    ymin, ymax = _nice_range(ref_low, ref_high, vals)

    iw, ih = W - ML - MR, H - MT - MB

    def X(i):
        if len(pts) == 1:
            return ML + iw / 2
        return ML + iw * i / (len(pts) - 1)

    def Y(v):
        return MT + ih * (1 - (v - ymin) / (ymax - ymin))

    out = [f'<svg class="trend" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="指标趋势折线图，共 {len(pts)} 个年度数据点">']

    # 水平参考范围带
    if ref_low is not None and ref_high is not None:
        y0, y1 = Y(ref_high), Y(ref_low)
        out.append(f'<rect class="refband" x="{ML}" y="{y0:.1f}" '
                   f'width="{iw}" height="{max(0, y1-y0):.1f}"/>')
        for v, cls in ((ref_high, "refline"), (ref_low, "refline")):
            out.append(f'<line class="{cls}" x1="{ML}" x2="{ML+iw}" '
                       f'y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>')

    # 三条网格线 + y 刻度
    for i in range(3):
        v = ymin + (ymax - ymin) * i / 2
        y = Y(v)
        out.append(f'<line class="grid" x1="{ML}" x2="{ML+iw}" y1="{y:.1f}" '
                   f'y2="{y:.1f}"/>')
        out.append(f'<text class="lbl" x="{ML-5}" y="{y+3:.1f}" '
                   f'text-anchor="end">{v:g}</text>')

    # 面积
    coords = [(X(i), Y(p["value"])) for i, p in enumerate(pts)]
    area = ("M " + f"{coords[0][0]:.1f} {MT+ih:.1f} L "
            + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
            + f" L {coords[-1][0]:.1f} {MT+ih:.1f} Z")
    out.append(f'<path class="area" d="{area}"/>')
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    out.append(f'<path class="line" d="{line}"/>')

    # 数据点 + 年份轴
    for i, (p, (x, y)) in enumerate(zip(pts, coords)):
        abn = p.get("flag") in ("H", "L")
        cls = "pt abn" if abn else "pt"
        r = 5.5 if abn else 4
        year = p["exam_date"][:4]
        out.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" '
                   f'y1="{MT+ih:.1f}" y2="{MT+ih+3:.1f}"/>')
        out.append(f'<text class="lbl" x="{x:.1f}" y="{H-8}" '
                   f'text-anchor="middle">{year}</text>')
        val = f"{p['value']:g}"
        tip = f"{year}年：{p['value']:g} {unit}（{'偏高' if p.get('flag')=='H' else '偏低' if p.get('flag')=='L' else '在参考范围内'}）"
        out.append(
            f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r}">'
            f'<title>{html.escape(tip)}</title></circle>')
        # 仅端点直接标数值（含异常箭头，颜色不是唯一通道）
        if i == 0 or i == len(pts) - 1:
            arrow = " ↑" if p.get("flag") == "H" else " ↓" if p.get("flag") == "L" else ""
            vcls = "v abn" if abn else "v"
            dx = -6 if i == 0 else 6
            anchor = "end" if i == 0 else "start"
            out.append(f'<text class="{vcls}" x="{x+dx:.1f}" y="{y-7:.1f}" '
                       f'text-anchor="{anchor}">{val}{arrow}</text>')

    if ref_low is not None and ref_high is not None:
        out.append(f'<text class="lbl" x="{ML+iw}" y="{Y(ref_high)-3:.1f}" '
                   f'text-anchor="end">参考上限 {ref_high:g}{html.escape(unit)}</text>')
    out.append("</svg>")
    return "".join(out)
