"""Synthetic name and place vocabulary.

Words rather than numbers, so this is not a profile parameter: `profiles.yml` holds the
tunables and this holds the strings they are drawn from. Everything here is invented. Street
names are constructed, cities are real place names carrying no personal data, and the email
domain is `example.invalid`, which RFC 2606 reserves so that no address generated here can
reach anybody.
"""

from __future__ import annotations

GIVEN_NAMES = (
    "Alina", "Anders", "Anneke", "Bastien", "Bianca", "Casper", "Cecilia", "Damian",
    "Dorota", "Eero", "Elena", "Emil", "Esther", "Fabien", "Federica", "Freja",
    "Gabriel", "Greta", "Hannes", "Helena", "Ignacio", "Ines", "Ivar", "Jana",
    "Joakim", "Julien", "Kasper", "Katarzyna", "Lars", "Laura", "Lena", "Lorenzo",
    "Maarten", "Magda", "Marek", "Marta", "Mateo", "Mikkel", "Nadia", "Niels",
    "Nora", "Oskar", "Paulina", "Pieter", "Rafael", "Renata", "Rikke", "Sander",
    "Sigrid", "Simone", "Sofia", "Stefan", "Tereza", "Thijs", "Tobias", "Ulrike",
    "Valentina", "Viktor", "Wiktor", "Yannick", "Zofia", "Zoltan",
)

FAMILY_NAMES = (
    "Aalto", "Andersen", "Bakker", "Bergman", "Bianchi", "Bruun", "Castellano", "Dahl",
    "Delacroix", "Dubois", "Eriksen", "Ferrari", "Fischer", "Fontaine", "Garcia", "Gruber",
    "Haapala", "Hansen", "Hoffmann", "Jansen", "Jelinek", "Kaminski", "Keller", "Kovacs",
    "Kowalski", "Laurent", "Lehtinen", "Lindqvist", "Lombardi", "Marchetti", "Martens",
    "Mendes", "Moreau", "Mueller", "Nielsen", "Novak", "Nowak", "Olsen", "Pedersen",
    "Peeters", "Pereira", "Petrov", "Ricci", "Rossi", "Ruiz", "Schmidt", "Schneider",
    "Sorensen", "Stefanov", "Suarez", "Svensson", "Tamm", "Vermeulen", "Virtanen",
    "Vogel", "Wagner", "Weber", "Wojcik", "Zielinski", "Zimmermann",
)

STREET_STEMS = (
    "Acacia", "Alder", "Aspen", "Beech", "Birch", "Bridge", "Castle", "Cedar",
    "Chapel", "Clover", "Cypress", "Elm", "Fountain", "Garden", "Harbour", "Hazel",
    "Juniper", "Laurel", "Linden", "Maple", "Market", "Meadow", "Mill", "Oak",
    "Orchard", "Poplar", "Quarry", "River", "Rowan", "Spruce", "Station", "Sycamore",
    "Thistle", "Verbena", "Willow", "Yarrow",
)

STREET_SUFFIXES = ("Street", "Avenue", "Lane", "Square", "Terrace", "Way", "Walk", "Close")

# Cities by residence country. Between three and six each, enough that a city is not a proxy
# for a country and few enough that the postal code prefix still means something.
CITIES: dict[str, tuple[str, ...]] = {
    "DE": ("Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt", "Leipzig"),
    "FR": ("Paris", "Lyon", "Marseille", "Toulouse", "Nantes"),
    "NL": ("Amsterdam", "Rotterdam", "Utrecht", "Eindhoven"),
    "ES": ("Madrid", "Barcelona", "Valencia", "Seville", "Bilbao"),
    "IT": ("Rome", "Milan", "Turin", "Bologna", "Naples"),
    "AT": ("Vienna", "Graz", "Linz", "Salzburg"),
    "BE": ("Brussels", "Antwerp", "Ghent", "Bruges"),
    "PL": ("Warsaw", "Krakow", "Gdansk", "Wroclaw", "Poznan"),
    "IE": ("Dublin", "Cork", "Galway", "Limerick"),
    "PT": ("Lisbon", "Porto", "Braga", "Coimbra"),
    "SE": ("Stockholm", "Gothenburg", "Malmo", "Uppsala"),
    "FI": ("Helsinki", "Espoo", "Tampere", "Turku"),
    "DK": ("Copenhagen", "Aarhus", "Odense", "Aalborg"),
    "EE": ("Tallinn", "Tartu", "Narva"),
    "LT": ("Vilnius", "Kaunas", "Klaipeda"),
    "LV": ("Riga", "Daugavpils", "Liepaja"),
    "NO": ("Oslo", "Bergen", "Trondheim", "Stavanger"),
    "GB": ("London", "Manchester", "Birmingham", "Edinburgh"),
    "CH": ("Zurich", "Geneva", "Basel", "Bern"),
    "TR": ("Istanbul", "Ankara", "Izmir"),
    "US": ("New York", "Chicago", "Austin", "Seattle"),
    "CA": ("Toronto", "Montreal", "Vancouver"),
    "JP": ("Tokyo", "Osaka", "Kyoto"),
    "SG": ("Singapore",),
    "AU": ("Sydney", "Melbourne", "Brisbane"),
    "IN": ("Mumbai", "Bengaluru", "Delhi"),
    "AE": ("Dubai", "Abu Dhabi"),
    "ZA": ("Cape Town", "Johannesburg"),
    "BR": ("Sao Paulo", "Rio de Janeiro"),
    "MX": ("Mexico City", "Guadalajara"),
}

# Postal code shapes per country, as (digits, prefix). The district used before gold is
# left(postal_code, 2), which pii_classification.md records as a deliberate simplification: EU
# postcode formats differ and a production system would apply a country-specific rule.
POSTCODE_DIGITS: dict[str, int] = {
    "DE": 5, "FR": 5, "NL": 4, "ES": 5, "IT": 5, "AT": 4, "BE": 4, "PL": 5,
    "IE": 5, "PT": 4, "SE": 5, "FI": 5, "DK": 4, "EE": 5, "LT": 5, "LV": 4,
    "NO": 4, "GB": 5, "CH": 4, "TR": 5, "US": 5, "CA": 5, "JP": 5, "SG": 6,
    "AU": 4, "IN": 6, "AE": 5, "ZA": 4, "BR": 5, "MX": 5,
}

MERCHANT_STEMS = (
    "Alpine", "Amber", "Anchor", "Aurora", "Basalt", "Beacon", "Bluebird", "Cardinal",
    "Cascade", "Cobalt", "Compass", "Copper", "Crescent", "Delta", "Ember", "Everest",
    "Falcon", "Fjord", "Granite", "Harbour", "Heron", "Indigo", "Ironwood", "Juniper",
    "Kestrel", "Lantern", "Lighthouse", "Magnolia", "Meridian", "Northwind", "Obsidian",
    "Opal", "Osprey", "Pinnacle", "Quartz", "Ravello", "Redwood", "Saffron", "Sandpiper",
    "Sequoia", "Silverline", "Solstice", "Summit", "Tamarind", "Thornfield", "Tidewater",
    "Vermilion", "Vertex", "Wayfarer", "Westgate", "Wildflower", "Zephyr",
)

MERCHANT_SUFFIX_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "Retail": ("Retail", "Stores", "Trading", "Supply Co", "Market"),
    "Dining": ("Kitchen", "Cafe", "Bistro", "Grill", "Eatery"),
    "Fuel": ("Fuels", "Service Station", "Energy", "Petroleum"),
    "Travel": ("Travel", "Holidays", "Air", "Lodging", "Transit"),
    "Health": ("Pharmacy", "Health", "Apothecary"),
    "Digital": ("Digital", "Media", "Online", "Networks"),
    "Entertainment": ("Entertainment", "Leisure", "Arcade"),
    "Cash": ("Cash Services", "ATM Network"),
    "Financial": ("Financial", "Payments", "Services"),
    "Charity": ("Foundation", "Trust", "Relief"),
    "Government": ("Authority", "Council", "Agency"),
}

AGENT_PARTNERS = (
    "Bluebird Convenience", "Cornerstone Kiosk", "Daylight Grocers", "Eastgate Newsagent",
    "Fairhaven Post Point", "Greenfield Stores", "Harbourside Kiosk", "Ivybridge Pharmacy",
    "Junction Market", "Kingsway Convenience", "Lakeside Newsagent", "Milestone Grocers",
)

# Merchant names are dirty by design (spec 002): casing, punctuation and trailing location
# noise are left as an acquirer would have sent them, so conformance in silver has real work.
DIRTY_SUFFIXES = (
    " #{store}", " STORE {store}", " - {city}", " {city} {store}", "*{store}", " LTD",
    " GMBH", " B.V.", " S.A.", " //{store}",
)


def street_address(street_index: int, suffix_index: int, number: int) -> str:
    return (
        f"{number} {STREET_STEMS[street_index % len(STREET_STEMS)]} "
        f"{STREET_SUFFIXES[suffix_index % len(STREET_SUFFIXES)]}"
    )


def cities_for(country_code: str) -> tuple[str, ...]:
    return CITIES.get(country_code, ("Central",))


def postcode_digits(country_code: str) -> int:
    return POSTCODE_DIGITS.get(country_code, 5)
