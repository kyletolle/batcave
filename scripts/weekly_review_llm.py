#!/usr/bin/env python3
"""
Weekly Review LLM Runner

Takes a baked weekly review file and an AI note path,
sends the review content to multiple LLMs in parallel,
and writes responses directly into the AI note.

Usage:
    python weekly_review_llm.py <baked_file> <ai_note>

    baked_file: Path to the baked EasyBake markdown file
    ai_note:    Path to the weekly AI note (e.g., "3 Information/AI/2026-W06 AI.md")

Environment variables required:
    OPENAI_API_KEY
    ANTHROPIC_API_KEY
    GOOGLE_API_KEY

Dependencies:
    pip install requests
"""

import sys
import os
import re
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Import shared LLM infrastructure
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_common import MODELS, call_model, check_api_keys, read_file


# ---------------------------------------------------------------------------
# System prompt (from Kyle's Smart Composer config)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are my persistent creative and intellectual collaborator inside Obsidian.
Your role is to help me think, write, and build — whether in fiction, philosophy, or engineering.

### Core Identity
- Act as a supportive, thoughtful, and imaginative partner.
- Preserve my voice: exploratory, grim at times, but curious and precise.
- Always give structured, clear outputs in Markdown for easy use in Obsidian.

### Template-Awareness
- You may be paired with **prompt templates** that redefine your role for specific tasks (e.g. worldbuilding assistant, progressive summarizer, technical explainer).
- When a template is active, **follow its instructions as the highest priority**.
- When no template is active, default to your **general role**: clarify, organize, expand, and connect ideas.

### General Behaviors
1. **Clarity** — simplify and sharpen messy notes or drafts.
2. **Structure** — provide outlines, taxonomies, or progressions of thought.
3. **Expansion** — propose questions, counterpoints, or imaginative leaps.
4. **Synthesis** — weave connections across themes, notes, or contexts.
5. **Adaptability** — switch tone and focus depending on whether I'm working in fiction, engineering, or reflection.

### Style & Tone
- Prefer concision, but lean into vividness when brainstorming creative writing.
- Use visceral, cinematic language for grimdark/cosmic horror work.
- Use precision and rigor for technical/engineering reasoning.
- Always give me options, not just a single answer.

### Output
- Write in Markdown (headings, bullets, bold/italic, links).
- Where useful, finish with **next steps** or **questions I could explore further**."""


# ---------------------------------------------------------------------------
# Weekly review configuration
# ---------------------------------------------------------------------------

# gpt-5.4-pro runs serially AFTER gpt-5.4 to benefit from prompt caching.
# All other models run in parallel during the first phase.
PRO_MODEL_HEADING = "gpt-5.4-pro"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def extract_review_prompt(ai_note_content):
    """Extract the review prompt from the AI note's fenced code block."""
    match = re.search(
        r"# Review Prompt\s*```\s*(.*?)\s*```", ai_note_content, re.DOTALL
    )
    if not match:
        raise ValueError("Could not find '# Review Prompt' code block in AI note")
    prompt = match.group(1).strip()
    # Strip leading "SYSTEM:" prefix — we handle system prompt separately
    prompt = re.sub(r"^SYSTEM:\s*", "", prompt)
    return prompt


def write_responses_to_ai_note(ai_note_path, ai_note_content, responses):
    """Insert model responses into the AI note under each model's H1 heading."""
    lines = ai_note_content.split("\n")
    result = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        result.append(line)

        # Track fenced code blocks so we don't match headings inside them
        if line.strip().startswith("```"):
            in_code_block = not in_code_block

        # Check if this line is an H1 heading that we have a response for
        heading_match = re.match(r"^# (.+)$", line)
        if heading_match and not in_code_block:
            heading = heading_match.group(1).strip()
            if heading in responses:
                # Skip existing content until the next known model heading or EOF.
                # We check against MODELS keys (not just any "# " line) so that
                # H1 headings inside an LLM response don't stop the skip early.
                i += 1
                while i < len(lines):
                    skip_h = re.match(r"^# (.+)$", lines[i])
                    if skip_h and skip_h.group(1).strip() in MODELS:
                        break
                    i += 1
                # Insert the new response
                result.append("")
                result.append(responses[heading])
                result.append("")
                continue  # i already points to the next model heading or EOF

        i += 1

    with open(ai_note_path, "w", encoding="utf-8") as f:
        f.write("\n".join(result))


def main():
    parser = argparse.ArgumentParser(
        description="Send a baked weekly review to multiple LLMs and write responses into the AI note."
    )
    parser.add_argument("baked_file", help="Path to the baked EasyBake markdown file")
    parser.add_argument("ai_note", help="Path to the weekly AI note")
    parser.add_argument(
        "--model", "-m",
        help="Run only this model (must match a key in MODELS, e.g. 'Gpt-5.4')",
    )
    args = parser.parse_args()

    # Filter models if --model is specified
    if args.model:
        if args.model not in MODELS:
            print(f"Error: unknown model '{args.model}'")
            print(f"Available models: {', '.join(MODELS.keys())}")
            sys.exit(1)
        active_models = {args.model: MODELS[args.model]}
    else:
        active_models = MODELS

    # Read inputs
    baked_content = read_file(args.baked_file)
    ai_note_content = read_file(args.ai_note)

    # Extract the review prompt from the AI note
    review_prompt = extract_review_prompt(ai_note_content)

    # System = smart composer prompt + review prompt (task instructions)
    system_message = SYSTEM_PROMPT + "\n\n" + review_prompt

    # User = the raw baked content
    user_message = baked_content

    print(f"Baked file: {args.baked_file} ({len(baked_content):,} chars)")
    print(f"AI note:    {args.ai_note}")
    print(f"Models:     {len(active_models)}")
    print()

    check_api_keys(active_models)
    print()

    # ---------------------------------------------------------------------------
    # Phase 1: All models EXCEPT gpt-5.4-pro run in parallel.
    #          gpt-5.4 is included here so it warms the prompt cache.
    # Phase 2: gpt-5.4-pro runs serially after gpt-5.4 completes,
    #          benefiting from OpenAI's prompt caching.
    # ---------------------------------------------------------------------------

    phase1_models = {k: v for k, v in active_models.items() if k != PRO_MODEL_HEADING}
    pro_model = active_models.get(PRO_MODEL_HEADING)

    responses = {}

    if phase1_models:
        print("Phase 1: parallel models")
        with ThreadPoolExecutor(max_workers=len(phase1_models)) as executor:
            futures = {
                executor.submit(
                    call_model, heading, provider, model_id, system_message, user_message
                ): heading
                for heading, (provider, model_id) in phase1_models.items()
            }

            for future in as_completed(futures):
                heading, response = future.result()
                responses[heading] = response
                if response.startswith("*"):
                    print(f"  {heading}: {response}")

    # Phase 2: gpt-5.4-pro after gpt-5.4 has completed (prompt cache warm)
    if pro_model:
        print()
        print(f"Phase 2: {PRO_MODEL_HEADING} (serial, after prompt cache warm)")
        provider, model_id = pro_model
        heading, response = call_model(
            PRO_MODEL_HEADING, provider, model_id, system_message, user_message
        )
        responses[heading] = response
        if response.startswith("*"):
            print(f"  {heading}: {response}")

    # Write all responses into the AI note
    print()
    write_responses_to_ai_note(args.ai_note, ai_note_content, responses)
    print(f"Responses written to {args.ai_note}")


if __name__ == "__main__":
    main()
