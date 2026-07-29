"""Loader for the per-strategy documentation in `strategies/meta/`.

Each registered strategy may have a `meta/<name>.json` file holding the
prose that describes it: a summary, the trading logic, strengths and
weaknesses, tuned parameters and headline backtest stats.

This lived as a 945-line hardcoded array inside the dashboard's
`/strategies` React page, which meant the docs drifted from the code
they described and nothing linked a strategy's prose to its module. The
files now sit next to the strategy modules and are served by the bot's
`/strategies` endpoint, so the dashboard renders whatever the bot
actually has registered.

Metadata is optional by design: a strategy with no JSON file still
appears in the listing with its live name/symbol/timeframe, just without
prose. A new strategy is therefore never blocked on someone writing
documentation for it.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("hypertrade")

_META_DIR = Path(__file__).parent / "meta"

# Keys a metadata file may contribute. Anything else is dropped rather
# than passed through, so a stray field can't silently reach the
# dashboard.
#
# `name`, `symbol` and `timeframe` are deliberately absent: those are
# live values owned by the registered strategy object. Letting a JSON
# file supply them would let stale documentation rename or re-symbol a
# running strategy in the UI.
_ALLOWED_KEYS = frozenset(
    {"tvUrl", "summary", "logic", "strengths", "weaknesses", "params", "stats"}
)


@lru_cache(maxsize=1)
def load_all_metadata() -> dict[str, dict[str, Any]]:
    """Read every `meta/*.json` once, keyed by strategy name.

    Cached: the files ship inside the image and cannot change while the
    process runs, so re-reading them per request would be pure waste.

    A malformed or unreadable file is logged and skipped rather than
    raising — bad documentation must not stop the bot from booting and
    trading.
    """
    out: dict[str, dict[str, Any]] = {}
    if not _META_DIR.is_dir():
        logger.warning("Strategy metadata dir missing: %s", _META_DIR)
        return out

    for path in sorted(_META_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("Skipping unreadable strategy metadata: %s", path.name)
            continue
        if not isinstance(data, dict):
            logger.warning(
                "Skipping strategy metadata %s: expected an object, got %s",
                path.name,
                type(data).__name__,
            )
            continue
        # Keyed by filename — that is what pairs metadata with its
        # module, so a mismatched `name` field inside the file is
        # simply ignored.
        out[path.stem] = {k: v for k, v in data.items() if k in _ALLOWED_KEYS}
    return out


def metadata_for(name: str) -> dict[str, Any]:
    """Metadata for one strategy, or an empty dict when undocumented."""
    return load_all_metadata().get(name, {})
