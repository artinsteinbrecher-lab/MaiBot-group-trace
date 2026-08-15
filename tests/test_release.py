from __future__ import annotations

from unittest import TestCase

from scripts.build_release import collect_release_files, validate_release_files


class ReleaseTests(TestCase):
    def test_release_file_set_is_complete_and_private_data_free(self) -> None:
        files = collect_release_files()
        validate_release_files(files)
        names = {path.name for path in files}
        self.assertIn("plugin.py", names)
        self.assertIn("_manifest.json", names)
        self.assertNotIn("config.toml", names)
        self.assertNotIn("group_trace.sqlite3", names)
