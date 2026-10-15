import json
import pathlib
import re
import shutil
import subprocess

import pytest


HTML = pathlib.Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _js_function(name):
    # Top-level functions in index.html start at column 0 and end at the first "}" on its own line.
    m = re.search(r"^function " + name + r"\(.*?^\}$", HTML, re.S | re.M)
    assert m, name
    return m.group(0)


def _run(status, body):
    script = "\n".join([
        "const I18N = {en: {mikan_fetch_failed: 'Mikan fetch failed'}}; const lang = 'en';",
        "const t = k => I18N.en[k];",
        "let calls = 0;",
        "globalThis.fetch = async () => { calls++; return {ok: %s, status: %d, json: async () => (%s)}; };"
        % ("true" if 200 <= status < 300 else "false", status, json.dumps(body)),
        "const gpickCache = new Map();",
        _js_function("errText"),
        _js_function("subgroupsFor"),
        "(async () => {",
        "  const out = {};",
        "  try { out.first = await subgroupsFor(7); } catch (e) { out.error = e.message; }",
        "  await new Promise(r => setTimeout(r, 0));",
        "  out.cached = gpickCache.has(7);",
        "  try { await subgroupsFor(7); } catch (e) {}",
        "  out.calls = calls;",
        "  console.log(JSON.stringify(out));",
        "})();",
    ])
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_failed_subgroups_fetch_rejects_and_is_not_cached():
    out = _run(502, {"detail": {"code": "mikan_fetch_failed", "message": "timeout"}})
    assert out.get("error") == "Mikan fetch failed: timeout"
    assert out["cached"] is False
    assert out["calls"] == 2   # the next open fetches again


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_good_subgroups_fetch_stays_cached():
    out = _run(200, {"subgroups": [{"id": 370, "name": "LoliHouse"}]})
    assert out["first"] == [{"id": 370, "name": "LoliHouse"}]
    assert out["cached"] is True
    assert out["calls"] == 1
