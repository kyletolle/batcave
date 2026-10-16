"""The read-along pane: formatted markdown with one word-span per spoken word.

Before this, the player pane was a flat wall of spans over sanitize()'s output,
so headings, lists and tables collapsed into one paragraph the moment Speak was
pressed. read_along() renders the pane and assembles the spoken text in one
pass. The contract everything downstream leans on (the client's span stamping,
_align_words, the chunk cache) is that span N is word N of the spoken text's
whitespace split — so most of these tests pin that invariant under markdown
that tempts it to break.
"""

import re

import batspeaker_core as core


def spans(html):
    return re.findall(r'<span class="w[^"]*" data-g="(\d+)">(.*?)</span>', html)


def assert_contract(md):
    html, text = core.read_along(md)
    got = spans(html)
    assert [int(g) for g, _ in got] == list(range(len(got)))
    assert len(got) == len(text.split())
    return html, text


def test_formatting_survives():
    html, _ = assert_contract("## Title\n\nA **bold** claim.\n\n- one\n- two")
    assert "<h2>" in html and "<ul>" in html and html.count("<li>") == 2
    assert "<strong>" in html


def test_code_block_shown_not_spoken():
    html, text = assert_contract("Before.\n\n```\nrm -rf nothing\n```\n\nAfter.")
    assert "rm -rf nothing" in html
    assert "rm" not in text.split()
    assert text == "Before.\n\nAfter."


def test_inline_code_is_spoken():
    _, text = assert_contract("Run `todoist list` now.")
    assert text == "Run todoist list now."


def test_em_dash_becomes_spoken_comma():
    html, text = assert_contract("one — two and three—four")
    assert "—" in html
    assert text == "one, two and three, four"


def test_glued_punctuation_rides_previous_word():
    """`**word**.` puts the period outside the span but inside the spoken word."""
    html, text = assert_contract("It is **done**. Really `x`'s fine.")
    assert text == "It is done. Really x's fine."


def test_bare_url_shown_not_spoken():
    html, text = assert_contract("See https://example.com/a now.")
    assert "https://example.com/a" in html
    assert text == "See now."


def test_links_lose_anchor_keep_words():
    html, text = assert_contract("A [good link](https://x.com) and [[Note|label]].")
    assert "<a " not in html
    assert html.count('class="w lnk"') == 2
    assert text == "A good link and label."


def test_table_has_no_pipes_and_reads_by_row():
    md = "| Cmd | Use |\n|---|--:|\n| `/wrap` | End **it** |\n| `/ping` | Check |"
    html, text = assert_contract(md)
    assert "|" not in text
    assert "<table>" in html and 'style="text-align:right"' in html
    assert text.split("\n\n") == ["Cmd, Use.", "/wrap, End it.", "/ping, Check."]


def test_paragraphs_separate_in_spoken_text():
    _, text = assert_contract("First para.\n\nSecond para.")
    assert text == "First para.\n\nSecond para."


def test_embeds_and_images_dropped():
    html, text = assert_contract("Look ![[pic.png]] and ![alt](x.png) here.")
    assert "pic" not in html and "alt" not in text
    assert text == "Look and here."


def test_emphasis_markers_not_spoken():
    _, text = assert_contract("a stray * and snake_case_name")
    assert "*" not in text and "_" not in text


def test_html_in_prose_stays_escaped():
    html, text = assert_contract("Use <placeholder> & friends.")
    assert "<placeholder>" not in html
    assert "&lt;placeholder&gt;" in html
    assert "<placeholder>" in text


def test_turn_spoken_text_matches_pane():
    turn = {"runs": [("Short narration.", True), ("## Result\n\nIt **works**.", False)]}
    html, text = core.turn_read_along(turn)
    assert core.turn_spoken_text(turn) == text == "Result\n\nIt works."
    assert len(spans(html)) == len(text.split())
