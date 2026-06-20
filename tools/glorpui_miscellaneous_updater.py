#!/usr/bin/env python3
"""
Miscellaneous GlorpUI updaters that regenerate mod files from the EU5 game files.

Each helper scrapes the live game install and rewrites a generated mod file so the
mod stays in sync across game patches. Run after updating to a new EU5 version.

Currently included:
  - Positive-trait filter triggers: regenerates glorpui_has_positive_<category>_trait
    scripted triggers (an OR of every non-bad trait in the category) for the categories
    the engine can't filter positively on its own, consumed by the "Has Positive X
    Trait" character search filters. A trait counts as negative if it has is_bad = yes
    or a negative custom tag.

The game directory is auto-detected from the known Steam install locations. Set
'game_directory' (or 'beta_game_directory') in tools/config.toml to override, or
pass --game-dir.

Usage:
    python tools/glorpui_miscellaneous_updater.py                 # standard EU5 install
    python tools/glorpui_miscellaneous_updater.py -b              # closed beta install
    python tools/glorpui_miscellaneous_updater.py --game-dir DIR  # explicit game directory
"""

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "config.toml"

STEAM_GAME_PATHS = [
    Path(r"C:\Steam\steamapps\common\Europa Universalis V\game"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Europa Universalis V\game"),
    Path(r"C:\Program Files\Steam\steamapps\common\Europa Universalis V\game"),
]

BETA_STEAM_GAME_PATHS = [
    Path(r"C:\Steam\steamapps\common\Project Caesar Review\game"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Project Caesar Review\game"),
    Path(r"C:\Program Files\Steam\steamapps\common\Project Caesar Review\game"),
]

UTF8_BOM = b"\xef\xbb\xbf"

TRAITS_SUBPATH = Path("in_game") / "common" / "traits"
TRAIT_TRIGGER_OUTPUT = (
    PROJECT_ROOT
    / "in_game"
    / "common"
    / "scripted_triggers"
    / "glorpui_generated_trait_scripted_triggers.txt"
)

# Categories the engine can't filter positively on its own, so the filter needs a
# generated OR-list of their non-bad traits. Army (general) and navy (admiral) are
# absent: they have no flagged-negative traits, so has_trait_category handles them.
POSITIVE_TRIGGER_CATEGORIES = ["cabinet", "ruler"]

TRAIT_BLOCK_RE = re.compile(r"^(\w+)\s*=\s*\{")
CATEGORY_RE = re.compile(r"^category\s*=\s*(\w+)")
IS_BAD_RE = re.compile(r"^is_bad\s*=\s*yes\b")
NEGATIVE_TAG_RE = re.compile(r"\bnegative\b")


def parse_braces(line):
    """Return (net_depth_change, has_open) for a line, ignoring strings and comments."""
    in_string = False
    depth = 0
    has_open = False
    for ch in line:
        if ch == "#" and not in_string:
            break
        if ch == '"':
            in_string = not in_string
        if not in_string:
            if ch == "{":
                depth += 1
                has_open = True
            elif ch == "}":
                depth -= 1
    return depth, has_open


def find_block_end(lines, start):
    """Find the end of a brace-delimited block starting at `start`. Returns end index (exclusive)."""
    brace_depth = 0
    seen_open = False
    i = start
    while i < len(lines):
        delta, has_open = parse_braces(lines[i])
        brace_depth += delta
        if has_open:
            seen_open = True
        i += 1
        if seen_open and brace_depth <= 0:
            break
    return i


def _block_category(lines, start, end):
    """Return the trait block's category value, or None."""
    for i in range(start, end):
        match = CATEGORY_RE.match(lines[i].strip())
        if match:
            return match.group(1)
    return None


def _block_is_negative(lines, start, end):
    """Return True if a trait block is flagged negative (is_bad = yes or a negative tag)."""
    for i in range(start, end):
        stripped = lines[i].strip()
        if IS_BAD_RE.match(stripped):
            return True
        if stripped.startswith("custom_tags") and NEGATIVE_TAG_RE.search(stripped):
            return True
    return False


def collect_traits(game_root):
    """Return (name, category, is_negative) for every trait across all trait files."""
    traits_dir = game_root / TRAITS_SUBPATH
    if not traits_dir.is_dir():
        print(f"ERROR: Traits directory not found: {traits_dir}")
        sys.exit(1)

    traits = []
    for path in sorted(traits_dir.glob("*.txt")):
        try:
            lines = path.read_text(encoding="utf-8-sig").split("\n")
        except (OSError, UnicodeDecodeError):
            continue

        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue

            match = TRAIT_BLOCK_RE.match(stripped)
            if match:
                end = find_block_end(lines, i)
                category = _block_category(lines, i, end)
                if category is not None:
                    traits.append(
                        (match.group(1), category, _block_is_negative(lines, i, end))
                    )
                i = end
                continue

            if "{" in stripped:
                i = find_block_end(lines, i)
            else:
                i += 1

    return traits


def write_trait_triggers(traits):
    """Write the generated glorpui_has_positive_<category>_trait scripted triggers (BOM + LF)."""
    blocks = []
    for category in POSITIVE_TRIGGER_CATEGORIES:
        positive = [n for (n, cat, neg) in traits if cat == category and not neg]
        block = [f"glorpui_has_positive_{category}_trait = {{", "\tOR = {"]
        block += [f"\t\thas_trait = {name}" for name in positive]
        block += ["\t}", "}"]
        blocks.append("\n".join(block))

    header = (
        "# Auto-generated by tools/glorpui_miscellaneous_updater.py - positive (non-bad) "
        "traits per category from the EU5 game files."
    )
    text = header + "\n\n" + "\n\n".join(blocks) + "\n"

    TRAIT_TRIGGER_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    TRAIT_TRIGGER_OUTPUT.write_bytes(UTF8_BOM + text.encode("utf-8"))


def _load_config():
    """Return parsed config.toml as a dict, or empty dict if unavailable."""
    if tomllib is None:
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _resolve_game_dir(args):
    """Resolve the EU5 game root directory.

    Honors --game-dir, then config.toml (game_directory / beta_game_directory),
    then the known Steam install locations. Exits if no game directory is found.
    """
    if args.game_dir:
        game_dir = Path(args.game_dir)
        if game_dir.is_dir():
            return game_dir
        print(f"ERROR: Game directory not found: {game_dir}")
        sys.exit(1)

    cfg = _load_config()
    if args.beta:
        cfg_dir = cfg.get("beta_game_directory", "")
        search_paths = BETA_STEAM_GAME_PATHS
        config_key = "beta_game_directory"
        label = "EU5 closed beta"
    else:
        cfg_dir = cfg.get("game_directory", "")
        search_paths = STEAM_GAME_PATHS
        config_key = "game_directory"
        label = "EU5 game"

    if cfg_dir and Path(cfg_dir).is_dir():
        return Path(cfg_dir)

    for p in search_paths:
        if p.is_dir():
            return p

    print(f"ERROR: Could not locate {label} directory.")
    print(f"Set '{config_key}' in config.toml or use --game-dir.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate GlorpUI mod files scraped from the EU5 game files."
    )
    parser.add_argument(
        "-b",
        "--beta",
        action="store_true",
        help="Read from the closed beta install (Project Caesar Review).",
    )
    parser.add_argument(
        "--game-dir",
        help="EU5 game directory (overrides config.toml and auto-detection).",
    )
    args = parser.parse_args()

    game_root = _resolve_game_dir(args)
    print(f"Source: {game_root}")

    traits = collect_traits(game_root)
    write_trait_triggers(traits)

    for category in POSITIVE_TRIGGER_CATEGORIES:
        positive = sum(1 for (_, cat, neg) in traits if cat == category and not neg)
        total = sum(1 for (_, cat, _) in traits if cat == category)
        print(f"Positive {category} traits: {positive}/{total}")
    print(f"-> {TRAIT_TRIGGER_OUTPUT.relative_to(PROJECT_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
