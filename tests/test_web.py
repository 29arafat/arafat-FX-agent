"""PWA ফাইলের কাঠামো: ম্যানিফেস্ট, আইকন, Service Worker-এর তালিকা, নিরাপত্তা। (ব্রাউজার টেস্ট এখানে নয়, তা হাতে চালানো হয়েছে)"""
import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_manifest_valid_and_icons_exist():
    m = json.loads(read("manifest.webmanifest"))
    assert m["display"] == "standalone" and m["lang"] == "bn" and m["start_url"] == "./"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i.get("purpose") == "maskable" for i in m["icons"])
    for i in m["icons"]:
        assert (WEB / i["src"]).stat().st_size > 500, i["src"]


def test_index_references_exist():
    html = read("index.html")
    assert 'lang="bn"' in html and "viewport-fit=cover" in html and 'rel="manifest"' in html
    for ref in re.findall(r'(?:src|href)="([^"#][^"]*)"', html):
        if ref.startswith(("http", "#")):
            continue
        assert (WEB / ref).exists(), f"{ref} নেই"


def test_service_worker_shell_files_exist():
    sw = read("sw.js")
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    files = re.findall(r'"([^"]+)"', shell)
    assert len(files) >= 8
    for f in files:
        assert f == "./" or (WEB / f).exists(), f"{f} নেই"
    assert 'VERSION = "fx-' in sw


def test_app_is_safe_and_uses_no_secrets():
    js = read("app.js") + read("config.js") + read("sw.js")
    assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{20,}", js)
    assert "raw.githubusercontent.com/29arafat/arafat-FX-agent/data/" in read("config.js")
    for k in ("TOKEN", "API_KEY", "PRICE_API"):
        assert k not in js


def test_headers_file_has_sw_no_cache():
    h = read("_headers")
    assert "/sw.js" in h and "no-cache" in h


def test_app_uses_saudi_time_and_decimals_for_all_pairs():
    import yaml
    cfg = yaml.safe_load((WEB.parent / "config.yaml").read_text(encoding="utf-8"))
    c = read("config.js")
    assert "utcOffsetHours: 3" in c and cfg["notify"]["display_utc_offset_hours"] == 3
    for pair, v in cfg["pairs"].items():
        assert re.search(rf"{pair}: {v['decimals']}\b", c), f"{pair} decimals মেলেনি"


def test_not_indexed_by_search_engines():
    assert 'name="robots" content="noindex' in read("index.html")
    assert "X-Robots-Tag: noindex" in read("_headers")
