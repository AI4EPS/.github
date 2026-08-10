# AI4EPS org profile

`profile/README.md` renders on <https://github.com/AI4EPS>: a title and a map of
the cities our tools and documentation are used from.

| Script | Workflow | Runs |
|---|---|---|
| `visitor_map.py` | `visitor-map.yml` | monthly — redraws the map from GA4 |
| `github_traffic.py` | `github-traffic.yml` | daily — archives GitHub views/clones |
| `build_city_coords.py` | — | on demand, when the map reports a low match rate |

Daily is not a preference for the traffic job: GitHub serves only a 14-day
window, and whatever falls out of it cannot be recovered.

## Secrets

| Name | Value |
|---|---|
| `GA_PROPERTY_ID` | `317041191` |
| `GA_SA_KEY` | JSON key for a service account with Viewer on that property |
| `AI4EPS_TRAFFIC_TOKEN` | PAT with `repo` scope — the default `GITHUB_TOKEN` is scoped to this repo alone and 403s on the others |

## Locally

```sh
export GA_PROPERTY_ID=317041191
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/ga/ga-reader.json
python scripts/visitor_map.py

export GH_TOKEN=$(gh auth token)
python scripts/github_traffic.py --org AI4EPS
```

`data/ne1-relief.jpg` is the basemap, committed so the render never depends on
naturalearthdata.com.
