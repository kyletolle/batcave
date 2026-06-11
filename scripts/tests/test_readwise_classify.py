"""Regression tests for readwise_classify.py.

Focus: the classifier. The TOPICS regex patterns drift easily — a tweak to
match one title can accidentally swallow others. These tests pin down the
obvious positives and, where the rules are ambiguous (AI vs software,
politics vs news), document current behavior.
"""

import pytest

import readwise_classify as rc


def doc(title="", summary="", author="", category="article", tags=None, doc_id="x"):
    """Build a minimal Readwise doc dict matching the API response shape."""
    return {
        "id": doc_id,
        "title": title,
        "summary": summary,
        "author": author,
        "category": category,
        "tags": {t: {} for t in (tags or [])},
    }


# ---- Topic regex patterns ----

class TestTopicRegexes:
    """Spot-check each topic with representative positive matches.
    Negative cases checked in TestClassifyRouting."""

    @pytest.mark.parametrize("text", [
        "What GPT-5 means for knowledge workers",
        "Claude Code vs Cursor",
        "How to do prompt engineering well",
        "RAG pipelines with vector databases",
        "Anthropic's new model",
        "vibe coding with AI agents",
    ])
    def test_ai_matches(self, text):
        assert rc.TOPICS["topic-ai"].search(text)

    @pytest.mark.parametrize("text", [
        "Why we refactored our monolith",
        "Kubernetes best practices",
        "A staff engineer's guide to system design",
        "TypeScript patterns for React",
        "Postgres full-text search",
    ])
    def test_software_matches(self, text):
        assert rc.TOPICS["topic-software"].search(text)

    @pytest.mark.parametrize("text", [
        "Writing craft: dialogue and subtext",
        "World-building advice for fantasy novelists",
        "Grimdark and cosmic horror",
        "The writer who couldn't outline",
    ])
    def test_writing_matches(self, text):
        assert rc.TOPICS["topic-writing"].search(text)

    @pytest.mark.parametrize("text", [
        "Enshittification is not slowing",
        "Cory Doctorow on monopoly power",
        "Union organizing strikes back",
        "Wealth concentration in the top 1%",
    ])
    def test_politics_matches(self, text):
        assert rc.TOPICS["topic-politics"].search(text)

    @pytest.mark.parametrize("text", [
        "Obsidian for note-taking",
        "Zettelkasten and second brain",
        "Deep work and time blocking",
        "My Readwise workflow",
    ])
    def test_productivity_matches(self, text):
        assert rc.TOPICS["topic-productivity"].search(text)

    @pytest.mark.parametrize("text", [
        "Parental leave and postpartum sleep",
        "Daycare drop-off strategies",
        "Work-life balance as a father",
    ])
    def test_parenting_matches(self, text):
        assert rc.TOPICS["topic-parenting"].search(text)

    @pytest.mark.parametrize("text", [
        "Warhammer 40k painting guide",
        "Open-world RPG design",
        "Tabletop D&D session prep",
    ])
    def test_gaming_matches(self, text):
        assert rc.TOPICS["topic-gaming"].search(text)


# ---- classify() routing ----

class TestClassifyRouting:
    def test_skips_bruce_authored_by_author(self):
        docs = [doc(title="Weekly Review 2026-W15", author="Bruce in the Batcave")]
        results, skipped_bruce, _ = rc.classify(docs)
        assert skipped_bruce == 1
        assert all(len(items) == 0 for items in results.values())

    def test_skips_bruce_authored_by_tag(self):
        docs = [doc(title="Some Note", tags=["bruce"])]
        _, skipped_bruce, _ = rc.classify(docs)
        assert skipped_bruce == 1

    @pytest.mark.parametrize("bruce_tag", [
        "weekly-review", "boabw", "ai-distilled", "bruce-panel", "morning-brief",
    ])
    def test_skips_known_bruce_tags(self, bruce_tag):
        docs = [doc(title="...", tags=[bruce_tag])]
        _, skipped_bruce, _ = rc.classify(docs)
        assert skipped_bruce == 1

    def test_skips_already_tagged_by_default(self):
        docs = [doc(title="AI paper", tags=["topic-ai"])]
        _, _, skipped_tagged = rc.classify(docs)
        assert skipped_tagged == 1

    def test_reclassifies_when_all_flag_set(self):
        docs = [doc(title="AI paper", tags=["topic-ai"])]
        results, _, skipped_tagged = rc.classify(docs, reclassify_all=True)
        assert skipped_tagged == 0
        assert any(d["id"] == "x" for d in results["topic-ai"])

    def test_routes_epub_to_books(self):
        docs = [doc(title="The Hobbit", category="epub")]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-books"]) == 1
        assert results["topic-books"][0]["title"] == "The Hobbit"

    def test_routes_pdf_to_books(self):
        docs = [doc(title="Some Paper", category="pdf")]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-books"]) == 1

    def test_routes_video_to_video(self):
        docs = [doc(title="A talk", category="video")]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-video"]) == 1

    def test_category_routing_beats_regex(self):
        """A video titled 'AI safety talk' routes to topic-video, not topic-ai."""
        docs = [doc(title="AI safety talk", category="video")]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-video"]) == 1
        assert len(results["topic-ai"]) == 0

    def test_regex_classification_via_title(self):
        docs = [doc(title="GPT-5 changes everything", category="article")]
        results, _, _ = rc.classify(docs)
        assert any(d["id"] == "x" for d in results["topic-ai"])

    def test_regex_classification_via_summary(self):
        docs = [doc(
            title="Untitled piece",
            summary="A deep dive on kubernetes and microservice architecture",
            category="article",
        )]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-software"]) == 1

    def test_no_match_goes_to_misc(self):
        docs = [doc(title="Absolutely nothing relevant here", category="article")]
        results, _, _ = rc.classify(docs)
        assert len(results["topic-misc"]) == 1

    def test_first_match_wins(self):
        """A title matching multiple topics lands in whichever comes first in
        TOPICS dict insertion order. Lock this so order changes are explicit."""
        # "Claude" matches topic-ai; TOPICS is ordered with topic-ai first.
        docs = [doc(title="Claude for programming", category="article")]
        results, _, _ = rc.classify(docs)
        # topic-ai matches first (earlier in dict)
        assert len(results["topic-ai"]) == 1
        assert len(results["topic-software"]) == 0

    def test_entry_includes_id_title_existing_tags(self):
        docs = [doc(title="AI paper", doc_id="abc123", tags=["already"])]
        results, _, _ = rc.classify(docs)
        entry = results["topic-ai"][0]
        assert entry == {"id": "abc123", "title": "AI paper", "existing_tags": ["already"]}

    def test_handles_none_fields_gracefully(self):
        # Readwise sometimes returns title=None / summary=None — classify must not crash
        docs = [{
            "id": "x", "title": None, "summary": None, "author": None,
            "category": "article", "tags": {},
        }]
        results, _, _ = rc.classify(docs)
        # Empty-text doc falls through to misc
        assert len(results["topic-misc"]) == 1

    def test_handles_list_tags_shape(self):
        # Some responses return tags as a list, not a dict — classify guards for this
        docs = [{
            "id": "x", "title": "Something", "summary": "", "author": "",
            "category": "article", "tags": ["not-a-dict"],
        }]
        # Does not crash; lands in misc (nothing matches)
        results, _, _ = rc.classify(docs)
        assert len(results["topic-misc"]) == 1

    def test_empty_input_returns_empty_buckets(self):
        results, skipped_bruce, skipped_tagged = rc.classify([])
        assert skipped_bruce == 0
        assert skipped_tagged == 0
        assert all(len(v) == 0 for v in results.values())
        # All known topic keys are present even when empty
        assert "topic-misc" in results
        assert "topic-books" in results
        assert "topic-video" in results


# ---- False-positive guards (things that should NOT match) ----

class TestFalsePositives:
    """Cases where a topic regex could over-reach. Pin them down."""

    def test_generic_word_alone_does_not_trigger_ai(self):
        # Plain "ai" should match (it's a word boundary), but unrelated context
        # like Thai food shouldn't produce a surprise match — the \b guards handle this.
        assert not rc.TOPICS["topic-ai"].search("Thai food recipes")
        assert not rc.TOPICS["topic-ai"].search("Pair programming tips")

    def test_plain_business_content_not_in_politics(self):
        # "capital" alone shouldn't fire topic-politics — the regex asks for 'capitalism'
        text = "Capital gains tax for individuals"
        assert not rc.TOPICS["topic-politics"].search(text)

    def test_plain_sleep_mention_not_in_health(self):
        # topic-health's sleep pattern requires a qualifier (quality/hygiene/debt/etc.)
        assert not rc.TOPICS["topic-health"].search("I slept in late today")
