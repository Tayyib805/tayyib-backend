"""Unit tests for article and comment text helpers."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "article_text", Path(__file__).resolve().parents[1] / "app/services/article_text.py"
)
t = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t)


def test_slugify():
    assert t.slugify("E471: Mono- & Diglycerides") == "e471-mono-diglycerides"
    assert t.slugify("Gélatine — what to know") == "gelatine-what-to-know"
    assert t.slugify("!!!") == "article"


def test_read_minutes():
    assert t.read_minutes("") == 1
    assert t.read_minutes("word " * 660) == 3


def test_clean_comment_strips_invisible_characters_and_blank_runs():
    assert t.clean_comment("  hi​ there\r\n\r\n\r\n\r\nok  ") == "hi there\n\nok"


def test_comment_problems():
    assert t.comment_problem("a") is not None
    assert t.comment_problem("x" * 1001) is not None
    assert t.comment_problem("see https://a.com and https://b.com") is not None
    assert t.comment_problem("THIS IS ALL SHOUTING HERE") is not None
    assert t.comment_problem("Jazak Allah khair, this helped.") is None
    assert t.comment_problem("Source: https://example.org/ruling") is None


def test_public_name_never_leaks_email():
    assert t.public_name("jdarw002", "jdarw002@gmail.com") == "tayyib. member"
    assert t.public_name("someone@mail.com", "x@y.com") == "tayyib. member"
    assert t.public_name(None, "x@y.com") == "tayyib. member"
    assert t.public_name("Yusuf A.", "y@y.com") == "Yusuf A."


def test_clean_display_name():
    assert t.clean_display_name("  Amina   K ") == "Amina K"
    assert t.clean_display_name("a") is None
    assert t.clean_display_name("visit www.spam.com") is None


def test_clean_sources():
    sources = [
        {"title": "Ruling", "url": "https://example.org/r"},
        {"title": "", "url": "https://example.org"},
        {"title": "Bad", "url": "javascript:alert(1)"},
        "nope",
    ]
    assert t.clean_sources(sources) == [{"title": "Ruling", "url": "https://example.org/r"}]
