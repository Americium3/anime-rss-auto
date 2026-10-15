#!/usr/bin/env python3
"""Tests: the panel's toast queue must never drop or duplicate a message.

Run:  python test_toast_queue.py        (needs node on PATH, otherwise skipped)

The toast code in static/index.html is cut out verbatim and run under node with
a fake element and fake timers, so these cases exercise the shipped code. The
window that matters is the 260ms fade between two toasts: the old text is still
in the element but no longer on screen, and the next message is already off the
queue, held only by the pending timer.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

INDEX = Path(__file__).with_name("static") / "index.html"
NODE = shutil.which("node")

HARNESS = r"""
let now = 0, timers = [], nextId = 1;
function setTimeout(fn, ms) { const id = nextId++; timers.push({id, at: now + ms, fn}); return id; }
function clearTimeout(id) { timers = timers.filter(t => t.id !== id); }
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > end) break;
    timers.shift(); now = t.at; t.fn();
  }
  now = end;
}
const shown = [];
const cls = new Set();
const el = {
  textContent: "",
  classList: {
    add(c) { if (c === "show" && !cls.has(c)) shown.push(el.textContent); cls.add(c); },
    remove(c) { cls.delete(c); },
    contains(c) { return cls.has(c); },
  },
};
function $(id) { return el; }
%CODE%
const steps = %STEPS%;
for (const s of steps) { if (typeof s === "number") advance(s); else toast(s); }
console.log(JSON.stringify(shown));
"""


def _toast_code() -> str:
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(r"const toastQ = \[\];.*?\nfunction nextToast\(\) \{.*?\n\}\n", src, re.S)
    assert m, "toast code not found in index.html"
    return m.group(0)


@unittest.skipUnless(NODE, "node not on PATH")
class ToastQueueTests(unittest.TestCase):

    def run_steps(self, steps: list) -> list[str]:
        js = HARNESS.replace("%CODE%", _toast_code()).replace("%STEPS%", json.dumps(steps))
        out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_plain_queue_shows_both(self) -> None:
        self.assertEqual(self.run_steps(["A", "B", 20000]), ["A", "B"])

    def test_repeat_while_on_screen_is_not_queued(self) -> None:
        self.assertEqual(self.run_steps(["A", 1000, "A", 20000]), ["A"])

    def test_repeat_of_outgoing_during_fade_keeps_queued_toast(self) -> None:
        # A is fading out and B sits in the 260ms timer; a repeat of A must not
        # cancel that timer.
        shown = self.run_steps(["A", "B", 3500, 100, "A", 20000])
        self.assertIn("B", shown)
        self.assertEqual(shown[:2], ["A", "B"])

    def test_repeat_of_incoming_during_fade_is_not_queued_twice(self) -> None:
        shown = self.run_steps(["A", "B", 3500, 100, "B", 20000])
        self.assertEqual(shown, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
