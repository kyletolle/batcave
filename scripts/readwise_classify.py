#!/usr/bin/env python3
"""
Readwise Reader Topic Classifier

Fetches all documents from Later + Shortlist, classifies by topic using
title + summary regex matching, and outputs results as JSON.

Usage:
    readwise-classify                    # Classify untagged items
    readwise-classify --all              # Reclassify everything (ignore existing topic-* tags)
    readwise-classify --preview          # Show what would be tagged without writing JSON
    readwise-classify --stats            # Just show current tag distribution

Outputs: /tmp/readwise_classification.json
"""

import os
import sys
import json
import re
import argparse
import requests
from collections import Counter

TOKEN = os.environ.get("READWISE_TOKEN")
HEADERS = {"Authorization": f"Token {TOKEN}"} if TOKEN else {}
BASE = "https://readwise.io/api/v3"


def require_token():
    if not TOKEN:
        print("Error: READWISE_TOKEN not set. Run: source ~/.env.sh", file=sys.stderr)
        sys.exit(1)

# --- Topic definitions ---

TOPICS = {
    "topic-ai": re.compile('|'.join([
        r'\bai\b', r'\ba\.i\.\b', r'\bartificial intelligence\b',
        r'\bllm\b', r'\bllms\b', r'\blarge language model',
        r'\bgpt[-\s]?\d', r'\bchatgpt\b', r'\bgpt\b',
        r'\bclaude\b', r'\banthropic\b', r'\bopenai\b', r'\bopen ai\b',
        r'\bgemini\b', r'\bcopilot\b', r'\bmistral\b', r'\bdeepseek\b',
        r'\bdeep[ -]?mind\b',
        r'\bmachine learning\b', r'\bdeep learning\b',
        r'\bneural net', r'\btransformer\b',
        r'\bvibe cod', r'\bai[-\s]?(assisted|generated|powered|driven|native)',
        r'\bprompt engineer', r'\bai agent', r'\bagentic\b',
        r'\bmodel (training|fine.?tun|inference|weights|alignment)',
        r'\brlhf\b', r'\breinforcement learning\b',
        r'\bsam altman\b', r'\bdario amodei\b',
        r'\bchatbot\b', r'\bgenerative ai\b', r'\bgenai\b', r'\bgen ai\b',
        r'\bcomputer vision\b', r'\bimage generat',
        r'\bdiffusion model', r'\bstable diffusion\b', r'\bmidjourney\b', r'\bdall[-\s]?e\b',
        r'\bai (replace|displace|jobs|workforce|layoff|hiring|productivity)',
        r'\bai (ethics|safety|alignment|regulation|governance|policy|risk)',
        r'\bai (hype|bubble|doom|existential)',
        r'\bcursor\b.*\b(editor|ide|code)\b',
        r'\bhugging\s?face\b', r'\bperplexity\b',
        r'\bsynthetic (data|media|voice|text)',
        r'\bcontext window\b', r'\brag\b.*retrieval',
        r'\bembedding', r'\bvector (database|search|store)',
        r'\bclaude[ -]code\b', r'\bclaude\.ai\b',
    ]), re.IGNORECASE),

    "topic-software": re.compile('|'.join([
        r'\bsoftware engineer', r'\bprogramming\b', r'\bdeveloper experience\b',
        r'\btypescript\b', r'\bjavascript\b', r'\bpython\b', r'\brust\b', r'\bruby\b',
        r'\breact\b', r'\bnode\.?js\b', r'\bnext\.?js\b', r'\bsvelte\b',
        r'\bkubernetes\b', r'\bdocker\b', r'\bmicroservice',
        r'\bdevops\b', r'\bci/?cd\b', r'\bdeployment\b',
        r'\btesting\b', r'\btdd\b', r'\bunit test', r'\bintegration test',
        r'\bdatabase\b', r'\bsql\b', r'\bpostgres', r'\bredis\b', r'\bmongo',
        r'\bgit\b', r'\bgithub\b', r'\bopen.?source\b',
        r'\bweb dev', r'\bfrontend\b', r'\bbackend\b', r'\bfull.?stack\b',
        r'\bdebugging\b', r'\brefactor', r'\bcode review\b',
        r'\bapi\b.*\b(design|gateway|rest|graphql)\b',
        r'\bstaff engineer', r'\bsenior engineer', r'\btech lead\b',
        r'\bsystem design\b', r'\barchitect', r'\bscalab',
        r'\bobservability\b', r'\bmonitoring\b',
        r'\bagile\b', r'\bscrum\b',
        r'\bmonolith', r'\blegacy code\b', r'\btechnical debt\b',
        r'\bffmpeg\b', r'\blinux\b', r'\bserverless\b',
        r'\baws\b', r'\bcloud\b.*\b(computing|native)',
        r'\bruby on rails\b', r'\bdjango\b',
        r'\bobsidian\b.*\b(plugin|dev|build|creat)',
    ]), re.IGNORECASE),

    "topic-writing": re.compile('|'.join([
        r'\bwriting\b.*\b(craft|process|advice|tip|habit)',
        r'\bwriter\b', r'\bauthor\b.*\b(interview|process|advice)',
        r'\bnovel\b', r'\bnovelist\b', r'\bfiction\b',
        r'\bstorytelling\b', r'\bnarrative\b.*\b(structure|craft|technique)',
        r'\bprose\b', r'\bdialogue\b.*\b(writing|craft)',
        r'\bcharacter\b.*\b(arc|development|writing)',
        r'\bpublishing\b', r'\beditor\b.*\b(manuscript|publish)',
        r'\bcreative (process|writing|nonfiction)\b',
        r'\bfantasy\b.*\b(writing|world|genre)',
        r'\bworld.?building\b', r'\bmagic system\b',
        r'\bgrimdark\b', r'\bcosmic horror\b',
        r'\bcraft\b.*\b(essay|lesson|technique)',
        r'\bwriters.?(block|group|workshop|room)',
        r'\bplotting\b', r'\boutlin', r'\bpantsing\b',
    ]), re.IGNORECASE),

    "topic-politics": re.compile('|'.join([
        r'\bcapitalism\b', r'\bsocialism\b', r'\bneoliberal',
        r'\blabor\b.*\b(rights|movement|union|market)',
        r'\bunion\b.*\b(organize|bust|strike)',
        r'\bpolitics\b', r'\bpolitical\b.*\b(economy|theory|power)',
        r'\bdemocracy\b', r'\bauthoritarian',
        r'\binequality\b', r'\bwealth\b.*\b(gap|tax|concentrat)',
        r'\bbillionaire\b', r'\boligarch',
        r'\bcory doctorow\b', r'\bpluralistic\b', r'\bdoctorow\b',
        r'\bsurveillance\b(?!.*\bai\b)', r'\benshittification\b',
        r'\bcorporat\b.*\b(greed|power|abuse|consolidat)',
        r'\bmonopoly\b', r'\bantitrust\b',
        r'\bworker\b.*\b(rights|exploit|wages)',
    ]), re.IGNORECASE),

    "topic-productivity": re.compile('|'.join([
        r'\bobsidian\b', r'\blogseq\b', r'\bnotion\b',
        r'\bnote.?taking\b', r'\bsecond brain\b', r'\bpkm\b', r'\bzettelkasten\b',
        r'\bproductivity\b', r'\btodoist\b',
        r'\btime management\b', r'\btime block', r'\bdeep work\b',
        r'\bhabit\b.*\b(track|build|stack|form)',
        r'\bworkflow\b', r'\bautomation\b',
        r'\bfocus\b.*\b(mode|time|block|protocol)',
        r'\bdigital garden\b', r'\bpersonal knowledge\b',
        r'\bmarkdown\b.*\b(note|tool|editor)',
        r'\bself.?host', r'\bhomelab\b',
        r'\brss\b.*\b(reader|feed)', r'\breadwise\b',
        r'\bpomodoro\b', r'\bgtd\b',
    ]), re.IGNORECASE),

    "topic-health": re.compile('|'.join([
        r'\bhealth\b', r'\bnutrition\b', r'\bdiet\b(?!.*\bmedia\b)',
        r'\bcholesterol\b', r'\bblood pressure\b',
        r'\bexercise\b', r'\bfitness\b', r'\bworkout\b', r'\bstrength train',
        r'\bsleep\b.*\b(quality|hygiene|debt|science|habit)',
        r'\bmelatonin\b', r'\bcircadian\b',
        r'\bmental health\b', r'\bdepression\b', r'\banxiety\b', r'\btherapy\b',
        r'\bmeditation\b', r'\bmindfulness\b',
        r'\bbiohack', r'\bsupplement', r'\bvitamin\b',
        r'\bburnout\b', r'\bstress\b.*\b(management|chronic|reduce)',
        r'\blongevity\b',
    ]), re.IGNORECASE),

    "topic-parenting": re.compile('|'.join([
        r'\bparent', r'\bfather', r'\bmother', r'\bdad\b', r'\bmom\b',
        r'\bbaby\b', r'\btoddler\b', r'\binfant\b',
        r'\bchild\b.*\b(develop|rear|care|behav)',
        r'\bdaycare\b', r'\bpreschool\b',
        r'\bsleep train', r'\bfamily\b.*\b(life|balance|time|routine)',
        r'\bwork.?life balance\b',
        r'\bparental leave\b', r'\bpostpartum\b',
    ]), re.IGNORECASE),

    "topic-gaming": re.compile('|'.join([
        r'\bvideo game', r'\bgaming\b', r'\bgamer\b',
        r'\bplaystation\b', r'\bxbox\b', r'\bnintendo\b',
        r'\bwarhammer\b', r'\b40k\b', r'\btotal war\b',
        r'\bd&d\b', r'\bttrpg\b', r'\btabletop\b',
        r'\brpg\b', r'\bopen.?world\b.*\b(rpg|game)',
        r'\bgame (review|design|dev)',
    ]), re.IGNORECASE),

    "topic-media": re.compile('|'.join([
        r'\bmovie\b', r'\bfilm\b.*\b(review|direct|recommend)',
        r'\btv show\b', r'\bnetflix\b', r'\bhbo\b',
        r'\bpodcast\b', r'\bmusic\b.*\b(album|review|recommend)',
        r'\bcomic\b', r'\bgraphic novel\b', r'\bmanga\b',
        r'\bbatman\b', r'\bmarvel\b', r'\bdc comics\b',
        r'\bbook\b.*\b(recommend|list|club|review)',
        r'\blord of the rings\b', r'\btolkien\b',
        r'\bkexp\b', r'\blive\b.*\bperform',
    ]), re.IGNORECASE),

    "topic-business": re.compile('|'.join([
        r'\bcareer\b', r'\bjob\b.*\b(search|hunt|market|interview)',
        r'\bleadership\b', r'\bmanagement\b.*\b(style|tip|advice)',
        r'\bstartup\b', r'\bentrepreneur', r'\bfounder\b',
        r'\binvest(ing|ment|or)\b', r'\bstock\b.*\bmarket',
        r'\breal estate\b', r'\bretirement\b', r'\broth\b.*\bira\b',
        r'\bpersonal finance\b', r'\bbudget', r'\bsaving',
        r'\bproduct management\b', r'\bproduct manager\b',
        r'\bsubscription\b.*\b(model|economy|business)',
    ]), re.IGNORECASE),

    "topic-life": re.compile('|'.join([
        r'\bpersonal (development|growth|essay)\b',
        r'\bself.?(awareness|improvement|discovery|knowledge|help)\b',
        r'\bstoicism\b', r'\bstoic\b', r'\bexistential',
        r'\bmeaning\b.*\b(life|purpose|work)',
        r'\brelationship\b.*\b(advice|tip|communication)',
        r'\bgrief\b', r'\bidentity\b', r'\bloneliness\b',
        r'\bgratitude\b', r'\bjournal', r'\breflect',
        r'\bmasculinit', r'\bcreativity\b(?!.*\bai\b)',
        r'\bflow state\b', r'\bdeep thinking\b',
        r'\bminimal', r'\bdigital\b.*\b(detox|minim|wellness)',
        r'\bsocial media\b.*\b(quit|break|harm|toxic)',
    ]), re.IGNORECASE),

    "topic-news": re.compile('|'.join([
        r'\breport(s|ed|ing)?\b', r'\bannounce[sd]?\b', r'\blaunch(es|ed)?\b',
        r'\braise[sd]?\b.*\b(million|billion|funding)',
        r'\bacquir', r'\bipo\b',
        r'\bsued?\b', r'\blawsuit\b', r'\bsettle[sd]?\b',
        r'\boutage\b', r'\bbreach\b',
        r'\belection\b', r'\btrump\b', r'\bbiden\b', r'\bcongress\b',
        r'\bnew (study|report|data|research|law|rule|policy)\b',
        r'\blayoff', r'\blaid off\b',
        r'\bdata\b.*\b(collect|harvest|share[sd]|sold|leak)',
        r'\bscandal\b', r'\bcontrovers', r'\binvestigation\b',
    ]), re.IGNORECASE),
}

# Bruce-authored skip signals
BRUCE_TAGS = {'bruce', 'weekly-review', 'boabw', 'slack-mining', 'craft-deep-read',
              'bruce-panel', 'scout-brief', 'morning-brief', 'vault-audit',
              'ai-distilled', 'ai-review', 'ai-distillation', 'extraction',
              'prep', 'use-case', 'walk-brief', 'synthesis', 'takeaways',
              'method-2', 'method-4', 'method-5', 'method-6', 'gap-analysis'}

TOPIC_TAGS = set(TOPICS.keys()) | {'topic-books', 'topic-video', 'topic-misc'}


def fetch_all(location):
    docs = []
    cursor = None
    while True:
        params = {"location": location, "page_size": 100}
        if cursor:
            params["pageCursor"] = cursor
        r = requests.get(f"{BASE}/list/", headers=HEADERS, params=params)
        r.raise_for_status()
        data = r.json()
        docs.extend(data.get("results", []))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
    return docs


def classify(docs, reclassify_all=False):
    results = {topic: [] for topic in TOPICS}
    results["topic-books"] = []
    results["topic-video"] = []
    results["topic-misc"] = []
    skipped_bruce = 0
    skipped_tagged = 0

    for doc in docs:
        doc_id = doc.get("id", "")
        title = doc.get("title") or ""
        author = doc.get("author") or ""
        summary = doc.get("summary") or ""
        category = doc.get("category") or ""
        tags = doc.get("tags") or {}
        tag_names = set(tags.keys()) if isinstance(tags, dict) else set()

        # Skip Bruce-authored
        if "Bruce in the Batcave" in author or tag_names & BRUCE_TAGS:
            skipped_bruce += 1
            continue

        # Skip already tagged (unless --all)
        if not reclassify_all and tag_names & TOPIC_TAGS:
            skipped_tagged += 1
            continue

        # Category-based classification
        if category in ("epub", "pdf"):
            results["topic-books"].append({"id": doc_id, "title": title, "existing_tags": list(tag_names)})
            continue
        if category == "video":
            results["topic-video"].append({"id": doc_id, "title": title, "existing_tags": list(tag_names)})
            continue

        # Regex classification on title + summary + author
        text = f"{title} {summary} {author}"
        matched = False
        for topic, pattern in TOPICS.items():
            if pattern.search(text):
                results[topic].append({"id": doc_id, "title": title, "existing_tags": list(tag_names)})
                matched = True
                break

        if not matched:
            results["topic-misc"].append({"id": doc_id, "title": title, "existing_tags": list(tag_names)})

    return results, skipped_bruce, skipped_tagged


def main():
    parser = argparse.ArgumentParser(description="Classify Readwise Reader documents by topic")
    parser.add_argument("--all", action="store_true", help="Reclassify everything, ignoring existing topic-* tags")
    parser.add_argument("--preview", action="store_true", help="Show what would be tagged without writing JSON")
    parser.add_argument("--stats", action="store_true", help="Just show current tag distribution")
    args = parser.parse_args()

    require_token()
    print("Fetching documents...", flush=True)
    later = fetch_all("later")
    shortlist = fetch_all("shortlist")
    all_docs = later + shortlist
    print(f"Total: {len(all_docs)} ({len(later)} later + {len(shortlist)} shortlist)")

    if args.stats:
        tag_counts = Counter()
        for doc in all_docs:
            tags = doc.get("tags") or {}
            tag_names = set(tags.keys()) if isinstance(tags, dict) else set()
            topic_tags = tag_names & TOPIC_TAGS
            if topic_tags:
                for t in topic_tags:
                    tag_counts[t] += 1
            elif "Bruce in the Batcave" in (doc.get("author") or ""):
                tag_counts["(bruce-authored)"] += 1
            else:
                tag_counts["(untagged)"] += 1
        print("\nCurrent distribution:")
        for tag, count in tag_counts.most_common():
            print(f"  {tag:25s} {count:>4d}")
        return

    results, skipped_bruce, skipped_tagged = classify(all_docs, reclassify_all=args.all)

    total_to_tag = sum(len(v) for v in results.values())
    print(f"\nSkipped (Bruce-authored): {skipped_bruce}")
    print(f"Skipped (already tagged): {skipped_tagged}")
    print(f"To tag: {total_to_tag}")

    for topic in sorted(results.keys(), key=lambda t: -len(results[t])):
        items = results[topic]
        if items:
            print(f"\n  {topic}: {len(items)} items")
            if args.preview:
                for d in items[:5]:
                    print(f"    - {d['title'][:75]}")
                if len(items) > 5:
                    print(f"    ... and {len(items) - 5} more")

    if not args.preview:
        output_path = "/tmp/readwise_classification.json"
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults written to {output_path}")
        print("Run '/readwise-tag' in Claude Code to apply tags via MCP.")
    else:
        print("\n(Preview mode — no files written)")


if __name__ == "__main__":
    main()
