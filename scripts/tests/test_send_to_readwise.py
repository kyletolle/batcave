"""Regression tests for send_to_readwise.py.

Covers the text-processing pipeline (frontmatter/wikilink/callout stripping,
title/slug, markdown→HTML). The Readwise API call itself is not tested — it's
a thin wrapper around requests.post and mocking our own code has low value.
"""

import textwrap
from unittest.mock import patch, MagicMock

import pytest

import send_to_readwise as str_mod


class TestStripFrontmatter:
    def test_removes_frontmatter(self):
        content = "---\ntitle: Foo\ntags: [a, b]\n---\n\n# Body\nContent here.\n"
        out = str_mod.strip_frontmatter(content)
        assert out == "# Body\nContent here.\n"

    def test_no_frontmatter_unchanged(self):
        content = "# Body\nContent here.\n"
        assert str_mod.strip_frontmatter(content) == content

    def test_empty_frontmatter(self):
        content = "---\n---\n\n# Body\n"
        out = str_mod.strip_frontmatter(content)
        assert out == "# Body\n"

    def test_unclosed_frontmatter_preserved(self):
        # No closing --- means we return content unchanged (safer than eating it)
        content = "---\ntitle: Foo\n# Body\n"
        out = str_mod.strip_frontmatter(content)
        assert out == content

    def test_does_not_strip_horizontal_rule_mid_doc(self):
        content = "# Body\n\n---\n\nMore content.\n"
        assert str_mod.strip_frontmatter(content) == content


class TestStripWikilinks:
    def test_plain_wikilink(self):
        assert str_mod.strip_wikilinks("See [[Some Note]] here.") == "See Some Note here."

    def test_display_alias(self):
        assert str_mod.strip_wikilinks("[[Real Name|Display]]") == "Display"

    def test_embed_stripped(self):
        assert str_mod.strip_wikilinks("Before ![[Embedded]] after") == "Before  after"

    def test_multiple_wikilinks(self):
        src = "[[A]] and [[B|bee]] and [[C]]"
        assert str_mod.strip_wikilinks(src) == "A and bee and C"

    def test_wikilink_with_slashes(self):
        # Obsidian allows [[folder/Note]]
        assert str_mod.strip_wikilinks("[[folder/Note]]") == "folder/Note"

    def test_no_wikilinks_unchanged(self):
        src = "Just plain markdown with a [link](http://x.com)."
        assert str_mod.strip_wikilinks(src) == src


class TestStripCalloutSyntax:
    def test_callout_with_title(self):
        src = "> [!note] Important Thing\n> body text"
        out = str_mod.strip_callout_syntax(src)
        assert out == "> **Important Thing**\n> body text"

    def test_callout_without_title(self):
        src = "> [!warning]\n> danger here"
        out = str_mod.strip_callout_syntax(src)
        assert out.startswith("> \n")  # callout marker removed

    def test_preserves_plain_blockquote(self):
        src = "> just a quote\n> line two"
        assert str_mod.strip_callout_syntax(src) == src

    def test_multiple_callouts(self):
        src = "> [!note] A\n> one\n\n> [!warning] B\n> two"
        out = str_mod.strip_callout_syntax(src)
        assert "**A**" in out
        assert "**B**" in out
        assert "[!" not in out


class TestTitleFromFilename:
    def test_strips_md_extension(self):
        assert str_mod.title_from_filename("foo.md") == "foo"

    def test_handles_path(self):
        assert str_mod.title_from_filename("/a/b/Weekly Note.md") == "Weekly Note"

    def test_no_extension(self):
        assert str_mod.title_from_filename("README") == "README"


class TestSlugFromTitle:
    def test_lowercase_and_dashes(self):
        assert str_mod.slug_from_title("Hello World") == "hello-world"

    def test_strips_punctuation(self):
        assert str_mod.slug_from_title("Hello, World!") == "hello-world"

    def test_collapses_whitespace(self):
        assert str_mod.slug_from_title("  too   much space ") == "too-much-space"

    def test_underscores_to_dashes(self):
        assert str_mod.slug_from_title("snake_case_title") == "snake-case-title"

    def test_preserves_unicode_word_chars(self):
        # Python's \w is unicode-aware by default; keep non-ASCII letters
        out = str_mod.slug_from_title("Café Paris")
        assert out == "café-paris"


class TestPrepareNote:
    def test_full_pipeline(self):
        src = textwrap.dedent("""\
            ---
            tags: [draft]
            ---

            # Body

            See [[Note|a link]] and ![[Embed]].

            > [!note] Callout
            > inside
        """)
        out = str_mod.prepare_note(src)
        assert "tags:" not in out
        assert "[[" not in out and "]]" not in out
        assert "![[Embed]]" not in out
        assert "a link" in out
        assert "[!note]" not in out
        assert "**Callout**" in out


class TestMarkdownToHtml:
    def test_wraps_body_in_html_skeleton(self):
        html = str_mod.markdown_to_html("# Title\n\nBody.", "My Title")
        assert "<!DOCTYPE html>" in html
        assert "<title>My Title</title>" in html
        assert "<h1>Title</h1>" in html
        assert "<p>Body.</p>" in html

    def test_renders_lists(self):
        html = str_mod.markdown_to_html("- a\n- b\n- c\n", "t")
        assert "<li>a</li>" in html
        assert "<li>b</li>" in html

    def test_renders_blockquote(self):
        html = str_mod.markdown_to_html("> quote", "t")
        assert "<blockquote>" in html


class TestSendToReadwise:
    """The network call itself — mocked. We're testing that we build the request correctly."""

    def _fake_response(self, ok=True, status=200, payload=None):
        resp = MagicMock()
        resp.ok = ok
        resp.status_code = status
        resp.json.return_value = payload or {"id": "doc_123", "url": "https://read.readwise.io/x"}
        return resp

    def test_posts_to_readwise_api(self):
        with patch("send_to_readwise.requests.post") as post:
            post.return_value = self._fake_response()
            result = str_mod.send_to_readwise(
                "tok", "Title", "<html></html>", tags=["t1"], url_slug="my-slug"
            )
            assert post.called
            args, kwargs = post.call_args
            assert args[0] == "https://readwise.io/api/v3/save/"
            assert kwargs["headers"]["Authorization"] == "Token tok"
            assert kwargs["json"]["title"] == "Title"
            assert kwargs["json"]["html"] == "<html></html>"
            assert kwargs["json"]["tags"] == ["t1"]
            assert kwargs["json"]["url"].endswith("/my-slug")
            assert result["id"] == "doc_123"

    def test_defaults_tags_and_slug(self):
        with patch("send_to_readwise.requests.post") as post:
            post.return_value = self._fake_response()
            str_mod.send_to_readwise("tok", "My Note Title", "<html></html>")
            _, kwargs = post.call_args
            assert kwargs["json"]["tags"] == ["vault-note"]
            assert kwargs["json"]["url"].endswith("/my-note-title")

    def test_raises_on_http_error(self):
        with patch("send_to_readwise.requests.post") as post:
            bad = MagicMock()
            bad.ok = False
            bad.status_code = 401
            bad.json.return_value = {"detail": "nope"}
            bad.raise_for_status.side_effect = Exception("401 Unauthorized")
            post.return_value = bad
            with pytest.raises(Exception):
                str_mod.send_to_readwise("tok", "t", "<html></html>")
