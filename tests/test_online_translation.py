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
        renamed = online.load_settings({"translation": {"engine": "online", "provider": "deepseek",
                                                        "base_url": "https://api.example.com/v1/", "model": "m"}})
        self.assertEqual((renamed.base_url, renamed.model), ("https://api.example.com/v1", "m"))
        default = online.load_settings({})
        self.assertEqual(default.engine, "online" if os.name == "nt" else "apple")

    def test_connection_check_needs_a_chinese_answer(self) -> None:
        self.assertTrue(online.test_connection(SETTINGS, transport=FakeModel()).startswith("第"))


class SetupFlowTests(unittest.TestCase):
    """bin/uci setup translation: a rejected key is never saved; a working one is saved after the test sentence."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.dict(os.environ, {"UCI_CONFIG": str(Path(directory.name) / "config.yaml"), online.KEY_ENV: ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.saved: dict[str, str] = {}
        for name, value in (("save_api_key", lambda provider, key: self.saved.__setitem__(provider, key)),
                            ("api_key", lambda provider: self.saved.get(provider))):
            patcher = mock.patch.object(online, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_deepseek_is_the_default_and_qwen_the_other_choice(self) -> None:
        from src.setup import translation_setup

        self.assertEqual(online.RECOMMENDED, ("deepseek", "qwen"))
        answers = iter(["", ])
        lines: list[str] = []
        with mock.patch.object(translation_setup, "save_key", return_value=True) as save:
            translation_setup.wizard(read=lambda _: next(answers), secret=lambda _: "sk-test", out=lines.append)
        self.assertEqual(save.call_args.args[0].provider, "deepseek")
        self.assertTrue(any("platform.deepseek.com" in line for line in lines))

    def test_a_rejected_key_is_not_saved_and_a_working_one_is(self) -> None:
        from src.setup import translation_setup

        settings = translation_setup.use("deepseek", out=lambda _: None)
        lines: list[str] = []
        with mock.patch.object(online, "test_connection", side_effect=online.TranslationKeyRejected("HTTP 401")):
            self.assertFalse(translation_setup.save_key(settings, "sk-wrong", out=lines.append))
        self.assertEqual(self.saved, {})
        with mock.patch.object(online, "test_connection", return_value="火箭安全降落在无人船上。"):
            self.assertTrue(translation_setup.save_key(settings, " sk-good \n", out=lines.append))
        self.assertEqual(self.saved, {"deepseek": "sk-good"})

    def test_windows_is_told_it_needs_a_model(self) -> None:
        from src.core import compat
        from src.setup import translation_setup

        lines: list[str] = []
        with mock.patch.object(compat, "WINDOWS", True):
            translation_setup.offer(interactive=False, out=lines.append)
        self.assertIn("Windows 没有", lines[0])
        self.assertTrue(any("translation use deepseek" in line for line in lines))


class WindowsWhisperTests(unittest.TestCase):
    def test_build_tags_compare_and_a_pinned_build_is_used_when_github_is_unreachable(self) -> None:
        from src.queue.engine_check import is_newer
        from src.setup import environment

        self.assertTrue(is_newer("b5454", "b5130"))
        self.assertFalse(is_newer("b5130", "b5454"))
        installed = []
        with mock.patch.object(environment, "latest_windows_build", side_effect=OSError("rate limit")), \
                mock.patch.object(environment, "_install_windows_whisper", side_effect=lambda *args: installed.append(args[:3])):
            environment._update_windows_whisper(None, lambda _: None)
            tag, commit = environment.WINDOWS_FALLBACK_BUILD
            self.assertEqual(installed, [(tag, environment.windows_build_url(tag), commit)])
            with self.assertRaises(RuntimeError):  # an existing install is kept, not replaced by the pinned one
                environment._update_windows_whisper("b5500", lambda _: None)


if __name__ == "__main__":
    unittest.main()
