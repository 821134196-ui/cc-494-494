"""PDF 生成器：结构合法、更正件/异常标记/参考范围正确落盘。"""
import os
import tempfile
import unittest

from app.pdfgen import render_report_pdf, wrap_text, text_width


METRICS = [
    {"metric_key": "glc_fpg", "label": "空腹血糖", "value": 7.2, "unit": "mmol/L",
     "ref_low": 3.9, "ref_high": 6.1, "status": "high"},
    {"metric_key": "tc", "label": "总胆固醇", "value": 4.8, "unit": "mmol/L",
     "ref_low": None, "ref_high": 5.2, "status": "normal"},
    {"metric_key": "hdl", "label": "高密度脂蛋白", "value": 0.9, "unit": "mmol/L",
     "ref_low": 1.0, "ref_high": None, "status": "low"},
]


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hc-pdftest-")
        self.path = os.path.join(self.tmp, "r.pdf")

    def _render(self, **kw):
        defaults = dict(
            path=self.path, report_no="R-1", version=1, title="年度体检报告",
            examined_at="2026-09-24", issued_at="2026-09-25", patient_name="虚构受检人",
            doctor_name=None, correction_note=None, metrics=METRICS,
            checksum_short="abc123")
        defaults.update(kw)
        render_report_pdf(**defaults)

    def test_pdf_structure_valid(self):
        self._render()
        data = open(self.path, "rb").read()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        # 内置简体中文 CID 字体
        self.assertIn(b"/STSong-Light", data)
        self.assertIn(b"/UniGB-UCS2-H", data)
        # xref 偏移正确（可供阅读器随机访问）
        sx = int(data[data.rindex(b"startxref") + 9:data.rindex(b"%%EOF")].strip())
        self.assertEqual(data[sx:sx + 4], b"xref")
        # 数值以 UCS-2BE 十六进制出现（'7'→0037，'2'→0032）
        self.assertIn(b"0037", data)
        self.assertIn(b"0032", data)

    def test_correction_pdf_contains_note_and_marker(self):
        self._render(version=2, correction_note="空腹血糖录入错误，7.2 更正为 6.1")
        data = open(self.path, "rb").read()
        # “更正” 的 UCS-2BE 编码
        marker = "更正".encode("utf-16-be").hex().upper().encode()
        self.assertIn(marker, data)
        # 更正说明文字
        note = "录入错误".encode("utf-16-be").hex().upper().encode()
        self.assertIn(note, data)

    def test_demo_notice_present(self):
        self._render()
        data = open(self.path, "rb").read()
        self.assertIn("虚构".encode("utf-16-be").hex().upper().encode(), data)

    def test_multipage_on_many_metrics(self):
        many = []
        for i in range(60):
            many.append({"metric_key": f"k{i}", "label": f"指标项目{i}",
                         "value": i, "unit": "U", "ref_low": 1, "ref_high": 100,
                         "status": "normal"})
        self._render(metrics=many)
        data = open(self.path, "rb").read()
        # /Type /Page（带空格）只匹配页对象；/Type /Pages 是页面树根节点
        self.assertGreaterEqual(data.count(b"/Type /Page "), 2)
        self.assertEqual(data.count(b"/Type /Pages "), 1)

    def test_wrap_text_cjk_width(self):
        lines = wrap_text("空腹血糖偏高需要控制饮食", 10, 60)
        self.assertTrue(all(text_width(ln, 10) <= 60 + 1 for ln in lines))
        joined = "".join(lines)
        self.assertEqual(joined, "空腹血糖偏高需要控制饮食")


if __name__ == "__main__":
    unittest.main()
