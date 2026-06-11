# batcave

Tooling from a personal knowledge system: an Obsidian vault, a VPS, and an AI agent that lives on it.

This repo is the code half of an ongoing human/AI collaboration. I'm Kyle. The agent is Bruce (Claude, running via Claude Code on a Hetzner VPS, with persistent memory and standing access to my vault, Todoist, Readwise, and calendar). Nearly every line here was written by Bruce, working from decisions we made together in conversation. I direct, review, and veto; Bruce designs, implements, and maintains.

That makes this repo two things at once: a working toolbox, and a demonstration of what agentic engineering looks like in practice (not a demo built for show, but the actual accumulated output of months of daily use).

## How this code gets made

The workflow behind every tool here follows the same arc:

1. **A real friction surfaces.** I notice something tedious (triaging Todoist, turning articles into audio, logging sleep times) and describe it in plain language, usually via speech-to-text.
2. **We design in dialogue.** Bruce asks the questions that matter, proposes options with trade-offs, and we settle the decisions before any code exists. Plans live as notes in the vault.
3. **Bruce implements.** I review behavior, not diffs, on the first pass. Tests come along where the logic warrants them (see `scripts/tests/`).
4. **Lessons get encoded, not just remembered.** When something goes wrong, the fix becomes structure: the Todoist CLI logs every mutation to an audit trail because we once needed one and didn't have it. Mutations are forbidden outside that CLI because raw API calls bypass the log. The guardrails in this code are scar tissue from real incidents.

## What's here

### `bin/` — command wrappers

Small entry points installed to `~/.local/bin`. Most handle environment sourcing and delegate to `scripts/`; some are self-contained (`pagerduty-alert`, `ob-sync-check`, `qmd-query`).

### `scripts/` — the tools

| Area | Scripts | What they do |
|------|---------|--------------|
| Task management | `todoist.py`, `todoist_brief.py` | Full Todoist CLI (list/add/complete/search/bulk) with a JSONL audit trail of every mutation, plus a sanitized four-tier brief generator for morning summaries |
| Text-to-speech | `read_aloud.py`, `tts.sh`, `daily_read_aloud.sh` | URL or document → cleaned text → chunked multi-provider TTS (OpenAI/Deepgram/ElevenLabs) → MP3s. Includes a daily cron digest |
| Bat-Speaker | `batspeaker_hook.py`, `batspeaker_server.py` | Claude Code Stop-hook that auto-converts agent responses to audio in a rolling "listen" note. Toggleable, multi-engine, queue-based |
| Readwise | `send_to_readwise.py`, `send_ai_note.py`, `readwise_classify.py`, `readwise_tag_apply.py`, `readwise_reader_cleanup.py` | Send any vault note to Reader as clean HTML; classify and tag the Reader library by topic |
| Weekly review | `weekly_review_llm.py`, `llm_panel.py`, `llm_common.py` | Send a baked weekly reflection to a panel of LLMs (different vendors, same prompt) and collect their takes into one note |
| Sleep tracking | `sleep_duration.py`, `sleep_update.py` | Duration math across day boundaries; ISO-week routing to write fields into the right weekly note |
| Vault maintenance | `vault_health.py`, `shard_gardens_audit.py`, `add_day_and_publish_to_daily_notes.py`, `apply_taxonomy_v2.py` | Link/frontmatter health checks; LanguageTool Premium audit over published notes with caching and a custom dictionary |
| Search infra | `qmd-sync.sh`, `patch-qmd-reranker.sh` | Keep a local semantic search index (QMD) in sync with the vault |

### `vps/` — provisioning

Scripts to create, configure, and verify the box itself: `provision-vps.sh` (Hetzner create from CLI), `setup-vps.sh` (hardening: Tailscale-only SSH, UFW, services), `bootstrap-vps.sh`, `verify-vps.sh`, plus monitoring glue (`disk-watch.sh`, `notify-failed-service.sh`).

## Caveats

This is personal tooling, published as reference rather than product. Paths assume my vault layout (`~/vault`), secrets load from `~/.env.sh`, and nothing here is packaged for installation. Read it for the patterns: audited mutation layers, provider abstractions, cron-friendly gather scripts that keep deterministic work out of the LLM's context window.

## License

MIT
