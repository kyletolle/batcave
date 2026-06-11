"""Regression tests for send_ai_note.py.

The critical function is `extract_model_responses` — if it regresses, the
weekly Readwise AI note gets polluted with the review prompt and model
list instead of just the model responses.
"""

import textwrap

import send_ai_note as san


# A minimal AI note. The real template has placeholder H1s under "# Model Names"
# that the script also keeps (they get duplicated above the real responses); that
# quirk is covered by test_placeholder_h1s_under_model_names_are_kept below.
SAMPLE_AI_NOTE = textwrap.dedent("""\
    ---
    tags: [weekly-review]
    ---

    # Models to Use

    - Gpt-5.4
    - Claude-opus-4.6

    # Review Prompt

    ```
    You are reviewing Kyle's week.

    FYI 2026-W15 is the current week and 2026-W16 is the coming week.
    ```

    # Bruce (Opus 4.6 via Claude Code)

    ## Highlights

    Bruce response body.

    # Gpt-5.4

    ## Highlights

    GPT response body.

    # Claude-opus-4.6

    ## Highlights

    Claude response body.
""")


class TestExtractModelResponses:
    def test_returns_week_label(self):
        week, _ = san.extract_model_responses(SAMPLE_AI_NOTE)
        assert week == "2026-W15"

    def test_default_week_label_when_fyi_missing(self):
        content = "# Bruce\n\nresponse\n"
        week, _ = san.extract_model_responses(content)
        assert week == "Weekly Review"

    def test_strips_models_to_use_section(self):
        _, body = san.extract_model_responses(SAMPLE_AI_NOTE)
        assert "Models to Use" not in body
        assert "- Gpt-5.4" not in body  # the bullet list under it

    def test_strips_review_prompt_section(self):
        _, body = san.extract_model_responses(SAMPLE_AI_NOTE)
        assert "Review Prompt" not in body
        assert "You are reviewing" not in body
        assert "FYI 2026-W15" not in body

    def test_keeps_model_responses(self):
        _, body = san.extract_model_responses(SAMPLE_AI_NOTE)
        assert "# Bruce (Opus 4.6 via Claude Code)" in body
        assert "Bruce response body." in body
        assert "GPT response body." in body
        assert "Claude response body." in body

    def test_separates_models_with_hr(self):
        _, body = san.extract_model_responses(SAMPLE_AI_NOTE)
        # Between model headings we expect a --- separator
        bruce_idx = body.index("# Bruce (Opus")
        gpt_idx = body.index("# Gpt-5.4")
        between = body[bruce_idx:gpt_idx]
        assert "---" in between

    def test_first_model_has_no_leading_separator(self):
        _, body = san.extract_model_responses(SAMPLE_AI_NOTE)
        # Strip wikilink pass doesn't change this — body starts with the first model H1
        stripped = body.lstrip("\n")
        assert stripped.startswith("# Bruce")

    def test_wikilinks_stripped_from_output(self):
        content = textwrap.dedent("""\
            # Review Prompt

            FYI 2026-W15 is the current week and 2026-W16 is the coming week.

            # Bruce

            See [[Some Note]] and [[Other|alias]] for context.
        """)
        _, body = san.extract_model_responses(content)
        assert "[[" not in body
        assert "Some Note" in body
        assert "alias" in body

    def test_code_block_h1_not_treated_as_heading(self):
        """An H1-looking line inside a code block must not end a section."""
        content = textwrap.dedent("""\
            # Review Prompt

            FYI 2026-W15 is the current week and 2026-W16 is the coming week.

            Here is a sample:

            ```markdown
            # This is not a real heading
            just prose inside a code block
            ```

            # Real Model Response

            Body text.
        """)
        _, body = san.extract_model_responses(content)
        # The code-block content should NOT appear in body (it was inside Review Prompt section)
        assert "This is not a real heading" not in body
        assert "# Real Model Response" in body
        assert "Body text." in body

    def test_code_block_inside_model_response_preserved(self):
        content = textwrap.dedent("""\
            # Review Prompt

            FYI 2026-W15 is the current week and 2026-W16 is the coming week.

            # Bruce

            Here's code:

            ```python
            # This is a Python comment
            print("hello")
            ```

            After the code.
        """)
        _, body = san.extract_model_responses(content)
        # The Python comment is inside a code block within a model response — should survive
        assert "# This is a Python comment" in body
        assert "print(\"hello\")" in body
        assert "After the code." in body

    def test_placeholder_h1s_under_model_names_are_kept(self):
        """Template quirk: the `# Model Names` section is structural, but any H1
        placeholder BELOW it (e.g. `# Gpt-5.4`) ends the structural section and
        gets emitted. This duplicates the response header when the actual model
        writes content later. Documented here so a future fix is intentional."""
        content = textwrap.dedent("""\
            # Model Names

            # Gpt-5.4

            # Claude-opus-4.6

            # Review Prompt

            FYI 2026-W15 is the current week and 2026-W16 is the coming week.

            # Bruce

            Actual response.
        """)
        _, body = san.extract_model_responses(content)
        assert "Actual response." in body
        assert "# Bruce" in body
        # Current behavior: placeholder H1s are kept and duplicated
        assert body.count("# Gpt-5.4") == 1  # only the placeholder, since no real response

    def test_no_model_responses_returns_empty_body(self):
        content = textwrap.dedent("""\
            # Models to Use

            List of models.

            # Review Prompt

            FYI 2026-W15 is the current week and 2026-W16 is the coming week.
        """)
        week, body = san.extract_model_responses(content)
        assert week == "2026-W15"
        assert body.strip() == ""
