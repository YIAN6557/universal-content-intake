import unittest

from src.providers.article import _html_metadata, decode_html, parse_html

# SingleFile writes a comment with the (localized) save date before <meta charset>,
# which made lxml read the whole UTF-8 page as Latin-1.
SINGLEFILE_PAGE = (
    "<!DOCTYPE html> <html lang=zh-cn><!--\n Page saved with SingleFile \n saved date: Mon Oct 05 2026 "
    "21:53:16 GMT+0800 (台北标准时间)\n--><meta charset=utf-8><title>X 上的 疯狂的烤妹儿</title>"
    '<meta property="og:title" content="【邪修起号 Vol.02】实操指南"></head><body><article>'
    '<h1>【邪修起号 Vol.02】实操指南</h1><img src="https://example.com/a.jpg" alt="文章封面图片"></article></body></html>'
).encode("utf-8")


class HtmlDecodingTests(unittest.TestCase):
    def test_utf8_page_with_non_ascii_before_the_charset_keeps_its_chinese_text(self) -> None:
        tree = parse_html(SINGLEFILE_PAGE)
        self.assertEqual(tree.findtext(".//title"), "X 上的 疯狂的烤妹儿")
        self.assertEqual(tree.xpath("//img/@alt"), ["文章封面图片"])
        metadata = _html_metadata(SINGLEFILE_PAGE, "https://x.com/example")
        self.assertIn("邪修起号", " ".join(str(value) for value in metadata.values()))
        self.assertNotIn("ã", " ".join(str(value) for value in metadata.values()))

    def test_declared_charsets_bom_and_xml_declarations(self) -> None:
        gbk = '<html><head><meta http-equiv="Content-Type" content="text/html; charset=gbk"><title>中文标题</title></head></html>'
        self.assertIn("中文标题", decode_html(gbk.encode("gbk")))
        self.assertEqual(decode_html("﻿<p>标题</p>".encode("utf-8")), "<p>标题</p>")
        self.assertEqual(decode_html(b"<meta charset=no-such-codec><p>ok</p>"), "<meta charset=no-such-codec><p>ok</p>")
        xhtml = '<?xml version="1.0" encoding="utf-8"?><html><head><title>标题</title></head></html>'.encode("utf-8")
        self.assertEqual(parse_html(xhtml).findtext(".//title"), "标题")


if __name__ == "__main__":
    unittest.main()
