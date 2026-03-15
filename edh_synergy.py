#!/usr/bin/env python3
"""
EDH Synergy Graph Analyzer — Tag-based
Reads a decklist CSV, fetches oracle tags from Scryfall via an
inverted tag lookup (~1 request per tag), then scores synergies
using a tag-pair synergy table.

First run: fetches tags from Scryfall and caches to <deckname>_tags.json
Subsequent runs: loads from cache instantly, zero API calls.
"""

import csv
import re
import sys
import time
import json
import argparse
from pathlib import Path
from itertools import combinations
from collections import defaultdict

import requests
import networkx as nx
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

# ── Constants ─────────────────────────────────────────────────────────────────

SCRYFALL_BASE = "https://api.scryfall.com"

CATEGORY_COLORS = {
    "graveyard": "#1D9E75",
    "sacrifice":  "#D85A30",
    "draw":       "#378ADD",
    "ramp":       "#BA7517",
    "removal":    "#888780",
    "token":      "#7F77DD",
    "other":      "#B4B2A9",
}

EDGE_COLORS = {
    "strong":   "#D85A30",
    "moderate": "#378ADD",
    "light":    "#AAAAAA",
}

# ── CSV loaders ───────────────────────────────────────────────────────────────

# Priority-ordered tag → category mapping for auto-inference
CATEGORY_FROM_TAGS = [
    ("sacrifice",  {"sacrifice-outlet", "death-trigger", "blood-artist-ability",
                    "sacrifice-self", "persist", "gives-persist",
                    "gains-undying", "gives-undying"}),
    ("graveyard",  {"mill", "self-mill", "recursion", "reanimate",
                    "affinity-for-graveyard", "affinity-for-spells-in-graveyard",
                    "graveyard-payoff", "dredge", "gives-dredge",
                    "threshold", "undergrowth"}),
    ("token",      {"repeatable-token-generator", "repeatable-creature-tokens",
                    "synergy-token-creature", "token-doubler"}),
    ("draw",       {"draw", "pure-draw", "card-advantage", "loot", "impulse-draw"}),
    ("ramp",       {"mana-ramp", "mana-dork", "fetchland", "extra-land",
                    "land-ramp", "bounceland"}),
    ("removal",    {"creature-removal", "artifact-removal", "enchantment-removal",
                    "multi-removal", "boardwipe", "mass-removal"}),
]

def infer_category(tags: set[str]) -> str:
    """Infer a display category from a card's tags using priority order."""
    for category, tag_set in CATEGORY_FROM_TAGS:
        if tags & tag_set:
            return category
    return "other"


def _make_card(name: str, category: str = "") -> dict:
    """Create a card dict with consistent structure."""
    return {
        "name":              name,
        "category":          category.strip().lower(),
        "category_from_csv": bool(category.strip() and category.strip().lower() != "other"),
        "tags":              set(),
    }


def load_decklist_csv(path: str) -> list[dict]:
    """Load from our CSV format (columns: name, optional category)."""
    cards = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        has_category = "category" in (reader.fieldnames or [])
        for row in reader:
            name = row.get("name", "").strip()
            if not name:
                continue
            category = row.get("category", "").strip() if has_category else ""
            cards.append(_make_card(name, category))
    msg = f"  Loaded {len(cards)} cards from {path}"
    if not has_category:
        msg += " (no category column — will infer from tags)"
    print(msg)
    return cards


def load_decklist_text(path: str) -> list[dict]:
    """
    Load from plain text format — one card per line.
    Handles common formats:
      Card Name
      1 Card Name
      1x Card Name
      1x Card Name (SET)
      1x Card Name (SET) 123
    Lines starting with // or # are treated as section headers and skipped.
    """
    cards = []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            # Strip quantity prefix: "1x ", "1 ", "4x " etc.
            import re as _re
            line = _re.sub(r"^\d+x?\s+", "", line)
            # Strip set/collector info: "(SET) 123" or "(SET)"
            line = _re.sub(r"\s*\([^)]+\)\s*\d*\s*$", "", line).strip()
            if line and line not in seen:
                cards.append(_make_card(line))
                seen.add(line)
    print(f"  Loaded {len(cards)} cards from {path} (text format — categories will be inferred)")
    return cards


def load_decklist_archidekt(deck_id: str) -> list[dict]:
    """
    Fetch a deck from Archidekt by deck ID or URL.
    Extracts card names and uses Archidekt categories where available.
    """
    # Extract numeric ID from a full URL if needed
    import re as _re
    match = _re.search(r"/decks/(\d+)", deck_id)
    if match:
        deck_id = match.group(1)
    deck_id = deck_id.strip()

    print(f"  Fetching deck {deck_id} from Archidekt...")
    r = requests.get(
        f"https://archidekt.com/api/decks/{deck_id}/",
        headers={"User-Agent": "SynergyGraphAnalyzer/1.0"},
        timeout=15,
    )
    if not r.ok:
        print(f"  Error: Archidekt returned HTTP {r.status_code}")
        sys.exit(1)

    data = r.json()
    cards = []
    seen = set()

    for card_entry in data.get("cards", []):
        # Skip cards in categories marked as not part of the deck
        categories = card_entry.get("categories", [])
        if any(c.get("isPrimary") == False for c in categories):
            continue

        oracle = card_entry.get("card", {}).get("oracleCard", {})
        name = oracle.get("name", "").strip()
        if not name or name in seen:
            continue

        # Use the primary Archidekt category if available
        category = ""
        for c in categories:
            if c.get("isPrimary"):
                category = c.get("name", "").strip().lower()
                break

        cards.append(_make_card(name, category))
        seen.add(name)

    deck_name = data.get("name", deck_id)
    print(f"  Loaded {len(cards)} cards from Archidekt deck: {deck_name}")
    return cards


def load_decklist(source: str) -> list[dict]:
    """
    Auto-detect input type and load decklist accordingly.
    Accepts:
      - Path to a .csv file
      - Path to a .txt file (plain text format)
      - Archidekt deck ID (numeric) or URL (archidekt.com/decks/...)
    """
    # Archidekt URL or numeric ID
    if source.startswith("http") or source.isdigit():
        return load_decklist_archidekt(source)

    path = Path(source)
    if not path.exists():
        print(f"Error: file not found: {source}")
        sys.exit(1)

    if path.suffix.lower() == ".csv":
        return load_decklist_csv(source)
    else:
        # Treat any non-CSV file as plain text (covers .txt, .dec, .mwdeck etc.)
        return load_decklist_text(source)


def load_synergy_table(path: str) -> list[dict]:
    rules = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tag_a     = row.get("tag_a", "").strip().lower()
            tag_b     = row.get("tag_b", "").strip().lower()
            label     = row.get("label", "").strip()
            archetype = row.get("archetype", "").strip().lower()
            try:
                weight = max(1, min(3, int(row.get("weight", 1))))
            except ValueError:
                weight = 1
            if tag_a and tag_b:
                rules.append({"tag_a": tag_a, "tag_b": tag_b,
                               "weight": weight, "label": label,
                               "archetype": archetype})
    print(f"  Loaded {len(rules)} synergy rules from {path}")
    return rules

# ── Name normalisation ────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """
    Strip punctuation and lowercase for fuzzy name matching.
    'Kokusho, the Evening Star' -> 'kokusho the evening star'
    'Yahenni, Undying Partisan' -> 'yahenni undying partisan'
    """
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()

# ── Scryfall tag fetching ─────────────────────────────────────────────────────

def fetch_tags_inverted(cards: list[dict], known_tags: set[str],
                        session: requests.Session) -> list[dict]:
    """
    For each tag in the synergy table, query Scryfall once to get all cards
    with that tag, then check which are in our decklist.
    API calls: len(known_tags) -- typically 50-80.
    """
    # Index each deck card under multiple name variants to handle DFCs
    # and decklist names missing punctuation (commas, apostrophes etc.)
    name_lookup = {}
    for card in cards:
        for variant in (card["name"].lower(),
                        normalize_name(card["name"])):
            name_lookup[variant] = card

    total = len(known_tags)
    print(f"  Querying {total} tags (one request each)...")

    for i, tag in enumerate(sorted(known_tags), start=1):
        try:
            url    = f"{SCRYFALL_BASE}/cards/search"
            params = {"q": f"oracletag:{tag}", "order": "name"}
            tagged_names = set()

            while url:
                r = session.get(url, params=params, timeout=10)
                params = {}  # only pass params on first request
                if r.status_code == 404:
                    break    # tag doesn't exist on Scryfall
                if not r.ok:
                    print(f"    Warning: tag '{tag}' returned HTTP {r.status_code}")
                    break
                data = r.json()
                for sf_card in data.get("data", []):
                    full = sf_card["name"].lower()
                    tagged_names.add(full)
                    tagged_names.add(normalize_name(full))
                    # For DFCs, also index the front face name alone
                    if "//" in sf_card["name"]:
                        front = sf_card["name"].split("//")[0].strip().lower()
                        tagged_names.add(front)
                        tagged_names.add(normalize_name(front))
                url = data.get("next_page")
                if url:
                    time.sleep(0.05)

            matched = 0
            for variant, card in name_lookup.items():
                if variant in tagged_names:
                    card["tags"].add(tag)
                    matched += 1

            print(f"    [{i:>3}/{total}] oracletag:{tag:<42} -> {matched} deck cards")

        except Exception as e:
            print(f"    Error querying tag '{tag}': {e}")

        time.sleep(0.08)

    return cards


def fetch_edhrec_ranks(cards: list[dict],
                       session: requests.Session) -> list[dict]:
    """
    Fetch edhrec_rank for all cards in one or two API calls
    using the /cards/collection batch endpoint (max 75 per request).
    Stores rank in card["edhrec_rank"]; cards not found get None.
    """
    identifiers = [{"name": c["name"]} for c in cards]
    name_lookup = {}
    for card in cards:
        name_lookup[card["name"].lower()]         = card
        name_lookup[normalize_name(card["name"])] = card

    print(f"  Fetching EDHREC ranks (batch)...")
    for batch_start in range(0, len(identifiers), 75):
        batch = identifiers[batch_start:batch_start + 75]
        r = session.post(
            f"{SCRYFALL_BASE}/cards/collection",
            json={"identifiers": batch},
            timeout=15,
        )
        if not r.ok:
            print(f"  Warning: batch fetch failed (HTTP {r.status_code})")
            continue
        data = r.json()
        for sf_card in data.get("data", []):
            name = sf_card.get("name", "").lower()
            rank = sf_card.get("edhrec_rank")
            for variant in (name, normalize_name(name)):
                if variant in name_lookup:
                    name_lookup[variant]["edhrec_rank"] = rank
            if "//" in sf_card.get("name", ""):
                front = sf_card["name"].split("//")[0].strip().lower()
                for variant in (front, normalize_name(front)):
                    if variant in name_lookup:
                        name_lookup[variant]["edhrec_rank"] = rank
        for nf in data.get("not_found", []):
            print(f"    Not found: {nf.get('name', nf)}")
        time.sleep(0.1)

    found = sum(1 for c in cards if c.get("edhrec_rank") is not None)
    print(f"  EDHREC ranks fetched for {found}/{len(cards)} cards.")
    return cards


def fetch_all_cards(cards: list[dict], known_tags: set[str]) -> list[dict]:
    """Fetch EDHREC ranks and oracle tags for all cards."""
    session = requests.Session()
    session.headers.update({"User-Agent": "SynergyGraphAnalyzer/1.0"})
    cards = fetch_edhrec_ranks(cards, session)
    cards = fetch_tags_inverted(cards, known_tags, session)
    infer_missing_categories(cards)
    return cards


def infer_missing_categories(cards: list[dict]) -> None:
    """For cards without an explicit CSV category, infer from tags."""
    inferred = 0
    for card in cards:
        if not card.get("category_from_csv"):
            card["category"] = infer_category(card.get("tags", set()))
            inferred += 1
    if inferred:
        print(f"  Inferred categories for {inferred} cards from tags.")

# ── Cache ─────────────────────────────────────────────────────────────────────

def load_tags_from_cache(cards: list[dict], cache_path: str) -> list[dict]:
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    for card in cards:
        if card["name"] in cache:
            card["tags"]        = set(cache[card["name"]]["tags"])
            card["edhrec_rank"] = cache[card["name"]].get("edhrec_rank")
    infer_missing_categories(cards)
    print(f"  Loaded tag cache from {cache_path}")
    return cards


def save_tags_to_cache(cards: list[dict], cache_path: str) -> None:
    cache = {c["name"]: {
        "tags":        sorted(c.get("tags", set())),
        "edhrec_rank": c.get("edhrec_rank"),
    } for c in cards}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(f"  Tag cache saved to {cache_path}")

# ── Synergy matching ──────────────────────────────────────────────────────────

def find_edges(cards: list[dict], rules: list[dict],
               archetype_filter: str | None = None) -> list[dict]:
    """
    For each rule, find all card pairs where one card has tag_a and the
    other has tag_b. Accumulate weight across all matching rules.
    Also handles the stacking case where both cards share the same tag pair.
    """
    filtered_rules = rules
    if archetype_filter:
        filtered_rules = [r for r in rules if r["archetype"] == archetype_filter]
        print(f"  Filtering to archetype: {archetype_filter} ({len(filtered_rules)} rules)")

    n = len(cards)
    print(f"  Matching {len(filtered_rules)} rules across "
          f"{n} cards ({n*(n-1)//2} pairs)...")

    tag_to_cards = defaultdict(set)
    for card in cards:
        for tag in card.get("tags", set()):
            tag_to_cards[tag].add(card["name"])

    edges = {}

    for rule in filtered_rules:
        ta, tb = rule["tag_a"], rule["tag_b"]
        cards_with_a = tag_to_cards.get(ta, set())
        cards_with_b = tag_to_cards.get(tb, set())

        # Directed: A has tag_a, B has tag_b (and reverse)
        for name_a in cards_with_a:
            for name_b in cards_with_b:
                if name_a == name_b:
                    continue
                key = (name_a, name_b) if name_a < name_b else (name_b, name_a)
                if key not in edges:
                    edges[key] = {"source": key[0], "target": key[1],
                                  "weight": 0, "reasons": []}
                edges[key]["weight"] += rule["weight"]
                if rule["label"] not in edges[key]["reasons"]:
                    edges[key]["reasons"].append(rule["label"])

        # Stacking: both cards have tag_a AND tag_b
        if ta != tb:
            both = cards_with_a & cards_with_b
            for name_a, name_b in combinations(both, 2):
                key = (name_a, name_b) if name_a < name_b else (name_b, name_a)
                if key not in edges:
                    edges[key] = {"source": key[0], "target": key[1],
                                  "weight": 0, "reasons": []}
                stack_w = max(1, rule["weight"] - 1)
                edges[key]["weight"] += stack_w
                label = f"{rule['label']} (stacks)"
                if label not in edges[key]["reasons"]:
                    edges[key]["reasons"].append(label)

    edge_list = list(edges.values())
    print(f"  Found {len(edge_list)} synergy edges.")
    return edge_list

# ── Graph ─────────────────────────────────────────────────────────────────────

def build_graph(cards: list[dict], edges: list[dict]) -> nx.Graph:
    G = nx.Graph()
    for c in cards:
        G.add_node(c["name"],
                   category=c.get("category", "other"),
                   tags=c.get("tags", set()))
    for e in edges:
        if G.has_node(e["source"]) and G.has_node(e["target"]):
            G.add_edge(e["source"], e["target"],
                       weight=e["weight"], reasons=e["reasons"])
    return G


def compute_scores(G: nx.Graph) -> dict:
    return {n: sum(d["weight"] for _, _, d in G.edges(n, data=True))
            for n in G.nodes}

# ── Reporting ─────────────────────────────────────────────────────────────────

def print_tag_summary(cards: list[dict]) -> None:
    tag_counts = defaultdict(int)
    for card in cards:
        for tag in card.get("tags", set()):
            tag_counts[tag] += 1
    ranked = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    print(f"\n{'─'*55}")
    print("  Tags found in this deck (by frequency)")
    print(f"{'─'*55}")
    for tag, count in ranked:
        bar = "█" * count
        print(f"  {tag:<42} {bar} {count}")


def print_top_cards(G: nx.Graph, scores: dict, top_n: int = 15) -> None:
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    max_s  = ranked[0][1] if ranked else 1
    print(f"\n{'─'*60}")
    print(f"  Top {top_n} cards by synergy score")
    print(f"{'─'*60}")
    for name, score in ranked[:top_n]:
        cat = G.nodes[name].get("category", "?")
        bar = "█" * int(score / max_s * 24)
        print(f"  {name:<35} {bar:<24} {score:>4}  [{cat}]")


def print_strongest_edges(G: nx.Graph, top_n: int = 15) -> None:
    edges = sorted(G.edges(data=True), key=lambda e: e[2]["weight"], reverse=True)
    print(f"\n{'─'*75}")
    print(f"  Top {top_n} synergy connections")
    print(f"{'─'*75}")
    for u, v, d in edges[:top_n]:
        w = d["weight"]
        bar = "●" * min(w, 10)
        reasons = ", ".join(d.get("reasons", [])[:3])
        print(f"  {bar:<10} {u:<30} <-> {v:<30}  {reasons}")


def print_untagged_cards(cards: list[dict]) -> None:
    untagged = [c["name"] for c in cards if not c.get("tags")]
    if untagged:
        print(f"\n{'─'*50}")
        print("  Cards with no tags found")
        print(f"{'─'*50}")
        for name in untagged:
            print(f"  * {name}")


def export_edges_csv(G: nx.Graph, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["source", "target", "weight", "reasons"])
        for u, v, d in sorted(G.edges(data=True),
                               key=lambda e: e[2]["weight"], reverse=True):
            writer.writerow([u, v, d["weight"],
                             " | ".join(d.get("reasons", []))])
    print(f"  Edges exported to: {path}")


def export_scores_csv(G: nx.Graph, scores: dict, cards: list[dict],
                      path: str, power_weight: float = 0.4) -> None:
    """
    Export per-card scores. Includes:
      synergy_score    — raw accumulated edge weight
      connection_count — number of synergy connections
      edhrec_rank      — from Scryfall (lower = more played)
      power_score      — normalised 0-1 from edhrec_rank (higher = better)
      combined_score   — weighted average of synergy and power (0-1 each)
    """
    card_meta = {c["name"]: c for c in cards}

    # Normalise synergy scores to 0-1
    raw_scores = list(scores.values())
    max_syn = max(raw_scores) if raw_scores else 1
    min_syn = min(raw_scores) if raw_scores else 0
    syn_range = max_syn - min_syn or 1

    # Normalise EDHREC rank to 0-1 (lower rank = higher power score)
    ranks = [c.get("edhrec_rank") for c in cards if c.get("edhrec_rank") is not None]
    max_rank = max(ranks) if ranks else 1
    min_rank = min(ranks) if ranks else 0
    rank_range = max_rank - min_rank or 1

    synergy_weight = 1.0 - power_weight

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "category", "synergy_score", "connection_count",
                         "edhrec_rank", "power_score", "combined_score", "tags"])
        for name, score in ranked:
            meta     = card_meta.get(name, {})
            category = G.nodes[name].get("category", "other")
            conns    = G.degree(name)
            tags     = " | ".join(sorted(meta.get("tags", set())))
            rank     = meta.get("edhrec_rank")

            norm_syn  = (score - min_syn) / syn_range
            if rank is not None:
                # Invert rank: lower rank (more played) = higher power score
                norm_power = 1.0 - (rank - min_rank) / rank_range
            else:
                norm_power = ""

            if norm_power != "":
                combined = round(synergy_weight * norm_syn +
                                 power_weight   * norm_power, 4)
            else:
                combined = ""

            writer.writerow([name, category, score, conns,
                             rank if rank is not None else "",
                             round(norm_power, 4) if norm_power != "" else "",
                             combined, tags])
    print(f"  Scores exported to: {path}")

# ── Visualisation ─────────────────────────────────────────────────────────────

def edge_color(weight: int) -> str:
    if weight >= 5:   return EDGE_COLORS["strong"]
    elif weight >= 3: return EDGE_COLORS["moderate"]
    return EDGE_COLORS["light"]


def draw_graph(G: nx.Graph, scores: dict, title: str,
               min_weight: int = 2, save_path: str | None = None) -> None:
    edges_to_show = [(u, v) for u, v, d in G.edges(data=True)
                     if d["weight"] >= min_weight]
    if not edges_to_show:
        print(f"  No edges at min_weight={min_weight}.")
        return

    sub = G.edge_subgraph(edges_to_show).copy()
    if not sub.nodes:
        print("  No nodes to display.")
        return

    node_sizes  = [100 + scores.get(n, 0) * 20 for n in sub.nodes]
    node_colors = [CATEGORY_COLORS.get(sub.nodes[n].get("category", "other"),
                                        "#888780") for n in sub.nodes]
    ec = [edge_color(sub[u][v]["weight"]) for u, v in sub.edges]
    ew = [min(sub[u][v]["weight"] * 0.4, 4.0) for u, v in sub.edges]

    k = 2.5 / max(len(sub.nodes) ** 0.5, 1)
    pos = nx.spring_layout(sub, seed=42, k=k, iterations=80)

    fig, ax = plt.subplots(figsize=(20, 13))
    fig.patch.set_facecolor("#1a1a1a")
    ax.set_facecolor("#1a1a1a")
    ax.set_title(title, color="white", fontsize=14, pad=14)

    nx.draw_networkx_edges(sub, pos, ax=ax, edge_color=ec, width=ew, alpha=0.5)
    nx.draw_networkx_nodes(sub, pos, ax=ax, node_size=node_sizes,
                            node_color=node_colors, alpha=0.9)
    labels = {n: (n[:15] + "..." if len(n) > 16 else n) for n in sub.nodes}
    nx.draw_networkx_labels(sub, pos, labels, ax=ax,
                             font_size=7, font_color="white")

    cat_patches = [mpatches.Patch(color=c, label=cat.capitalize())
                   for cat, c in CATEGORY_COLORS.items()
                   if any(sub.nodes[n].get("category") == cat for n in sub.nodes)]
    edge_legend = [
        Line2D([0],[0], color=EDGE_COLORS["strong"],   lw=2.5, label="Strong (>=5)"),
        Line2D([0],[0], color=EDGE_COLORS["moderate"], lw=1.8, label="Moderate (3-4)"),
        Line2D([0],[0], color=EDGE_COLORS["light"],    lw=1.0, label="Light (1-2)"),
    ]
    ax.legend(handles=cat_patches + edge_legend, loc="lower left",
               fontsize=8, framealpha=0.3, facecolor="#111", labelcolor="white")
    ax.axis("off")
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        print(f"  Saved: {save_path}")
    plt.show()

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Commander Synergy Graph -- Scryfall tag-based"
    )
    p.add_argument("--decklist",     default="decklist.csv",
                   help="Decklist source: CSV file, plain text file (.txt), or Archidekt deck ID / URL")
    p.add_argument("--tags",         default="synergy_tags.csv",
                   help="Synergy tag-pair table CSV")
    p.add_argument("--cache",        default=None,
                   help="Tag cache JSON (auto-named from decklist if omitted)")
    p.add_argument("--no-fetch",     action="store_true",
                   help="Skip fetching; use existing cache only (cache must exist)")
    p.add_argument("--min-weight",   type=int, default=3,
                   help="Minimum edge weight to display (default: 3)")
    p.add_argument("--archetype",    default=None,
                   help="Filter synergy rules to one archetype (e.g. aristocrats)")
    p.add_argument("--top",          type=int, default=15,
                   help="Number of top results to print")
    p.add_argument("--export-edges", action="store_true",
                   help="Export all detected edges to <deck>_edges.csv")
    p.add_argument("--power-weight", type=float, default=0.4,
                   help="Weight given to EDHREC power score in combined score (default: 0.4)")
    p.add_argument("--no-display",   action="store_true",
                   help="Save PNG without opening a window")
    return p.parse_args()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    print("=" * 60)
    print("  Commander Synergy Graph Analyzer -- Tag-based")
    print("=" * 60)

    for path in [args.decklist, args.tags]:
        if not Path(path).exists():
            print(f"Error: file not found: {path}")
            sys.exit(1)

    cache_path = args.cache or Path(args.decklist).stem + "_tags.json"

    print("\n[1/4] Loading inputs...")
    cards = load_decklist(args.decklist)
    rules = load_synergy_table(args.tags)
    known_tags = {r["tag_a"] for r in rules} | {r["tag_b"] for r in rules}
    print(f"  Synergy table references {len(known_tags)} unique tags.")

    print("\n[2/4] Fetching card tags...")
    if args.no_fetch:
        if not Path(cache_path).exists():
            print(f"Error: --no-fetch requires cache: {cache_path}")
            sys.exit(1)
        cards = load_tags_from_cache(cards, cache_path)
    else:
        if Path(cache_path).exists():
            print(f"  Cache found at {cache_path}, loading...")
            cards = load_tags_from_cache(cards, cache_path)
        else:
            cards = fetch_all_cards(cards, known_tags)
            save_tags_to_cache(cards, cache_path)

    print_untagged_cards(cards)
    print_tag_summary(cards)

    print("\n[3/4] Detecting synergies...")
    edges  = find_edges(cards, rules, archetype_filter=args.archetype)
    G      = build_graph(cards, edges)
    scores = compute_scores(G)

    print_top_cards(G, scores, top_n=args.top)
    print_strongest_edges(G, top_n=args.top)

    if args.export_edges:
        export_edges_csv(G, f"{Path(args.decklist).stem}_edges.csv")

    export_scores_csv(G, scores, cards, f"{Path(args.decklist).stem}_scores.csv",
                      power_weight=args.power_weight)

    print("\n[4/4] Rendering graph...")
    if args.no_display:
        import matplotlib
        matplotlib.use("Agg")

    deck_stem = Path(args.decklist).stem
    draw_graph(G, scores,
               title=f"Synergy Graph -- {deck_stem} (weight >= {args.min_weight})",
               min_weight=args.min_weight,
               save_path=f"{deck_stem}_synergy.png")

    print("\nDone.")


if __name__ == "__main__":
    main()
