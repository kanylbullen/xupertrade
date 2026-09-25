"""Suite-wide test fixtures.

Two things every test gets, both about keeping the CI gate fast and
deterministic rather than about any one module:

* **No tenacity backoff.** `fetch_candles` and
  `HyperLiquidExchange._run_with_retry` retry with
  `wait_exponential(min=1, max=4)`, so every test that drives a read
  through its failure path used to sleep 1 s + 2 s of wall clock.
  The retry *logic* (attempt count, which errors retry, reraise) still
  runs unchanged; only the wait between attempts becomes zero.

* **A derandomised hypothesis profile for CI.** `HYPOTHESIS_PROFILE=ci`
  (set by `.github/workflows/ci.yml`) seeds hypothesis from each test's
  own source, so a required merge check tests the same examples on
  every run instead of turning red on a PR that did not touch the code
  a new random example happened to break. Local runs keep the default
  random profile, which is where new counterexamples should be found.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import settings as hypothesis_settings
from tenacity import wait_exponential

hypothesis_settings.register_profile(
    "ci",
    derandomize=True,
    # A derandomised run replays nothing it did not generate itself,
    # and parallel xdist workers writing one example database is churn.
    database=None,
    print_blob=True,
)
hypothesis_settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


def _no_wait(self, retry_state) -> float:  # noqa: ARG001 — tenacity signature
    return 0.0


@pytest.fixture(autouse=True)
def _tenacity_no_wait(monkeypatch):
    """Zero every `wait_exponential` backoff for the duration of a test.

    Patched on the class, not per call site: both production users
    build a fresh `wait_exponential(...)` inside the function, so there
    is no module attribute to swap, and a class-level patch also covers
    any call site added later.
    """
    monkeypatch.setattr(wait_exponential, "__call__", _no_wait)
