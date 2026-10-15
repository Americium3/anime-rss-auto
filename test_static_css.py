#!/usr/bin/env python3
"""Tests: the panel stylesheet in static/index.html must parse as intended.

Run:  python test_static_css.py         (stdlib unittest, no browser)

A comment that closes early leaves its remaining prose as bare text, which the
browser folds into the next rule's selector list; one bad selector drops the
whole rule silently, so nothing in the page looks broken except the styling.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _css_without_comments() -> str:
    html = INDEX.read_text(encoding="utf-8")
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


class StylesheetCase(unittest.TestCase):
    def test_no_stray_comment_terminator(self):
        css = _css_without_comments()
        self.assertEqual(css.count("*/"), 0, "a comment closes early and leaves prose as CSS")

    def test_glass_rule_selector_is_clean(self):
        css = _css_without_comments()
        m = re.search(r"([^{}]*)\{\s*-webkit-backdrop-filter:\s*blur\(var\(--glass-blur\)\)", css)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).strip().startswith(".card, .deskpanel"), m.group(1).strip()[:120])


if __name__ == "__main__":
    unittest.main()
