from __future__ import annotations



import io
import json
import os
from pathlib import Path
import pandas as pd
import streamlit as st

try:
    st.set_page_config(page_title="UK Address Cleaner", layout="wide")
except Exception:
    pass  # Already configured (e.g. when running as part of a multipage app)

# Optional logo
try:
    st.image("RAND Main Logo (1).png", width=150)
except Exception:
    pass

st.title("Address Parsing Tool")
st.write(
    "Upload any CSV/Excel containing messy address strings and get clean fields: "
    "**building_number, address line 1–4, town_or_city, postcode, block, confidence**."
)

# =========================================================
# Rules storage (user-defined corrections persist to disk)
# =========================================================
RULES_FILE = Path("address_rules.json")


def load_rules() -> dict:
    if RULES_FILE.exists():
        try:
            return json.loads(RULES_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_rules(rules: dict) -> None:
    try:
        RULES_FILE.write_text(json.dumps(rules, indent=2))
    except Exception as e:
        st.warning(f"Could not save rules: {e}")


if "rules" not in st.session_state:
    st.session_state.rules = load_rules()


def rule_key(s: str) -> str:
    """Normalise an address string into a stable key for rule lookup."""
    import re as _re
    return _re.sub(r"\s+", " ", (s or "").strip().upper())


# =========================================================
# Patched parser (v2)
# =========================================================
import re
import pandas as pd
from typing import Optional

# ---------------------------------------------------------------------------
# Regex building blocks (largely inherited from v1, trimmed where reasonable)
# ---------------------------------------------------------------------------

POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b", re.I)
POSTCODE_AREA_RE = re.compile(r"\b([A-Z]{1,2})(\d{1,2}[A-Z]?)\b", re.I)  # "IG2", "CM14", "E6"

STREET_TYPES_LIST = [
    "ROAD", "STREET", "AVENUE", "CLOSE", "LANE", "DRIVE", "WAY", "COURT", "PLACE",
    "CRESCENT", "GARDENS", "GARDEN", "GROVE", "HILL", "PARK", "SQUARE", "TERRACE",
    "WALK", "MEWS", "RISE", "VIEW", "VALE", "ROW", "END", "GREEN", "FIELDS", "FIELD",
    "WHARF", "QUAY", "QUAYSIDE", "BRIDGE", "FERRY", "FORD", "HARBOUR", "HARBOR",
    "MARINA", "PIER", "DOCK", "LOCK", "LOCKS", "CANAL", "RIVER", "WATERSIDE",
    "HIGHWAY", "BYWAY", "PARKWAY", "BOULEVARD", "CAUSEWAY", "APPROACH", "BYPASS",
    "MOTORWAY", "CARRIAGEWAY",
    "PATH", "PATHWAY", "FOOTPATH", "TRACK", "TRAIL", "CHASE", "BROW", "BANK",
    "LEA", "LEAS", "LEY", "DELL", "DENE", "COPSE", "COPPICE", "SPINNEY",
    "WOOD", "WOODS", "WOODLAND", "WOODLANDS", "FOREST", "HEATH", "MOOR",
    "MEADOW", "MEADOWS", "FELL", "CRAG", "PEAK", "TOR",
    "CIRCUS", "CIRCLE", "OVAL", "LOOP", "TRIANGLE", "PLAZA", "PIAZZA", "PRECINCT",
    "PARADE", "PROMENADE", "ARCADE", "ESPLANADE", "EMBANKMENT", "STRAND",
    "QUADRANT",
    "BRAE", "BRAES", "WYND", "VENNEL", "GAIT", "GATE", "GATES",
    "GLEN", "BEN", "LOCH", "STRATH", "CAIRN", "CROFT", "HOWE", "MUIR",
    "NESS", "HAUGH", "KNOWE", "DYKE", "SHAW", "HOLM",
    "HEOL", "FFORDD", "MAES", "LON", "RHODFA",
    "MARKET", "MARKETPLACE", "COMMON", "COMMONS", "ALLEY", "ALLEYWAY",
    "PASSAGE", "PASSAGEWAY", "STEPS", "STAIRS", "YARD", "COURTYARD", "CLOISTER",
    "CLOISTERS", "GATEHOUSE", "LODGE", "HALL", "HOUSE", "MANOR", "GRANGE",
    "ABBEY", "PRIORY", "CHAPEL", "CHURCH", "MINSTER", "CATHEDRAL",
    "RIDGE", "RIDGEWAY", "DALE", "DALES", "DOWN", "DOWNS", "SLOPE",
    "SHORE", "BEACH", "BAY", "COVE", "CLIFF", "CLIFFS", "POINT", "PROMONTORY",
    "ISLAND", "ISLE",
    "LINK", "LINKS", "SPUR", "RUN", "ROUND", "REACH", "REACHES",
    "CROSS", "CROSSING", "CROSSINGS", "CROSSROADS", "JUNCTION", "INTERCHANGE",
    "ROUNDABOUT", "HOLLOW", "VILLAS", "VILLA",
    "COTTAGES", "COTTAGE", "FARM", "FARMS", "BARN", "BARNS", "STABLES",
    "ORCHARD", "ORCHARDS", "GARTH", "PADDOCK", "PADDOCKS",
    "NOOK", "CORNER", "CORNERS", "TURN", "TURNS", "TURNPIKE", "PIKE",
    "RETREAT", "HAVEN", "SANCTUARY", "GATEWAY", "DRIVEWAY", "THOROUGHFARE",
]
_seen, _unique = set(), []
for t in STREET_TYPES_LIST:
    if t not in _seen:
        _seen.add(t); _unique.append(t)
_unique.sort(key=len, reverse=True)
STREET_TYPES = "|".join(_unique)

# Known UK county names — when a locality chunk ends in one of these, peel it off
# into its own address line so town_or_city stays clean.
UK_COUNTIES = {
    "ESSEX", "KENT", "SURREY", "SUSSEX", "HAMPSHIRE", "DORSET", "DEVON",
    "CORNWALL", "SOMERSET", "WILTSHIRE", "BERKSHIRE", "BUCKINGHAMSHIRE",
    "OXFORDSHIRE", "HERTFORDSHIRE", "BEDFORDSHIRE", "CAMBRIDGESHIRE",
    "NORFOLK", "SUFFOLK", "LINCOLNSHIRE", "NOTTINGHAMSHIRE", "DERBYSHIRE",
    "LEICESTERSHIRE", "WARWICKSHIRE", "WORCESTERSHIRE", "HEREFORDSHIRE",
    "STAFFORDSHIRE", "SHROPSHIRE", "CHESHIRE", "LANCASHIRE", "YORKSHIRE",
    "CUMBRIA", "NORTHUMBERLAND", "DURHAM", "TYNE AND WEAR", "MERSEYSIDE",
    "GREATER MANCHESTER", "WEST MIDLANDS", "SOUTH YORKSHIRE", "WEST YORKSHIRE",
    "NORTH YORKSHIRE", "EAST YORKSHIRE", "RUTLAND", "NORTHAMPTONSHIRE",
    "GLOUCESTERSHIRE", "AVON", "MIDDLESEX", "LONDON",
    "EAST SUSSEX", "WEST SUSSEX",
}

# A curated list of known UK towns/cities that commonly appear in these addresses.
# When a locality chunk contains one of these words, that word becomes the town
# and the rest becomes supplementary locality lines.
KNOWN_TOWNS = {
    # London boroughs / areas
    "LONDON", "ILFORD", "ROMFORD", "DAGENHAM", "BARKING", "NEWHAM", "HAVERING",
    "REDBRIDGE", "HORNCHURCH", "UPMINSTER", "RAINHAM", "SOUTH WOODFORD",
    "WOODFORD", "MANOR PARK", "CANNING TOWN", "NORTH WOOLWICH", "BECKTON",
    "STRATFORD", "WALTHAMSTOW", "LEYTON", "LEYTONSTONE", "CHINGFORD", "CHADWELL HEATH",
    "BARKINGSIDE", "WANSTEAD", "GANTS HILL", "NEWBURY PARK", "HAINAULT",
    # Essex towns
    "BASILDON", "LAINDON", "PITSEA", "BRENTWOOD", "DODDINGHURST", "WICKFORD",
    "WILLOWDALE", "BILLERICAY", "CHELMSFORD", "COLCHESTER", "SOUTHEND",
    "SOUTHEND-ON-SEA", "CLACTON", "HARLOW", "EPPING", "LOUGHTON",
    # Other common
    "LEEDS", "MANCHESTER", "BIRMINGHAM", "LIVERPOOL", "NEWCASTLE", "BRISTOL",
    "SHEFFIELD", "GLASGOW", "EDINBURGH", "CARDIFF", "BELFAST", "NOTTINGHAM",
    "OXFORD", "CAMBRIDGE", "READING", "CROYDON", "BROMLEY", "KINGSTON",
    "LEWISHAM", "CATFORD", "GREENWICH", "HACKNEY", "ISLINGTON",
}

HOUSE_TOKEN = r"(?:\d+[A-Z]?)(?:\s*(?:&|AND|/|,|-)\s*\d+[A-Z]?)*"
STREET_BODY = r"[A-Z0-9\s'\u2019\.\-/&]+?"

ADDR_PATTERN = re.compile(
    rf"(?:^|\s)({HOUSE_TOKEN})\s+({STREET_BODY}\b(?:{STREET_TYPES}))\b"
)
STREET_ONLY_PATTERN = re.compile(
    rf"(?:^|[\s,;])({STREET_BODY}\b(?:{STREET_TYPES}))\b"
)

# Single-word street type that might form part of a compound street name
SINGLE_STREET_TYPE_RE = re.compile(rf"^\s*({STREET_TYPES})\s+({STREET_TYPES})\b", re.I)


def _extend_compound_street(matched_street: str, after_text: str) -> tuple[str, int]:
    """
    If `matched_street` is a single street-type word (e.g. "GREEN") and
    `after_text` begins with another street-type word (e.g. "LANE 626 ..."),
    extend the matched street to include both ("GREEN LANE") and report how
    many characters of `after_text` were consumed.

    Returns (extended_street, consumed_chars).
    """
    upper = matched_street.strip().upper()
    if " " in upper:
        return matched_street, 0  # already multi-word
    if upper not in {t for t in STREET_TYPES.split("|")}:
        return matched_street, 0  # not a pure single street type

    m = re.match(rf"^\s+({STREET_TYPES})\b", after_text, re.I)
    if not m:
        return matched_street, 0
    extra = m.group(1).strip()
    return f"{matched_street.strip()} {extra}", m.end()

PREFIX_STREET_TYPES = r"HEOL|FFORDD|MAES|LON|RHODFA"
PREFIX_STREET_PATTERN = re.compile(
    rf"(?:^|\s)({HOUSE_TOKEN})?\s*\b({PREFIX_STREET_TYPES})\s+([A-Z][A-Z\s'\u2019\-]{{1,60}}?)(?=,|\s{{2,}}|\s+[A-Z]{{1,2}}\d|$)",
    re.I,
)

BLOCK_KEYWORDS = (
    r"FLATS?|APTS?|APARTMENTS?|UNITS?|UNIT|BLOCKS?|BLOCK|BLK|"
    r"SUITES?|ROOMS?|FLOORS?|LEVELS?|"
    r"PENTHOUSES?|STUDIOS?|MAISONETTES?|ANNEXES?|ANNEXE|ANNEX|"
    r"DWELLINGS?|LOTS?|CHALETS?|CABINS?|BASEMENTS?|GROUND\s+FLOOR|"
    r"FIRST\s+FLOOR|SECOND\s+FLOOR|THIRD\s+FLOOR|FOURTH\s+FLOOR|"
    r"FIFTH\s+FLOOR|TOP\s+FLOOR|LOWER\s+GROUND|UPPER\s+GROUND|"
    r"REAR\s+OF|FRONT\s+OF|SIDE\s+OF|"
    r"STAIRWELLS?"
)

NAMED_BUILDING_KEYWORDS = (
    r"HOUSE|LODGE|HALL|TOWER|COURT|MANSIONS?|BUILDINGS?|"
    r"MILLS?|BARN|GRANGE|VILLAS?|COTTAGE|FARM|MANOR|ABBEY|PRIORY|POINT|HEIGHTS?"
)
NAMED_BUILDING_PATTERN = re.compile(
    rf"(?:^|(?<=,\s)|(?<=\s))([A-Z][A-Za-z'\u2019\-]+(?:\s+[A-Z][A-Za-z'\u2019\-]+){{0,4}})\s+\b({NAMED_BUILDING_KEYWORDS})\b",
    re.I,
)

LONDON_AREAS = {"E", "EC", "N", "NW", "SE", "SW", "W", "WC"}


def normalise_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def titlecase_street(s: str) -> str:
    if not s:
        return s
    s = s.title()
    s = re.sub(r"\bAnd\b", "and", s)
    s = re.sub(r"\bOf\b", "of", s)
    s = re.sub(r"\bThe\b(?!\s*$)", "the", s)
    return s


def extract_postcode(s: str):
    last = None
    if not s:
        return None, None
    for m in POSTCODE_RE.finditer(s):
        last = m
    if last:
        return last.group(0).upper(), (last.start(), last.end())
    return None, None


def postcode_area(pc):
    if not pc:
        return None
    m = re.match(r"([A-Z]{1,2})\d", pc, re.I)
    return m.group(1).upper() if m else None


def is_london_postcode(pc) -> bool:
    a = postcode_area(pc)
    return bool(a and a in LONDON_AREAS)


def is_valid_uk_postcode(pc) -> bool:
    if not pc:
        return False
    return bool(re.fullmatch(r"[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}", pc, re.I))


# ---------------------------------------------------------------------------
#   NEW: preamble stripper
# ---------------------------------------------------------------------------

def strip_preamble(full: str, postcode: Optional[str] = None) -> str:
    """
    Preamble stripping:
      - Look for the LATEST reasonable anchor in the prefix (everything before
        the postcode). Preferences, in order:
          1. Last "<house> <street> <STREETTYPE>" — strongest signal.
          2. Last "<street> <STREETTYPE>" — but skip matches that are just a
             known town/locality (e.g. "Manor Park", "Chadwell Heath"). Those
             tokens happen to end with a street-type word but they are NOT
             streets.
          3. Last "<house> <TitleCase word(s)>" — last-resort anchor.

      ADDR_PATTERN and STREET_ONLY_PATTERN are case-sensitive uppercase-only,
      so we run them against the uppercased prefix and translate the indices
      back to the original-case string.
    """
    s = normalise_spaces(full)
    pc_match = None
    for pc_match in POSTCODE_RE.finditer(s):
        pass
    if not pc_match:
        return s

    prefix = s[: pc_match.start()]
    prefix_upper = prefix.upper()

    # 1) "<house> <street> <STREETTYPE>"
    last = None
    for m in ADDR_PATTERN.finditer(prefix_upper):
        last = m
    if last:
        return s[last.start():].strip()

    # 2) "<street> <STREETTYPE>" — prefer non-locality matches.
    so_matches = list(STREET_ONLY_PATTERN.finditer(prefix_upper))
    # Filter out matches that ARE a known town/county
    def _is_locality(text: str) -> bool:
        u = text.strip().upper()
        return u in KNOWN_TOWNS or u in UK_COUNTIES
    real_streets = [m for m in so_matches if not _is_locality(m.group(1))]
    if real_streets:
        last_so = real_streets[-1]
        return s[last_so.start():].strip()
    # If only locality matches exist, still use the last one as a weak anchor.
    if so_matches:
        last_so = so_matches[-1]
        return s[last_so.start():].strip()

    # 3) "<house> <TitleCase words>" generic fallback
    last2 = None
    for m in re.finditer(
        rf"\b({HOUSE_TOKEN})\s+([A-Z][A-Za-z'\u2019\-]+(?:\s+[A-Z][A-Za-z'\u2019\-]+){{0,5}})",
        prefix,
    ):
        last2 = m
    if last2:
        return s[last2.start():].strip()

    return s


# ---------------------------------------------------------------------------
#   NEW: odd/even range extractor
# ---------------------------------------------------------------------------

ODDEVEN_RE = re.compile(r"\(?\s*(\d+\s*[-–]\s*\d+)\s*\(?\s*(EVENS?|ODDS?)\s*\)?", re.I)
ODDEVEN_PAREN_RE = re.compile(r"\(\s*(EVENS?|ODDS?)\s*\)", re.I)
BARE_ODDEVEN_RE = re.compile(r"\b(EVENS?|ODDS?)\b", re.I)


def extract_oddeven(s: str):
    """
    Find '(N-M EVEN)', '(N-M ODD)', 'N-M (EVENS)', '(EVEN) N-M' etc. in the string.
    Returns (label, cleaned_string). The label looks like "2-16 Even".
    IMPORTANT: we keep the NUMBER RANGE in the cleaned string (because the range is
    often the real house-number for the building), only the EVEN/ODD marker is removed.
    """
    label = ""
    m = ODDEVEN_RE.search(s)
    if m:
        rng = re.sub(r"\s*[-–]\s*", "-", m.group(1))
        parity = m.group(2).title().rstrip("s")  # "Even", "Odd"
        label = f"{rng} {parity}"
        # Replace the whole match with just the range, keeping it in the stream
        s = s[: m.start()] + " " + rng + " " + s[m.end():]
    # Nuke remaining stray "(EVEN)", "(ODD)" parenthesised markers
    s = ODDEVEN_PAREN_RE.sub(" ", s)
    # And bare "EVEN"/"ODD"/"EVENS"/"ODDS" words sitting next to ranges
    s = BARE_ODDEVEN_RE.sub(" ", s)
    # And standalone '(N-M)' ranges that are just the same range repeated
    s = re.sub(r"\(\s*\d+\s*[-–]\s*\d+\s*\)", " ", s)
    s = normalise_spaces(s)
    return label, s


# ---------------------------------------------------------------------------
#   Flat token extraction (mostly unchanged)
# ---------------------------------------------------------------------------

def extract_flat_tokens(text: str) -> dict:
    m = re.search(r"\b(FLATS?|UNITS?|FLAT|UNIT|APARTMENTS?|APTS?)\b\s*,?\s*(.+)", text, re.I)
    if not m:
        return {}
    tail = m.group(2).strip()
    trunc = re.search(r"(?<=\s)\d+\s+[A-Z][a-z]", tail)
    if trunc:
        tail = tail[:trunc.start()].strip()
    pc_trunc = POSTCODE_RE.search(tail)
    if pc_trunc:
        tail = tail[:pc_trunc.start()].strip()
    tail = tail.strip(") ,;")
    # Nuke any remaining EVEN/ODD residue inside the tail
    tail = re.sub(r"\b(?:EVENS?|ODDS?)\b", "", tail, flags=re.I)
    tail = re.sub(r"[()]", "", tail)
    tail = normalise_spaces(tail)
    if not tail:
        return {}
    tail_norm = re.sub(r"\s+to\s+",  "-",   tail, flags=re.I)
    tail_norm = re.sub(r"\s+and\s+", " & ", tail_norm, flags=re.I)
    tail_norm = re.sub(r"\s*&\s*",   " & ", tail_norm)
    tail_norm = re.sub(r"\s*,\s*",   ",",   tail_norm)

    mrange = re.match(r"^([A-Z]|\d+)\s*-\s*([A-Z]|\d+)$", tail_norm, re.I)
    if mrange:
        a, b = mrange.group(1).upper(), mrange.group(2).upper()
        ttype = "range_letters" if (len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha()) else "range_numbers"
        return {"display": f"{a} - {b}", "tokens": [a, b], "type": ttype}

    parts = re.split(r"\s*(?:&|/|,)\s*", tail_norm)
    parts = [p.strip().upper() for p in parts if p.strip()]
    if not parts:
        return {}
    if all(re.match(r"^\d+$", p) for p in parts):
        ttype = "list_numbers"
    elif all(re.match(r"^[A-Z]$", p) for p in parts):
        ttype = "list_letters"
    else:
        ttype = "list_mixed"
    return {"display": " & ".join(parts), "tokens": parts, "type": ttype}


BLOCK_PATTERN = re.compile(
    rf"\b(?:{BLOCK_KEYWORDS})\b\s*[A-Z0-9&\-\s\.\,/]{{0,60}}?(?=\s{{2,}}|$|(?:\s+[A-Z][A-Z]+\s+[A-Z][A-Z]+))",
    re.I,
)


def normalise_block_and_unit(block_raw):
    if not block_raw:
        return None, {}
    b = block_raw.strip().strip("() ,;-\"'")
    b = re.sub(r"\s*[:#]\s*", " ", b)
    b = re.sub(r"\bBlk\b", "Block", b, flags=re.I)
    # Scrub stray EVEN/ODD residue that leaks in from bare BLOCK_PATTERN matches
    b = re.sub(r"\b(?:EVENS?|ODDS?)\b", "", b, flags=re.I)
    b = re.sub(r"[()]", "", b)
    b = normalise_spaces(b)

    flat_info = extract_flat_tokens(b)
    if flat_info:
        disp = flat_info["display"]
        prefix = "Flats" if ("range" in flat_info.get("type", "") or len(flat_info["tokens"]) > 1) else "Flat"
        block_str = f"{prefix} {disp}"
    else:
        b_clean = re.sub(r"^[\d\s,]+(?=[A-Za-z])", "", b).strip()
        block_str = b_clean if b_clean else b
    return block_str.title(), flat_info


# ---------------------------------------------------------------------------
#   NEW: locality splitter that separates town from county
# ---------------------------------------------------------------------------

def split_locality(locality: str) -> tuple[str, str, str, str]:
    """
    Returns (town_or_city, line2, line3, line4).

    Strategy:
      1. Split the locality string on double-spaces, commas, semicolons, slashes.
      2. For each chunk, pull off a trailing county if present (put county on its
         own line).
      3. Search each chunk for a KNOWN_TOWN (multi-word or single). Whichever chunk
         yields a town gives us town_or_city; the leftover words become a
         supplementary line.
      4. If no KNOWN_TOWN is found, fall back to the first non-county chunk.
    """
    if not locality:
        return "", "", "", ""

    raw_parts = [p.strip() for p in re.split(r"\s{2,}|,|;|/", locality) if p.strip()]
    if not raw_parts:
        raw_parts = [locality.strip()]

    # Phase 1: peel trailing counties off each chunk.
    expanded: list[str] = []
    for p in raw_parts:
        words = p.split()
        if len(words) >= 2 and words[-1].upper() in UK_COUNTIES:
            head = " ".join(words[:-1])
            tail = words[-1]
            if head:
                expanded.append(head)
            expanded.append(tail)
        else:
            expanded.append(p)

    # Phase 2: for each chunk, hunt for a known town. If we find one, split the
    # chunk into (pre_town, town, post_town). pre/post go on a supplementary line.
    towns: list[str] = []
    counties: list[str] = []
    other: list[str] = []

    def hunt_town(chunk: str):
        """Return (town, leftover) if a known town is found, else (None, chunk)."""
        words = chunk.split()
        upper = [w.upper() for w in words]
        # Try 2-word towns first
        for size in (2, 1):
            for i in range(len(words) - size + 1):
                candidate = " ".join(upper[i : i + size])
                if candidate in KNOWN_TOWNS:
                    town = " ".join(words[i : i + size])
                    leftover = " ".join(words[:i] + words[i + size :])
                    return town, leftover.strip()
        return None, chunk

    for p in expanded:
        if p.upper() in UK_COUNTIES:
            counties.append(p.title())
            continue
        town, leftover = hunt_town(p)
        if town and not towns:
            towns.append(town.title())
            if leftover:
                other.append(leftover.title())
        else:
            other.append(p.title())

    # If no town found, pick the first non-county chunk as town
    if not towns and other:
        towns.append(other.pop(0))

    town_str = towns[0] if towns else ""
    # Filter out pieces that duplicate the town
    other = [o for o in other if o.upper() != town_str.upper()]

    line2 = town_str
    line3 = ""
    line4 = ""
    if other and counties:
        line3 = other[0]
        line4 = counties[0]
    elif other:
        line3 = other[0]
        if len(other) > 1:
            line4 = other[1]
    elif counties:
        line3 = counties[0]

    return town_str, line2, line3, line4


# ---------------------------------------------------------------------------
#   Confidence scoring (unchanged logic)
# ---------------------------------------------------------------------------

def score_confidence(parsed: dict, original: str) -> tuple[str, list]:
    flags: list[str] = []
    pc = parsed.get("postcode", "")
    has_postcode = bool(pc)
    if not pc:
        flags.append("no_postcode")
    elif not is_valid_uk_postcode(pc):
        flags.append("malformed_postcode")
    line1 = parsed.get("address line 1", "")
    has_street = bool(line1)
    if not line1:
        flags.append("no_street")
    else:
        if not re.search(rf"\b(?:{STREET_TYPES})\b", line1, re.I):
            flags.append("unusual_street_type")
    if not parsed.get("building_number"):
        flags.append("no_building_number")
    if not parsed.get("town_or_city"):
        flags.append("no_town_or_city")
    if original and len(original.strip()) < 15:
        flags.append("very_short_input")
    if original and not has_street and not has_postcode:
        flags.append("unparseable")
    if not has_postcode or not has_street:
        label = "Low"
    elif flags:
        label = "Medium"
    else:
        label = "High"
    return label, flags


# ---------------------------------------------------------------------------
#   Main parse
# ---------------------------------------------------------------------------

def parse_address(full: str) -> dict:
    original = full if isinstance(full, str) else (str(full) if full is not None else "")
    s0 = normalise_spaces(original)
    if not s0:
        return dict(building_number="", **{"address line 1": "", "address line 2": "",
                                           "address line 3": "", "address line 4": ""},
                    town_or_city="", postcode="", block="",
                    confidence="Low", confidence_flags="empty_input")

    # -------- Step 1: pull postcode from the full original string --------
    postcode, _ = extract_postcode(s0)

    # -------- Step 2: capture PARENTHESISED flat clause early --------
    # We need this before we start nuking parens out of the string for
    # street-matching. This is what populates the block column and tells us
    # what the building label is.
    PAREN_BLOCK = re.compile(rf"\(([^)]*\b(?:{BLOCK_KEYWORDS})\b[^)]*?)\)", re.I)
    paren_block_text = ""
    mparen_all = list(PAREN_BLOCK.finditer(s0))
    if mparen_all:
        paren_block_text = mparen_all[0].group(1)  # use first — often preamble

    # -------- Step 3: capture Named Building from the preamble --------
    #   Only search the PRE-POSTCODE part, because searching the whole string
    #   can pick up "Slewins Lane" type items after named buildings in the tail.
    named_building = ""
    search_text = s0
    if postcode:
        pcm = None
        for pcm in POSTCODE_RE.finditer(s0):
            pass
        if pcm:
            search_text = s0[: pcm.start()]

    # Only accept named building if it appears at the very start of the
    # string (before any digit) — that's the canonical "preamble" slot.
    # E.g. "Sissulu Court, Redclyffe Road ..." -> yes, "Sissulu Court"
    #      "2-196 (EVEN) HAYNES PARK COURT, Havering ..." -> yes, "Haynes Park Court"
    nb_match = NAMED_BUILDING_PATTERN.match(re.sub(r"^[\d\s\-()&,.]+", "", search_text))
    if nb_match:
        name = nb_match.group(1).strip()
        kw = nb_match.group(2).strip()
        if name and name.upper() not in {"UPPER", "LOWER", "NORTH", "SOUTH", "EAST", "WEST"}:
            named_building = f"{name} {kw}".title()

    # -------- Step 4: strip odd/even markers and remember label --------
    oddeven_label, s_clean = extract_oddeven(s0)

    # -------- Step 5: scrub postcode-area prefixes stuck to streets --------
    # "Queens Road, CM14 (...) Queens Road, CM14 (...)  Brentwood Essex CM14 4HD"
    # -> drop the ", CM14" and " CM14 " occurrences (keep only the full postcode).
    if postcode:
        area = postcode_area(postcode)
        if area:
            # Remove ", IG2" / " IG2 " / " IG2," variants; but only when followed
            # by a space, comma, or end — not the real postcode ("IG2 6UT").
            s_clean = re.sub(
                rf"(?<=[,\s]){area}\d{{1,2}}[A-Z]?(?=[\s,])(?!\s*\d[A-Z]{{2}})",
                " ",
                s_clean,
                flags=re.I,
            )
            s_clean = normalise_spaces(s_clean)

    # -------- Step 6: drop all parenthesised blobs from the working string --
    # (we already captured what we needed). BUT: if we captured an odd/even
    # label with a number range, make sure that range survives somewhere as a
    # free-standing token so the street matcher can anchor on it.
    # E.g. "(Flats, 18-28 (even))" is captured; we nuke the parens; then we
    # put "18-28" back at the start of what's left so "18-28 Hampstead Gardens"
    # can be matched.
    s_clean = re.sub(r"\([^)]*\)", " ", s_clean)
    s_clean = normalise_spaces(s_clean)
    if oddeven_label:
        # Extract just the numeric range from the label
        mrng = re.match(r"(\d+-\d+)", oddeven_label)
        if mrng:
            rng = mrng.group(1)
            # Only inject if the range isn't already present as a free token
            if not re.search(rf"\b{rng}\b", s_clean):
                s_clean = f"{rng} " + s_clean

    # -------- Step 7: strip the messy preamble, keep canonical tail --------
    canonical = strip_preamble(s_clean, postcode)

    # Drop any leading "Flats ..." / "Flat ..." / "Unit ..." phrase that sneaks
    # into the canonical tail before the real house-number-and-street.
    UNIT_TAKING = r"FLATS?|APTS?|APARTMENTS?|UNITS?|UNIT|BLOCKS?|BLOCK|BLK|SUITES?|ROOMS?|STUDIOS?|MAISONETTES?|ANNEXES?|ANNEXE|ANNEX|DWELLINGS?|LOTS?|FLOORS?|LEVELS?"
    canonical = re.sub(
        rf"^\s*(?:{UNIT_TAKING})\b[^A-Z]*?(?=\d+\s+[A-Z])",
        "",
        canonical,
        flags=re.I,
    )
    canonical = normalise_spaces(canonical)

    # Drop trailing postcode tokens from canonical
    canonical = POSTCODE_RE.sub(" ", canonical)
    canonical = normalise_spaces(canonical)

    # -------- Step 8: build the block string --------
    block = None
    flat_info = {}
    block_from_parens = False
    if paren_block_text:
        block, flat_info = normalise_block_and_unit(paren_block_text)
        block_from_parens = True
    else:
        mblock = BLOCK_PATTERN.search(s0)
        if mblock:
            block, flat_info = normalise_block_and_unit(mblock.group(0))

    if oddeven_label:
        if block:
            if "Even" not in block and "Odd" not in block:
                block = f"{block} ({oddeven_label})"
        else:
            block = f"Flats {oddeven_label}"

    # -------- Step 9: find the street --------
    house = ""
    address_line_1 = ""
    remainder = canonical.upper()
    used_fallback = False
    street_resolved = False  # set to True when we have a usable street; skips fallbacks

    # (a) Classic "<house> <street> <STREETTYPE>" — take LAST match,
    # but skip matches where the street is immediately followed by a
    # named-building keyword (COURT/HOUSE/HALL/LODGE etc.), because those
    # are really the *building*, not the street.
    all_matches = list(ADDR_PATTERN.finditer(canonical.upper()))
    m2 = None
    skipped_for_building = None  # remember a "Haynes Park [Court]" we saw
    tail_bk = re.compile(rf"^\s+\b({NAMED_BUILDING_KEYWORDS})\b", re.I)
    if all_matches:
        for cand in reversed(all_matches):
            after = canonical.upper()[cand.end():]
            tail = tail_bk.match(after)
            if tail:
                skipped_for_building = (cand, tail)
                continue
            m2 = cand
            break
        # If every candidate was followed by a building keyword (e.g. "Haynes Park Court"
        # with no real street match afterwards), treat that earlier match as the NAMED
        # BUILDING and look for a STREET_ONLY match starting after its building-keyword tail.
        if m2 is None and skipped_for_building:
            cand, tail = skipped_for_building
            nb_street = cand.group(2).strip().title()
            nb_kw = tail.group(1).title()
            if not named_building:
                named_building = f"{nb_street} {nb_kw}"
            # Look for a real street after the building keyword
            after_bk_pos = cand.end() + tail.end()
            rest = canonical[after_bk_pos:]
            rest_upper = rest.upper()
            so_matches = list(STREET_ONLY_PATTERN.finditer(rest_upper))
            # Filter known-locality matches (Manor Park etc.)
            def _is_loc(t: str) -> bool:
                u = t.strip().upper()
                return u in KNOWN_TOWNS or u in UK_COUNTIES
            real_so = [m for m in so_matches if not _is_loc(m.group(1))]
            so = real_so[-1] if real_so else (so_matches[-1] if so_matches else None)
            if so:
                street_raw = so.group(1).strip()
                address_line_1 = titlecase_street(street_raw)
                remainder = rest_upper[so.end():].strip(" ,")
                house = cand.group(1).strip() if cand.group(1) else ""
                street_resolved = True
        if m2 is None and not street_resolved:
            # No clean ADDR match available, but we have a building-suffixed one;
            # use it as a last-resort street anchor (better than nothing).
            m2 = all_matches[-1]
    if m2 is not None and not street_resolved:
        house = (m2.group(1) or "").strip()
        street_raw = m2.group(2).strip()
        street_raw = re.sub(rf"^[\s,)\]]*{HOUSE_TOKEN}\s+", "", street_raw)
        house = re.sub(r"^(\d+[A-Z]?)\s+\1\b", r"\1", house)
        # Compound street extension: "Green" + " Lane" -> "Green Lane"
        after_text = canonical.upper()[m2.end():]
        extended, consumed = _extend_compound_street(street_raw, after_text)
        street_raw = extended
        consumed_extra = consumed
        street_tc = titlecase_street(street_raw)
        address_line_1 = (f"{house} {street_tc}".strip() if house else street_tc)
        remainder = canonical.upper()[m2.end() + consumed_extra:].strip(" ,")
        street_resolved = True

        # Secondary: if an EARLIER ADDR match is followed by a building keyword,
        # capture it as the named building (e.g. "Haynes Park Court").
        if not named_building and m2 is not all_matches[0]:
            for cand in all_matches:
                after = canonical.upper()[cand.end():]
                tail = tail_bk.match(after)
                if tail:
                    nb_street = cand.group(2).strip().title()
                    nb_kw = tail.group(1).title()
                    named_building = f"{nb_street} {nb_kw}"
                    break

    if not street_resolved:
        # (b) Welsh/Gaelic prefix style
        mp = None
        for mp in PREFIX_STREET_PATTERN.finditer(canonical):
            pass
        if mp:
            house = (mp.group(1) or "").strip()
            prefix_word = mp.group(2).title()
            name = mp.group(3).strip().title()
            street_tc = f"{prefix_word} {name}".strip()
            address_line_1 = (f"{house} {street_tc}".strip() if house else street_tc)
            remainder = canonical[mp.end():].strip(" ,").upper()
        else:
            # (c) street-type only, no house number
            so_matches = list(STREET_ONLY_PATTERN.finditer(canonical.upper()))
            # Filter out matches that ARE just a known town/county name
            # (e.g. "Manor Park", "South Woodford") — those aren't streets, they're locality.
            def is_known_locality(text: str) -> bool:
                u = text.strip().upper()
                return u in KNOWN_TOWNS or u in UK_COUNTIES
            filtered = [m for m in so_matches if not is_known_locality(m.group(1))]
            m3 = filtered[-1] if filtered else (so_matches[-1] if so_matches else None)
            if m3:
                street_raw = m3.group(1).strip()
                # Compound street extension
                after_text = canonical.upper()[m3.end():]
                extended, consumed = _extend_compound_street(street_raw, after_text)
                street_raw = extended
                m3_end_extra = consumed
                address_line_1 = titlecase_street(street_raw)
                remainder = canonical.upper()[m3.end() + m3_end_extra:].strip(" ,")
                # Look for a house number somewhere after the street in the canonical
                # (e.g. "Wycombe Road 2B Ilford" -> street="Wycombe Road", house="2B")
                trailing_house = re.match(
                    rf"\s*({HOUSE_TOKEN})\s+", canonical[m3.end() + m3_end_extra:]
                )
                if trailing_house:
                    house = trailing_house.group(1).strip()
                    address_line_1 = f"{house} {titlecase_street(street_raw)}".strip()
                    remainder = canonical[m3.end() + m3_end_extra + trailing_house.end():].strip(" ,").upper()
                used_fallback = (house == "")  # still fallback if no house found
            else:
                # (d) Last resort: "number + 1-6 Capitalised Words"
                m4 = None
                for m4 in re.finditer(
                    rf"\b({HOUSE_TOKEN})\s+([A-Z][A-Za-z'\u2019\-]+(?:\s+[A-Z][A-Za-z'\u2019\-]+){{0,5}})",
                    canonical,
                ):
                    pass
                if m4:
                    house = m4.group(1).strip()
                    street_raw = m4.group(2).strip()
                    address_line_1 = f"{house} {titlecase_street(street_raw)}".strip()
                    remainder = canonical[m4.end():].strip(" ,").upper()
                    used_fallback = True
                else:
                    address_line_1 = ""
                    remainder = canonical.upper()
                    used_fallback = True

    # -------- Step 10: clean up remainder -> locality lines --------
    remainder = re.sub(r"[^A-Z\s'\u2019\.\-/&()]", " ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip()

    # Step 10a: trim trailing locality/town words from the STREET match.
    # If address_line_1 contains a known town or county at the END, peel it off
    # into the remainder for the locality parser.
    #   "17-23 Widbrook Doddinghurst Brentwood" -> "17-23 Widbrook" + "Doddinghurst Brentwood"
    if address_line_1:
        words = address_line_1.split()
        # Find the furthest-left position where a known town starts inside line 1
        peel_at = None
        for i in range(1, len(words)):  # never peel the first word (usually house num)
            window = " ".join(words[i:]).upper()
            # Check 1-word and 2-word trailing phrases for known town/county
            if words[i].upper() in KNOWN_TOWNS | UK_COUNTIES:
                peel_at = i
                break
            if i + 1 < len(words):
                two = f"{words[i]} {words[i+1]}".upper()
                if two in KNOWN_TOWNS | UK_COUNTIES:
                    peel_at = i
                    break
        if peel_at is not None:
            trailing = " ".join(words[peel_at:])
            address_line_1 = " ".join(words[:peel_at]).strip()
            remainder = (trailing.upper() + " " + remainder).strip()

    locality = remainder.title() if remainder else ""
    town_from_locality, line2, line3, line4 = split_locality(locality)

    # If the named building came from the preamble and doesn't ALREADY equal
    # part of the resolved street line, put it in line 4.
    if named_building:
        nb_upper = named_building.upper()
        already_in_line1 = (address_line_1 and nb_upper in address_line_1.upper())
        if not already_in_line1:
            if not line4:
                line4 = named_building
            elif not line3:
                line3 = named_building
            elif line4 != named_building and line3 != named_building:
                # Make room: shift line3 down if there's nothing in line4 that matters
                pass

    # -------- Step 11: town_or_city --------
    town_or_city = "London" if is_london_postcode(postcode) else (town_from_locality or "")

    # -------- Step 12: building number decision --------
    def flat_type(fi): return fi.get("type", "") if fi else ""
    def flats_are_letters(fi): return "letter" in flat_type(fi)

    def flats_share_house_root(h, fi):
        if not h or not fi: return False
        base = re.match(r"^(\d+)", h)
        if not base: return False
        root = base.group(1)
        return all(re.match(rf"^{root}\b", t) for t in fi.get("tokens", []))

    if not flat_info:
        building_number = house
    elif flats_are_letters(flat_info):
        if block_from_parens and house:
            building_number = house
        else:
            building_number = flat_info["display"] if flat_info["display"] else house
    elif flats_share_house_root(house, flat_info):
        building_number = house
    else:
        building_number = house if house else flat_info["display"]

    # -------- Step 13: assemble --------
    parsed = {
        "building_number": building_number,
        "address line 1": address_line_1,
        "address line 2": line2,
        "address line 3": line3,
        "address line 4": line4,
        "town_or_city": town_or_city,
        "postcode": (postcode or ""),
        "block": (block or ""),
    }
    label, flags = score_confidence(parsed, original)
    if used_fallback and address_line_1:
        flags.append("no_house_number_matched")
        if label == "High":
            label = "Medium"
    parsed["confidence"] = label
    parsed["confidence_flags"] = ",".join(flags) if flags else "ok"
    return parsed



# =========================================================
# Rule-aware wrapper around parse_address
# =========================================================
def parse_address_with_rules(full: str, apply_rules: bool = True) -> dict:
    if apply_rules:
        key = rule_key(full or "")
        rule = st.session_state.rules.get(key)
        if rule:
            out = dict(rule)
            out["confidence"] = "High"
            out["confidence_flags"] = "user_rule"
            return out
    return parse_address(full)


# =========================================================
# Sidebar
# =========================================================
with st.sidebar:
    st.header("Options")
    sample_toggle = st.toggle("Use sample data", value=False)
    conf_filter = st.selectbox(
        "Filter rows by confidence",
        ["All rows", "Low only", "Low + Medium"],
        index=0,
    )

    st.markdown("---")
    st.subheader("Custom rules")
    st.caption(
        f"**{len(st.session_state.rules)}** saved rule(s). "
        "Rules are matched by the normalised original address string."
    )
    if st.button("Clear all rules"):
        st.session_state.rules = {}
        save_rules({})
        st.success("All rules cleared.")

    if st.session_state.rules:
        with st.expander("View saved rules"):
            for k, v in list(st.session_state.rules.items())[:50]:
                st.code(
                    f"{k[:80]}{'…' if len(k) > 80 else ''}\n"
                    f"→ {v.get('address line 1','')}, {v.get('postcode','')}",
                    language=None,
                )


# =========================================================
# Load data
# =========================================================
@st.cache_data(show_spinner=False)
def load_file(upload) -> pd.DataFrame:
    if upload.name.lower().endswith(".csv"):
        return pd.read_csv(upload)
    return pd.read_excel(upload)


if sample_toggle:
    df = pd.DataFrame(
        [
            {"address": "BROCKLEY ROAD(141), BROCKLEY, SE4 Flats A to C 141 Upper Brockley Road Brockley  SE4 1TF"},
            {"address": "Brunel House, RM8 (Flats) BRUNEL HOUSE 4 CHANCELLOR WAY DAGENHAM ESSEX RM8 2GQ"},
            {"address": "Sissulu Court, Redclyffe Road E6 SISSULU COURT REDCLYFFE ROAD NEWHAM LONDON E6 1DW"},
            {"address": "2-196 (EVEN) HAYNES PARK COURT, Havering 2-196 (EVEN) HAYNES PARK COURT  SLEWINS LANE HORNCHURCH ESSEX RM11 2DB"},
            {"address": "Wid Terrace, CM15 (2 - 16 Evens) 2-16 (Evens) Wid Terrace Church Lane Doddinghurst Brentwood CM15 0DA"},
            {"address": "Penthouse, 1-3 Canada Square, Canary Wharf, London E14 5AB"},
        ]
    )
else:
    upload = st.file_uploader("Upload CSV or Excel", type=["csv", "xlsx", "xls"])
    if upload is not None:
        df = load_file(upload)
    else:
        df = None

if df is None:
    st.info("Upload a file or turn on 'Use sample data' in the sidebar.")
    st.stop()

# =========================================================
# Column selection
# =========================================================
st.subheader("1) Choose column(s)")
cols = list(df.columns)

# Smart default: pick first column containing "address", else the first column
default_addr_idx = 0
for i, c in enumerate(cols):
    if "address" in str(c).lower():
        default_addr_idx = i
        break

address_col = st.selectbox(
    "Address text column",
    options=cols,
    index=default_addr_idx,
)

pc_options = ["<none>"] + cols
default_pc_idx = 0
for i, c in enumerate(cols):
    if "postcode" in str(c).lower() or "post_code" in str(c).lower() or "zip" in str(c).lower():
        default_pc_idx = i + 1
        break

postcode_col = st.selectbox(
    "(Optional) Separate postcode column",
    options=pc_options,
    index=default_pc_idx,
)

run = st.button("Clean addresses", type="primary")

# =========================================================
# Run parsing
# =========================================================
if run or st.session_state.get("parsed_df") is not None:

    if run:
        work = df.copy()
        full_strings = work[address_col].astype(str)
        if postcode_col != "<none>":
            pc_series = work[postcode_col].fillna("").astype(str)
            need_pc = ~full_strings.str.contains(POSTCODE_RE)
            full_strings = full_strings.where(
                ~need_pc, (full_strings + " " + pc_series).str.strip()
            )

        parsed = full_strings.apply(parse_address_with_rules).apply(pd.Series)

        if postcode_col != "<none>":
            parsed["postcode"] = parsed["postcode"].where(
                parsed["postcode"].ne(""), work[postcode_col].fillna("")
            )

        original_cols = work.columns.tolist()
        clashes = {c: f"original_{c}" for c in original_cols if c in parsed.columns}
        work_safe = work.rename(columns=clashes)
        if address_col in work_safe.columns:
            work_safe = work_safe.rename(columns={address_col: "original_address"})

        output_cols = [
            "building_number",
            "address line 1",
            "address line 2",
            "address line 3",
            "address line 4",
            "town_or_city",
            "postcode",
            "block",
            "confidence",
            "confidence_flags",
        ]
        output = pd.concat([work_safe, parsed[output_cols]], axis=1)

        if output.columns.duplicated().any():
            output = output.loc[:, ~output.columns.duplicated()].copy()

        st.session_state.parsed_df = output
        st.session_state.address_col_name = "original_address"

    output = st.session_state.parsed_df

    # ----- Summary stats -----
    st.subheader("2) Results")
    n_low = int((output["confidence"] == "Low").sum())
    n_med = int((output["confidence"] == "Medium").sum())
    n_high = int((output["confidence"] == "High").sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Total rows", len(output))
    with c2:
        st.metric("🟢 High", n_high)
    with c3:
        st.metric("🟡 Medium", n_med)
    with c4:
        st.metric("🔴 Low", n_low)
    with c5:
        st.metric("Rules active", len(st.session_state.rules))

    display_df = output
    if conf_filter == "Low only":
        display_df = output[output["confidence"] == "Low"]
    elif conf_filter == "Low + Medium":
        display_df = output[output["confidence"].isin(["Low", "Medium"])]

    PAGE_SIZE = 500
    total_rows = len(display_df)
    if total_rows > PAGE_SIZE:
        max_page = max(1, (total_rows - 1) // PAGE_SIZE + 1)
        page = st.number_input(
            f"Page (showing {PAGE_SIZE} rows at a time, {total_rows} total)",
            min_value=1,
            max_value=max_page,
            value=1,
            step=1,
        )
        page_df = display_df.iloc[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    else:
        page_df = display_df

    st.dataframe(
        page_df,
        use_container_width=True,
        column_config={
            "confidence": st.column_config.SelectboxColumn(
                "Confidence",
                options=["High", "Medium", "Low"],
                width="small",
            ),
            "original_address": st.column_config.TextColumn("Original address", width="large"),
            "address line 1": st.column_config.TextColumn("Address line 1"),
            "postcode": st.column_config.TextColumn("Postcode", width="small"),
        },
    )

    # =========================================================
    # Rule editor: let user correct any row -> saved as rule
    # =========================================================
    st.subheader("3) Teach the parser (add a rule)")
    st.caption(
        "If an address was parsed wrongly, pick the row, set the correct values, "
        "and save it as a rule. The parser will use that rule next time it sees the same input."
    )

    idx_low = output.index[output["confidence"] == "Low"].tolist()
    idx_med = output.index[output["confidence"] == "Medium"].tolist()
    idx_high = output.index[output["confidence"] == "High"].tolist()
    ordered_idx = idx_low + idx_med + idx_high

    CONF_ICON = {"Low": "🔴", "Medium": "🟡", "High": "🟢", "": "⚪"}
    picked = st.selectbox(
        "Choose a row to review / correct",
        options=ordered_idx,
        format_func=lambda i: (
            f"{CONF_ICON.get(str(output.loc[i, 'confidence']), '⚪')} "
            f"[{output.loc[i, 'confidence']}] "
            f"{str(output.loc[i, 'original_address'])[:100]}"
        ),
    )

    if picked is not None:
        row = output.loc[picked]
        with st.form(f"rule_form_{picked}"):
            st.markdown(f"**Original:** `{row['original_address']}`")
            conf_icon = CONF_ICON.get(str(row["confidence"]), "⚪")
            st.markdown(
                f"**Confidence:** {conf_icon} `{row['confidence']}`  \n"
                f"**Flags:** `{row['confidence_flags']}`"
            )
            cc1, cc2 = st.columns(2)
            with cc1:
                new_building = st.text_input("building_number", value=str(row["building_number"] or ""))
                new_l1 = st.text_input("address line 1", value=str(row["address line 1"] or ""))
                new_l2 = st.text_input("address line 2", value=str(row["address line 2"] or ""))
                new_l3 = st.text_input("address line 3", value=str(row["address line 3"] or ""))
            with cc2:
                new_l4 = st.text_input("address line 4", value=str(row["address line 4"] or ""))
                new_town = st.text_input("town_or_city", value=str(row["town_or_city"] or ""))
                new_pc = st.text_input("postcode", value=str(row["postcode"] or ""))
                new_block = st.text_input("block", value=str(row["block"] or ""))

            submitted = st.form_submit_button("💾 Save as rule & re-apply", type="primary")
            if submitted:
                key = rule_key(row["original_address"])
                st.session_state.rules[key] = {
                    "building_number": new_building,
                    "address line 1": new_l1,
                    "address line 2": new_l2,
                    "address line 3": new_l3,
                    "address line 4": new_l4,
                    "town_or_city": new_town,
                    "postcode": new_pc,
                    "block": new_block,
                }
                save_rules(st.session_state.rules)

                for i in output.index:
                    k = rule_key(output.loc[i, "original_address"])
                    r = st.session_state.rules.get(k)
                    if r:
                        for col, val in r.items():
                            output.at[i, col] = val
                        output.at[i, "confidence"] = "High"
                        output.at[i, "confidence_flags"] = "user_rule"

                st.session_state.parsed_df = output
                st.success(f"Rule saved. Now {len(st.session_state.rules)} rule(s) active.")
                st.rerun()

    # =========================================================
    # Downloads
    # =========================================================
    st.subheader("4) Download")
    csv_bytes = output.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="cleaned_addresses.csv",
        mime="text/csv",
    )

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="xlsxwriter") as writer:
        output.to_excel(writer, sheet_name="Cleaned", index=False)
    st.download_button(
        "Download Excel",
        data=bio.getvalue(),
        file_name="cleaned_addresses.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if st.session_state.rules:
        st.download_button(
            "Download rules (JSON)",
            data=json.dumps(st.session_state.rules, indent=2).encode("utf-8"),
            file_name="address_rules.json",
            mime="application/json",
        )

    rules_upload = st.file_uploader("Import rules JSON (merges with existing)", type=["json"])
    if rules_upload is not None:
        try:
            incoming = json.loads(rules_upload.read().decode("utf-8"))
            if isinstance(incoming, dict):
                st.session_state.rules.update(incoming)
                save_rules(st.session_state.rules)
                st.success(
                    f"Imported {len(incoming)} rule(s). Total: {len(st.session_state.rules)}."
                )
            else:
                st.error("Rules file must be a JSON object mapping key -> address fields.")
        except Exception as e:
            st.error(f"Could not parse rules: {e}")

    st.caption(
        "**How it works**: parser strips the messy preamble, anchors on the last "
        "house-number-and-street, then assigns locality lines. Counties (Essex, Kent, …) "
        "are split off the town; named buildings (Sissulu Court, Brunel House) go to "
        "address line 4; flat/unit/block info is captured separately. "
        "🟢 High: clean parse · 🟡 Medium: parsed with uncertainty · 🔴 Low: needs review. "
        "Custom rules always override the parser for matching inputs."
    )
else:
    st.info("Set your columns and press **Clean addresses** to parse.")
