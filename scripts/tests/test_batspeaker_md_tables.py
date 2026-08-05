"""Pipe tables in Bat-Speaker's reading-card renderer.

md_to_html is a hand-rolled renderer, so table support is hand-rolled too. The
failure it replaces was loud: without a table branch, every row fell through to
the paragraph fallback and got glued into one line of pipes and dashes. These
tests pin the shape that fixes it — and, just as importantly, the cases that
must NOT be read as tables.

The reader (batcave-private) renders tables through markdown-it instead; the two
renderers agree on the emitted structure (div.tbl-wrap > table > thead/tbody and
inline text-align on aligned cells) so one stylesheet shape serves both.
"""

import re

import batspeaker_core as core


def cells(html, tag):
    # The lookahead keeps `<th ...>` from also matching `<thead>`.
    return re.findall(rf"<{tag}(?=[ >])[^>]*>(.*?)</{tag}>", html, re.DOTALL)


BASIC = """| Command | Purpose | Cost |
|---------|---------|-----:|
| `/goodnight` | End-of-session **entry** | 12 |
| `/checkpoint` | Forced depth | 3 |"""


def test_renders_table_structure():
    html = core.md_to_html(BASIC)
    assert '<div class="tbl-wrap">' in html
    assert html.count("<table>") == 1
    assert "<thead>" in html and "<tbody>" in html
    assert html.count("<tr>") == 3
    assert cells(html, "th") == ["Command", "Purpose", "Cost"]


def test_no_raw_pipes_survive():
    """The regression itself: pipes and dashes must not reach the reader."""
    html = core.md_to_html(BASIC)
    assert "|" not in html
    assert "-----" not in html


def test_inline_markup_inside_cells():
    html = core.md_to_html(BASIC)
    assert "<code>/goodnight</code>" in html
    assert "<strong>entry</strong>" in html


def test_column_alignment_is_inline():
    html = core.md_to_html("| L | C | R |\n|:---|:--:|---:|\n| a | b | c |")
    assert html.count('style="text-align:left"') == 2
    assert html.count('style="text-align:center"') == 2
    assert html.count('style="text-align:right"') == 2


def test_ragged_rows_are_padded_and_truncated():
    html = core.md_to_html("| A | B | C |\n|---|---|---|\n| one |\n| a | b | c | d |")
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    for row in rows:
        assert row.count("<t") == 3  # every row lands on the header's column count
    assert "<td>d</td>" not in html


def test_escaped_pipe_stays_in_the_cell():
    html = core.md_to_html("| Col |\n|-----|\n| a \\| b |")
    assert "<td>a | b</td>" in html


def test_table_interrupts_a_paragraph():
    html = core.md_to_html("Leading prose:\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<p>Leading prose:</p>" in html
    assert '<div class="tbl-wrap">' in html


def test_rule_under_paragraph_is_not_a_table():
    """`text` / `---` has no pipe in the header — it stays a rule."""
    html = core.md_to_html("Just a paragraph\n---\nMore prose.")
    assert "<table>" not in html
    assert "<hr>" in html


def test_column_count_mismatch_is_not_a_table():
    html = core.md_to_html("| A | B |\n|---|\n| 1 | 2 |")
    assert "<table>" not in html


def test_existing_blocks_still_render():
    html = core.md_to_html("# Head\n\n- one\n- two\n\n```py\nx = 1\n```\n\n> quoted")
    assert "<h1>Head</h1>" in html
    assert html.count("<li>") == 2
    assert "<pre><code>x = 1</code></pre>" in html
    assert "<blockquote>quoted</blockquote>" in html
