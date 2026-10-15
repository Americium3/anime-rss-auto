#!/usr/bin/env python3
"""Tests: a config.local.json that exists must never be swapped for defaults.

Run:  python test_config_load.py         (stdlib unittest, no network, no qB)

The built-in defaults are destructive (drop_deletes_files=True, no user pins, the
default old-season line). Falling back to {} when the user's file fails to parse
silently re-enables file deletion, so a broken file has to stop the process loudly.
"""
from __future__ import annotations

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import anime_rss as core


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.local.json"
        self._patch = mock.patch.object(core, "CONFIG_PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_missing_file_gives_empty_config(self):
        self.assertEqual(core.load_config(), {})

    def test_utf8_bom_is_accepted(self):
        # Notepad's "UTF-8 with BOM" option writes this prefix.
        self.path.write_bytes(b"\xef\xbb\xbf" + b'{"drop_deletes_files": false}')
        self.assertEqual(core.load_config(), {"drop_deletes_files": False})

    def test_syntax_error_aborts_instead_of_using_defaults(self):
        self.path.write_text('{"drop_deletes_files": false,}', encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            core.load_config()
        self.assertIn(str(self.path), str(cm.exception.code))


if __name__ == "__main__":
    unittest.main(verbosity=2)
