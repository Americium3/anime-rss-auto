#!/usr/bin/env python3
"""Tests: the timetable's 'now' rule must survive the reading added beside it.

Run:  python test_now_line_css.py       (stdlib unittest, reads static/index.html)

The struck rule (.now-line::after, a 1px gradient) is information and has to be
drawn. A later rule once reused the same pseudo-element for the time reading,
which replaced the gradient with a paper-coloured label in a 1px-high box. The
cascade is merged here in source order the way a browser would for plain rules.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("static") / "index.html"


def _style() -> str:
    text = HTML.read_text(encoding="utf-8")
    css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _top_level_rules(css: str):
    """(selector, body) for rules outside @-blocks, in source order."""
    out, depth, start, sel = [], 0, 0, ""
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                sel = css[start:i].strip()
                body_start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                if not sel.startswith("@"):
                    out.append((sel, css[body_start:i]))
                start = i + 1
    return out


def _merged(selector: str) -> dict:
    props: dict = {}
    for sel, body in _top_level_rules(_style()):
        if selector not in [s.strip() for s in sel.split(",")]:
            continue
        for decl in body.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                props[k.strip()] = " ".join(v.split())
    return props


class NowLineCase(unittest.TestCase):
    def test_rule_keeps_its_gradient(self):
        after = _merged(".now-line::after")
        self.assertIn("linear-gradient", after.get("background", ""))
        self.assertEqual(after.get("content"), '""')
        self.assertNotIn("position", after)

    def test_reading_is_a_child_with_its_own_box(self):
        read = _merged(".now-line .now-read")
        self.assertTrue(read, "no rule styles the now-line reading")
        self.assertNotEqual(read.get("height"), "1px")
        text = HTML.read_text(encoding="utf-8")
        self.assertIn('class="now-read"', text)


if __name__ == "__main__":
    unittest.main()
