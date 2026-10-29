"""Shared test setup: never touch the real recommendations cache under data/cache/."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_recommendations_cache(tmp_path, monkeypatch):
    from cvgen import gaps
    monkeypatch.setattr(gaps, "RECOMMENDATIONS_CACHE", tmp_path / "recommendations_cache.json")
