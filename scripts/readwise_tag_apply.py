#!/usr/bin/env python3
"""
Readwise Reader Tag Applier

Reads classification results from /tmp/readwise_classification.json and applies
topic tags via the Readwise Reader update API. Pure Python, no Claude in loop.

Usage:
    readwise-tag-apply                       # Apply all tags from default JSON
    readwise-tag-apply --input PATH          # Use a different JSON file
    readwise-tag-apply --topic topic-ai      # Apply only one topic
    readwise-tag-apply --dry-run             # Show what would happen without writing
    readwise-tag-apply --rate 5              # Requests per second (default 5)
"""

import os
import sys
import json
import time
import argparse
import requests

TOKEN = os.environ.get("READWISE_TOKEN")
HEADERS = {
    "Authorization": f"Token {TOKEN}" if TOKEN else "",
    "Content-Type": "application/json",
}
UPDATE_URL = "https://readwise.io/api/v3/update/{doc_id}/"
DEFAULT_INPUT = "/tmp/readwise_classification.json"


def require_token():
    if not TOKEN:
        print("Error: READWISE_TOKEN not set. Run: source ~/.env.sh", file=sys.stderr)
        sys.exit(1)


def update_tags(doc_id, tags):
    """PATCH a single doc with the full tag list. Returns (ok, message)."""
    try:
        r = requests.patch(
            UPDATE_URL.format(doc_id=doc_id),
            headers=HEADERS,
            json={"tags": list(tags)},
            timeout=10,
        )
        if r.status_code == 200:
            return True, None
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return False, str(e)


def apply_topic(topic, docs, dry_run, sleep_s):
    """Apply a topic tag to a list of docs. Returns (new_count, error_count, errors)."""
    new_count = 0
    error_count = 0
    errors = []

    for doc in docs:
        doc_id = doc["id"]
        existing = set(doc.get("existing_tags") or [])
        if topic in existing:
            continue
        new_tags = sorted(existing | {topic})

        if dry_run:
            new_count += 1
            continue

        ok, msg = update_tags(doc_id, new_tags)
        if ok:
            new_count += 1
        else:
            error_count += 1
            errors.append((doc_id, doc.get("title", "")[:60], msg))

        time.sleep(sleep_s)

    return new_count, error_count, errors


def main():
    parser = argparse.ArgumentParser(description="Apply topic tags from classification JSON")
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"Classification JSON path (default: {DEFAULT_INPUT})")
    parser.add_argument("--topic", help="Apply only one topic (e.g. topic-ai)")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing")
    parser.add_argument("--rate", type=float, default=5.0, help="Requests per second (default 5)")
    args = parser.parse_args()

    require_token()

    if not os.path.exists(args.input):
        print(f"Error: {args.input} not found. Run readwise-classify first.", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        results = json.load(f)

    sleep_s = 1.0 / args.rate if args.rate > 0 else 0

    if args.topic:
        if args.topic not in results:
            print(f"Error: topic '{args.topic}' not in classification JSON", file=sys.stderr)
            print(f"Available: {sorted(results.keys())}", file=sys.stderr)
            sys.exit(1)
        topics_to_run = [args.topic]
    else:
        topics_to_run = sorted(results.keys(), key=lambda t: -len(results[t]))

    total_docs = sum(len(results[t]) for t in topics_to_run)
    if args.dry_run:
        print(f"DRY RUN — would tag {total_docs} docs across {len([t for t in topics_to_run if results[t]])} topics\n")
    else:
        print(f"Applying tags to {total_docs} docs at {args.rate} req/s "
              f"(~{total_docs * sleep_s:.0f}s)\n")

    print(f"{'Tag':<25} {'Count':>6} {'New':>6} {'Errors':>7}")
    print("-" * 50)

    grand_new = 0
    grand_err = 0
    all_errors = []

    for topic in topics_to_run:
        docs = results[topic]
        if not docs:
            continue
        new, err, errors = apply_topic(topic, docs, args.dry_run, sleep_s)
        grand_new += new
        grand_err += err
        all_errors.extend(errors)
        print(f"{topic:<25} {len(docs):>6} {new:>6} {err:>7}")

    print("-" * 50)
    print(f"{'TOTAL':<25} {total_docs:>6} {grand_new:>6} {grand_err:>7}")

    if all_errors:
        print("\nFirst 10 errors:")
        for doc_id, title, msg in all_errors[:10]:
            print(f"  {doc_id} | {title}")
            print(f"    → {msg}")


if __name__ == "__main__":
    main()
