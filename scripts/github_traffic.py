#!/usr/bin/env python
"""Archive GitHub traffic for every AI4EPS repo.

GitHub keeps only a 14-day rolling window of views and clones. Anything older is
gone and cannot be backfilled, so the only way to have a long record is to append
to one as it goes. That is all this does: fetch the window, merge it into a CSV,
keep the union.

Auth: GH_TOKEN with `repo` scope. The Actions default GITHUB_TOKEN is NOT enough
— it is scoped to the repo it runs in, so it returns 403 for every other repo in
the org. Use a PAT stored as a secret.

Two cautions when summarising data/github-traffic.csv:

  * `uniques` does not add across days. GitHub reports unique visitors per day,
    so anyone who returns on Tuesday is counted twice in a weekly sum.
  * Clones are mostly machines. A recent 14-day window showed 18,021 clones
    against 1,616 page views; CI re-clones constantly. Unique visitors is the
    human-scale figure.
"""

import argparse
import os
import sys
import time

import pandas as pd
import requests

API = "https://api.github.com"
HEADERS_VERSION = "2022-11-28"


def session(token):
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": HEADERS_VERSION,
        "Authorization": f"Bearer {token}",
    })
    return s


def get(s, url, **kwargs):
    """One retry on the secondary rate limit, which returns 403 with a header
    rather than 429."""
    for attempt in range(3):
        response = s.get(url, timeout=30, **kwargs)
        if response.status_code == 403 and "rate limit" in response.text.lower():
            wait = int(response.headers.get("retry-after", 60))
            print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        return response
    return response


def repos(s, org):
    out, page = [], 1
    while True:
        r = get(s, f"{API}/orgs/{org}/repos", params={"per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out += batch
        page += 1
    return out


def traffic(s, full_name, kind):
    """kind is 'views' or 'clones'. Returns per-day rows, or None if the token
    lacks push access to that repo (403)."""
    r = get(s, f"{API}/repos/{full_name}/traffic/{kind}", params={"per": "day"})
    if r.status_code == 403:
        return None
    r.raise_for_status()
    return r.json().get(kind, [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="AI4EPS")
    parser.add_argument("--out", default="data/github-traffic.csv")
    parser.add_argument("--snapshot", default="data/github-repos.csv")
    parser.add_argument("--include-forks", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("set GH_TOKEN (a PAT with repo scope)")

    s = session(token)
    listing = repos(s, args.org)
    if not args.include_forks:
        listing = [r for r in listing if not r["fork"]]
    print(f"{len(listing)} repos in {args.org}")

    rows, denied = [], []
    for repo in sorted(listing, key=lambda r: r["name"].lower()):
        name = repo["full_name"]
        for kind in ("views", "clones"):
            data = traffic(s, name, kind)
            if data is None:
                denied.append(f"{name}:{kind}")
                continue
            for day in data:
                rows.append({
                    "repo": repo["name"],
                    "date": day["timestamp"][:10],
                    "metric": kind,
                    "count": day["count"],
                    "uniques": day["uniques"],
                })

    if denied:
        print(f"no push access on {len(denied)} endpoints "
              f"(needs a PAT with repo scope): {', '.join(denied[:4])}...",
              file=sys.stderr)
    if not rows:
        sys.exit("no traffic rows returned — check the token's scope")

    fresh = pd.DataFrame(rows)
    if os.path.exists(args.out):
        fresh = pd.concat([pd.read_csv(args.out), fresh], ignore_index=True)
    # Same repo/date/metric can arrive on several runs while the day is inside
    # the window; the last read of a completed day is the authoritative one.
    fresh = (fresh.drop_duplicates(subset=["repo", "date", "metric"], keep="last")
                  .sort_values(["repo", "metric", "date"]))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fresh.to_csv(args.out, index=False)

    # Stars and forks are point-in-time, not windowed, so one row per run is
    # enough to reconstruct the curve later.
    snapshot = pd.DataFrame([{
        "pulled": pd.Timestamp.now("UTC").date().isoformat(),
        "repo": r["name"],
        "stars": r["stargazers_count"],
        "forks": r["forks_count"],
        "open_issues": r["open_issues_count"],
        # No watcher count here: the org listing omits subscribers_count, and its
        # watchers_count is a legacy alias of stars, so it carries nothing.
    } for r in listing])
    if os.path.exists(args.snapshot):
        snapshot = pd.concat([pd.read_csv(args.snapshot), snapshot], ignore_index=True)
    snapshot = snapshot.drop_duplicates(subset=["pulled", "repo"], keep="last")
    snapshot.to_csv(args.snapshot, index=False)

    views = fresh[fresh.metric == "views"]
    clones = fresh[fresh.metric == "clones"]
    print(f"{len(fresh):,} traffic rows covering {fresh.date.min()} to {fresh.date.max()}")
    print(f"  views  {views['count'].sum():,} ({views.uniques.sum():,} unique)")
    print(f"  clones {clones['count'].sum():,} ({clones.uniques.sum():,} unique)")
    print(f"  stars  {snapshot.groupby('pulled').stars.sum().iloc[-1]:,} across "
          f"{len(listing)} repos")
    print(f"wrote {args.out} and {args.snapshot}")


if __name__ == "__main__":
    main()
