"""Which build is running.

`bot/Dockerfile` writes `build-info.json` next to the `hypertrade`
package at image build time: the `GIT_SHA` build arg (passed through
from the deploy environment by `docker-compose.yml`) and the UTC time
the image was built. Nothing here can tell a stale image from a fresh
one on its own; it only reports what the image says about itself, so
`GET /api/version` can be compared with `git rev-parse origin/master`.

Outside an image (local runs, tests) the file is absent and both
fields read as unknown rather than failing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict

BUILD_INFO_PATH = Path(__file__).resolve().parent.parent / "build-info.json"

UNKNOWN_SHA = "unknown"

# A git object name, abbreviated or full. Anything else — the compose
# default "unknown", an empty arg, a stray quote that broke the JSON —
# reports as unknown instead of echoing arbitrary text on an
# unauthenticated endpoint.
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_BUILT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class BuildInfo(TypedDict):
    sha: str
    built_at: str | None


def load_build_info(path: Path | None = None) -> BuildInfo:
    """Read and validate the baked-in build info; never raises."""
    try:
        raw = json.loads((path or BUILD_INFO_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    sha = raw.get("sha")
    sha = sha.lower() if isinstance(sha, str) else ""
    built_at = raw.get("built_at")
    return {
        "sha": sha if _SHA_RE.fullmatch(sha) else UNKNOWN_SHA,
        "built_at": (
            built_at
            if isinstance(built_at, str) and _BUILT_AT_RE.fullmatch(built_at)
            else None
        ),
    }
