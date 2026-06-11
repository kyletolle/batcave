#!/usr/bin/env python3
"""
LLM Panel — Multi-model consensus and lens analysis.

Modes:
  consensus  Same prompt to all models, then synthesize agreements/disagreements/insights.
  lens       Different analytical lenses on the same source, then synthesize perspectives.

Usage:
  llm-panel consensus "What are the key tensions in this plot?" --source chapter.md
  llm-panel consensus --prompt-file question.md --source notes.md
  llm-panel lens --lenses lenses.md --source chapter.md
  llm-panel lens --lenses lenses.md --source chapter.md --models Gpt-5.4,Claude-opus-4.6

Options:
  --output FILE         Output note path (default: auto-generated in 0 Inbox/)
  --title TEXT          Title for the output note
  --no-synthesis        Skip the synthesis pass
  --synthesizer MODEL   Model for synthesis (default: Claude-opus-4.6)
  --readwise            Send output to Readwise after writing
  --models M1,M2,...    Comma-separated models to use (default: panel models)
  --include-pro         Include gpt-5.4-pro in the panel

Environment variables: see llm_common.py
"""

import sys
import os
import re
import argparse
import subprocess
from datetime import date
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_common import (
    MODELS, PANEL_MODELS, call_model, check_api_keys, read_file,
)


# ---------------------------------------------------------------------------
# Synthesis prompts
# ---------------------------------------------------------------------------

CONSENSUS_SYNTHESIS_SYSTEM = """\
You are synthesizing responses from multiple AI models that were all given the same prompt.

Your task:
1. **Agreements** — What do most or all models converge on? What's the consensus view?
2. **Unique Insights** — What did only one or two models surface that others missed? These are often the most valuable.
3. **Disagreements** — Where do models meaningfully diverge? What's at stake in the disagreement?
4. **Synthesis** — Weave everything into a coherent answer that's richer than any single model's response.

Use H2 (##) headings for each section. Be concise but thorough. Don't just list what each model said — synthesize."""

LENS_SYNTHESIS_SYSTEM = """\
You are synthesizing responses from multiple AI models, each analyzing the same material through a different analytical lens.

Your task:
1. **Cross-cutting Themes** — What patterns emerge across multiple lenses?
2. **Productive Tensions** — Where do different lenses reveal contradictions or tensions worth exploring?
3. **Unique Contributions** — What does each lens uniquely illuminate that others couldn't see?
4. **Synthesis** — Weave these perspectives into a multi-dimensional understanding.

Use H2 (##) headings for each section. Be concise but thorough. Don't just summarize each lens — find the connections."""


# ---------------------------------------------------------------------------
# Lens file parser
# ---------------------------------------------------------------------------

def parse_lenses(content):
    """Parse a lens file into an OrderedDict of {lens_name: lens_prompt}.

    Format: H1 headings define lens names, content below is the prompt.
    """
    lenses = OrderedDict()
    current_name = None
    current_lines = []

    for line in content.split("\n"):
        heading = re.match(r"^# (.+)$", line)
        if heading:
            if current_name:
                lenses[current_name] = "\n".join(current_lines).strip()
            current_name = heading.group(1).strip()
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)

    if current_name:
        lenses[current_name] = "\n".join(current_lines).strip()

    return lenses


# ---------------------------------------------------------------------------
# Output note builder
# ---------------------------------------------------------------------------

def build_output_note(mode, prompt_text, source_path, models_used, responses,
                      synthesis, lens_assignments=None):
    """Build the output markdown note."""
    today = date.today().isoformat()
    tags = f"llm-panel, {mode}"

    lines = [
        "---",
        f"created_on: {today}",
        "origin: ai",
        f"tags: [{tags}]",
        "---",
        "",
    ]

    # Header block
    lines.append(f"**Mode:** {mode.title()}")

    if mode == "consensus":
        # Truncate very long prompts in the header
        display_prompt = prompt_text if len(prompt_text) < 500 else prompt_text[:500] + "..."
        lines.append(f"**Prompt:** {display_prompt}")
    else:
        lines.append(f"**Lenses:** {', '.join(lens_assignments.keys())}")

    if source_path:
        lines.append(f"**Source:** {source_path}")

    lines.append(f"**Models:** {', '.join(models_used)}")
    lines.append("")

    # Synthesis (goes first — the payoff)
    if synthesis:
        lines.append("---")
        lines.append("")
        lines.append("# Synthesis")
        lines.append("")
        lines.append(synthesis)
        lines.append("")

    lines.append("---")
    lines.append("")

    # Individual responses
    if mode == "consensus":
        for model_name in models_used:
            lines.append(f"# {model_name}")
            lines.append("")
            lines.append(responses.get(model_name, "*No response*"))
            lines.append("")
    else:
        # Lens mode: heading is lens name, with model noted
        for lens_name, model_name in lens_assignments.items():
            lines.append(f"# {lens_name}")
            lines.append(f"*Model: {model_name}*")
            lines.append("")
            lines.append(responses.get(lens_name, "*No response*"))
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------

def resolve_models(args):
    """Determine which models to use based on args."""
    if args.models:
        names = [n.strip() for n in args.models.split(",")]
        active = OrderedDict()
        for name in names:
            if name not in MODELS:
                print(f"Error: unknown model '{name}'")
                print(f"Available: {', '.join(MODELS.keys())}")
                sys.exit(1)
            active[name] = MODELS[name]
        return active

    base = OrderedDict(PANEL_MODELS)
    if getattr(args, "include_pro", False):
        base["gpt-5.4-pro"] = MODELS["gpt-5.4-pro"]
    return base


def run_parallel(active_models, system_messages, user_message):
    """Run models in parallel. system_messages is either a single string
    (consensus) or a dict mapping heading->system (lens)."""
    responses = {}

    def get_system(heading):
        if isinstance(system_messages, dict):
            return system_messages[heading]
        return system_messages

    with ThreadPoolExecutor(max_workers=len(active_models)) as executor:
        futures = {
            executor.submit(
                call_model, heading, provider, model_id,
                get_system(heading), user_message
            ): heading
            for heading, (provider, model_id) in active_models.items()
        }
        for future in as_completed(futures):
            heading, response = future.result()
            responses[heading] = response
            if response.startswith("*"):
                print(f"  {heading}: {response}")

    return responses


def run_synthesis(synthesizer_name, synthesis_system, all_responses_text):
    """Run the synthesis pass on collected responses."""
    if synthesizer_name not in MODELS:
        print(f"Error: unknown synthesizer model '{synthesizer_name}'")
        print(f"Available: {', '.join(MODELS.keys())}")
        sys.exit(1)

    provider, model_id = MODELS[synthesizer_name]
    print(f"\nSynthesis pass ({synthesizer_name})...")
    _, synthesis = call_model(
        "synthesis", provider, model_id,
        synthesis_system, all_responses_text
    )
    return synthesis


def run_consensus(args):
    """Consensus mode: same prompt to all models, then synthesize."""
    # Resolve prompt
    if args.prompt_file:
        prompt = read_file(args.prompt_file)
    elif args.prompt:
        prompt = args.prompt
    else:
        print("Error: provide a prompt (positional) or --prompt-file")
        sys.exit(1)

    # Resolve source
    source_content = ""
    if args.source:
        source_content = read_file(args.source)

    active_models = resolve_models(args)

    print(f"Mode:       consensus")
    print(f"Prompt:     {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    if args.source:
        print(f"Source:     {args.source} ({len(source_content):,} chars)")
    print(f"Models:     {len(active_models)} — {', '.join(active_models.keys())}")
    print()
    check_api_keys(active_models)
    print()

    # Build messages: if source provided, prompt is system context, source is user message.
    # If no source, prompt is the user message with a minimal system framing.
    if source_content:
        system_msg = f"You are an expert analyst. The user will provide source material. Your task:\n\n{prompt}"
        user_msg = source_content
    else:
        system_msg = "You are an expert analyst. Answer the user's question thoroughly."
        user_msg = prompt

    # Run all models in parallel
    print("Running panel...")
    responses = run_parallel(active_models, system_msg, user_msg)

    # Synthesis pass
    synthesis = None
    if not args.no_synthesis:
        # Build the synthesis input: all responses labeled by model
        parts = []
        for name in active_models:
            resp = responses.get(name, "*No response*")
            if not resp.startswith("*"):
                parts.append(f"## {name}\n\n{resp}")
        all_text = "\n\n---\n\n".join(parts)

        synthesis_context = f"The original prompt was:\n\n> {prompt}\n\n"
        if args.source:
            synthesis_context += f"(Models were given {len(source_content):,} chars of source material.)\n\n"
        synthesis_context += "Below are the responses from each model:\n\n" + all_text

        synthesis = run_synthesis(args.synthesizer, CONSENSUS_SYNTHESIS_SYSTEM, synthesis_context)

    # Build and write output
    title = args.title or prompt[:60].rstrip(". ")
    output_path = args.output or os.path.join(
        os.environ.get("VAULT_PATH", "."),
        f"0 Inbox/LLM Panel — {title}.md"
    )

    note = build_output_note(
        mode="consensus",
        prompt_text=prompt,
        source_path=args.source,
        models_used=list(active_models.keys()),
        responses=responses,
        synthesis=synthesis,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(note)
    print(f"\nOutput written to {output_path}")

    return output_path


def run_lens(args):
    """Lens mode: different prompts per model on the same source, then synthesize."""
    lenses = parse_lenses(read_file(args.lenses))
    if not lenses:
        print("Error: no lenses found in lens file (use H1 headings)")
        sys.exit(1)

    source_content = read_file(args.source)
    active_models = resolve_models(args)

    # Assign lenses to models (zip — shorter list wins)
    model_list = list(active_models.items())
    lens_list = list(lenses.items())
    assignments = list(zip(lens_list, model_list))

    if len(lens_list) != len(model_list):
        print(f"Note: {len(lens_list)} lenses, {len(model_list)} models — using {len(assignments)} pairs.")

    # Build the assignment map: lens_name -> model_name, and model_heading -> system_msg
    lens_to_model = OrderedDict()
    model_systems = {}
    lens_models = OrderedDict()  # model subset actually being used

    for (lens_name, lens_prompt), (model_name, model_config) in assignments:
        lens_to_model[lens_name] = model_name
        model_systems[model_name] = f"You are an expert analyst applying a specific analytical lens.\n\n**Your lens: {lens_name}**\n\n{lens_prompt}"
        lens_models[model_name] = model_config

    print(f"Mode:       lens")
    print(f"Source:     {args.source} ({len(source_content):,} chars)")
    print(f"Lenses:     {len(assignments)}")
    for lens_name, model_name in lens_to_model.items():
        print(f"  {lens_name} → {model_name}")
    print()
    check_api_keys(lens_models)
    print()

    # Run all lenses in parallel (each model gets its own system prompt)
    print("Running lenses...")
    raw_responses = run_parallel(lens_models, model_systems, source_content)

    # Remap responses from model_name keys to lens_name keys
    responses = OrderedDict()
    for lens_name, model_name in lens_to_model.items():
        responses[lens_name] = raw_responses.get(model_name, "*No response*")

    # Synthesis pass
    synthesis = None
    if not args.no_synthesis:
        parts = []
        for lens_name, model_name in lens_to_model.items():
            resp = responses.get(lens_name, "*No response*")
            if not resp.startswith("*"):
                parts.append(f"## {lens_name} (analyzed by {model_name})\n\n{resp}")
        all_text = "\n\n---\n\n".join(parts)

        lens_desc = "\n".join(
            f"- **{ln}** ({mn}): {lenses[ln][:100]}..."
            for ln, mn in lens_to_model.items()
        )
        synthesis_context = (
            f"The lenses applied were:\n{lens_desc}\n\n"
            f"(Each model analyzed {len(source_content):,} chars of source material.)\n\n"
            f"Below are the responses from each lens:\n\n{all_text}"
        )

        synthesis = run_synthesis(args.synthesizer, LENS_SYNTHESIS_SYSTEM, synthesis_context)

    # Build and write output
    title = args.title or "Lens Analysis"
    output_path = args.output or os.path.join(
        os.environ.get("VAULT_PATH", "."),
        f"0 Inbox/LLM Panel — {title}.md"
    )

    note = build_output_note(
        mode="lens",
        prompt_text=None,
        source_path=args.source,
        models_used=[m for m in lens_to_model.values()],
        responses=responses,
        synthesis=synthesis,
        lens_assignments=lens_to_model,
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(note)
    print(f"\nOutput written to {output_path}")

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_common_args(parser):
    """Add arguments shared by both subcommands."""
    parser.add_argument("--output", "-o", help="Output note path")
    parser.add_argument("--title", "-t", help="Title for the output note")
    parser.add_argument("--no-synthesis", action="store_true",
                        help="Skip the synthesis pass")
    parser.add_argument("--synthesizer", default="Claude-opus-4.6",
                        help="Model for synthesis (default: Claude-opus-4.6)")
    parser.add_argument("--readwise", action="store_true",
                        help="Send output to Readwise after writing")
    parser.add_argument("--models", "-m",
                        help="Comma-separated models to use (default: panel models)")
    parser.add_argument("--include-pro", action="store_true",
                        help="Include gpt-5.4-pro in the panel")


def main():
    parser = argparse.ArgumentParser(
        description="LLM Panel — multi-model consensus and lens analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Consensus subcommand
    cons = subparsers.add_parser("consensus",
        help="Same prompt to all models, then synthesize")
    cons.add_argument("prompt", nargs="?", help="The prompt to send")
    cons.add_argument("--prompt-file", "-f", help="Read prompt from file")
    cons.add_argument("--source", "-s", help="Source material file")
    add_common_args(cons)

    # Lens subcommand
    lens = subparsers.add_parser("lens",
        help="Different lenses on the same source, then synthesize")
    lens.add_argument("--lenses", "-l", required=True, help="Lens config file (H1 = lens name, body = prompt)")
    lens.add_argument("--source", "-s", required=True, help="Source material file")
    add_common_args(lens)

    args = parser.parse_args()

    if args.mode == "consensus":
        output_path = run_consensus(args)
    else:
        output_path = run_lens(args)

    # Send to Readwise if requested
    if args.readwise:
        print("\nSending to Readwise...")
        title_flag = []
        if args.title:
            title_flag = ["--title", args.title]
        result = subprocess.run(
            ["send-to-readwise", output_path, "--tags", "llm-panel"] + title_flag,
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("  Sent to Readwise.")
        else:
            print(f"  Readwise send failed: {result.stderr}")


if __name__ == "__main__":
    main()
