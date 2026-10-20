"""The app shell's own invariants: every element id is unique (two boards sharing an id write into
the same element), and every id the script looks up exists."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "news247" / "web" / "static" / "app"


def test_every_id_in_the_shell_is_unique():
    ids = re.findall(r'\bid="([^"]+)"', (APP / "index.html").read_text())
    dupes = sorted(k for k, n in Counter(ids).items() if n > 1)
    assert dupes == []


def test_the_boards_the_script_fills_exist():
    html = set(re.findall(r'\bid="([^"]+)"', (APP / "index.html").read_text()))
    js = (APP / "app.js").read_text()
    # ids generated in the script itself (templates) count too
    html |= set(re.findall(r'\bid="([A-Za-z][\w-]*)"', js))
    wanted = set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', js))
    missing = sorted(wanted - html)
    assert missing == []
