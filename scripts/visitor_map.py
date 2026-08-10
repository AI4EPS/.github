#!/usr/bin/env python
"""Render the visitor map for the AI4EPS org profile.

Pulls city-level visitors from the GA4 Data API and draws them over a shaded
relief basemap. Writes profile/assets/visitors-relief-light.jpg in place, so the
markdown never has to change, and appends a country snapshot to
data/visitors-history.csv.

Credentials: GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account key
with Viewer on the property; GA_PROPERTY_ID as the numeric property id.
"""

import argparse
import os
import sys
from datetime import date

import cartopy.crs as ccrs
import matplotlib
import numpy as np
import pandas as pd
import pycountry
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from PIL import Image  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

NOT_A_PLACE = {"(not set)", "(other)", "", "ZZ"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Pacific-centred, which puts the two densest regions (western US and East Asia)
# in one view. 150 rather than a true 180: the seam sits at centre+180, so 180
# would cut down the Greenwich meridian and split Africa in half.
CENTER_LON = 150

# Antarctica contributes no data and a third of the frame, so the frame is
# clipped — symmetric about the equator, since an asymmetric clip tilts the
# globe's outline. Cities span -45.9 to 65.6, so +/-70 keeps every dot clear.
LAT_RANGE = [-70, 70]

LEGEND_STOPS = [10, 100, 1000]

# Red rather than a blue: on this basemap a mid blue sits at the same value as
# the ocean. Red is furthest from both the ocean and the tan land, and it is the
# convention for epicentres on seismicity maps.
DOT = "#e31a1c"
DOT_EDGE = "#ffffff"
INK = "#1a1a1a"
PANEL = "white"

# 15% white over the basemap. Natural Earth I renders more saturated than the
# USGS poster's base; the haze closes that gap and buys contrast for the red.
HAZE_KEEP = 0.85

# matplotlib sizes a scatter marker by area in points squared, while dot_radius
# returns a diameter in pixels. At dpi 200 one point is 200/72 pixels.
POINTS_PER_PIXEL = 72.0 / 200.0


def run_report(client, prop, dimensions, metrics, start, end, limit=100000):
    request = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        limit=limit,
    )
    response = client.run_report(request)
    rows = [
        [v.value for v in row.dimension_values] + [v.value for v in row.metric_values]
        for row in response.rows
    ]
    frame = pd.DataFrame(rows, columns=list(dimensions) + list(metrics))
    for metric in metrics:
        frame[metric] = pd.to_numeric(frame[metric], errors="coerce").fillna(0).astype(int)
    return frame


def dot_radius(users, span):
    """Log-scaled radius with a visible floor. The claim this figure makes is
    reach, so a one-user city has to remain findable; strict area proportionality
    would render most of the 2,000 cities as sub-pixel specks."""
    return 3.0 + 13.0 * (np.log10(np.asarray(users) + 1) / span)


def haze(array, keep):
    """Blend the relief toward white. Returns uint8: imshow treats a float array
    as 0-1 and silently clips, so a 0-255 float renders the basemap blank."""
    return (array * keep + 255.0 * (1 - keep)).astype(np.uint8)


def render(frame, path, relief, headline, source, width=12.0, dpi=200):
    projection = ccrs.Robinson(central_longitude=CENTER_LON)

    # Size the canvas to the clipped map so no white band is left above or below.
    # x comes from the projection's own limits, not from transforming -180/180:
    # with a central_longitude of 150 both fold onto the same relative longitude.
    x0, x1 = projection.x_limits
    bounds = projection.transform_points(
        ccrs.PlateCarree(),
        np.full(2, float(CENTER_LON)), np.array(LAT_RANGE, dtype=float))
    y0, y1 = bounds[0, 1], bounds[1, 1]
    height = width * (y1 - y0) / (x1 - x0)

    fig = plt.figure(figsize=(width, height), dpi=dpi)
    # Opaque white: the relief is photographic, so the figure ships as JPEG (a
    # fifth the size of PNG) and JPEG has no alpha channel.
    fig.patch.set_facecolor("white")
    axes = fig.add_axes([0, 0, 1, 1], projection=projection)
    axes.set_global()
    axes.spines["geo"].set_visible(False)
    axes.patch.set_alpha(0)

    axes.imshow(
        haze(np.asarray(Image.open(relief).convert("RGB"), dtype=float), HAZE_KEEP),
        origin="upper", extent=[-180, 180, -90, 90],
        transform=ccrs.PlateCarree(), interpolation="bilinear", zorder=0,
    )

    # Descending: the biggest circles are drawn first and end up underneath, so a
    # small city inside a large one stays visible. Ascending buries exactly the
    # sparse dots that carry the "used everywhere" claim.
    frame = frame.sort_values("users", ascending=False)
    span = np.log10(frame.users.max() + 1)
    area = (dot_radius(frame.users, span) * 2 * POINTS_PER_PIXEL) ** 2
    axes.scatter(frame.lon, frame.lat, transform=ccrs.PlateCarree(),
                 s=area, c=DOT, alpha=0.62,
                 edgecolors=DOT_EDGE, linewidths=0.3, zorder=3)

    axes.set_xlim(x0, x1)
    axes.set_ylim(y0, y1)

    handles = [
        Line2D([], [], marker="o", linestyle="none", label=f"{stop:,}",
               markerfacecolor=DOT, markeredgecolor=DOT_EDGE, markeredgewidth=0.35,
               alpha=0.72, markersize=dot_radius(stop, span) * 2 * POINTS_PER_PIXEL)
        for stop in LEGEND_STOPS
    ]
    legend = axes.legend(
        handles=handles, loc="lower left", title="Users per city",
        bbox_to_anchor=(0.055, 0.035), framealpha=0.85, facecolor=PANEL,
        labelcolor=INK, edgecolor="#b4c3ce", fontsize=9,
        labelspacing=0.75, borderpad=0.7,
    )
    legend.get_title().set_fontsize(9)
    legend.get_title().set_color(INK)
    # Left-align the title with the swatches; matplotlib centres it by default.
    legend._legend_box.align = "left"
    legend.set_zorder(5)

    # Anchored in the South Pacific: the position was chosen by projecting every
    # city and scanning for a rectangle clear of all of them, then bounded at
    # x=0.82 because Robinson narrows near -65 and anything further right drifts
    # off the globe.
    for y, text, size, weight in ((0.092, headline, 13, "bold"),
                                  (0.048, source, 8.5, "normal")):
        axes.text(0.820, y, text, transform=axes.transAxes,
                  fontsize=size, fontweight=weight, color=INK,
                  va="bottom", ha="right", zorder=6,
                  bbox=dict(facecolor=PANEL, alpha=0.72, edgecolor="none", pad=3))

    fig.savefig(path, pad_inches=0, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--property", default=os.environ.get("GA_PROPERTY_ID"))
    parser.add_argument("--relief", default="data/ne1-relief.jpg")
    parser.add_argument("--coords", default="data/city-coords.csv")
    parser.add_argument("--history", default="data/visitors-history.csv")
    parser.add_argument("--out", default="profile/assets/visitors-relief-light.jpg")
    args = parser.parse_args()

    if not args.property:
        sys.exit("set GA_PROPERTY_ID (numeric property id, not G-XXXXXXX)")

    client = BetaAnalyticsDataClient()
    window = ("2015-08-14", "yesterday")  # 2015-08-14 is the GA4 API's floor

    dates = run_report(client, args.property, ["date"], ["activeUsers"], *window)
    dates = dates[dates.date.str.len() == 8]
    if dates.empty:
        sys.exit("GA returned no rows — check the property id and the Viewer grant")
    first, last = dates.date.min(), dates.date.max()

    # activeUsers is de-duplicated per row, so summing it across a dimension
    # counts anyone who appeared in two places twice. Headline totals have to
    # come from an undimensioned query.
    totals = run_report(client, args.property, [],
                        ["activeUsers", "screenPageViews"], *window)
    users = int(totals.activeUsers.iloc[0])

    countries = run_report(client, args.property, ["countryId"],
                           ["activeUsers", "screenPageViews"], *window, limit=300)
    countries = countries[~countries.countryId.isin(NOT_A_PLACE)]

    cities = run_report(client, args.property, ["city", "region", "countryId"],
                        ["activeUsers"], *window)
    cities = cities[~cities.city.isin(NOT_A_PLACE)]
    located = cities.merge(pd.read_csv(args.coords),
                           on=["city", "region", "countryId"], how="inner")
    located = located.rename(columns={"activeUsers": "users"})
    matched = located.users.sum() / max(cities.activeUsers.sum(), 1)
    print(f"{len(located):,} cities placed ({matched * 100:.1f}% of city-level "
          f"users); rerun build_city_coords.py if that share drops")

    headline = (f"{users:,} users  ·  {len(countries)} countries  ·  "
                f"{len(located):,} cities")
    source = (f"AI4EPS   ·   {MONTHS[int(first[4:6]) - 1]} {first[:4]} – "
              f"{MONTHS[int(last[4:6]) - 1]} {last[:4]}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    render(located, args.out, args.relief, headline, source)

    snapshot = pd.DataFrame({
        "pulled": date.today().isoformat(),
        "window_start": first,
        "window_end": last,
        "country": countries.countryId,
        "iso3": countries.countryId.map(
            lambda c: getattr(pycountry.countries.get(alpha_2=c), "alpha_3", None)),
        "users": countries.activeUsers,
        "views": countries.screenPageViews,
    }).dropna(subset=["iso3"])
    os.makedirs(os.path.dirname(args.history) or ".", exist_ok=True)
    snapshot.to_csv(args.history, mode="a", index=False,
                    header=not os.path.exists(args.history))

    print(f"{users:,} users / {len(countries)} countries / {len(located):,} cities")


if __name__ == "__main__":
    main()
