"""Matching a customer question to a help article, and knowing when we cannot."""

import os

import pytest

needs_key = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"),
    reason="semantic search calls the embeddings API; keyword path is covered below",
)


@pytest.mark.parametrize("message,topic", [
    ("I want a refund for my order",              "refund"),
    ("my recharge failed",                        "recharge"),
    ("my internet is not working",                "network"),
])
def test_keyword_matcher_handles_our_own_vocabulary(core, message, topic):
    assert core.keyword_kb_article(message)["topic"] == topic


def test_keyword_matcher_reports_how_it_matched(core):
    assert core.keyword_kb_article("refund please")["how"] == "keyword"


def test_keyword_matcher_gives_up_on_paraphrase(core):
    """The limitation that motivated semantic search: this is a refund question
    with not one word in common with the refund article."""
    assert core.keyword_kb_article("kab tak paisa wapas milega") is None


def test_no_article_for_something_we_never_documented(core):
    assert core.keyword_kb_article("Do you sponsor cricket teams?") is None


def test_knowledge_gap_is_flagged(core):
    assert core.knowledge_gap_detector("Do you sponsor cricket teams?") is True
    assert core.knowledge_gap_detector("I need a refund") is False


def test_gap_needs_both_matchers_to_miss(srv):
    """A gap means the knowledge base AND the FAQ list both had nothing."""
    assert srv.is_knowledge_gap("mera recharge nahi hua") is False
    assert srv.is_knowledge_gap("Can I get a GST invoice?") is True


def test_cosine_is_sane(core):
    assert core.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert core.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert core.cosine([0, 0], [1, 0]) == 0.0          # no divide-by-zero


@needs_key
def test_semantic_search_beats_keywords_on_paraphrase(core):
    match = core.find_kb_article("kab tak paisa wapas milega")
    assert match is not None
    assert match["topic"] == "refund"
    assert match["how"] == "semantic"


@needs_key
def test_semantic_search_still_declines_the_undocumented(core):
    assert core.find_kb_article("Do you sponsor cricket teams?") is None
