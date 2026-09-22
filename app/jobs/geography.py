"""Where a job is, relative to where the candidate wants to work.

Geographic targeting fix (2026-09-14): discovery, eligibility, preferences and
admission treated a location as an opaque string, so a US-heavy board produced a
US-heavy opportunity pool for a candidate in Hyderabad. This module is the one
place a location is read, in two deterministic layers:

* :func:`parse_places` — tenant independent: which countries, metros, states and
  multi-country zones a posting names, and whether it says it is remote.
* :class:`GeographyPolicy` — relative to one candidate's target
  (``Hyderabad, Telangana, India`` by default through the profile location):
  PRIMARY, remote within the target country, another city in the target country,
  international, or unconfirmed.

Nothing is inferred beyond the text. An unrecognised location is UNKNOWN, never
the target; "Remote" without a country is never the candidate's country; "US
Remote" is never "India Remote". Changing the target (Hyderabad -> Bengaluru ->
Pune, or India -> international) is configuration, not code: any city in the
gazetteer carries its metro aliases, and any other city is matched by its name.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional

INDIA = "India"
US = "United States"

_WORLD = frozenset({"asia", "europe", "north america", "latin america", "middle east", "africa", "oceania"})

#: canonical metro -> (country, state, aliases). Aliases are ASCII lower case.
_METROS: dict[str, tuple[str, Optional[str], tuple[str, ...]]] = {
    # India
    "Hyderabad": (INDIA, "Telangana", ("hyderabad", "hyderbad", "secunderabad", "cyberabad", "gachibowli", "hitec city", "hitech city", "madhapur", "kondapur", "nanakramguda", "kokapet", "manikonda", "hyderabad metropolitan area")),
    "Bengaluru": (INDIA, "Karnataka", ("bengaluru", "bangalore", "whitefield", "electronic city", "koramangala", "marathahalli")),
    "Pune": (INDIA, "Maharashtra", ("pune", "pimpri", "chinchwad", "hinjewadi", "kharadi")),
    "Mumbai": (INDIA, "Maharashtra", ("mumbai", "bombay", "navi mumbai", "thane", "powai", "andheri")),
    "Delhi NCR": (INDIA, None, ("delhi", "new delhi", "delhi ncr", "gurgaon", "gurugram", "noida", "greater noida", "faridabad", "ghaziabad")),
    "Chennai": (INDIA, "Tamil Nadu", ("chennai", "madras")),
    "Kolkata": (INDIA, "West Bengal", ("kolkata", "calcutta")),
    "Ahmedabad": (INDIA, "Gujarat", ("ahmedabad", "gandhinagar", "gift city")),
    "Kochi": (INDIA, "Kerala", ("kochi", "cochin")),
    "Thiruvananthapuram": (INDIA, "Kerala", ("thiruvananthapuram", "trivandrum")),
    "Coimbatore": (INDIA, "Tamil Nadu", ("coimbatore",)),
    "Jaipur": (INDIA, "Rajasthan", ("jaipur",)),
    "Chandigarh": (INDIA, None, ("chandigarh", "mohali", "panchkula")),
    "Indore": (INDIA, "Madhya Pradesh", ("indore",)),
    "Bhubaneswar": (INDIA, "Odisha", ("bhubaneswar",)),
    "Visakhapatnam": (INDIA, "Andhra Pradesh", ("visakhapatnam", "vizag")),
    "Vijayawada": (INDIA, "Andhra Pradesh", ("vijayawada",)),
    "Warangal": (INDIA, "Telangana", ("warangal",)),
    "Mysuru": (INDIA, "Karnataka", ("mysuru", "mysore")),
    "Mangaluru": (INDIA, "Karnataka", ("mangaluru", "mangalore")),
    "Nagpur": (INDIA, "Maharashtra", ("nagpur",)),
    "Lucknow": (INDIA, "Uttar Pradesh", ("lucknow",)),
    # United States
    "San Francisco Bay Area": (US, "California", ("san francisco", "south san francisco", "bay area", "palo alto", "mountain view", "menlo park", "sunnyvale", "san jose", "santa clara", "redwood city", "san mateo", "foster city", "oakland", "cupertino", "fremont", "berkeley", "burlingame", "emeryville", "milpitas")),
    "New York City": (US, "New York", ("new york city", "new york", "nyc", "brooklyn", "manhattan", "jersey city", "hoboken")),
    "Seattle": (US, "Washington", ("seattle", "bellevue", "redmond", "kirkland")),
    "Los Angeles": (US, "California", ("los angeles", "santa monica", "culver city", "irvine", "pasadena")),
    "Boston": (US, "Massachusetts", ("boston", "somerville")),
    "Chicago": (US, "Illinois", ("chicago",)),
    "Austin": (US, "Texas", ("austin",)),
    "Denver": (US, "Colorado", ("denver", "boulder")),
    "Atlanta": (US, "Georgia", ("atlanta",)),
    "Washington DC": (US, None, ("washington dc", "washington d c", "reston", "mclean")),
    "Miami": (US, "Florida", ("miami",)),
    "Dallas": (US, "Texas", ("dallas", "plano")),
    "Houston": (US, "Texas", ("houston",)),
    "Phoenix": (US, "Arizona", ("phoenix", "scottsdale")),
    "Salt Lake City": (US, "Utah", ("salt lake city", "lehi")),
    "Pittsburgh": (US, "Pennsylvania", ("pittsburgh",)),
    "Philadelphia": (US, "Pennsylvania", ("philadelphia",)),
    "Raleigh": (US, "North Carolina", ("raleigh", "durham")),
    "Nashville": (US, "Tennessee", ("nashville",)),
    "Minneapolis": (US, "Minnesota", ("minneapolis",)),
    "Detroit": (US, "Michigan", ("detroit", "ann arbor")),
    "San Diego": (US, "California", ("san diego",)),
    "Portland": (US, "Oregon", ("portland",)),
}

#: state / province -> country.
_SUBREGIONS: dict[str, str] = {
    **{s: INDIA for s in ("Telangana", "Andhra Pradesh", "Karnataka", "Maharashtra", "Tamil Nadu", "Kerala", "Gujarat", "West Bengal", "Haryana", "Uttar Pradesh", "Rajasthan", "Punjab", "Madhya Pradesh", "Odisha", "Goa", "Bihar", "Assam")},
    **{s: US for s in ("California", "Washington", "Texas", "Massachusetts", "Illinois", "Colorado", "Georgia", "Florida", "Oregon", "Utah", "Arizona", "North Carolina", "Virginia", "Pennsylvania", "Ohio", "Michigan", "Minnesota", "Tennessee", "New Jersey", "Maryland", "Wisconsin", "Missouri", "Indiana", "Connecticut", "New Mexico", "Nevada", "Kentucky", "Iowa", "Idaho", "Delaware", "Hawaii", "Alaska", "Alabama", "Arkansas", "Kansas", "Louisiana", "Maine", "Mississippi", "Montana", "Nebraska", "New Hampshire", "North Dakota", "Oklahoma", "Rhode Island", "South Carolina", "South Dakota", "Vermont", "West Virginia", "Wyoming", "District of Columbia")},
    **{s: "Canada" for s in ("Ontario", "British Columbia", "Quebec", "Alberta")},
}

#: country -> (zones, aliases).
_COUNTRIES: dict[str, tuple[frozenset, tuple[str, ...]]] = {
    INDIA: (frozenset({"asia"}), ("india", "bharat")),
    US: (frozenset({"north america"}), ("united states", "united states of america", "usa")),
    "Canada": (frozenset({"north america"}), ("canada", "toronto", "vancouver", "montreal", "waterloo", "ottawa", "calgary", "edmonton")),
    "Mexico": (frozenset({"latin america", "north america"}), ("mexico", "mexico city", "ciudad de mexico", "guadalajara", "monterrey")),
    "United Kingdom": (frozenset({"europe"}), ("united kingdom", "england", "scotland", "wales", "great britain", "london", "manchester", "edinburgh", "bristol", "glasgow", "belfast", "leeds")),
    "Ireland": (frozenset({"europe"}), ("ireland", "dublin", "cork", "galway")),
    "Germany": (frozenset({"europe"}), ("germany", "deutschland", "berlin", "munich", "munchen", "hamburg", "frankfurt", "cologne", "stuttgart")),
    "France": (frozenset({"europe"}), ("france", "paris", "lyon")),
    "Netherlands": (frozenset({"europe"}), ("netherlands", "amsterdam", "rotterdam", "the hague", "utrecht", "eindhoven")),
    "Spain": (frozenset({"europe"}), ("spain", "madrid", "barcelona")),
    "Portugal": (frozenset({"europe"}), ("portugal", "lisbon", "porto")),
    "Italy": (frozenset({"europe"}), ("italy", "milan", "rome")),
    "Switzerland": (frozenset({"europe"}), ("switzerland", "zurich", "geneva", "lausanne")),
    "Sweden": (frozenset({"europe"}), ("sweden", "stockholm", "gothenburg")),
    "Denmark": (frozenset({"europe"}), ("denmark", "copenhagen")),
    "Norway": (frozenset({"europe"}), ("norway", "oslo")),
    "Finland": (frozenset({"europe"}), ("finland", "helsinki")),
    "Poland": (frozenset({"europe"}), ("poland", "warsaw", "krakow", "wroclaw")),
    "Czechia": (frozenset({"europe"}), ("czech republic", "czechia", "prague", "brno")),
    "Austria": (frozenset({"europe"}), ("austria", "vienna")),
    "Belgium": (frozenset({"europe"}), ("belgium", "brussels")),
    "Serbia": (frozenset({"europe"}), ("serbia", "belgrade", "novi sad")),
    "Romania": (frozenset({"europe"}), ("romania", "bucharest", "cluj")),
    "Ukraine": (frozenset({"europe"}), ("ukraine", "kyiv", "kiev", "lviv")),
    "Greece": (frozenset({"europe"}), ("greece", "athens")),
    "Hungary": (frozenset({"europe"}), ("hungary", "budapest")),
    "Estonia": (frozenset({"europe"}), ("estonia", "tallinn")),
    "Lithuania": (frozenset({"europe"}), ("lithuania", "vilnius")),
    "Turkey": (frozenset({"europe", "middle east"}), ("turkey", "turkiye", "istanbul")),
    "Israel": (frozenset({"middle east"}), ("israel", "tel aviv", "jerusalem", "haifa")),
    "United Arab Emirates": (frozenset({"middle east"}), ("united arab emirates", "dubai", "abu dhabi")),
    "Saudi Arabia": (frozenset({"middle east"}), ("saudi arabia", "riyadh")),
    "Qatar": (frozenset({"middle east"}), ("qatar", "doha")),
    "Egypt": (frozenset({"africa", "middle east"}), ("egypt", "cairo")),
    "Singapore": (frozenset({"asia"}), ("singapore",)),
    "Japan": (frozenset({"asia"}), ("japan", "tokyo", "osaka")),
    "South Korea": (frozenset({"asia"}), ("south korea", "korea", "seoul")),
    "China": (frozenset({"asia"}), ("china", "beijing", "shanghai", "shenzhen", "hangzhou")),
    "Hong Kong": (frozenset({"asia"}), ("hong kong",)),
    "Taiwan": (frozenset({"asia"}), ("taiwan", "taipei")),
    "Philippines": (frozenset({"asia"}), ("philippines", "manila", "cebu")),
    "Indonesia": (frozenset({"asia"}), ("indonesia", "jakarta")),
    "Malaysia": (frozenset({"asia"}), ("malaysia", "kuala lumpur")),
    "Vietnam": (frozenset({"asia"}), ("vietnam", "ho chi minh", "hanoi")),
    "Thailand": (frozenset({"asia"}), ("thailand", "bangkok")),
    "Pakistan": (frozenset({"asia"}), ("pakistan", "karachi", "lahore", "islamabad")),
    "Bangladesh": (frozenset({"asia"}), ("bangladesh", "dhaka")),
    "Sri Lanka": (frozenset({"asia"}), ("sri lanka", "colombo")),
    "Nepal": (frozenset({"asia"}), ("nepal", "kathmandu")),
    "Australia": (frozenset({"oceania"}), ("australia", "sydney", "melbourne", "brisbane", "perth", "canberra")),
    "New Zealand": (frozenset({"oceania"}), ("new zealand", "auckland", "wellington")),
    "Brazil": (frozenset({"latin america"}), ("brazil", "brasil", "sao paulo", "rio de janeiro")),
    "Argentina": (frozenset({"latin america"}), ("argentina", "buenos aires")),
    "Colombia": (frozenset({"latin america"}), ("colombia", "bogota", "medellin")),
    "Chile": (frozenset({"latin america"}), ("chile", "santiago")),
    "Peru": (frozenset({"latin america"}), ("peru", "lima")),
    "Costa Rica": (frozenset({"latin america"}), ("costa rica",)),
    "Nigeria": (frozenset({"africa"}), ("nigeria", "lagos")),
    "Kenya": (frozenset({"africa"}), ("kenya", "nairobi")),
    "South Africa": (frozenset({"africa"}), ("south africa", "cape town", "johannesburg")),
}

#: Multi-country zones. Whether one includes the target is decided against the
#: target country's zones, so "APAC" may include India while "EMEA" does not.
_ZONES: dict[str, frozenset] = {
    "north america": frozenset({"north america"}),
    "americas": frozenset({"north america", "latin america"}),
    "latam": frozenset({"latin america"}),
    "latin america": frozenset({"latin america"}),
    "south america": frozenset({"latin america"}),
    "emea": frozenset({"europe", "middle east", "africa"}),
    "europe": frozenset({"europe"}),
    "european union": frozenset({"europe"}),
    "nordics": frozenset({"europe"}),
    "dach": frozenset({"europe"}),
    "middle east": frozenset({"middle east"}),
    "mena": frozenset({"middle east", "africa"}),
    "africa": frozenset({"africa"}),
    "apac": frozenset({"asia", "oceania"}),
    "asia pacific": frozenset({"asia", "oceania"}),
    "apj": frozenset({"asia", "oceania"}),
    "asia": frozenset({"asia"}),
    "south asia": frozenset({"asia"}),
    "southeast asia": frozenset({"asia"}),
    "oceania": frozenset({"oceania"}),
    "anz": frozenset({"oceania"}),
    "worldwide": _WORLD,
    "global": _WORLD,
    "anywhere": _WORLD,
    "international": _WORLD,
}

#: Used only when reading a work-authorization statement ("Indian citizen").
_DEMONYMS = {"indian": INDIA, "american": US, "british": "United Kingdom", "canadian": "Canada", "german": "Germany", "irish": "Ireland", "singaporean": "Singapore", "australian": "Australia"}

#: "U.S" / "U.K" without the final period: a sentence-final "authorized to work in the U.S."
#: reaches the parser with its last period taken as the sentence end (audit fix 2026-09-14).
_CODES = {"IND": INDIA, "USA": US, "US": US, "U.S.": US, "U.S": US, "U.S.A.": US, "U.S.A": US, "UK": "United Kingdom", "U.K.": "United Kingdom", "U.K": "United Kingdom", "UAE": "United Arab Emirates"}
_CODE_RE = re.compile(r"(?<![A-Za-z.])(U\.S\.A\.?|U\.S\.?|USA|US|U\.K\.?|UK|UAE|EU|IND)(?![A-Za-z])")
_US_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
_STATE_CODE_RE = re.compile(r"(?:,|\(|\s-)\s*(" + "|".join(_US_STATE_CODES) + r")(?![A-Za-z])")
_REMOTE_RE = re.compile(r"(?<![a-z])(remote|work from home|wfh|telecommute|fully distributed|anywhere)(?![a-z])")


def _fold(text: str) -> str:
    """ASCII lower case with separators as spaces: 'São Paulo / US-Remote' -> 'sao paulo us remote'."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", re.sub(r"[-_/|()\[\]{}.;:·•]+", " ", ascii_text)).strip()


def _build_aliases() -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    for metro, (_, _, names) in _METROS.items():
        for name in names:
            aliases.setdefault(name, ("metro", metro))
    for region in _SUBREGIONS:
        aliases.setdefault(_fold(region), ("subregion", region))
    for country, (_, names) in _COUNTRIES.items():
        for name in names:
            aliases.setdefault(name, ("country", country))
    for zone in _ZONES:
        aliases.setdefault(zone, ("zone", zone))
    return aliases


_ALIASES = _build_aliases()
_ALIAS_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in sorted(_ALIASES, key=len, reverse=True)) + r")(?![a-z0-9])")
_DEMONYM_RE = re.compile(r"(?<![a-z])(" + "|".join(_DEMONYMS) + r")(?![a-z])")


def _contains_word(needle: str, haystack: str) -> bool:
    return bool(needle) and re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


@dataclass(frozen=True)
class Places:
    """What a set of location strings names. Tenant independent."""

    countries: frozenset = frozenset()
    metros: frozenset = frozenset()
    subregions: frozenset = frozenset()
    zones: frozenset = frozenset()
    remote: bool = False
    #: The multi-country zone names as written ("north america", "emea").
    zone_names: frozenset = frozenset()
    #: Folded text, so a target city outside the gazetteer can still be matched by name.
    text: str = ""

    @property
    def recognized(self) -> bool:
        return bool(self.countries or self.metros or self.subregions or self.zones)


def parse_places(texts: Iterable[Optional[str]], remote: bool = False, demonyms: bool = False) -> Places:
    """Countries, metros, states and zones named across ``texts``. Unknown words are ignored, never guessed."""
    raw = " ; ".join(t for t in texts if isinstance(t, str) and t.strip())
    folded = _fold(raw)
    countries: set = set()
    metros: set = set()
    subregions: set = set()
    zones: set = set()
    zone_names: set = set()
    for match in _ALIAS_RE.finditer(folded):
        kind, value = _ALIASES[match.group(1)]
        if kind == "metro":
            country, region, _ = _METROS[value]
            metros.add(value)
            countries.add(country)
            if region:
                subregions.add(region)
        elif kind == "subregion":
            subregions.add(value)
            countries.add(_SUBREGIONS[value])
        elif kind == "country":
            countries.add(value)
        else:
            zones |= _ZONES[value]
            zone_names.add(value)
    for match in _CODE_RE.finditer(raw):
        code = match.group(1)
        if code == "EU":
            zones.add("europe")
            zone_names.add("europe")
        else:
            countries.add(_CODES[code])
    for match in _STATE_CODE_RE.finditer(raw):
        code = match.group(1)
        # "Hyderabad, IN" is India and "Toronto, CA" is Canada, not Indiana / California.
        if (code == "IN" and INDIA in countries) or (code == "CA" and "Canada" in countries):
            continue
        countries.add(US)
    if demonyms:
        countries |= {_DEMONYMS[m.group(1)] for m in _DEMONYM_RE.finditer(folded)}
    return Places(frozenset(countries), frozenset(metros), frozenset(subregions), frozenset(zones), bool(remote or _REMOTE_RE.search(folded)), frozenset(zone_names), folded)


def job_location_texts(location: Optional[str], locations: Optional[Iterable[str]] = None, metadata: Optional[dict] = None, offices: bool = False) -> list[str]:
    """Every location string a stored posting carries: primary, split list and the source's secondary locations.

    Greenhouse office names are included only on request: boards file jobs under
    broad offices ("India - Remote" for a Bengaluru role, found on the real run
    2026-09-14), so an office name is a fallback, never a correction.
    """
    texts: list[Any] = [location, *(locations or [])]
    meta = metadata or {}
    for item in meta.get("secondaryLocations") or []:
        texts.append(item.get("location") if isinstance(item, dict) else item)
    categories = meta.get("categories")
    if isinstance(categories, dict):
        texts.extend(categories.get("allLocations") or [])
    if offices:
        for office in meta.get("offices") or []:
            if isinstance(office, dict):
                texts.append(office.get("name"))
    seen: list[str] = []
    for text in texts:
        if isinstance(text, str) and text.strip() and text.strip() not in seen:
            seen.append(text.strip())
    return seen


def job_places(location: Optional[str], locations: Optional[Iterable[str]] = None, metadata: Optional[dict] = None) -> Places:
    """A posting's places: its location strings, or its office names only when those strings name nothing."""
    remote = explicit_remote(metadata)
    places = parse_places(job_location_texts(location, locations, metadata), remote=remote)
    if places.recognized or places.remote:
        return places
    fallback = parse_places(job_location_texts(location, locations, metadata, offices=True), remote=remote)
    return fallback if fallback.recognized else places


def explicit_remote(metadata: Optional[dict]) -> bool:
    """The source's own remote flag. Description wording is deliberately not used: "remote-friendly" is not a work mode."""
    meta = metadata or {}
    return "REMOTE" in str(meta.get("workplaceType") or "").upper() or meta.get("isRemote") is True


class GeoClass(str, Enum):
    PRIMARY = "PRIMARY"  # in the target metro (or the target state / country when no city is targeted)
    COUNTRY_REMOTE = "COUNTRY_REMOTE"  # remote, restricted to the target country ("Remote - India")
    COUNTRY_OTHER = "COUNTRY_OTHER"  # another location in the target country ("Bengaluru")
    REMOTE_UNSPECIFIED = "REMOTE_UNSPECIFIED"  # remote with no country, or a zone that may include it ("APAC")
    FOREIGN_REMOTE = "FOREIGN_REMOTE"  # remote, restricted to other countries ("US Remote")
    FOREIGN = "FOREIGN"  # located only outside the target country
    UNKNOWN = "UNKNOWN"  # no usable location


class GeoTier(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    INTERNATIONAL = "INTERNATIONAL"
    UNCONFIRMED = "UNCONFIRMED"
    EXCLUDED = "EXCLUDED"
    NO_TARGET = "NO_TARGET"


@dataclass(frozen=True)
class GeoTarget:
    label: str
    country: Optional[str]
    subregion: Optional[str] = None
    metro: Optional[str] = None
    aliases: tuple = ()

    @property
    def zones(self) -> frozenset:
        return _COUNTRIES[self.country][0] if self.country in _COUNTRIES else frozenset()


def parse_target(text: Optional[str]) -> Optional[GeoTarget]:
    """'Hyderabad, Telangana, India' -> metro Hyderabad (with Secunderabad etc.), Telangana, India."""
    label = (text or "").strip()
    if not label:
        return None
    places = parse_places([label])
    country = next(iter(places.countries)) if len(places.countries) == 1 else None
    subregion = next(iter(places.subregions)) if len(places.subregions) == 1 else None
    if len(places.metros) == 1:
        metro = next(iter(places.metros))
        metro_country, metro_region, aliases = _METROS[metro]
        return GeoTarget(label, metro_country, metro_region or subregion, metro, aliases)
    first = _fold(label.split(",")[0])
    kind = _ALIASES.get(first, ("", ""))[0]
    if first and kind not in ("subregion", "country", "zone"):
        # A city the gazetteer does not know: matched by its own name only.
        return GeoTarget(label, country, subregion, first.title(), (first,))
    if subregion and country is None:
        country = _SUBREGIONS.get(subregion)
    return GeoTarget(label, country, subregion)


@dataclass(frozen=True)
class GeoAssessment:
    geo_class: GeoClass
    tier: GeoTier
    in_policy: bool
    detail: str
    countries: tuple = ()
    metros: tuple = ()
    remote: bool = False

    @property
    def excluded_at_discovery(self) -> bool:
        """Only a posting tied to other countries is dropped before ingestion; anything unconfirmed is kept."""
        return not self.in_policy and self.geo_class in (GeoClass.FOREIGN, GeoClass.FOREIGN_REMOTE)


def _where(places: Places) -> str:
    covered = {_METROS[m][0] for m in places.metros}
    parts = sorted(places.metros) + sorted(places.countries - covered) + sorted(z.upper() if len(z) <= 4 else z.title() for z in places.zone_names)
    return ", ".join(parts) or "an unrecognised location"


@dataclass(frozen=True)
class GeographyPolicy:
    """The candidate's job-market target. Inactive (everything in policy) until a target country is known."""

    target: Optional[GeoTarget] = None
    include_country_remote: bool = True
    include_other_cities: bool = False
    also_consider: tuple = ()
    allow_international: bool = False
    #: Postings that state no usable location, or only "Remote" with no country.
    #: Kept in policy by default so an unstated location is never a silent
    #: rejection; eligibility marks them UNCERTAIN, never LIKELY or PRIMARY.
    include_unconfirmed: bool = True
    #: Where the target came from: "preferences", "profile" or "none".
    source: str = "none"

    @property
    def active(self) -> bool:
        return self.target is not None and self.target.country is not None

    def classify(self, places: Places) -> GeoClass:
        target = self.target
        if target is None:
            return GeoClass.UNKNOWN
        if target.metro and any(_contains_word(alias, places.text) for alias in target.aliases):
            return GeoClass.PRIMARY
        if target.subregion and target.subregion in places.subregions and (not target.metro or not places.metros):
            # "Telangana, India" names the target's state and no other city.
            return GeoClass.PRIMARY
        if not target.metro and not target.subregion and target.country and target.country in places.countries:
            return GeoClass.PRIMARY
        if target.country and target.country in places.countries:
            return GeoClass.COUNTRY_REMOTE if places.remote else GeoClass.COUNTRY_OTHER
        if places.zones & target.zones:
            return GeoClass.REMOTE_UNSPECIFIED if places.remote else GeoClass.UNKNOWN
        if places.countries or places.zones:
            return GeoClass.FOREIGN_REMOTE if places.remote else GeoClass.FOREIGN
        return GeoClass.REMOTE_UNSPECIFIED if places.remote else GeoClass.UNKNOWN

    def assess_job(self, location: Optional[str], locations: Optional[Iterable[str]] = None, metadata: Optional[dict] = None) -> GeoAssessment:
        return self.assess(job_places(location, locations, metadata))

    def assess(self, places: Places) -> GeoAssessment:
        countries, metros = tuple(sorted(places.countries)), tuple(sorted(places.metros))
        if not self.active:
            return GeoAssessment(GeoClass.UNKNOWN, GeoTier.NO_TARGET, True, "no geographic target is configured", countries, metros, places.remote)
        target = self.target
        geo = self.classify(places)
        where = _where(places)

        def result(tier: GeoTier, in_policy: bool, detail: str) -> GeoAssessment:
            return GeoAssessment(geo, tier, in_policy, detail, countries, metros, places.remote)

        if geo is GeoClass.PRIMARY:
            return result(GeoTier.PRIMARY, True, f"{where}: in the primary target {target.label}")
        extra = self._also_considered(places, geo)
        if extra:
            return result(GeoTier.SECONDARY, True, f"{where}: matches the additional location '{extra}'")
        if geo is GeoClass.COUNTRY_REMOTE:
            if self.include_country_remote:
                return result(GeoTier.SECONDARY, True, f"remote within {target.country}")
            return result(GeoTier.EXCLUDED, False, f"remote within {target.country}, which the location preference does not include")
        if geo is GeoClass.COUNTRY_OTHER:
            if self.include_other_cities:
                return result(GeoTier.SECONDARY, True, f"{where}: another location in {target.country}")
            return result(GeoTier.EXCLUDED, False, f"{where}: another location in {target.country}, which the location preference does not include")
        if geo in (GeoClass.FOREIGN, GeoClass.FOREIGN_REMOTE):
            mode = "remote, restricted to " if geo is GeoClass.FOREIGN_REMOTE else ""
            if self.allow_international:
                return result(GeoTier.INTERNATIONAL, True, f"{mode}{where}: international, allowed by the location preference")
            return result(GeoTier.EXCLUDED, False, f"{mode}{where}: outside {target.country}; international opportunities are not enabled")
        detail = "remote with no stated country" if geo is GeoClass.REMOTE_UNSPECIFIED else "the posting states no usable location"
        if self.include_unconfirmed or self.allow_international:
            return result(GeoTier.UNCONFIRMED, True, f"{detail}; not confirmed to be in {target.label} or {target.country}, kept for review")
        return result(GeoTier.UNCONFIRMED, False, f"{detail}; not confirmed to be in {target.label} or {target.country}, and unconfirmed locations are not included")

    def _also_considered(self, places: Places, geo: GeoClass) -> Optional[str]:
        for entry in self.also_consider:
            folded = _fold(entry)
            if folded in ("remote", "anywhere", "remote anywhere"):
                if geo in (GeoClass.REMOTE_UNSPECIFIED, GeoClass.COUNTRY_REMOTE):
                    return entry
                continue
            extra = parse_target(entry)
            if extra is not None and GeographyPolicy(target=extra).classify(places) is GeoClass.PRIMARY:
                return entry
        return None


def policy_from_preferences(preferences: Any, profile_location: Optional[str]) -> GeographyPolicy:
    """The candidate's policy: the preference's primary target, else the profile location (never duplicated)."""
    primary = (getattr(preferences, "location_primary", None) or "").strip()
    fallback = (profile_location or "").strip()
    target = parse_target(primary or fallback)
    if primary and target is not None and target.country is None:
        # Audit fix (2026-09-14): a primary target naming no known country ("Tirupati",
        # a typo like "Hydrabad") made the policy inactive, which silently switched
        # geographic targeting off and put every US / EU posting back in policy. The
        # city is still matched by its own name, inside the profile location's country.
        home = parse_target(fallback)
        if home is not None and home.country is not None:
            target = GeoTarget(target.label, home.country, target.subregion, target.metro, target.aliases)
    return GeographyPolicy(
        target=target,
        include_country_remote=bool(getattr(preferences, "location_include_country_remote", True)),
        include_other_cities=bool(getattr(preferences, "location_include_other_cities", False)),
        also_consider=tuple(p for p in (getattr(preferences, "preferred_locations", None) or []) if p and p.strip()),
        allow_international=bool(getattr(preferences, "location_allow_international", False)),
        include_unconfirmed=bool(getattr(preferences, "location_include_unconfirmed", True)),
        source="preferences" if primary else ("profile" if fallback else "none"),
    )


@dataclass(frozen=True)
class DiscoveryGeography:
    """Every projected tenant's policy. A posting is dropped only when no tenant could want it."""

    policies: tuple = ()

    def excluded(self, location: Optional[str], locations: Optional[Iterable[str]] = None, metadata: Optional[dict] = None) -> Optional[GeoAssessment]:
        if not self.policies or any(not p.active for p in self.policies):
            return None
        assessments = [p.assess_job(location, locations, metadata) for p in self.policies]
        return assessments[0] if all(a.excluded_at_discovery for a in assessments) else None


def authorization_countries(text: Optional[str]) -> frozenset:
    """Countries a work-authorization statement names ('Indian citizen' -> India). Empty when none is named."""
    return parse_places([text], demonyms=True).countries if text else frozenset()


_WORK_IN_RE = re.compile(
    r"(?:authori[sz]ed|eligible|legally (?:able|permitted)|permitted|right) to work (?:in|for) (?:the )?([a-z .]{2,40}?)(?=[,;:()]|\.\s|\.$|\s(?:without|and|for|on|at|with|who|as|now|currently|by|is|are|to)\b|$)",
    re.IGNORECASE,
)


def required_work_countries(text: Optional[str]) -> frozenset:
    """Countries a job description requires authorization for ('authorized to work in the United States')."""
    found: set = set()
    for match in _WORK_IN_RE.finditer(text or ""):
        found |= parse_places([match.group(1)]).countries
    return frozenset(found)
