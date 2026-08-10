#!/usr/bin/env python
"""Build data/city-coords.csv: a lat/lon lookup for the cities GA actually reports.

GA4 gives a city name and a country, never coordinates, so the dot map needs a
gazetteer. We resolve once and commit the result so the daily job does not depend
on geonames.org staying up — the same third-party fragility that killed the
star-history badge.

Matching is on (city, region, country), not (city, country). Name collisions are
not rare and they are not harmless: Pasadena, Texas outranks Pasadena, California
on population, so a country-level match silently moves Caltech to the Gulf coast.
GA's region dimension resolves the ambiguity; population rank is only the fallback
when no region is reported.

Re-run when the unmatched share reported by visitors.py starts creeping up.
"""

import argparse
import io
import os
import sys
import unicodedata
import zipfile

import pandas as pd
import requests

# cities1000 rather than cities15000: our traffic includes small research towns
# (Stanford, Los Alamos, Menlo Park) that the 15k-population cut drops entirely.
GEONAMES = "https://download.geonames.org/export/dump/cities1000.zip"
ADMIN1 = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

COLUMNS = [
    "geonameid", "name", "asciiname", "alternatenames", "latitude", "longitude",
    "feature_class", "feature_code", "country_code", "cc2", "admin1", "admin2",
    "admin3", "admin4", "population", "elevation", "dem", "timezone", "modified",
]

# GA exonyms that GeoNames files under a different primary name. Keep this list
# to names actually verified against the dump — a wrong alias silently redirects
# a city that was already matching to a key that does not exist.
ALIASES = {
    ("new york", "US"): ("new york city", "US"),
}


def normalize(text):
    """Casefold and strip accents so 'Zürich' and 'Zurich' collide on purpose."""
    if not isinstance(text, str):
        return ""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return stripped.casefold().strip()


def fetch(path_or_url):
    if os.path.exists(path_or_url):
        return open(path_or_url, "rb").read()
    print(f"downloading {path_or_url}")
    response = requests.get(path_or_url, timeout=180)
    response.raise_for_status()
    return response.content


def load_geonames(path_or_url):
    raw = fetch(path_or_url)
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            member = [n for n in archive.namelist() if n.endswith(".txt")][0]
            raw = archive.read(member)
    return pd.read_csv(io.BytesIO(raw), sep="\t", names=COLUMNS, dtype=str,
                       quoting=3, na_filter=False)


def load_admin1(path_or_url):
    """(country, normalized region name) -> admin1 code, e.g. ('US','california')->'CA'."""
    raw = fetch(path_or_url)
    frame = pd.read_csv(io.BytesIO(raw), sep="\t", dtype=str, quoting=3,
                        na_filter=False,
                        names=["code", "name", "asciiname", "geonameid"])
    lookup = {}
    for row in frame.itertuples():
        if "." not in row.code:
            continue
        country, admin1 = row.code.split(".", 1)
        for label in {row.name, row.asciiname}:
            key = (country, normalize(label))
            if key[1]:
                lookup.setdefault(key, admin1)
    return lookup


def build_indexes(geo):
    """Two indexes: one keyed with the state/province, one without. Both keep the
    largest city when several share a name inside the same scope."""
    geo = geo.copy()
    geo["population"] = pd.to_numeric(geo.population, errors="coerce").fillna(0)
    geo = geo.sort_values("population", ascending=False)

    with_region, without_region = {}, {}
    for row in geo.itertuples():
        names = {row.name, row.asciiname}
        if row.alternatenames:
            names.update(row.alternatenames.split(",")[:12])
        point = (float(row.latitude), float(row.longitude))
        for candidate in names:
            key = normalize(candidate)
            if not key:
                continue
            with_region.setdefault((key, row.country_code, row.admin1), point)
            without_region.setdefault((key, row.country_code), point)
    return with_region, without_region


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property", default=os.environ.get("GA_PROPERTY_ID"))
    parser.add_argument("--geonames", default=GEONAMES)
    parser.add_argument("--admin1", default=ADMIN1)
    parser.add_argument("--out", default="data/city-coords.csv")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from visitors import run_report, NOT_A_PLACE

    if not args.property:
        sys.exit("set GA_PROPERTY_ID")

    client = BetaAnalyticsDataClient()
    cities = run_report(client, args.property, ["city", "region", "countryId"],
                        ["activeUsers"], "2015-08-14", "yesterday", limit=100000)
    cities = cities[~cities.city.isin(NOT_A_PLACE)]
    print(f"{len(cities)} GA city rows, {cities.activeUsers.sum():,} user-rows")

    regions = load_admin1(args.admin1)
    with_region, without_region = build_indexes(load_geonames(args.geonames))
    print(f"gazetteer: {len(with_region):,} city/region keys, "
          f"{len(without_region):,} city keys, {len(regions):,} regions")

    resolved, missing, by_region = [], [], 0
    for row in cities.itertuples():
        key = (normalize(row.city), row.countryId)
        key = ALIASES.get(key, key)
        admin1 = regions.get((row.countryId, normalize(row.region)))

        point = with_region.get((key[0], key[1], admin1)) if admin1 else None
        if point:
            by_region += 1
        else:
            point = without_region.get(key)

        if point:
            resolved.append((row.city, row.region, row.countryId, point[0], point[1]))
        else:
            missing.append((row.city, row.region, row.countryId, row.activeUsers))

    frame = pd.DataFrame(resolved,
                         columns=["city", "region", "countryId", "lat", "lon"])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    frame.to_csv(args.out, index=False)

    lost = sum(m[3] for m in missing)
    total = cities.activeUsers.sum()
    print(f"matched {len(frame):,}/{len(cities):,} rows "
          f"({by_region:,} pinned by region, {len(frame) - by_region:,} by population)")
    print(f"unmatched {lost:,} of {total:,} user-rows ({lost / total * 100:.1f}%)")
    if missing:
        print("largest unmatched:")
        for city, region, country, users in sorted(missing, key=lambda m: -m[3])[:10]:
            print(f"  {city}, {region} ({country}): {users:,}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
