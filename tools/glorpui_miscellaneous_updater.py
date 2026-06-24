#!/usr/bin/env python3
"""
Miscellaneous GlorpUI updaters that regenerate mod files from the EU5 game files.

Each helper scrapes the live game install and rewrites a generated mod file so the
mod stays in sync across game patches. Run after updating to a new EU5 version.

Currently included:
  - Positive-category filter triggers: regenerates the four glorpui_has_positive_<category>_trait
    scripted triggers (army, navy, cabinet, ruler) consumed by the "Has Positive X Trait" character
    search filters. Each is an OR of the category's positive traits and the character static
    modifiers classified into that category. A trait counts as negative if it has is_bad = yes or a
    negative custom tag; a character modifier is classified by its positive (higher-is-better)
    effect keys.

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
CHARACTER_MODIFIERS_SUBPATH = Path("main_menu") / "common" / "static_modifiers"
TRAIT_TRIGGER_OUTPUT = (
    PROJECT_ROOT
    / "in_game"
    / "common"
    / "scripted_triggers"
    / "glorpui_generated_trait_scripted_triggers.txt"
)

# army/navy traits carry no negative members, so a has_trait_category leaf covers their trait
# side; cabinet/ruler need a per-trait OR-list because each has bad traits to exclude.
CATEGORY_ORDER = ["army", "navy", "cabinet", "ruler"]
CATEGORY_TRAIT_CATEGORY = {"army": "general", "navy": "admiral"}

# Effect keys that mark a character static modifier as an army/navy/cabinet/ruler bonus. All are
# higher-is-better, so a positive value means beneficial. The shared keys help both army and navy.
ARMY_EXCLUSIVE_KEYS = {
    "siege_ability",
    "assault_ability",
    "army_movement_speed",
    "army_initiative",
    "discipline",
    "land_morale_modifier",
    "land_morale_recovery",
    "levy_combat_efficiency_modifier",
    "global_levy_size_modifier",
    "global_army_levy_size_modifier",
    "army_maintenance_efficiency",
    "army_artillery_power",
    "army_heavy_infantry_power",
    "army_light_infantry_power",
    "army_heavy_cavalry_power",
    "army_light_cavalry_power",
    "army_tradition_from_battle",
    "artillery_bonus_vs_fort",
    "fort_assumed_efficiency_character",
    "prestige_from_land_battle",
}
NAVY_EXCLUSIVE_KEYS = {
    "blockade_efficiency",
    "naval_morale_modifier",
    "naval_damage_done",
    "navy_movement_speed",
    "navy_initiative",
    "navy_maintenance_efficiency",
    "navy_galley_power",
    "navy_heavy_ship_power",
    "navy_light_ship_power",
    "global_maritime_presence_modifier",
    "prestige_from_naval_battle",
    "ship_capture_chance",
}
SHARED_MILITARY_KEYS = {
    "commander_combat_bonus",
    "combat_speed_modifier",
    "military_tactics",
    "possible_frontage_modifier",
}
CABINET_KEYS = {
    "character_cabinet_efficiency",
    "country_cabinet_efficiency",
    "cabinet_trait_impact_modifier",
    "estate_power_from_cabinet",
}
RULER_KEYS = {
    "monthly_legitimacy",
    "monthly_prestige",
    "monthly_republican_tradition",
    "monthly_devotion",
    "global_crown_estate_power",
    "diplomatic_reputation",
}

TRAIT_BLOCK_RE = re.compile(r"^(\w+)\s*=\s*\{")
CATEGORY_RE = re.compile(r"^category\s*=\s*(\w+)")
IS_BAD_RE = re.compile(r"^is_bad\s*=\s*yes\b")
NEGATIVE_TAG_RE = re.compile(r"\bnegative\b")
EFFECT_RE = re.compile(r"^(\w+)\s*=\s*(\S+)")

# The engine applies these per-skill base modifiers to every ruler/general/admiral/explorer, so
# they are not notable bonuses worth surfacing in the filters.
BASE_ATTRIBUTE_MODIFIER_RE = re.compile(r"^(?:ruler|general|admiral|explorer)_(?:adm|dip|mil)$")


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


def _block_effects(lines, start, end):
    """Return {effect_key: float} for numeric modifier effects that are direct children of the block.

    Lines nested inside a sub-block (game_data, any triggered block) are skipped by depth, and
    non-numeric values (yes/no flags, named constants) are dropped.
    """
    effects = {}
    depth = 0
    for i in range(start, end):
        delta, _ = parse_braces(lines[i])
        if depth == 1 and delta == 0:
            match = EFFECT_RE.match(lines[i].strip())
            if match:
                try:
                    effects[match.group(1)] = float(match.group(2))
                except ValueError:
                    pass
        depth += delta
    return effects


def collect_character_modifiers(game_root):
    """Return (name, {effect_key: float, ...}) for every category=character static modifier."""
    mods_dir = game_root / CHARACTER_MODIFIERS_SUBPATH
    if not mods_dir.is_dir():
        print(f"ERROR: Static modifiers directory not found: {mods_dir}")
        sys.exit(1)

    modifiers = []
    for path in sorted(mods_dir.glob("*.txt")):
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
                name = match.group(1)
                if (
                    _block_category(lines, i, end) == "character"
                    and not BASE_ATTRIBUTE_MODIFIER_RE.match(name)
                ):
                    modifiers.append((name, _block_effects(lines, i, end)))
                i = end
                continue

            if "{" in stripped:
                i = find_block_end(lines, i)
            else:
                i += 1

    return modifiers


def _has_positive(effects, keys):
    """Return True if the modifier has a value above zero on any key in the set."""
    return any(effects.get(key, 0.0) > 0.0 for key in keys)


def classify_modifier(effects):
    """Return the set of categories ('army'/'navy'/'cabinet'/'ruler') a modifier is a positive bonus for.

    A shared military key (helps both army and navy) classifies into a branch only when the other
    branch has no exclusive key, so generic leadership lands in both while army-specific effects pin
    it to army.
    """
    categories = set()

    army = _has_positive(effects, ARMY_EXCLUSIVE_KEYS)
    navy = _has_positive(effects, NAVY_EXCLUSIVE_KEYS)
    shared = _has_positive(effects, SHARED_MILITARY_KEYS)
    if army or (shared and not navy):
        categories.add("army")
    if navy or (shared and not army):
        categories.add("navy")

    if _has_positive(effects, CABINET_KEYS):
        categories.add("cabinet")
    if _has_positive(effects, RULER_KEYS):
        categories.add("ruler")

    return categories


def write_category_triggers(traits, modifiers):
    """Write the generated glorpui_has_positive_<category>_trait scripted triggers (BOM + LF)."""
    classified = [(name, classify_modifier(effects)) for (name, effects) in modifiers]

    blocks = []
    for category in CATEGORY_ORDER:
        trait_category = CATEGORY_TRAIT_CATEGORY.get(category)
        if trait_category is not None:
            members = [f"has_trait_category = {trait_category}"]
        else:
            members = [
                f"has_trait = {name}"
                for (name, cat, neg) in traits
                if cat == category and not neg
            ]
        members += [
            f"has_character_modifier = {name}"
            for (name, cats) in classified
            if category in cats
        ]

        block = [f"glorpui_has_positive_{category}_trait = {{", "\tOR = {"]
        block += [f"\t\t{member}" for member in members]
        block += ["\t}", "}"]
        blocks.append("\n".join(block))

    header = (
        "# Auto-generated by tools/glorpui_miscellaneous_updater.py - positive traits and character "
        "static modifiers per category from the EU5 game files."
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
    modifiers = collect_character_modifiers(game_root)
    write_category_triggers(traits, modifiers)

    classified = [classify_modifier(effects) for (_, effects) in modifiers]
    for category in CATEGORY_ORDER:
        modifier_count = sum(1 for cats in classified if category in cats)
        trait_category = CATEGORY_TRAIT_CATEGORY.get(category)
        if trait_category is not None:
            trait_desc = f"has_trait_category = {trait_category}"
        else:
            trait_count = sum(1 for (_, cat, neg) in traits if cat == category and not neg)
            trait_desc = f"{trait_count} traits"
        print(f"Positive {category}: {trait_desc} + {modifier_count} character modifiers")
    print(f"Character modifiers scanned: {len(modifiers)}")
    print(f"-> {TRAIT_TRIGGER_OUTPUT.relative_to(PROJECT_ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
