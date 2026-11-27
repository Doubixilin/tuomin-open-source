"""Brand assets ship with the page and the bundle."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "tuomin_gateway" / "service" / "static"
ICON = ROOT / "packaging" / "icon"


def test_logo_is_served_and_referenced_by_the_page():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert (STATIC / "tuomin-logo.svg").read_text(encoding="utf-8").startswith("<svg")
    assert 'href="./tuomin-logo.svg"' in html and 'src="./tuomin-logo.svg"' in html


def test_bundle_icon_assets_exist_and_match_the_master_svg():
    assert (ICON / "tuomin.svg").is_file() and (ICON / "menubar.svg").is_file()
    assert (ICON / "Tuomin.icns").stat().st_size > 10_000
    for name in ("menubar.png", "menubar@2x.png", "tuomin-1024.png"):
        assert (ICON / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name
    spec = (ROOT / "packaging" / "tuomin_gateway.spec").read_text(encoding="utf-8")
    assert 'icon=str(project_root / "packaging/icon/Tuomin.icns")' in spec
    assert (STATIC / "tuomin-logo.svg").read_bytes() == (ICON / "tuomin.svg").read_bytes()
