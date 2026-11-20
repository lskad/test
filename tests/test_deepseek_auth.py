import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src import deepseek_auth


class DeepSeekAuthTests(unittest.TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(sandbox.cleanup)
        home = patch.object(Path, "home", return_value=Path(sandbox.name))
        home.start()
        self.addCleanup(home.stop)
        self.target = Path(sandbox.name) / ".agent-month-lab" / "deepseek.dpapi"

    def test_absent_and_blank(self):
        self.assertEqual(deepseek_auth.load_key(), "")
        with self.assertRaises(ValueError):
            deepseek_auth.save_key(" ")
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_roundtrip_and_replace(self):
        for value in ("first-test-placeholder", "second-test-placeholder"):
            deepseek_auth.save_key(value)
            self.assertNotIn(value.encode(), self.target.read_bytes())
            self.assertEqual(deepseek_auth.load_key(), value)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_failed_replace_preserves_existing_file(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"original test bytes")
        with patch.object(deepseek_auth, "_transform", return_value=b"new test bytes"):
            with patch.object(os, "replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    deepseek_auth.save_key("test-placeholder")
        self.assertEqual(self.target.read_bytes(), b"original test bytes")
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
