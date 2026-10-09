import json
import os
import unittest
from unittest import mock

from src.media import online_translation as online
from src.media.subtitles import Cue
from src.media.translation import apply_translation_response, make_translation_request

SETTINGS = online.Settings("online", "deepseek", "deepseek-flash", "https://api.deepseek.com")


def cues(count: int) -> tuple[Cue, ...]:
    return tuple(Cue(f"c{n}", float(n), n + 0.9, f"line {n}", "en") for n in range(count))


class FakeModel:
    """Answers like a chat API; can drop lines or fail a number of times first."""

    def __init__(self, *, drop: set[str] | None = None, failures: list[tuple[int, dict]] | None = None) -> None:
        self.drop = drop or set()
        self.failures = list(failures or [])
        self.bodies: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        request = json.loads(body)
        self.bodies.append(request)
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer sk-test"
        if self.failures:
            status, payload = self.failures.pop(0)
            return status, json.dumps(payload).encode()
        lines = json.loads(request["messages"][-1]["content"])["lines"]
        answer = {"lines": [{"id": line["id"], "zh": f"第{line['text'][5:]}行"} for line in lines if line["id"] not in self.drop]}
        self.drop = set()  # drop only once
        return 200, json.dumps({"choices": [{"message": {"content": f"```json\n{json.dumps(answer, ensure_ascii=False)}\n```"}}]}).encode()


class OnlineTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {online.KEY_ENV: "sk-test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def translate(self, source, model):
        request = make_translation_request(source, source_language="en")
        response = online.translate_request(request, SETTINGS, transport=model, sleep=lambda _: None)
        return apply_translation_response(source, response)

    def test_cues_are_translated_in_batches_and_keep_their_identity(self) -> None:
        model = FakeModel()
        translated = self.translate(cues(95), model)
        self.assertEqual(len(model.bodies), 3)  # 40 + 40 + 15
        self.assertEqual(translated[7].translated_text, "第7行")
        self.assertEqual(model.bodies[0]["model"], "deepseek-flash")
        self.assertEqual(model.bodies[0]["response_format"], {"type": "json_object"})

    def test_a_dropped_line_is_asked_for_again(self) -> None:
        model = FakeModel(drop={"c3"})
        translated = self.translate(cues(5), model)
        self.assertEqual([cue.translated_text for cue in translated], ["第0行", "第1行", "第2行", "第3行", "第4行"])
        self.assertEqual(len(model.bodies), 2)

    def test_rate_limits_are_retried_and_a_bad_key_is_explained(self) -> None:
        model = FakeModel(failures=[(429, {"error": {"message": "rate limit"}}), (503, {})])
        self.assertEqual(self.translate(cues(2), model)[1].translated_text, "第1行")
        with self.assertRaises(online.TranslationAccountError) as caught:
            self.translate(cues(1), FakeModel(failures=[(401, {"error": {"message": "invalid api key"}})]))
        self.assertIn("API Key", str(caught.exception))
        with self.assertRaises(online.TranslationAccountError):
            self.translate(cues(1), FakeModel(failures=[(402, {"error": {"message": "Insufficient Balance"}})]))

    def test_an_unsupported_optional_parameter_is_dropped_once(self) -> None:
        model = FakeModel(failures=[(400, {"error": {"message": "response_format is not supported"}})])
        self.translate(cues(1), model)
        self.assertIn("response_format", model.bodies[0])
        self.assertNotIn("response_format", model.bodies[1])

    def test_nothing_is_sent_until_a_model_and_key_are_set(self) -> None:
        with mock.patch.dict(os.environ, {online.KEY_ENV: ""}), mock.patch("src.queue.secrets.read_secret", return_value=None):
            with self.assertRaises(online.TranslationNotConfigured):
                online.translate_request(make_translation_request(cues(1), source_language="en"), SETTINGS,
                                         transport=FakeModel())
        empty = online.Settings("online", "", "", "")
        self.assertEqual(online.configured(empty), "还没有选择翻译模型")

    def test_settings_come_from_the_user_config_with_preset_defaults(self) -> None:
        settings = online.load_settings({"translation": {"engine": "online", "provider": "qwen"}})
        self.assertEqual((settings.base_url, settings.model), ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"))
        custom = online.load_settings({"translation": {"engine": "online", "provider": "custom",
                                                       "base_url": "https://api.example.com/v1/", "model": "m"}})
        self.assertEqual(custom.base_url, "https://api.example.com/v1")
        default = online.load_settings({})
        self.assertEqual(default.engine, "online" if os.name == "nt" else "apple")

    def test_connection_check_needs_a_chinese_answer(self) -> None:
        self.assertTrue(online.test_connection(SETTINGS, transport=FakeModel()).startswith("第"))


if __name__ == "__main__":
    unittest.main()
