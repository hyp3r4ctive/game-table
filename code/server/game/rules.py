"""Campaign-level rule toggles. DM flips these on the master view.

Each rule is one entry in RULE_DEFINITIONS with:
  label       - human-readable name shown in the form
  type        - "bool" (checkbox) or "choice" (select)
  default     - default value when the campaign hasn't set one
  options     - list of (value, label) pairs (choice type only)
  category    - groups rules in the UI; CATEGORIES sets display order
  description - optional sub-line under the widget

Add a rule by adding one entry. The endpoint, the form, and any reader
calling get_rule() pick it up automatically.
"""

RULE_DEFINITIONS: dict = {
    "sight": {
        "label": "Require line of sight",
        "type": "bool",
        "default": True,
        "category": "enforcement",
        "description": "Players targeting unseen creatures get held for DM approval.",
    },
    "slots": {
        "label": "Require unspent spell slot",
        "type": "bool",
        "default": True,
        "category": "enforcement",
        "description": "Casting without a free slot of the right level holds for approval.",
    },
    "range": {
        "label": "Enforce spell/attack range",
        "type": "bool",
        "default": True,
        "category": "enforcement",
        "description": "Out-of-range targeting holds for approval.",
    },
    "components": {
        "label": "Require material components",
        "type": "bool",
        "default": True,
        "category": "enforcement",
        "description": "Casting without declared materials holds for approval.",
    },
    "action_economy": {
        "label": "Enforce action economy",
        "type": "bool",
        "default": True,
        "category": "combat",
        "description": "Track per-turn action / bonus action / reaction / movement budget.",
    },
    "flanking": {
        "label": "Flanking",
        "type": "choice",
        "default": "none",
        "options": [
            ("none", "Off — no flanking benefit"),
            ("advantage", "Flanking grants advantage on melee attacks"),
        ],
        "category": "combat",
        "description": "5e DMG optional rule.",
    },
    "crit_rule": {
        "label": "Critical hit damage",
        "type": "choice",
        "default": "double_dice",
        "options": [
            ("double_dice", "Roll twice the damage dice (5e standard)"),
            ("max_then_dice", "Max one set of dice, roll another (brutal homebrew)"),
        ],
        "category": "combat",
    },
    "crit_fumble_table": {
        "label": "Critical fumble table",
        "type": "bool",
        "default": False,
        "category": "combat",
        "description": "Natural 1 on attack rolls triggers a d6 fumble (slip, drop weapon, etc.). Logged as a narrative consequence; mechanical enforcement is up to the DM.",
    },
    "pack_tactics_auto": {
        "label": "Pack Tactics auto-advantage",
        "type": "bool",
        "default": False,
        "category": "combat",
        "description": "Creatures with 'pack_tactics' in class_features get advantage on attack rolls when at least one ally is adjacent to the target.",
    },
    "diagonal_cost": {
        "label": "Diagonal cost (square grid)",
        "type": "choice",
        "default": "5_5_5",
        "options": [
            ("5_5_5", "5/5/5 — every diagonal costs one square"),
            ("5_10_5", "5/10/5 — alternating, 5e PHB optional rule"),
        ],
        "category": "movement",
        "description": "Hex grids ignore this — every step is one cell.",
    },
}

CATEGORIES: list[str] = ["enforcement", "combat", "movement"]


def get_rule(campaign, key: str):
    """Read a rule's value for a campaign, falling back to the registered default.

    `campaign` may be a Campaign row or None (returns the default).
    """
    if key not in RULE_DEFINITIONS:
        raise KeyError(f"unknown rule: {key}")
    rules = (getattr(campaign, "rules", None) if campaign else None) or {}
    return rules.get(key, RULE_DEFINITIONS[key]["default"])


def coerce_form_value(key: str, raw: str | None) -> object:
    """Convert a raw form-field value to the typed value for this rule."""
    defn = RULE_DEFINITIONS[key]
    if defn["type"] == "bool":
        return raw == "on"
    if defn["type"] == "choice":
        valid = {opt[0] for opt in defn["options"]}
        return raw if raw in valid else defn["default"]
    raise ValueError(f"unknown rule type for {key}: {defn['type']}")
