# The panel's JavaScript cannot be run here (no Node), so these checks read
# the source of static/index.html.
import pathlib
import re

HTML = (pathlib.Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def _function_body(name):
    start = HTML.index(f"function {name}(")
    end = HTML.index("\n}\n", start)
    return HTML[start:end]


def test_reopen_never_shows_another_shows_options_while_fetch_is_pending():
    # The cache holds a pending promise, so a cache check is true while show
    # A's read is still out. If the loading placeholder is gated on it, a
    # reopen of A shows show B's buttons (switchGroup(B, ...)) until A lands.
    body = _function_body("toggleGroupPicker")
    before_await = body[:body.index("await subgroupsFor(")]
    writes = [l for l in before_await.splitlines() if "list.innerHTML" in l]
    assert writes, "the list must be cleared before waiting for the subgroups"
    assert not any(re.search(r"\bif\s*\(", l) for l in writes), writes
