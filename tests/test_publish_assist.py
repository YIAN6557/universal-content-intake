from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from src.media.publish_assist import (
    COPY_LIMIT,
    TITLE_LIMIT,
    build_copy,
    build_publish_assist,
    char_count,
    clean_title,
    description_lead,
    fit_title,
    hard_cut,
    strip_series_suffix,
)
from src.media import publish_writer as writer_module
from src.media.publish_writer import parse_writer_output

CUES = [
    "大家好，今天我们来聊聊人工智能代理。",
    "它们正在改变软件开发的方式。",
    "我们会看三个真实案例，并比较它们的成本和效果。",
    "最后给出一个简单的上手清单。",
]


class TitleRuleTests(unittest.TestCase):
    def test_title_within_thirty_characters_is_used_directly(self) -> None:
        self.assertEqual(fit_title("为什么人工智能代理会改变软件开发的方式｜实测三个案例"),
                         ("为什么人工智能代理会改变软件开发的方式｜实测三个案例", "direct"))

    def test_31_to_40_characters_are_compressed_by_dropping_decorations_and_trailing_clauses(self) -> None:
        result, rule = fit_title("为什么人工智能代理会彻底改变整个软件开发行业的方式｜我们实测了三个真实案例")
        self.assertEqual(rule, "compressed")
        self.assertEqual(result, "为什么人工智能代理会彻底改变整个软件开发行业的方式")
        self.assertLessEqual(char_count(result), TITLE_LIMIT)

    def test_over_40_characters_is_regenerated_not_quoted(self) -> None:
        long_title = "我们花了整整三个月时间测试了市面上所有主流的人工智能编程代理工具，这是最终的完整结论和建议，欢迎大家一起讨论"
        result, rule = fit_title(long_title, CUES)
        self.assertEqual(rule, "regenerated")
        self.assertLessEqual(char_count(result), TITLE_LIMIT)
        self.assertNotEqual(result, long_title)

    def test_empty_translation_falls_back_to_first_subtitle_sentence(self) -> None:
        result, rule = fit_title("", CUES)
        self.assertEqual(rule, "generated_from_subtitles")
        self.assertLessEqual(char_count(result), TITLE_LIMIT)

    def test_hard_cut_never_splits_a_latin_word(self) -> None:
        self.assertEqual(hard_cut("OpenAI发布了全新的GPT模型并且大幅降低了推理成本", 20), "OpenAI发布了全新的GPT模型并且大")
        cut = hard_cut("这是关于 Kubernetes autoscaling 的介绍", 12)
        self.assertFalse(cut.endswith("autosc"))
        self.assertLessEqual(char_count(cut), 12)

    def test_clean_title_removes_emoji_hashtags_and_brackets(self) -> None:
        self.assertEqual(clean_title("🔥【重磅】人工智能周报 #AI (第3期)"), "人工智能周报")


class CopyRuleTests(unittest.TestCase):
    def test_copy_is_extracted_from_opening_subtitles_within_limit(self) -> None:
        blurb, rule = build_copy(CUES)
        self.assertEqual(rule, "subtitle_extract")
        self.assertLessEqual(char_count(blurb), COPY_LIMIT)
        self.assertTrue(blurb.startswith("大家好"))

    def test_single_overlong_sentence_is_cut_with_ellipsis(self) -> None:
        blurb, _ = build_copy(["".join(chr(0x4E00 + index) for index in range(200)) + "。"])
        self.assertLessEqual(char_count(blurb), COPY_LIMIT)
        self.assertTrue(blurb.endswith("…"))

    def test_caption_annotations_repeats_and_duplicate_cues_are_removed(self) -> None:
        blurb, _ = build_copy([
            "我在你身上看到了火花[音乐]我在你身上看到了火花",
            "我在你身上看到了火花",
            "♪ 这并不奇怪\n[掌声]",
        ])
        self.assertEqual(blurb, "我在你身上看到了火花，这并不奇怪。")

    def test_repeated_lyric_clauses_across_cues_are_kept_once(self) -> None:
        blurb, _ = build_copy(["突然有一个下降在你的", "突然有一个下降在你的", "突然有一个下降在你的训练损失", "这并不奇怪"])
        self.assertEqual(blurb, "突然有一个下降在你的训练损失，这并不奇怪。")

    def test_no_subtitles_uses_title_or_stays_empty(self) -> None:
        self.assertEqual(build_copy([], title="人工智能周报"), ("人工智能周报", "title_only"))
        self.assertEqual(build_copy([]), ("", "unavailable"))


class PublishAssistRecordTests(unittest.TestCase):
    def test_record_uses_translated_cues_and_translator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            path = temp / "stage3/translation/source-abc/translated-cues.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"cues": [{"text": "x", "translated_text": text} for text in CUES]}, ensure_ascii=False), encoding="utf-8")
            record = build_publish_assist(
                original_title="I'm upping my p(doom)",
                original_language="en",
                temp_dir=temp,
                stage3={"translation": {"status": "success"}},
                translator=lambda title, language: "我把末日概率调高了",
            )
        self.assertEqual(record["title"], "我把末日概率调高了")
        self.assertEqual(record["title_rule"], "direct")
        self.assertEqual(record["title_translation"], "apple_translation")
        self.assertEqual(record["copy_rule"], "subtitle_extract")

    def test_translator_failure_never_raises(self) -> None:
        def broken(title: str, language: str) -> str:
            raise OSError("helper missing")

        with tempfile.TemporaryDirectory() as directory:
            record = build_publish_assist(
                original_title="Title", original_language="en", temp_dir=Path(directory), stage3={}, translator=broken,
            )
        self.assertEqual(record["title_translation"], "failed:OSError")
        self.assertEqual(record["title_rule"], "unavailable")
        self.assertEqual(record["copy"], "")

    def test_chinese_source_title_is_not_translated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = build_publish_assist(
                original_title="人工智能周报第三期", original_language="zh-Hans", temp_dir=Path(directory), stage3={},
                translator=lambda *_: self.fail("translator must not be called"),
            )
        self.assertEqual(record["title"], "人工智能周报第三期")
        self.assertEqual(record["title_translation"], "skipped_source_is_zh_hans")


    def test_copy_comes_from_the_translated_description_lead_not_promos(self) -> None:
        description = (
            "Figure's new humanoid robot F.03 can now fold laundry and load a dishwasher on its own. "
            "We went to the lab to see how it learns new chores.\n\n"
            "Subscribe: https://youtube.com/example\nFollow us on Instagram: https://instagram.com/example"
        )
        seen: list[str] = []

        def translator(text: str, language: str) -> str:
            seen.append(text)
            return "Figure 的新款人形机器人 F.03 现在可以自己叠衣服、装洗碗机。我们去实验室看它如何学习新家务。" if "laundry" in text else "机器人学会做家务"

        with tempfile.TemporaryDirectory() as directory:
            record = build_publish_assist(
                original_title="Robots can do chores now | The Example Show", original_language="en",
                temp_dir=Path(directory), stage3={}, translator=translator, author="Example Tech",
                source_details={"description": description},
            )
        self.assertEqual(seen[0], "Robots can do chores now")
        self.assertNotIn("Subscribe", seen[1])
        self.assertEqual(record["copy_rule"], "description_translation")
        self.assertTrue(record["copy"].startswith("Figure 的新款人形机器人"))

    def test_description_lead_skips_links_promos_and_chapters(self) -> None:
        self.assertEqual(description_lead("Check out our merch at https://x.com/shop\n00:00 Intro\nA long look at how chips are made today.\nhttps://link"),
                         "A long look at how chips are made today.")
        self.assertEqual(strip_series_suffix("Dots are OpenAI's cute lil guys | The Example Show"), "Dots are OpenAI's cute lil guys")

    def test_writer_output_is_used_and_failure_falls_back_to_rules(self) -> None:
        contexts: list[dict] = []

        def writer(context):
            contexts.append(dict(context))
            return {"title": "机器人终于会叠衣服了", "copy": "实验室实拍。", "hashtags": ["机器人"], "engine": "claude-opus-5-5"}

        with tempfile.TemporaryDirectory() as directory:
            record = build_publish_assist(
                original_title="Robots fold laundry", original_language="en", temp_dir=Path(directory), stage3={},
                translator=None, author="Example Tech", source_details={"description": "x", "tags": ["robots"]}, writer=writer,
            )
            self.assertEqual((record["title"], record["copy_rule"], record["hashtags"]), ("机器人终于会叠衣服了", "llm", ["机器人"]))
            self.assertEqual(contexts[0]["author"], "Example Tech")

            def broken(context):
                raise ValueError("stop_reason=refusal")

            record = build_publish_assist(
                original_title="人工智能周报", original_language="zh-Hans", temp_dir=Path(directory), stage3={},
                translator=None, writer=broken,
            )
        self.assertEqual(record["title"], "人工智能周报")
        self.assertIn("refusal", record["llm_error"])

    def test_writer_output_is_validated(self) -> None:
        self.assertEqual(parse_writer_output({"title": " 标题 ", "copy": "文案", "hashtags": ["#a", "a", "b"]})["hashtags"], ["a", "b"])
        with self.assertRaises(ValueError):
            parse_writer_output({"title": "", "copy": "文案", "hashtags": []})

    def test_gemini_writer_sends_schema_request_and_skips_thought_parts(self) -> None:
        envelope = {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True},
            {"text": json.dumps({"title": "Atlas 新一代灵巧手", "copy": "13 个自由度。", "hashtags": ["机器人"]}, ensure_ascii=False)},
        ]}}]}
        requests = []

        def urlopen(request, timeout, context=None):
            requests.append(request)
            return io.BytesIO(json.dumps(envelope).encode("utf-8"))

        with mock.patch.dict("os.environ", {"UCI_PUBLISH_WRITER": "", "UCI_GEMINI_MODEL": ""}), \
                mock.patch.object(writer_module, "_keychain_api_key", lambda service=None: "k" if service == "UCI Gemini API" else None), \
                mock.patch.object(writer_module.urllib.request, "urlopen", urlopen):
            write = writer_module.publish_writer()
            result = write({"original_title": "New Hands for Atlas"})
        self.assertEqual((result["title"], result["engine"]), ("Atlas 新一代灵巧手", "gemini-3.5-flash-lite"))
        self.assertTrue(requests[0].full_url.endswith("/gemini-3.5-flash-lite:generateContent"))
        self.assertEqual(requests[0].get_header("X-goog-api-key"), "k")
        body = json.loads(requests[0].data)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("New Hands for Atlas", body["contents"][0]["parts"][0]["text"])

    def test_gemini_writer_http_error_omits_key_and_absent_key_disables(self) -> None:
        def urlopen(request, timeout, context=None):
            raise urllib.error.HTTPError(request.full_url, 429, "quota", {}, None)

        with mock.patch.dict("os.environ", {"UCI_PUBLISH_WRITER": ""}), \
                mock.patch.object(writer_module, "_keychain_api_key", lambda service=None: "secret-key"), \
                mock.patch.object(writer_module.urllib.request, "urlopen", urlopen):
            with self.assertRaises(ValueError) as caught:
                writer_module.gemini_publish_writer()({})
        self.assertEqual(str(caught.exception), "gemini HTTP 429")
        with mock.patch.dict("os.environ", {"UCI_PUBLISH_WRITER": ""}), \
                mock.patch.object(writer_module, "_keychain_api_key", lambda service=None: None):
            self.assertIsNone(writer_module.gemini_publish_writer())
        with mock.patch.dict("os.environ", {"UCI_PUBLISH_WRITER": "off"}):
            self.assertIsNone(writer_module.publish_writer())


if __name__ == "__main__":
    unittest.main()
