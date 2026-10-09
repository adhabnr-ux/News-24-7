"""Serves the relay's installer and News247 itself straight from the running monitor, so a Mac
can be set up with one command and always gets the same version as the server."""

from __future__ import annotations

import io
import shlex
import tarfile
import time
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .. import __version__

DEPENDENCIES = [
    "aiohttp>=3.9",
    "feedparser>=6.0",
    "PyYAML>=6.0",
    "tzdata>=2024.1",
    "cryptography>=41",
    "pg8000>=1.31",
    "segno>=1.5",
]

PYPROJECT = f"""[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "news247"
version = "{__version__}"
description = "News247 market-news monitor and iMessage relay"
requires-python = ">=3.10"
dependencies = {DEPENDENCIES!r}

[project.scripts]
news247 = "news247.cli:main"

[tool.setuptools.packages.find]
include = ["news247*"]

[tool.setuptools.package-data]
news247 = ["web/static/*", "web/static/app/*", "web/static/app/fonts/*", "data/*.yaml", "relay/*.sh"]
""".replace("'", '"')


@lru_cache(maxsize=1)
def source_tarball() -> bytes:
    """A pip-installable sdist of the News247 package this server is running."""
    root = Path(str(resources.files("news247")))
    base = f"news247-{__version__}"
    buf = io.BytesIO()
    now = time.time()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add_bytes(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(f"{base}/{name}")
            info.size, info.mtime, info.mode = len(data), now, 0o644
            tar.addfile(info, io.BytesIO(data))

        add_bytes("pyproject.toml", PYPROJECT.encode())
        add_bytes("README.md", b"News247 - see https://github.com/adhabnr-ux/News-24-7\n")
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if path.is_dir() or "__pycache__" in rel.parts or path.suffix in (".pyc", ".pyo"):
                continue
            add_bytes(f"news247/{rel.as_posix()}", path.read_bytes())
    return buf.getvalue()


def install_script(server: str, secret: str, package_url: str) -> str:
    """The installer with this monitor's address and relay secret filled in."""
    body = resources.files("news247.relay").joinpath("install_relay.sh").read_text(encoding="utf-8")
    preset = (
        f"N247_SERVER={shlex.quote(server)}\n"
        f"N247_SECRET={shlex.quote(secret)}\n"
        f"N247_PACKAGE_URL={shlex.quote(package_url)}\n"
    )
    shebang, rest = body.split("\n", 1)
    return f"{shebang}\n# Filled in by your News247 monitor — keep this private, it contains your relay secret.\n{preset}{rest}"
