#!/usr/bin/env python3
"""
readwise_reader_cleanup.py — guarded deletion of personal Reader documents.

Deletes Reader documents by ID via the Reader API v3 DELETE endpoint, with
strong safety rails for the "personal sweep" (morning briefs, weekly-review
artifacts, sovereignty design doc).

Safety design:
  - HARD GUARD: a document is only eligible if category == 'article' AND it
    carries at least one allowlisted personal tag (morning-brief / weekly-review
    / sovereignty). If ANY input doc fails the guard, the whole run ABORTS and
    nothing is deleted. Epubs/PDFs/books physically cannot pass (wrong category
    AND no such tags).
  - HIGHLIGHT CAPTURE: before deleting, each doc's Reader highlights (id + text +
    note) are recorded into the audit log, so classic-side highlights can be
    reconciled/located later even if Reader deletion removes them.
  - DRY-RUN by default. Pass --execute to actually delete.
  - JSONL audit log (real deletions only) at readwise_cleanup_audit.jsonl next
    to this script. Each line ties a document_id -> its metadata + highlights.
  - Paced for rate limits; honors HTTP 429 Retry-After.

Usage:
  source ~/.env.sh && python3 readwise_reader_cleanup.py BUCKET_FILE            # dry run
  source ~/.env.sh && python3 readwise_reader_cleanup.py BUCKET_FILE --execute  # delete

  BUCKET_FILE: one document_id per line. Blank lines and #-comments ignored.
"""
import os
import sys
import json
import time
import argparse
import datetime
import urllib.request
import urllib.error

API = "https://readwise.io/api/v3"
TOKEN = os.environ.get("READWISE_TOKEN")
ALLOWLIST_TAGS = {"morning-brief", "weekly-review", "sovereignty"}
ALLOWED_CATEGORY = "article"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Audit log: durable, in-vault, alongside todoist_audit.jsonl precedent.
AUDIT_PATH = os.path.expanduser("~/vault/3 Information/Scripts/readwise_cleanup_audit.jsonl")
# Highlight cache: durable + private + out-of-vault (regenerable; holds highlight text).
DATA_DIR = os.path.expanduser("~/.local/share/readwise-cleanup")
HL_CACHE = os.path.join(DATA_DIR, "highlights_cache.json")
DELETE_PAUSE_S = 1.5  # spacing between deletes


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def req(method, url, data=None, max_retries=5):
    if not TOKEN:
        die("READWISE_TOKEN not set. Run: source ~/.env.sh")
    headers = {"Authorization": f"Token {TOKEN}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(max_retries):
        r = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read().decode() if resp.length != 0 else ""
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "5"))
                print(f"  rate-limited; waiting {wait}s...", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            if e.code == 404:
                return 404, None
            body_txt = e.read().decode(errors="replace")
            die(f"{method} {url} -> HTTP {e.code}: {body_txt}")
        except urllib.error.URLError as e:
            print(f"  network error ({e}); retry {attempt+1}/{max_retries}", file=sys.stderr)
            time.sleep(3)
    die(f"{method} {url} failed after {max_retries} retries")


def normalize_tags(tags):
    """Reader returns tags as dict{name:..} or list[str] or list[dict]. Normalize to set of names."""
    if not tags:
        return set()
    if isinstance(tags, dict):
        return set(tags.keys())
    out = set()
    for t in tags:
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, dict) and "name" in t:
            out.add(t["name"])
    return out


def get_document(doc_id):
    status, payload = req("GET", f"{API}/list/?id={doc_id}")
    if status == 404 or not payload or not payload.get("results"):
        return None
    return payload["results"][0]


def load_all_highlights(refresh=False):
    """Fetch all Reader category=highlight docs once; map parent_id -> [highlights]. Cached in /tmp."""
    if not refresh and os.path.exists(HL_CACHE):
        try:
            with open(HL_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    print("  building highlight map (one-time crawl of Reader highlights)...", file=sys.stderr)
    by_parent = {}
    cursor = None
    pages = 0
    while True:
        url = f"{API}/list/?category=highlight"
        if cursor:
            url += f"&pageCursor={cursor}"
        status, payload = req("GET", url)
        if not payload:
            break
        for h in payload.get("results", []):
            pid = h.get("parent_id")
            if not pid:
                continue
            by_parent.setdefault(pid, []).append({
                "highlight_id": h.get("id"),
                "text": (h.get("content") or h.get("text") or "").strip(),
                "note": h.get("notes") or "",
            })
        cursor = payload.get("nextPageCursor")
        pages += 1
        if not cursor:
            break
        time.sleep(0.4)
    print(f"  crawled {pages} page(s); {sum(len(v) for v in by_parent.values())} highlights mapped.", file=sys.stderr)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(HL_CACHE, "w") as f:
            json.dump(by_parent, f)
    except Exception:
        pass
    return by_parent


def read_bucket(path):
    ids = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ids.append(line.split()[0])
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bucket_file", help="file with one document_id per line")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--refresh-highlights", action="store_true", help="force refetch of highlight map")
    args = ap.parse_args()

    ids = read_bucket(args.bucket_file)
    if not ids:
        die("no document IDs in bucket file")

    print(f"\n=== Readwise Reader cleanup — {'EXECUTE' if args.execute else 'DRY RUN'} ===")
    print(f"bucket: {args.bucket_file}  ({len(ids)} IDs)\n")

    # Pre-flight: fetch each doc, run the guard. Abort entirely on any violation.
    docs = []
    violations = []
    for doc_id in ids:
        d = get_document(doc_id)
        if d is None:
            violations.append((doc_id, "NOT FOUND (already gone?)"))
            continue
        cat = d.get("category")
        tags = normalize_tags(d.get("tags"))
        ok_cat = cat == ALLOWED_CATEGORY
        ok_tag = bool(tags & ALLOWLIST_TAGS)
        if not (ok_cat and ok_tag):
            reason = []
            if not ok_cat:
                reason.append(f"category={cat!r} (need {ALLOWED_CATEGORY!r})")
            if not ok_tag:
                reason.append(f"tags={sorted(tags)} (need one of {sorted(ALLOWLIST_TAGS)})")
            violations.append((doc_id, "; ".join(reason)))
        docs.append((doc_id, d, tags))

    hl_map = load_all_highlights(refresh=args.refresh_highlights)

    # Report
    print(f"{'TITLE':<48} {'CAT':<8} {'LOC':<8} {'HLs':<4} TAGS")
    print("-" * 100)
    total_hl = 0
    for doc_id, d, tags in docs:
        hls = hl_map.get(doc_id, [])
        total_hl += len(hls)
        title = (d.get("title") or "(untitled)")[:46]
        print(f"{title:<48} {str(d.get('category')):<8} {str(d.get('location')):<8} {len(hls):<4} {','.join(sorted(tags))}")
    print("-" * 100)
    print(f"{len(docs)} docs, {total_hl} highlights total\n")

    if violations:
        print("GUARD: FAIL — the following inputs are not eligible; NOTHING will be deleted:\n")
        for doc_id, why in violations:
            print(f"  {doc_id}: {why}")
        die("aborting due to guard violations (epub/wrong-tag/missing). Fix the bucket file.", code=2)
    print("GUARD: PASS — all inputs are articles carrying an allowlisted personal tag.\n")

    if not args.execute:
        print("DRY RUN complete. No deletions performed. Re-run with --execute to delete.\n")
        if total_hl:
            print(f"NOTE: {total_hl} highlight(s) will be captured to the audit log before deletion.\n")
        return

    # Execute
    print(f"EXECUTING deletion of {len(docs)} docs. Audit -> {AUDIT_PATH}\n")
    ts = datetime.datetime.now().isoformat()
    deleted = 0
    with open(AUDIT_PATH, "a") as audit:
        for doc_id, d, tags in docs:
            hls = hl_map.get(doc_id, [])
            status, _ = req("DELETE", f"{API}/delete/{doc_id}/")
            ok = status in (200, 204)
            entry = {
                "ts": ts,
                "action": "reader_delete",
                "document_id": doc_id,
                "title": d.get("title"),
                "author": d.get("author"),
                "category": d.get("category"),
                "location": d.get("location"),
                "tags": sorted(tags),
                "highlight_count": len(hls),
                "highlights": hls,
                "delete_http_status": status,
                "success": ok,
            }
            audit.write(json.dumps(entry) + "\n")
            audit.flush()
            mark = "ok" if ok else f"FAILED({status})"
            print(f"  [{mark}] {(d.get('title') or doc_id)[:60]}  ({len(hls)} hl captured)")
            if ok:
                deleted += 1
            time.sleep(DELETE_PAUSE_S)
    print(f"\nDone. {deleted}/{len(docs)} deleted. Audit appended to {AUDIT_PATH}\n")


if __name__ == "__main__":
    main()
