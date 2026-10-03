#!/usr/bin/env python3
"""Loads categories.json, the single list of categories.

Python:  from categories import load_categories
Shell:   python3 categories.py '{folder}|{db}|{port}|{name}'   # one formatted line per category
"""

import json
import sys
from pathlib import Path

CATEGORIES_FILE = Path(__file__).resolve().parent / "categories.json"
_REQUIRED = ("folder", "name", "db", "port", "slug")


def load_categories():
    """Return the category dicts in pipeline order; fails loudly if the file is missing or incomplete."""
    try:
        data = json.loads(CATEGORIES_FILE.read_text("utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{CATEGORIES_FILE} is missing — deploy it next to hub_server.py and the scripts.")
    cats = data["categories"]
    for cat in cats:
        missing = [k for k in _REQUIRED if k not in cat]
        if missing:
            raise SystemExit(f"categories.json: {cat.get('folder', '?')} is missing {', '.join(missing)}")
    return cats


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for cat in load_categories():
        print(sys.argv[1].format(**cat))
