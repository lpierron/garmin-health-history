# Garmin Health History

[![CI](https://github.com/lpierron/garmin-health-history/actions/workflows/ci.yml/badge.svg)](https://github.com/lpierron/garmin-health-history/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Download your [Garmin Connect](https://connect.garmin.com/) activity history and
produce a monthly summary as CSV and Excel.

`history.py` fetches daily stats for a date range, aggregates them per month, and
writes `history.csv` and `activity.xlsx`. `analysis.py` and `analysis_fine.py`
then explore the relationship between activity and health indicators (see
[Analysis](#analysis)).

## Requirements

- Python >= 3.13
- Dependencies: `garminconnect`, `pandas`, `openpyxl`, `tqdm`, plus `scipy` and
  `matplotlib` for the analysis scripts

## Installation

Using [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

Or with pip:

```sh
pip install garminconnect pandas openpyxl scipy matplotlib tqdm
```

## Credentials

Credentials are never written to the code or to disk. Provide them through
environment variables:

```sh
export GARMIN_EMAIL="you@example.com"
export GARMIN_PASSWORD="your_password"
```

If unset, the script prompts for them (the password is hidden). If two-factor
authentication is enabled, you'll be asked for the MFA code.

After the first successful login, session tokens are cached in
`~/.garmin_tokens` so later runs don't need a password.

## Usage

```sh
python history.py --start 2020-08-01
python history.py --start 2024-01-01 --end 2026-10-15
python history.py --start 2020-08-01 --all      # + pas, sommeil, prédictions de course
```

| Option     | Default      | Description                                            |
| ---------- | ------------ | ------------------------------------------------------ |
| `--start`  | `2020-08-01` | Start date (YYYY-MM-DD)                                |
| `--end`    | today        | End date (YYYY-MM-DD) — **exclusive**                  |
| `--steps`  | off          | Also fetch the daily step count                        |
| `--sleep`  | off          | Also fetch the sleep score and duration                |
| `--races`  | off          | Also fetch the 5K / half-marathon race predictions     |
| `--all`    | off          | Fetch every optional indicator (`--steps --sleep --races`) |
| `--verbose` | off         | Log API failures (they are otherwise ignored)          |

The optional indicators are fetched in **date ranges** rather than day by day
(1 API call per 28 days for steps and sleep, 1 per 365 days for the race
predictions), so a full backfill costs a few hundred calls instead of one per
day. Only the missing days are requested, and a value already in the cache is
never overwritten.

## Data availability

Garmin does not expose every indicator over the whole history — the coverage
depends on the device, its firmware and when the data was synced:

| Indicator                              | Source                       | Typically available from |
| -------------------------------------- | ---------------------------- | ------------------------ |
| Daily stats, VO2max, weight            | one call per day             | start of the history     |
| Steps                                   | `stats/steps/daily`          | first days with data     |
| Sleep duration                         | `sleep/daily`                | ~2018-06 (with holes)    |
| Sleep score                            | `sleep/daily`                | ~2026-06 (device-dependent) |
| 5K / half-marathon race predictions    | `racepredictions`            | ~2023-02                 |

Indicators that are missing are simply empty columns: the scripts never fail
because of them, and `analysis_fine.py` lists them as *Données indisponibles*.

## Output

Two files are written in the current directory:

- `history.csv`
- `activity.xlsx`

Both contain one row per month with these columns:

`month`, `days`, `weight`, `rhr`, `hr_max`, `bb_max`, `VO2max`, `max_VO2max`,
`stress`, `calories`, `steps`, `moderate`, `intense`, `sleep_score`,
`sleep_hours`, `pred_5k`, `pred_half`, `total`

where `total = moderate + 2 * intense`.

### Units

| Column                          | Unit                        | Example      |
| ------------------------------- | --------------------------- | ------------ |
| `steps`                         | steps per day (pas/j)       | `9075`       |
| `moderate`, `intense`, `total`  | minutes per month           | `540`        |
| `calories`                      | kcal per month              | `8003`       |
| `sleep_hours`                   | `hh:mm`                     | `08:14`      |
| `pred_5k`, `pred_half`          | `hh:mm:ss`                  | `00:31:35`   |
| `sleep_score`                   | points (0-100)              | `79.6`       |
| `weight`                        | kg                          | `89.4`       |
| `VO2max`                        | ml/kg/min                   | `39.9`       |
| `rhr`, `hr_max`                 | bpm                         | `47.8`       |

Durations are written in their readable form (`hh:mm`, `hh:mm:ss`); the
analysis scripts read them back as numbers, so `history.csv` stays the single
source of truth. A month with no measurement for an indicator is left empty
(not zero), so "no reading" is never mistaken for "value of zero".

### Weekly synthesis

After the last six months, the script prints a synthesis of the **last 7 days**
(rolling window) compared with the **12 previous weeks**:

- activity (moderate, intense, weighted total, active calories) summed over the
  week, as in `history.csv`, and health indicators (resting HR, max HR, stress,
  Body Battery, VO2max, weight) averaged over the week; the step count is
  averaged over the week (pas/j) and the durations (sleep, race predictions)
  are shown as `hh:mm` / `hh:mm:ss`;
- the gap to the reference period (the weekly average of the 12 previous weeks)
  for each indicator, with a favourable/unfavourable reading for the health
  indicators;
- a flag when the week falls outside the range of the reference weeks, and a
  short verdict.

The synthesis is computed from the daily cache, so it costs no extra API call.
Indicators with too few measurements to compare are listed as such instead of
being interpreted.

## How it works

- Daily data is cached in `garmin_daily_cache.json`; already-fetched days are not
  downloaded again. The last 3 days are always refreshed since their data may be
  incomplete.
- A failed API call never erases a cached value: a day is only updated with the
  fields the API actually returned.
- Optional indicators are fetched by date range and merged into the cache, only
  where a value is missing.
- A progress bar tracks the download; it is hidden automatically when the output
  is not a terminal.
- A short delay between requests is applied to be gentle on Garmin's servers.
- `--verbose` logs the API errors that are otherwise ignored.

The generated files (`history.csv`, `activity.xlsx`), the cache, and session
tokens are all gitignored.

## Analysis

Two scripts explore the link between physical activity and health indicators
(resting heart rate, stress, Body Battery, weight, VO2max).

### `analysis.py` — monthly view

Reads `history.csv` and reports:

- **Trends**: Kendall tau of each indicator against time, with a Theil-Sen
  slope per year.
- **Correlations**: Kendall tau-b between activity (moderate, intense,
  weighted total, active calories, steps) and health, same month and with a
  one-month lag (does activity in month M relate to health in M+1?). Activity
  columns are divided by `days` so months of different lengths are comparable
  (the step count is already a per-day average).
- **Multiple testing**: p-values are corrected with Benjamini-Hochberg (FDR);
  the report prints q-values and flags `*` when q < 0.05.
- **Contrast**: median health of the next month for the most active third vs
  the least active third (Mann-Whitney).

```sh
python analysis.py                      # reads history.csv
python analysis.py --csv other.csv --min-days 25 --alpha 0.10
```

Outputs: `correlations.csv`, `correlations.png` (heatmap), plus the report on
stdout.

### `analysis_fine.py` — daily/weekly view with seasonal correction

Reads `garmin_daily_cache.json` and works at the daily and weekly scale:

1. **Cleaning**: days with `calories < --min-cal` are treated as "watch not
   worn" and blanked (weight from the scale is kept).
2. **Adjustment**: each series is reduced to residuals after removing a slow
   trend (linear spline, one knot per year), annual seasonality (2 harmonics),
   and the day-of-week effect (daily scale only). So summer is no longer
   compared to winter, nor 2019 to 2026 — only short-term deviations from the
   habitual level.
3. **Correlations**: Kendall tau-b, same period and with lags (+1/+2 weeks,
   +1 day).
4. **p-values by circular substitution**: one series is circularly shifted to
   break the link with the other while keeping its autocorrelation; the
   standard deviation of tau under this null gives the p-value. Naive Kendall
   p-values are kept in the CSV (`p_naive`) for comparison, since consecutive
   days/weeks are not independent.
5. **FDR correction** (Benjamini-Hochberg) per timescale.

```sh
python analysis_fine.py
python analysis_fine.py --cache garmin_daily_cache.json --out results --alpha 0.05
```

Outputs: `correlations_fine.csv`, `correlations_weekly.png`,
`correlations_daily.png`, `seasonality.png`, `raw_vs_adjusted.png`, plus a
report on stdout (seasonality, trends with bootstrap CIs, significant pairs,
effect of the seasonal correction, active vs inactive weeks).

### Caveats

Correlation is not causation: a highly active month may also be a month in
good shape, or a summer month (seasonal confounding — handled in
`analysis_fine.py`). Weekly/monthly series are autocorrelated, so naive
p-values are optimistic; few points (n≈12–60 months) also means low
statistical power. Prefer effects that are consistent across several
indicators.

An indicator that is absent from the data (never fetched, or not available
for that period) is skipped and reported as *Données indisponibles* — it never
aborts the analysis.

Both scripts need the extra dependencies `scipy` and `matplotlib` (already in
`pyproject.toml`).

## Continuous integration

A GitHub Actions workflow (`.github/workflows/ci.yml`) installs the
dependencies with `uv`, checks that the scripts compile and import, runs the
unit tests, and runs both analysis scripts on synthetic data generated by
`tests/make_fixtures.py`. The same scripts are also run on a *partial* cache
(no steps, no sleep, no race predictions) to make sure a missing indicator
never breaks them. No personal Garmin data is needed to run the CI.

The same smoke test can be run locally:

```sh
uv run python -m unittest discover -s tests
uv run python tests/make_fixtures.py --out fixtures
uv run python analysis.py --csv fixtures/history.csv --out out
uv run python analysis_fine.py --cache fixtures/garmin_daily_cache.json --out out

# cache sans les indicateurs optionnels
uv run python tests/make_fixtures.py --out fixtures-partial --partial
uv run python analysis.py --csv fixtures-partial/history.csv --out out-partial
uv run python analysis_fine.py --cache fixtures-partial/garmin_daily_cache.json --out out-partial
```

## License

[MIT](LICENSE) — see the `LICENSE` file.
