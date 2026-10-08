import tempfile
import unittest
from pathlib import Path

from src.core.job import ContentType
from src.get_cli import GetError, Probe, cannot_download, deliver, detect, parse_request


def never_probe(url: str) -> Probe:
    raise AssertionError(f"declared or platform links must not be probed: {url}")


class RequestParsingTests(unittest.TestCase):
    def test_a_named_type_is_used_without_detection(self) -> None:
        cases = {
            "帮我下载这个链接里的PDF https://example.com/report": ContentType.DOCUMENT,
            "下载这个链接里面的视频 https://example.com/watch": ContentType.VIDEO,
            "把这个相册的图片都下载下来 https://example.com/a": ContentType.IMAGE_SET,
            "保存这个网页 https://example.com/page": ContentType.WEBPAGE,
            "download the video https://example.com/x": ContentType.VIDEO,
        }
        for text, expected in cases.items():
            link, named = parse_request(text.split())
            self.assertTrue(link.startswith("https://example.com/"), text)
            self.assertEqual(named, expected, text)

    def test_the_page_a_file_lives_on_does_not_compete_with_the_file(self) -> None:
        self.assertEqual(parse_request(["下载这个网页里的视频", "https://example.com/p"])[1], ContentType.VIDEO)
        self.assertEqual(parse_request(["下载这篇文章的PDF", "https://example.com/p"])[1], ContentType.DOCUMENT)

    def test_a_bare_link_names_no_type_and_conflicts_are_rejected(self) -> None:
        self.assertEqual(parse_request(["https://example.com/x"]), ("https://example.com/x", None))
        wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        self.assertEqual(parse_request([wiki])[0], wiki)
        self.assertEqual(parse_request([f"下载这篇（{wiki}）。"])[0], wiki)
        self.assertEqual(parse_request(["(see", "https://example.com/x)"])[0], "https://example.com/x")
        with self.assertRaises(GetError):
            parse_request(["下载视频和PDF", "https://example.com/x"])
        with self.assertRaises(GetError):
            parse_request(["没有链接"])
        # English words inside other words do not count ("rapid" is not "pdf", "videos" is not "video").
        self.assertEqual(parse_request(["https://example.com/rapid-update"])[1], None)


class DetectionTests(unittest.TestCase):
    def test_platforms_and_extensions_decide_without_a_network_probe(self) -> None:
        cases = {
            "https://www.youtube.com/watch?v=abc": ContentType.VIDEO,
            "https://m.bilibili.com/video/BV1": ContentType.VIDEO,
            "https://drive.google.com/file/d/abc/view": ContentType.DOCUMENT,
            "https://example.com/files/report.PDF": ContentType.DOCUMENT,
            "https://example.com/a/photo.jpg?w=200": ContentType.IMAGE,
            "https://cdn.example.com/clip.mp4": ContentType.VIDEO,
            "https://imgur.com/a/xyz": ContentType.IMAGE_SET,
            "rclone://remote/folder/book.epub": ContentType.DOCUMENT,
        }
        for url, expected in cases.items():
            self.assertEqual(detect(url, probe=never_probe).content_type, expected, url)
            self.assertFalse(detect(url, probe=never_probe).declared)

    def test_social_posts_ask_yt_dlp_whether_there_is_a_video(self) -> None:
        url = "https://x.com/someone/status/1"
        self.assertEqual(detect(url, probe=never_probe, has_video=lambda _: True).content_type, ContentType.VIDEO)
        self.assertEqual(detect(url, probe=never_probe, has_video=lambda _: False).content_type, ContentType.IMAGE_SET)

    def test_unknown_links_are_probed(self) -> None:
        def probe(mime: str, disposition: str = "", body: str = ""):
            return lambda url: Probe(mime, disposition, body)

        url = "https://example.com/download?id=7"
        self.assertEqual(detect(url, probe=probe("application/pdf")).content_type, ContentType.DOCUMENT)
        self.assertEqual(detect(url, probe=probe("image/png")).content_type, ContentType.IMAGE)
        self.assertEqual(detect(url, probe=probe("video/mp4")).content_type, ContentType.VIDEO)
        self.assertEqual(detect(url, probe=probe("text/html", 'attachment; filename="a.bin"')).content_type, ContentType.DOCUMENT)
        html = probe("text/html; charset=utf-8", body="<html>…</html>")
        self.assertEqual(detect(url, probe=html, article_chars=lambda _: 2000).content_type, ContentType.ARTICLE)
        self.assertEqual(detect(url, probe=html, article_chars=lambda _: 50).content_type, ContentType.WEBPAGE)

    def test_wechat_channels_videos_are_refused_unless_only_the_page_is_wanted(self) -> None:
        for url in ("https://weixin.qq.com/sph/AmZAkab0Lb", "https://channels.weixin.qq.com/web/pages/feed?id=1"):
            for content_type in (None, ContentType.VIDEO, ContentType.IMAGE):
                self.assertIn("视频号", cannot_download(url, content_type) or "", url)
            self.assertIsNone(cannot_download(url, ContentType.WEBPAGE))
        self.assertIsNone(cannot_download("https://mp.weixin.qq.com/s/abc", None))
        self.assertIsNone(cannot_download("https://www.youtube.com/watch?v=abc", None))


class DeliveryTests(unittest.TestCase):
    def job_output(self, root: Path, title: str, *names: str) -> list[Path]:
        output = root / "job" / "output"
        (output / "content").mkdir(parents=True)
        files = []
        for name in names:
            path = output / "content" / name
            path.write_text(name)
            files.append(path)
        info = output / "info.md"
        info.write_text(f"# Info\n- Original Title: {title}\n")
        return files + [info]

    def test_a_single_file_keeps_a_meaningful_name_and_gets_its_description_next_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moved = deliver(self.job_output(root, "Annual Report", "report.pdf"), root / "Downloads")
            self.assertEqual([p.name for p in moved], ["report.pdf", "report - 信息.md"])

    def test_generic_names_take_the_title_and_nothing_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Downloads").mkdir()
            (root / "Downloads" / "NASA Moon Base The First Six Months.mp4").write_text("older")
            moved = deliver(self.job_output(root, "NASA Moon Base: The First Six Months", "final.mp4"), root / "Downloads")
            self.assertEqual(moved[0].name, "NASA Moon Base The First Six Months (2).mp4")
            self.assertEqual((root / "Downloads" / "NASA Moon Base The First Six Months.mp4").read_text(), "older")

    def test_several_files_go_into_a_folder_named_after_the_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moved = deliver(self.job_output(root, "Apollo 11", "article.html", "article.md", "article.pdf"), root / "Downloads")
            self.assertEqual({p.parent.name for p in moved}, {"Apollo 11"})
            self.assertEqual(sorted(p.name for p in moved), ["Apollo 11.html", "Apollo 11.md", "Apollo 11.pdf", "信息.md"])


if __name__ == "__main__":
    unittest.main()
