"""The server decides which design the app shows, so going back needs no new build."""

import asyncio
import types

import pytest

from app.config import design_version
from app.routers import platform


def config_with(monkeypatch, **over):
    base = dict(
        MAP_TILE_URL="", MAP_TILE_URL_DARK="", MAP_TILE_ATTRIBUTION="", APPLE_SIGN_IN_ENABLED=False,
        SUBSCRIPTIONS_OPEN=False, REVENUECAT_PUBLIC_WEB_KEY="",
        REVENUECAT_ENTITLEMENT_STANDARD="standard", REVENUECAT_ENTITLEMENT_PRO="pro",
    )
    monkeypatch.setattr(platform, "get_settings", lambda: types.SimpleNamespace(**{**base, **over}))
    return asyncio.run(platform.app_config())


def test_the_new_design_is_served_unless_told_otherwise(monkeypatch):
    assert config_with(monkeypatch)["design"] == {"version": "new"}
    assert config_with(monkeypatch, APP_DESIGN="new")["design"] == {"version": "new"}


def test_setting_classic_brings_the_original_design_back(monkeypatch):
    assert config_with(monkeypatch, APP_DESIGN="classic")["design"] == {"version": "classic"}


@pytest.mark.parametrize("typed, served", [(" Classic ", "classic"), ("CLASSIC", "classic"), ("New", "new"), ("", "new"), (None, "new"), ("clasic", "new"), ("old", "new")])
def test_spacing_capitals_and_typos_never_break_the_app(typed, served):
    assert design_version(typed) == served


def test_the_rest_of_the_config_is_untouched(monkeypatch):
    out = config_with(monkeypatch, APP_DESIGN="classic")
    assert set(out) == {"map", "design", "sign_in", "subscriptions"}
    assert out["sign_in"] == {"apple": False}
