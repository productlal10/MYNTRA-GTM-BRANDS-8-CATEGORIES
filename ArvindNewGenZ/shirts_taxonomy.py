"""Shirts taxonomy, subcategory structure, brand targets, aliases, seed routes, and classifier engine."""

from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple, Any

# ==============================================================================
# TARGET BRANDS (7 ARVIND NEW GENZ BRANDS — SHIRTS, ₹700–₹3000)
# ==============================================================================

TARGET_SHIRTS_BRANDS = [
    "Almost Gods",
    "Raoco",
    "Gen.rage",
    "What The Flex",
    "QBmen",
    "Chapter 2 Drip",
    "Future Saints",
]

# Aliases for brand matching in Myntra PDP and listing data
BRAND_ALIASES: Dict[str, List[str]] = {
    "Almost Gods": [
        "Almost Gods",
        "ALMOST GODS",
        "AlmostGods",
    ],
    "Raoco": [
        "Raoco",
        "RAOCO",
    ],
    "Gen.rage": [
        "Gen.rage",
        "GENRAGE",
        "GEN.RAGE",
        "Genrage",
        "Gen Rage",
    ],
    "What The Flex": [
        "What The Flex",
        "WHAT THE FLEX",
    ],
    "QBmen": [
        "QBmen",
        "QB MEN",
        "QB Men",
        "QBMEN",
    ],
    "Chapter 2 Drip": [
        "Chapter 2 Drip",
        "CHAPTER 2 DRIP",
        "Chapter2 Drip",
        "Chapter 2",
    ],
    "Future Saints": [
        "Future Saints",
        "FUTURE SAINTS",
    ],
}

# Seed URLs for crawling shirts on Myntra (comprehensive sub-line and category coverage)
BRAND_SEED_URLS: Dict[str, List[str]] = {
    "Almost Gods": [
        "https://www.myntra.com/almost-gods",
        "https://www.myntra.com/shirts?f=Brand%3AAlmost%20Gods",
        "https://www.myntra.com/shirts?f=Brand%3AALMOST%20GODS",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AAlmost%20Gods",
    ],
    "Raoco": [
        "https://www.myntra.com/raoco",
        "https://www.myntra.com/shirts?f=Brand%3ARaoco",
        "https://www.myntra.com/shirts?f=Brand%3ARAOCO",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3ARaoco",
    ],
    "Gen.rage": [
        "https://www.myntra.com/shirts?f=Brand%3AGENRAGE",
        "https://www.myntra.com/genrage",
        "https://www.myntra.com/gen-rage",
        "https://www.myntra.com/shirts?f=Brand%3AGen.rage",
        "https://www.myntra.com/shirts?f=Brand%3AGEN.RAGE",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AGen.rage",
    ],
    "What The Flex": [
        "https://www.myntra.com/what-the-flex",
        "https://www.myntra.com/shirts?f=Brand%3AWhat%20The%20Flex",
        "https://www.myntra.com/shirts?f=Brand%3AWHAT%20THE%20FLEX",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AWhat%20The%20Flex",
    ],
    "QBmen": [
        "https://www.myntra.com/shirts?f=Brand%3AQB%20MEN",
        "https://www.myntra.com/qbmen",
        "https://www.myntra.com/qb-men",
        "https://www.myntra.com/shirts?f=Brand%3AQBmen",
        "https://www.myntra.com/shirts?f=Brand%3AQB%20Men",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AQBmen",
    ],
    "Chapter 2 Drip": [
        "https://www.myntra.com/chapter-2-drip",
        "https://www.myntra.com/chapter2-drip",
        "https://www.myntra.com/shirts?f=Brand%3AChapter%202%20Drip",
        "https://www.myntra.com/shirts?f=Brand%3ACHAPTER%202%20DRIP",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AChapter%202%20Drip",
    ],
    "Future Saints": [
        "https://www.myntra.com/future-saints",
        "https://www.myntra.com/shirts?f=Brand%3AFuture%20Saints",
        "https://www.myntra.com/shirts?f=Brand%3AFUTURE%20SAINTS",
        "https://www.myntra.com/men-casual-shirts?f=Brand%3AFuture%20Saints",
    ],
}

SHIRTS_LISTING_PATHS = [
    "shirts",
    "men-shirts",
    "casual-shirts",
    "formal-shirts",
    "women-shirts",
    "oversized-shirts",
    "denim-shirts",
    "linen-shirts",
    "printed-shirts",
]

# ==============================================================================
# COMPLETE SHIRTS SUBCATEGORY STRUCTURE (64 SUBCATEGORIES)
# ==============================================================================

SHIRTS_SUBCATEGORY_STRUCTURE = [
    "Casual Shirts",
    "Formal Shirts",
    "Party Wear Shirts",
    "Printed Shirts",
    "Solid Shirts",
    "Checked Shirts",
    "Striped Shirts",
    "Floral Shirts",
    "Graphic Shirts",
    "Denim Shirts",
    "Linen Shirts",
    "Cotton Shirts",
    "Corduroy Shirts",
    "Silk Shirts",
    "Satin Shirts",
    "Rayon Shirts",
    "Oversized Shirts",
    "Regular Fit Shirts",
    "Slim Fit Shirts",
    "Relaxed Fit Shirts",
    "Boxy Fit Shirts",
    "Full Sleeve Shirts",
    "Half Sleeve Shirts",
    "Short Sleeve Shirts",
    "Sleeveless Shirts",
    "Cuban Collar Shirts",
    "Camp Collar Shirts",
    "Mandarin Collar Shirts",
    "Band Collar Shirts",
    "Spread Collar Shirts",
    "Button-Down Shirts",
    "Oxford Shirts",
    "Hawaiian Shirts",
    "Resort Shirts",
    "Bowling Shirts",
    "Western Shirts",
    "Utility Shirts",
    "Military Shirts",
    "Safari Shirts",
    "Overshirts",
    "Shackets",
    "Longline Shirts",
    "Cropped Shirts",
    "Ruffle Shirts",
    "Peplum Shirts",
    "Wrap Shirts",
    "Henley Shirts",
    "Kurta-Style Shirts",
    "Embroidered Shirts",
    "Appliqué Shirts",
    "Sequin Shirts",
    "Textured Shirts",
    "Color Block Shirts",
    "Tie-Dye Shirts",
    "Tie-Up Shirts",
    "Open Collar Shirts",
    "Collarless Shirts",
    "Shirt Jackets",
    "Reversible Shirts",
    "Maternity Shirts",
    "Plus Size Shirts",
    "Unisex Shirts",
    "Boys' Shirts",
    "Girls' Shirts",
]

# Primary Categories grouping the 64 subcategories into logical intelligence buckets
SHIRTS_PRIMARY_CATEGORIES = [
    "Casual Shirts",
    "Formal Shirts",
    "Party Wear Shirts",
    "Printed & Patterned Shirts",
    "Fabric & Material Shirts",
    "Fit & Silhouette Shirts",
    "Collar & Neckline Shirts",
    "Theme & Aesthetic Shirts",
    "Layering & Shackets",
    "Contemporary & Women's Shirts",
    "Demographic & Sizing",
]

SHIRTS_CATEGORY_TREE: Dict[str, List[str]] = {
    "Casual Shirts": [
        "Casual Shirts",
        "Resort Shirts",
        "Hawaiian Shirts",
        "Bowling Shirts",
        "Camp Collar Shirts",
        "Cuban Collar Shirts",
        "Open Collar Shirts",
        "Full Sleeve Shirts",
        "Half Sleeve Shirts",
        "Short Sleeve Shirts",
        "Sleeveless Shirts",
    ],
    "Formal Shirts": [
        "Formal Shirts",
        "Spread Collar Shirts",
        "Button-Down Shirts",
        "Oxford Shirts",
        "Mandarin Collar Shirts",
        "Band Collar Shirts",
        "Collarless Shirts",
    ],
    "Party Wear Shirts": [
        "Party Wear Shirts",
        "Silk Shirts",
        "Satin Shirts",
        "Sequin Shirts",
        "Embroidered Shirts",
        "Appliqué Shirts",
    ],
    "Printed & Patterned Shirts": [
        "Printed Shirts",
        "Solid Shirts",
        "Checked Shirts",
        "Striped Shirts",
        "Floral Shirts",
        "Graphic Shirts",
        "Color Block Shirts",
        "Tie-Dye Shirts",
        "Textured Shirts",
    ],
    "Fabric & Material Shirts": [
        "Denim Shirts",
        "Linen Shirts",
        "Cotton Shirts",
        "Corduroy Shirts",
        "Rayon Shirts",
    ],
    "Fit & Silhouette Shirts": [
        "Oversized Shirts",
        "Regular Fit Shirts",
        "Slim Fit Shirts",
        "Relaxed Fit Shirts",
        "Boxy Fit Shirts",
    ],
    "Collar & Neckline Shirts": [
        "Cuban Collar Shirts",
        "Camp Collar Shirts",
        "Mandarin Collar Shirts",
        "Band Collar Shirts",
        "Spread Collar Shirts",
        "Button-Down Shirts",
        "Open Collar Shirts",
        "Collarless Shirts",
    ],
    "Theme & Aesthetic Shirts": [
        "Hawaiian Shirts",
        "Resort Shirts",
        "Bowling Shirts",
        "Western Shirts",
        "Utility Shirts",
        "Military Shirts",
        "Safari Shirts",
        "Henley Shirts",
        "Kurta-Style Shirts",
    ],
    "Layering & Shackets": [
        "Overshirts",
        "Shackets",
        "Shirt Jackets",
        "Reversible Shirts",
    ],
    "Contemporary & Women's Shirts": [
        "Longline Shirts",
        "Cropped Shirts",
        "Ruffle Shirts",
        "Peplum Shirts",
        "Wrap Shirts",
        "Tie-Up Shirts",
        "Henley Shirts",
        "Kurta-Style Shirts",
    ],
    "Demographic & Sizing": [
        "Maternity Shirts",
        "Plus Size Shirts",
        "Unisex Shirts",
        "Boys' Shirts",
        "Girls' Shirts",
    ],
}

TARGET_SHIRTS_TYPES: List[str] = list(SHIRTS_SUBCATEGORY_STRUCTURE)
for cat in SHIRTS_PRIMARY_CATEGORIES:
    if cat not in TARGET_SHIRTS_TYPES:
        TARGET_SHIRTS_TYPES.append(cat)

BRAND_SHIRTS_MATRIX = {brand: list(TARGET_SHIRTS_TYPES) for brand in TARGET_SHIRTS_BRANDS}

# ==============================================================================
# BACKWARDS COMPATIBILITY ALIASES FOR ETHNIC/POLO/ACTIVEWEAR IMPORTS
# ==============================================================================

ETHNIC_PRIMARY_CATEGORIES = SHIRTS_PRIMARY_CATEGORIES
ETHNIC_CATEGORY_TREE = SHIRTS_CATEGORY_TREE
ETHNIC_LISTING_PATHS = SHIRTS_LISTING_PATHS
TARGET_ETHNIC_BRANDS = TARGET_SHIRTS_BRANDS
TARGET_POLO_BRANDS = TARGET_SHIRTS_BRANDS
TARGET_ACTIVEWEAR_BRANDS = TARGET_SHIRTS_BRANDS
POLO_PRIMARY_CATEGORIES = SHIRTS_PRIMARY_CATEGORIES
ACTIVEWEAR_CATEGORIES = SHIRTS_PRIMARY_CATEGORIES
POLO_CATEGORY_TREE = SHIRTS_CATEGORY_TREE
ACTIVEWEAR_CATEGORY_TREE = SHIRTS_CATEGORY_TREE
POLO_CATEGORIES = SHIRTS_PRIMARY_CATEGORIES
TARGET_ETHNIC_TYPES = TARGET_SHIRTS_TYPES
TARGET_POLO_TYPES = TARGET_SHIRTS_TYPES
TARGET_ACTIVEWEAR_TYPES = TARGET_SHIRTS_TYPES
BRAND_ACTIVEWEAR_MATRIX = BRAND_SHIRTS_MATRIX
ACTIVEWEAR_FILTER_LABELS = TARGET_SHIRTS_TYPES


# ==============================================================================
# SHIRTS TAXONOMY CLASSIFIER ENGINE
# ==============================================================================

def _has_word(pattern: str, text: str) -> bool:
    return bool(re.search(rf"\b{re.escape(pattern)}\b", text, re.IGNORECASE))


def classify_shirts(
    title: str = "",
    article_type: str = "",
    sub_category: str = "",
    description: str = "",
    gender: str = "",
    attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """
    Classifies a product into the exact 64-subcategory Shirts taxonomy:
    1. Primary Category (Casual Shirts, Formal Shirts, Party Wear Shirts, etc.)
    2. Exact Sub-Category from the requested 64-structure
    3. Fabric (Cotton, Linen, Denim, Corduroy, Silk, Satin, Rayon, etc.)
    4. Fit (Slim Fit, Regular Fit, Relaxed Fit, Oversized, Boxy Fit)
    5. Pattern (Solid, Checked, Striped, Printed, Floral, Graphic, etc.)
    6. Sleeve (Full Sleeve, Half Sleeve, Short Sleeve, Sleeveless)
    7. Collar (Spread, Button-Down, Mandarin, Cuban, Camp, Band, etc.)
    8. Occasion (Casual, Formal, Party, Semi-Formal)
    """
    attrs = attributes or {}
    art_str = ""
    if isinstance(article_type, dict):
        art_str = str(article_type.get("typeName") or "")
    else:
        art_str = str(article_type or "")

    text_parts = [
        str(title or ""),
        art_str,
        str(sub_category or ""),
        str(description or ""),
        str(gender or ""),
        str(attrs.get("Type") or ""),
        str(attrs.get("Subcategory") or ""),
        str(attrs.get("Occasion") or ""),
        str(attrs.get("Fit") or ""),
        str(attrs.get("Collar") or ""),
        str(attrs.get("Sleeve Length") or attrs.get("Sleeve") or ""),
        str(attrs.get("Fabric") or attrs.get("Fabrics") or attrs.get("Material") or ""),
        str(attrs.get("Pattern") or attrs.get("Patterns") or ""),
        str(attrs.get("Print or Pattern Type") or ""),
        str(attrs.get("Weave Pattern") or attrs.get("Weave Type") or ""),
    ]
    text = " ".join(text_parts).lower()

    # 1. Determine Fabric (Prioritize structured PDP attributes first, then boundary-safe regex NLP)
    raw_f = str(attrs.get("Fabric") or attrs.get("Fabrics") or attrs.get("Material") or "").strip().lower()
    fabric = "Cotton"
    if raw_f:
        if "corduroy" in raw_f:
            fabric = "Corduroy"
        elif "linen" in raw_f and not re.search(r"\blinen\s*(?:look|feel)\b", raw_f):
            fabric = "Linen"
        elif "denim" in raw_f or "chambray" in raw_f:
            fabric = "Denim"
        elif "satin" in raw_f:
            fabric = "Satin"
        elif "silk" in raw_f and not re.search(r"\bsilk\s*(?:look|feel|finish)\b", raw_f):
            fabric = "Silk"
        elif any(r in raw_f for r in ("rayon", "viscose", "modal", "tencel", "lyocell")):
            fabric = "Rayon"
        elif "flannel" in raw_f:
            fabric = "Flannel"
        elif "seersucker" in raw_f:
            fabric = "Seersucker"
        elif "oxford" in raw_f:
            fabric = "Oxford Cotton"
        elif any(p in raw_f for p in ("polyester", "poly", "nylon", "acrylic")):
            fabric = "Polyester"
        elif "cotton" in raw_f:
            fabric = "Cotton"
        else:
            fabric = raw_f.title()
    else:
        # Fallback to title and description NLP with boundary protection
        if re.search(r"\bcorduroy\b", text):
            fabric = "Corduroy"
        elif re.search(r"\blinen\b(?!\s*(?:look|feel|blend\s*poly))", text):
            fabric = "Linen"
        elif re.search(r"\b(?:denim|chambray)\b", text):
            fabric = "Denim"
        elif re.search(r"\bsatin\b", text):
            fabric = "Satin"
        elif re.search(r"\bsilk\b(?!\s*(?:look|feel|finish))", text):
            fabric = "Silk"
        elif re.search(r"\b(?:rayon|viscose|modal|tencel|lyocell)\b", text):
            fabric = "Rayon"
        elif re.search(r"\bflannel\b", text):
            fabric = "Flannel"
        elif re.search(r"\bseersucker\b", text):
            fabric = "Seersucker"
        elif re.search(r"\boxford\b", text):
            fabric = "Oxford Cotton"
        elif re.search(r"\b(?:polyester|nylon)\b", text):
            fabric = "Polyester"
        elif re.search(r"\bcotton\b", text):
            fabric = "Cotton"

    # 2. Determine Pattern (Prioritize structured PDP attributes first)
    raw_p = str(attrs.get("Pattern") or attrs.get("Print or Pattern Type") or attrs.get("Patterns") or "").strip().lower()
    pattern = "Solid"
    if raw_p:
        if any(c in raw_p for c in ("check", "plaid", "tartan", "gingham", "windowpane", "buffalo")):
            pattern = "Checked"
        elif any(s in raw_p for s in ("stripe", "striped", "pinstripe", "candy stripe")):
            pattern = "Striped"
        elif any(f in raw_p for f in ("floral", "flower", "botanical", "tropical", "leaf")):
            pattern = "Floral"
        elif any(g in raw_p for g in ("graphic", "typography", "comic", "motifs")):
            pattern = "Graphic"
        elif any(t in raw_p for t in ("tie-dye", "tie dye", "shibori", "ombré", "ombre")):
            pattern = "Tie-Dye"
        elif any(b in raw_p for b in ("color block", "colour block", "colorblock")):
            pattern = "Color Block"
        elif any(x in raw_p for x in ("textured", "waffle", "dobby", "jacquard", "herringbone", "self design")):
            pattern = "Textured"
        elif any(pr in raw_p for pr in ("print", "printed", "polka", "geometric", "abstract", "paisley", "ethnic")):
            pattern = "Printed"
        elif "solid" in raw_p or "plain" in raw_p:
            pattern = "Solid"
        else:
            pattern = raw_p.title()
    else:
        if re.search(r"\b(?:check|checks|checked|plaid|tartan|gingham)\b", text):
            pattern = "Checked"
        elif re.search(r"\b(?:stripe|stripes|striped|pinstripe)\b", text):
            pattern = "Striped"
        elif re.search(r"\b(?:floral|botanical|tropical)\b", text):
            pattern = "Floral"
        elif re.search(r"\b(?:graphic|typography)\b", text):
            pattern = "Graphic"
        elif re.search(r"\b(?:tie-dye|tie dye|shibori|ombre)\b", text):
            pattern = "Tie-Dye"
        elif re.search(r"\b(?:color\s*block|colour\s*block|colorblock)\b", text):
            pattern = "Color Block"
        elif re.search(r"\b(?:textured|waffle|dobby|jacquard|herringbone|self\s*design)\b", text):
            pattern = "Textured"
        elif re.search(r"\b(?:print|printed|polka|geometric|abstract|paisley)\b", text):
            pattern = "Printed"
        elif re.search(r"\b(?:solid|plain)\b", text):
            pattern = "Solid"

    # 3. Determine Fit (Prioritize structured PDP attributes first)
    raw_fit = str(attrs.get("Fit") or "").strip().lower()
    fit = "Regular Fit"
    if raw_fit:
        if "oversize" in raw_fit or "oversized" in raw_fit:
            fit = "Oversized"
        elif "boxy" in raw_fit:
            fit = "Boxy Fit"
        elif "relax" in raw_fit or "relaxed" in raw_fit or "loose" in raw_fit:
            fit = "Relaxed Fit"
        elif "slim" in raw_fit or "skinny" in raw_fit or "tailored" in raw_fit or "custom" in raw_fit:
            fit = "Slim Fit"
        elif "regular" in raw_fit or "classic" in raw_fit or "standard" in raw_fit:
            fit = "Regular Fit"
        else:
            fit = raw_fit.title()
    else:
        if re.search(r"\b(?:oversize|oversized)\b", text):
            fit = "Oversized"
        elif re.search(r"\bboxy\b", text):
            fit = "Boxy Fit"
        elif re.search(r"\b(?:relax|relaxed|loose\s*fit)\b", text):
            fit = "Relaxed Fit"
        elif re.search(r"\b(?:slim|skinny|custom\s*fit|tailored\s*fit)\b", text):
            fit = "Slim Fit"
        elif re.search(r"\b(?:regular|classic|standard\s*fit)\b", text):
            fit = "Regular Fit"

    # 4. Determine Collar
    collar = "Spread Collar"
    if "cuban" in text:
        collar = "Cuban Collar"
    elif "camp" in text:
        collar = "Camp Collar"
    elif "mandarin" in text or "chinese" in text:
        collar = "Mandarin Collar"
    elif "band collar" in text or "banded collar" in text:
        collar = "Band Collar"
    elif "button-down" in text or "button down" in text:
        collar = "Button-Down"
    elif "spread" in text:
        collar = "Spread Collar"
    elif "collarless" in text or "no collar" in text:
        collar = "Collarless"
    elif "open collar" in text:
        collar = "Open Collar"
    elif "cutaway" in text:
        collar = "Cutaway Collar"

    # 5. Determine Sleeve
    sleeve = "Full Sleeve"
    if "sleeveless" in text:
        sleeve = "Sleeveless"
    elif "half sleeve" in text:
        sleeve = "Half Sleeve"
    elif "short sleeve" in text:
        sleeve = "Short Sleeve"
    elif "3/4" in text or "three-quarter" in text:
        sleeve = "3/4 Sleeve"
    elif "full sleeve" in text or "long sleeve" in text:
        sleeve = "Full Sleeve"

    # 6. Determine Occasion
    occasion = "Casual"
    if "formal" in text or "office" in text or "business" in text or "boardroom" in text:
        occasion = "Formal"
    elif "party" in text or "evening" in text or "club" in text:
        occasion = "Party Wear"
    elif "resort" in text or "beach" in text or "holiday" in text:
        occasion = "Resort Wear"

    # ==========================================================================
    # PRECISE MAPPING INTO THE 64 SUBCATEGORY STRUCTURE
    # ==========================================================================
    sub_cat = "Casual Shirts"
    primary_cat = "Casual Shirts"

    # Demographic check
    g_lower = str(gender or "").lower()
    t_lower = str(title or "").lower()
    if "girls" in t_lower or "girls" in g_lower or ("girl" in t_lower and "shirt" in t_lower):
        sub_cat = "Girls' Shirts"
        primary_cat = "Demographic & Sizing"
    elif "boys" in t_lower or "boys" in g_lower or ("boy" in t_lower and "shirt" in t_lower):
        sub_cat = "Boys' Shirts"
        primary_cat = "Demographic & Sizing"
    elif "maternity" in text or "pregnant" in text or "nursing" in text:
        sub_cat = "Maternity Shirts"
        primary_cat = "Demographic & Sizing"
    elif "plus size" in text or "plus-size" in text or "plus size" in t_lower or "sztori" in text:
        sub_cat = "Plus Size Shirts"
        primary_cat = "Demographic & Sizing"
    elif "unisex" in t_lower or g_lower == "unisex":
        sub_cat = "Unisex Shirts"
        primary_cat = "Demographic & Sizing"

    # Layering & Outer Shirts
    elif "shacket" in text or "shirt jacket" in text:
        if "shirt jacket" in text:
            sub_cat = "Shirt Jackets"
        else:
            sub_cat = "Shackets"
        primary_cat = "Layering & Shackets"
    elif "overshirt" in text:
        sub_cat = "Overshirts"
        primary_cat = "Layering & Shackets"
    elif "reversible" in text:
        sub_cat = "Reversible Shirts"
        primary_cat = "Layering & Shackets"

    # Distinctive Themes & Aesthetics
    elif "hawaiian" in text or "aloha" in text:
        sub_cat = "Hawaiian Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "bowling" in text:
        sub_cat = "Bowling Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "resort" in text or "vacation" in text:
        sub_cat = "Resort Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "safari" in text:
        sub_cat = "Safari Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "military" in text:
        sub_cat = "Military Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "utility" in text or "cargo" in text:
        sub_cat = "Utility Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "western" in text or "cowboy" in text or "yoke" in text:
        sub_cat = "Western Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "oxford" in text:
        sub_cat = "Oxford Shirts"
        primary_cat = "Formal Shirts"
    elif "kurta" in text or "kurti" in text or "tunic" in text:
        sub_cat = "Kurta-Style Shirts"
        primary_cat = "Theme & Aesthetic Shirts"
    elif "henley" in text:
        sub_cat = "Henley Shirts"
        primary_cat = "Theme & Aesthetic Shirts"

    # Silhouettes / Women's & Contemporary Cuts
    elif "peplum" in text:
        sub_cat = "Peplum Shirts"
        primary_cat = "Contemporary & Women's Shirts"
    elif "wrap" in text and "shirt" in text:
        sub_cat = "Wrap Shirts"
        primary_cat = "Contemporary & Women's Shirts"
    elif "ruffle" in text or "frill" in text:
        sub_cat = "Ruffle Shirts"
        primary_cat = "Contemporary & Women's Shirts"
    elif "tie-up" in text or "tie up" in text or "front tie" in text or "knot shirt" in text:
        sub_cat = "Tie-Up Shirts"
        primary_cat = "Contemporary & Women's Shirts"
    elif "cropped" in text or "crop shirt" in text:
        sub_cat = "Cropped Shirts"
        primary_cat = "Contemporary & Women's Shirts"
    elif "longline" in text or "long line" in text:
        sub_cat = "Longline Shirts"
        primary_cat = "Contemporary & Women's Shirts"

    # Collar Styles
    elif "cuban" in text:
        sub_cat = "Cuban Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "camp" in text:
        sub_cat = "Camp Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "mandarin" in text or "chinese collar" in text:
        sub_cat = "Mandarin Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "band collar" in text or "banded collar" in text:
        sub_cat = "Band Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "button-down" in text or "button down" in text:
        sub_cat = "Button-Down Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "spread collar" in text:
        sub_cat = "Spread Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "open collar" in text:
        sub_cat = "Open Collar Shirts"
        primary_cat = "Collar & Neckline Shirts"
    elif "collarless" in text or "no collar" in text:
        sub_cat = "Collarless Shirts"
        primary_cat = "Collar & Neckline Shirts"

    # Craft & Embellishment
    elif "sequin" in text or "sequinned" in text:
        sub_cat = "Sequin Shirts"
        primary_cat = "Party Wear Shirts"
    elif "applique" in text or "appliqué" in text:
        sub_cat = "Appliqué Shirts"
        primary_cat = "Party Wear Shirts"
    elif "embroidered" in text or "embroidery" in text or "chikankari" in text:
        sub_cat = "Embroidered Shirts"
        primary_cat = "Party Wear Shirts"
    elif "tie-dye" in text or "tie dye" in text or "shibori" in text:
        sub_cat = "Tie-Dye Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "color block" in text or "colour block" in text or "colorblock" in text:
        sub_cat = "Color Block Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "textured" in text or "waffle" in text or "dobby" in text or "jacquard" in text or "seersucker" in text:
        sub_cat = "Textured Shirts"
        primary_cat = "Printed & Patterned Shirts"

    # Specific Fabrics
    elif "corduroy" in text:
        sub_cat = "Corduroy Shirts"
        primary_cat = "Fabric & Material Shirts"
    elif "denim" in text or "chambray" in text:
        sub_cat = "Denim Shirts"
        primary_cat = "Fabric & Material Shirts"
    elif "linen" in text:
        sub_cat = "Linen Shirts"
        primary_cat = "Fabric & Material Shirts"
    elif "silk" in text:
        sub_cat = "Silk Shirts"
        primary_cat = "Party Wear Shirts"
    elif "satin" in text:
        sub_cat = "Satin Shirts"
        primary_cat = "Party Wear Shirts"
    elif "rayon" in text or "viscose" in text or "modal" in text:
        sub_cat = "Rayon Shirts"
        primary_cat = "Fabric & Material Shirts"

    # Distinct Fits & Silhouettes
    elif "oversize" in text or "oversized" in text:
        sub_cat = "Oversized Shirts"
        primary_cat = "Fit & Silhouette Shirts"
    elif "boxy" in text:
        sub_cat = "Boxy Fit Shirts"
        primary_cat = "Fit & Silhouette Shirts"
    elif "relaxed" in text:
        sub_cat = "Relaxed Fit Shirts"
        primary_cat = "Fit & Silhouette Shirts"
    elif "slim" in text:
        sub_cat = "Slim Fit Shirts"
        primary_cat = "Fit & Silhouette Shirts"
    elif "regular fit" in text:
        sub_cat = "Regular Fit Shirts"
        primary_cat = "Fit & Silhouette Shirts"

    # Sleeves (if highlighted specifically in title/type)
    elif "sleeveless" in text:
        sub_cat = "Sleeveless Shirts"
        primary_cat = "Casual Shirts"
    elif "short sleeve" in text:
        sub_cat = "Short Sleeve Shirts"
        primary_cat = "Casual Shirts"
    elif "half sleeve" in text:
        sub_cat = "Half Sleeve Shirts"
        primary_cat = "Casual Shirts"

    # Patterns
    elif "floral" in text or "flower" in text or "botanical" in text:
        sub_cat = "Floral Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "graphic" in text or "typography" in text:
        sub_cat = "Graphic Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "check" in text or "plaid" in text or "tartan" in text or "gingham" in text:
        sub_cat = "Checked Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "stripe" in text or "striped" in text:
        sub_cat = "Striped Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "print" in text or "printed" in text or "polka" in text or "abstract" in text:
        sub_cat = "Printed Shirts"
        primary_cat = "Printed & Patterned Shirts"
    elif "solid" in text or "plain" in text:
        sub_cat = "Solid Shirts"
        primary_cat = "Printed & Patterned Shirts"

    # Core Occasion & Baseline Types
    elif "party" in text or "evening" in text or "clubwear" in text:
        sub_cat = "Party Wear Shirts"
        primary_cat = "Party Wear Shirts"
    elif "formal" in text or "office" in text or "workwear" in text or "business" in text:
        sub_cat = "Formal Shirts"
        primary_cat = "Formal Shirts"
    elif "cotton" in text:
        sub_cat = "Cotton Shirts"
        primary_cat = "Fabric & Material Shirts"
    else:
        sub_cat = "Casual Shirts"
        primary_cat = "Casual Shirts"

    return {
        "category": primary_cat,
        "sub_category": sub_cat,
        "fabric": fabric,
        "fit": fit,
        "pattern": pattern,
        "sleeve": sleeve,
        "collar": collar,
        "occasion": occasion,
        "work": "Standard",
    }


classify_ethnic = classify_shirts
classify_polo = classify_shirts
classify_activewear = classify_shirts
