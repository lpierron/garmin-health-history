# Garmin History

Download your [Garmin Connect](https://connect.garmin.com/) activity history and
produce a monthly summary as CSV and Excel.

`history.py` fetches daily stats for a date range, aggregates them per month, and
writes `history.csv` and `activity.xlsx`.

## Requirements

- Python >= 3.13
- Dependencies: `garminconnect`, `pandas`, `openpyxl`

## Installation

Using [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

Or with pip:

```sh
pip install garminconnect pandas openpyxl
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
