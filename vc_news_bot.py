"""Daily VC news digest poster.

Reads a list of VCs from feeds.yaml, queries Google News RSS for each,
dedupes, filters to the last 36 hours, and posts fresh items to a Discord
channel via webhook. Tracks posted items in state.json so we never repeat.

Runs only when Europe/Rome local time is 09:xx, unless FORCE_RUN is set.
Set DRY_RUN=1 to print instead of posting.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROME = ZoneInfo("Europe/Rome")
ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state.json"
FEEDS_PATH = ROOT / "feeds.yaml"

WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
FORCE_RUN = bool(os.environ.get("FORCE_RUN"))
DRY_RUN = bool(os.environ.get("DRY_RUN"))

WINDOW_HOURS = 36
MAX_ITEMS_PER_VC = 5
MAX_STATE_ENTRIES = 3000


def gnews_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def is_rome_9am() -> bool:
    return datetime.now(ROME).hour == 9


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def first_two_sentences(text: str) -> str:
    text = clean(text)
    if not text:
        return ""
    sents = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sents[:2])


def canonical_link(link: str) -> str:
    return (link or "").split("?")[0].rstrip("/")


def fetch_for_vc(name: str, query: str) -> list[dict]:
    feed = feedparser.parse(gnews_url(query))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    items: list[dict] = []
    for entry in feed.entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
        items.append({
            "vc": name,
            "title": clean(entry.get("title", "(no title)")),
            "link": entry.get("link", ""),
            "summary": first_two_sentences(entry.get("summary", "")),
        })
        if len(items) >= MAX_ITEMS_PER_VC:
            break
    return items


def dedupe_by_link(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = canonical_link(it["link"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def post(payload: dict) -> None:
    if DRY_RUN or not WEBHOOK:
        print("[dry-run]", json.dumps(payload)[:400])
        return
    r = requests.post(WEBHOOK, json=payload, timeout=30)
    if r.status_code == 429:
        wait = 2.0
        try:
            wait = float(r.json().get("retry_after", 2))
        except Exception:
            pass
        time.sleep(wait + 0.5)
        r = requests.post(WEBHOOK, json=payload, timeout=30)
    r.raise_for_status()


def post_digest(items: list[dict]) -> None:
    today = datetime.now(ROME).strftime("%a %d %b %Y")
    if not items:
        post({"content": f"📭 **VC news — {today}**\nNothing new in the last {WINDOW_HOURS}h."})
        return
    post({"content": f"📰 **VC news — {today}** · {len(items)} item(s)"})
    for i in range(0, len(items), 10):
        batch = items[i:i + 10]
        embeds = []
        for it in batch:
            desc = f"**{it['vc']}**"
            if it["summary"]:
                desc += f"\n{it['summary'][:400]}"
            embeds.append({
                "title": it["title"][:256] or "(no title)",
                "url": it["link"],
                "description": desc,
            })
        post({"embeds": embeds})
        time.sleep(1)


def main() -> int:
    if not WEBHOOK and not DRY_RUN:
        print("DISCORD_WEBHOOK not set", file=sys.stderr)
        return 1

    now_rome = datetime.now(ROME)
    if not is_rome_9am() and not FORCE_RUN:
        print(f"Skipping: Rome time is {now_rome.strftime('%H:%M')} (not 09:xx)")
        return 0

    config = yaml.safe_load(FEEDS_PATH.read_text())
    state = load_state()
    seen: set[str] = set(state.get("seen", []))
    first_run = len(seen) == 0

    all_items: list[dict] = []
    for vc in config["vcs"]:
        try:
            items = fetch_for_vc(vc["name"], vc["query"])
        except Exception as e:
            print(f"[warn] {vc['name']}: {e}", file=sys.stderr)
            continue
        all_items.extend(items)
        time.sleep(0.3)  # be kind to news.google.com

    all_items = dedupe_by_link(all_items)
    fresh = [it for it in all_items if canonical_link(it["link"]) not in seen]

    if first_run:
        post({"content": (
            f"🤖 **VC news bot online** — tracking {len(config['vcs'])} firms.\n"
            f"First real digest tomorrow at 09:00 Europe/Rome. "
            f"(Skipping {len(fresh)} pre-existing item(s) to avoid spam.)"
        )})
    else:
        post_digest(fresh)

    new_links = [canonical_link(it["link"]) for it in all_items]
    combined = state.get("seen", []) + [l for l in new_links if l]
    # keep last N unique
    dedup_seen: list[str] = []
    dedup_set: set[str] = set()
    for link in reversed(combined):
        if link in dedup_set:
            continue
        dedup_set.add(link)
        dedup_seen.append(link)
        if len(dedup_seen) >= MAX_STATE_ENTRIES:
            break
    state["seen"] = list(reversed(dedup_seen))
    save_state(state)

    print(f"Done. Posted {0 if first_run else len(fresh)} item(s). State has {len(state['seen'])} link(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
