import tempfile
import unittest
from pathlib import Path

from src.setup import cli
from src.setup.cloud import SetupError


class CreatorTableTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "creators.tsv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_flags_and_notes_from_a_tab_separated_export(self) -> None:
        path = self.write(
            "creator_name\tchannel_id\tenabled\ttrusted_creator\tnotes\n"
            "Example Robotics\tUCaaaaaaaaaaaaaaaaaaaaaa\tTRUE\tTRUE\tofficial demos\n"
            "Example Space\tUCbbbbbbbbbbbbbbbbbbbbbb\tFALSE\tFALSE\t\n"
        )
        rows = cli.read_creator_table(path)
        self.assertEqual(rows[0], {"creator_name": "Example Robotics", "channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
                                   "enabled": True, "trusted_creator": True, "notes": "official demos"})
        self.assertEqual((rows[1]["enabled"], rows[1]["trusted_creator"]), (False, False))

    def test_missing_columns_default_to_enabled_and_untrusted(self) -> None:
        rows = cli.read_creator_table(self.write("creator_name\tchannel_id\nExample\tUCaaaaaaaaaaaaaaaaaaaaaa\n"))
        self.assertEqual((rows[0]["enabled"], rows[0]["trusted_creator"], rows[0]["notes"]), (True, False, ""))

    def test_rejects_handles_and_tables_without_a_header(self) -> None:
        with self.assertRaises(SetupError):
            cli.read_creator_table(self.write("creator_name\tchannel_id\nExample\t@example\n"))
        with self.assertRaises(SetupError):
            cli.read_creator_table(self.write("Example\tUCaaaaaaaaaaaaaaaaaaaaaa\n"))


class ClaspProjectTest(unittest.TestCase):
    def test_reads_parent_id_written_by_clasp_2_and_3(self) -> None:
        import json
        from unittest import mock
        from src.setup import cloud

        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / ".clasp.json"
            with mock.patch.object(cloud, "CLASP_JSON", settings):
                for parent in ("sheet-1", ["sheet-1"]):
                    settings.write_text(json.dumps({"scriptId": "script-1", "parentId": parent}))
                    self.assertEqual(cloud.project_ids(), {"script_id": "script-1", "spreadsheet_id": "sheet-1"})


class SettingValueTest(unittest.TestCase):
    def test_parses_booleans_numbers_and_text(self) -> None:
        self.assertEqual([cli.parse_value(v) for v in ("true", "FALSE", "2", "0.04", "08:00", "PODCAST,AD")],
                         [True, False, 2, 0.04, "08:00", "PODCAST,AD"])

    def test_quota_estimate_grows_with_creators_and_window(self) -> None:
        settings = {"discovery_window_start": "00:00", "discovery_window_end": "08:00", "discovery_cadence_minutes": 10}
        self.assertGreater(cli.quota_estimate(settings, 30), cli.quota_estimate(settings, 10))
        self.assertLess(cli.quota_estimate(settings, 30), 10000)


if __name__ == "__main__":
    unittest.main()
