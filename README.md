# Garmin History

Download your [Garmin Connect](https://connect.garmin.com/) activity history and
produce a monthly summary as CSV and Excel.

`history.py` fetches daily stats for a date range, aggregates them per month, and
writes `history.csv` and `activity.xlsx`. `analysis.py` and `analysis_fine.py`
then explore the relationship between activity and health indicators (see
[Analysis](#analysis)).

## Requirements

- Python >= 3.13
- Dependencies: `garminconnect`, `pandas`, `openpyxl`, plus `scipy` and
  `matplotlib` for the analysis scripts

## Installation

Using [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

Or with pip:

```sh
pip install garminconnect pandas openpyxl scipy matplotlib
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
```

| Option    | Default      | Description             |
| --------- | ------------ | ----------------------- |
| `--start` | `2020-08-01` | Start date (YYYY-MM-DD) |
| `--end`   | today        | End date (YYYY-MM-DD)   |

## Output

Two files are written in the current directory:

- `history.csv`
- `activity.xlsx`

Both contain one row per month with these columns:

`month`, `days`, `weight`, `rhr`, `hr_max`, `bb_max`, `VO2max`, `max_VO2max`,
`stress`, `calories`, `moderate`, `intense`, `total`

where `total = moderate + 2 * intense`.

## How it works

- Daily data is cached in `garmin_daily_cache.json`; already-fetched days are not
  downloaded again. The last 3 days are always refreshed since their data may be
  incomplete.
- A short delay between requests is applied to be gentle on Garmin's servers.

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
  weighted total, active calories) and health, same month and with a one-month
  lag (does activity in month M relate to health in M+1?). Activity columns are
  divided by `days` so months of different lengths are comparable.
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

Both scripts need the extra dependencies `scipy` and `matplotlib` (already in
`pyproject.toml`).
