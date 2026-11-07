"""A failed analysis must come back as failed, never as an 'uncertain' verdict."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

# The anthropic SDK and settings aren't needed to test the handling.
sys.modules.setdefault("anthropic", types.SimpleNamespace(AsyncAnthropic=object))
_cfg = types.ModuleType("app.config")
_cfg.get_settings = lambda: types.SimpleNamespace(ANTHROPIC_API_KEY="x", ANTHROPIC_MODEL="test-model")
_saved = sys.modules.get("app.config")
sys.modules["app.config"] = _cfg

_spec = importlib.util.spec_from_file_location(
    "claude_analysis", Path(__file__).resolve().parents[1] / "app/services/claude_analysis.py"
)
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)
if _saved is not None:
    sys.modules["app.config"] = _saved
else:
    del sys.modules["app.config"]


def _reply(text):
    async def fake(**kwargs):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    return fake


def _run(text="Soybeans, sea salt"):
    return asyncio.run(ca.analyze_ingredients(text, []))


def test_api_error_is_failed_not_uncertain():
    async def boom(**kwargs):
        raise RuntimeError("model not found")

    ca.create_message = boom
    r = _run()
    assert r["failed"] is True and "overall_status" not in r


def test_bad_json_and_bad_shape_fail():
    ca.create_message = _reply("Sorry, I can't help")
    assert _run()["failed"]
    ca.create_message = _reply('{"overall_status": "probably fine"}')
    assert _run()["failed"]


def test_empty_ingredients_fail():
    assert _run("   ")["failed"]


def test_good_reply_is_cleaned():
    ca.create_message = _reply(
        '```json\n{"overall_status": "halal", "confidence": "sure", "flagged_ingredients": [{"name": "x"}, "junk"]}\n```'
    )
    r = _run()
    assert r["overall_status"] == "halal" and r["confidence"] == "low"
    assert r["flagged_ingredients"] == [{"name": "x"}] and "failed" not in r
