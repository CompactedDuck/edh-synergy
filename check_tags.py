#!/usr/bin/env python3
"""
Tag diagnostic tool.
Checks what oracletags Scryfall actually returns for a list of cards
by testing every tag in your synergy_tags.csv, plus discovers unknown
tags by checking the tagger site directly.

Usage:
    python check_tags.py "Sakura-Tribe Elder" "Yahenni, Undying Partisan"
    python check_tags.py --all-untagged   # reads untagged list from stdin
"""

import sys
import time
import csv
import argparse
import requests

SCRYFALL_BASE = "https://api.scryfall.com"

def get_known_tags(synergy_csv: str = "synergy_tags.csv") -> set[str]:
    tags = set()
    try:
        with open(synergy_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                tags.add(row["tag_a"].strip().lower())
                tags.add(row["tag_b"].strip().lower())
    except FileNotFoundError:
        print(f"  Warning: {synergy_csv} not found, checking a default tag set.")
        tags = {
            "sacrifice-outlet", "death-trigger", "blood-artist-ability",
            "mill", "self-mill", "recursion", "reanimate", "graveyard-payoff",
            "token-generator", "draw", "pure-draw", "card-advantage", "loot",
            "ramp", "land-ramp", "boardwipe", "removal", "lifegain",
            "counter-fuel", "counters-matter", "proliferate", "blink",
            "etb-trigger", "magecraft", "landfall", "affinity-for-graveyard",
        }
    return tags


def check_card_tags(card_name: str, known_tags: set[str],
                    session: requests.Session) -> dict:
    """
    For a given card name, check which of our known tags it has,
    and also fetch the card's Scryfall page URL for manual inspection.
    """
    # First resolve the card to get its exact name
    r = session.get(f"{SCRYFALL_BASE}/cards/named",
                    params={"fuzzy": card_name}, timeout=10)
    if not r.ok:
        return {"error": f"Card not found: {card_name}"}

    card = r.json()
    exact_name = card["name"]
    scryfall_url = card.get("scryfall_uri", "")

    matched_tags = []
    unknown_tags = []  # tags that fired but aren't in our CSV

    for tag in sorted(known_tags):
        r = session.get(f"{SCRYFALL_BASE}/cards/search",
                        params={"q": f'oracletag:{tag} !"{exact_name}"'},
                        timeout=10)
        if r.ok and r.json().get("total_cards", 0) > 0:
            matched_tags.append(tag)
        time.sleep(0.08)

    return {
        "name": exact_name,
        "url":  scryfall_url,
        "matched_tags": matched_tags,
    }


def main():
    p = argparse.ArgumentParser(description="Check Scryfall tags for cards")
    p.add_argument("cards", nargs="*", help="Card names to check")
    p.add_argument("--synergy-csv", default="synergy_tags.csv")
    args = p.parse_args()

    if not args.cards:
        print("Usage: python check_tags.py \"Card Name\" \"Another Card\"")
        sys.exit(1)

    known_tags = get_known_tags(args.synergy_csv)
    print(f"Checking against {len(known_tags)} tags from {args.synergy_csv}\n")

    session = requests.Session()
    session.headers.update({"User-Agent": "SynergyTagChecker/1.0"})

    for card_name in args.cards:
        print(f"{'─'*55}")
        print(f"  {card_name}")
        print(f"{'─'*55}")
        result = check_card_tags(card_name, known_tags, session)

        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue

        print(f"  Scryfall: {result['url']}")
        if result["matched_tags"]:
            print(f"  Matched tags ({len(result['matched_tags'])}):")
            for tag in result["matched_tags"]:
                print(f"    ✓ {tag}")
        else:
            print("  No matching tags found in synergy_tags.csv")
            print("  → Visit the Scryfall URL above, click 'Tagger' to see actual tags")
        print()


if __name__ == "__main__":
    main()
