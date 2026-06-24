#!/usr/bin/env python3
"""
Country / continent data for eSIM package code generation.

Package code format:  [continent].[dialing_code_or_0].[GB]
  - continent digit per Nitzan's scheme (see CONTINENT_NAMES)
  - second number = country international dialing code, or 0 for regional/mixed packages
  - third number = GB amount
"""

# Continent digit scheme (defined by Nitzan)
CONTINENT_NAMES = {
    0: "מעורב (מדינות מיבשות שונות)",
    1: "אסיה",
    2: "אירופה",
    3: "צפון אמריקה",
    4: "דרום אמריקה",
    5: "אפריקה",
    6: "אוסטרליה/אוקיאניה",
    7: "כללי (ללא יבשת מדויקת)",
}

# Region slug (from /regions?region=XXX) -> continent digit
REGION_TO_CONTINENT = {
    "asia": 1,
    "europe": 2,
    "north-america": 3,
    "northamerica": 3,
    "south-america": 4,
    "southamerica": 4,
    "africa": 5,
    "oceania": 6,
    "australia": 6,
    "global": 7,
    "world": 7,
    "worldpass": 7,
}

# ISO 3166-1 alpha-2 (esim.dog URL slug) -> (English name, dialing code, continent digit)
COUNTRY_DATA = {
    # --- Asia (1) ---
    "th": ("Thailand", 66, 1),
    "jp": ("Japan", 81, 1),
    "vn": ("Vietnam", 84, 1),
    "ph": ("Philippines", 63, 1),
    "in": ("India", 91, 1),
    "sg": ("Singapore", 65, 1),
    "id": ("Indonesia", 62, 1),
    "my": ("Malaysia", 60, 1),
    "kr": ("South Korea", 82, 1),
    "cn": ("China", 86, 1),
    "hk": ("Hong Kong", 852, 1),
    "mo": ("Macao", 853, 1),
    "tw": ("Taiwan", 886, 1),
    "kh": ("Cambodia", 855, 1),
    "la": ("Laos", 856, 1),
    "mm": ("Myanmar", 95, 1),
    "lk": ("Sri Lanka", 94, 1),
    "np": ("Nepal", 977, 1),
    "bd": ("Bangladesh", 880, 1),
    "pk": ("Pakistan", 92, 1),
    "ae": ("United Arab Emirates", 971, 1),
    "sa": ("Saudi Arabia", 966, 1),
    "il": ("Israel", 972, 1),
    "tr": ("Turkey", 90, 1),
    "qa": ("Qatar", 974, 1),
    "kz": ("Kazakhstan", 7, 1),
    "ge": ("Georgia", 995, 1),
    "am": ("Armenia", 374, 1),
    "uz": ("Uzbekistan", 998, 1),
    "kg": ("Kyrgyzstan", 996, 1),
    "az": ("Azerbaijan", 994, 1),
    "jo": ("Jordan", 962, 1),
    "kw": ("Kuwait", 965, 1),
    "bh": ("Bahrain", 973, 1),
    "om": ("Oman", 968, 1),
    # --- Europe (2) ---
    "gb": ("United Kingdom", 44, 2),
    "fr": ("France", 33, 2),
    "de": ("Germany", 49, 2),
    "it": ("Italy", 39, 2),
    "es": ("Spain", 34, 2),
    "pt": ("Portugal", 351, 2),
    "nl": ("Netherlands", 31, 2),
    "be": ("Belgium", 32, 2),
    "ch": ("Switzerland", 41, 2),
    "at": ("Austria", 43, 2),
    "gr": ("Greece", 30, 2),
    "ie": ("Ireland", 353, 2),
    "pl": ("Poland", 48, 2),
    "cz": ("Czech Republic", 420, 2),
    "se": ("Sweden", 46, 2),
    "no": ("Norway", 47, 2),
    "dk": ("Denmark", 45, 2),
    "fi": ("Finland", 358, 2),
    "hu": ("Hungary", 36, 2),
    "ro": ("Romania", 40, 2),
    "hr": ("Croatia", 385, 2),
    "ru": ("Russia", 7, 2),
    "ua": ("Ukraine", 380, 2),
    # --- North America (3) ---
    "us": ("United States", 1, 3),
    "ca": ("Canada", 1, 3),
    "mx": ("Mexico", 52, 3),
    # --- South America (4) ---
    "br": ("Brazil", 55, 4),
    "ar": ("Argentina", 54, 4),
    "cl": ("Chile", 56, 4),
    "co": ("Colombia", 57, 4),
    "pe": ("Peru", 51, 4),
    # --- Africa (5) ---
    "za": ("South Africa", 27, 5),
    "eg": ("Egypt", 20, 5),
    "ma": ("Morocco", 212, 5),
    "ke": ("Kenya", 254, 5),
    "ng": ("Nigeria", 234, 5),
    # --- Oceania (6) ---
    "au": ("Australia", 61, 6),
    "nz": ("New Zealand", 64, 6),
}


# Hebrew names per country slug (fallback to English name if missing)
HEBREW_NAMES = {
    "th": "תאילנד", "jp": "יפן", "vn": "וייטנאם", "ph": "הפיליפינים", "in": "הודו",
    "sg": "סינגפור", "id": "אינדונזיה", "my": "מלזיה", "kr": "דרום קוריאה",
    "cn": "סין", "hk": "הונג קונג", "mo": "מקאו", "tw": "טאיוואן", "kh": "קמבודיה",
    "la": "לאוס", "mm": "מיאנמר", "lk": "סרי לנקה", "np": "נפאל", "bd": "בנגלדש",
    "pk": "פקיסטן", "ae": "איחוד האמירויות", "sa": "ערב הסעודית", "il": "ישראל",
    "tr": "טורקיה", "qa": "קטאר", "kz": "קזחסטן", "ge": "גאורגיה", "am": "ארמניה",
    "uz": "אוזבקיסטן", "kg": "קירגיזסטן", "az": "אזרבייג'ן", "jo": "ירדן",
    "kw": "כווית", "bh": "בחריין", "om": "עומאן",
    "gb": "בריטניה", "fr": "צרפת", "de": "גרמניה", "it": "איטליה", "es": "ספרד",
    "pt": "פורטוגל", "nl": "הולנד", "be": "בלגיה", "ch": "שווייץ", "at": "אוסטריה",
    "gr": "יוון", "ie": "אירלנד", "pl": "פולין", "cz": "צ'כיה", "se": "שוודיה",
    "no": "נורווגיה", "dk": "דנמרק", "fi": "פינלנד", "hu": "הונגריה", "ro": "רומניה",
    "hr": "קרואטיה", "ru": "רוסיה", "ua": "אוקראינה",
    "us": "ארצות הברית", "ca": "קנדה", "mx": "מקסיקו",
    "br": "ברזיל", "ar": "ארגנטינה", "cl": "צ'ילה", "co": "קולומביה", "pe": "פרו",
    "za": "דרום אפריקה", "eg": "מצרים", "ma": "מרוקו", "ke": "קניה", "ng": "ניגריה",
    "au": "אוסטרליה", "nz": "ניו זילנד",
}


def country_from_slug(slug: str):
    """Return (name, dialing_code, continent) for a country slug, or None."""
    return COUNTRY_DATA.get(slug.lower())


def hebrew_name(slug: str) -> str:
    """Hebrew country name, falling back to the English name, then the slug."""
    slug = slug.lower()
    if slug in HEBREW_NAMES:
        return HEBREW_NAMES[slug]
    data = COUNTRY_DATA.get(slug)
    return data[0] if data else slug.upper()


def make_country_code(slug: str, gb) -> str:
    """Generate package code for a single-country package: continent.dialing.GB"""
    data = country_from_slug(slug)
    gb_str = str(gb).replace("gb", "").replace("GB", "").strip()
    if not data:
        # Unknown country: use continent 7 (general) + 0, flag with the slug
        return f"7.0.{gb_str}  (?{slug})"
    _, dialing, continent = data
    return f"{continent}.{dialing}.{gb_str}"


def make_region_code(region_slug: str, gb, variant_countries: int = 0) -> str:
    """Generate package code for a regional package.
    Uses A=mini (<=12 countries) / B=grande (>12 countries).
    Format: continent.0[A|B].GB"""
    continent = REGION_TO_CONTINENT.get(region_slug.lower(), 7)
    gb_str = str(gb).replace("gb", "").replace("GB", "").strip()
    if variant_countries:
        tier = "A" if variant_countries <= 12 else "B"
        return f"{continent}.0{tier}.{gb_str}"
    return f"{continent}.0.{gb_str}"
