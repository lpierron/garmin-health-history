#!/usr/bin/env python3
"""
Données synthétiques pour la CI.

`analysis.py` et `analysis_fine.py` sont écrits pour l'historique Garmin
réel, que la CI n'a pas. Ce script fabrique un jeu de données plausible et
reproductible, avec exactement la structure que produit `history.py` :

  - `history.csv`            : une ligne par mois (sortie de `history.monthly()`)
  - `garmin_daily_cache.json` : une entrée par jour (structure de `fetch_day()`)

Aucune donnée personnelle n'est utilisée : tout est tiré d'un générateur
aléatoire à graine fixe.

Usage :
    python tests/make_fixtures.py --out fixtures
"""
import argparse
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from history import monthly  # noqa: E402


def _off_days(dates: list[date], rng: random.Random) -> set[date]:
    """Jours « montre non portée » : au plus 2 par semaine, pour que chaque
    semaine complète garde assez de jours valides (min 4 pour l'échelle
    hebdomadaire)."""
    off: set[date] = set()
    for i in range(0, len(dates), 7):
        week = dates[i:i + 7]
        for d in rng.sample(week, rng.choice([0, 0, 1, 1, 2])):
            off.add(d)
    return off


def daily_cache(start: date, days: int, seed: int = 0) -> dict:
    """Un cache journalier synthétique, au format de `history.fetch_day()`."""
    rng = random.Random(seed)
    dates = [start + timedelta(days=i) for i in range(days)]
    off = _off_days(dates, rng)
    cache: dict[str, dict] = {}

    for i, d in enumerate(dates):
        doy = d.timetuple().tm_yday
        season = math.sin(2 * math.pi * (doy - 15) / 365.25)  # pic mi-juin
        year = i / 365.25
        weekend = 1.0 if d.weekday() >= 5 else 0.0

        # activité : plus le week-end, un peu plus en été
        moderate = 22 + 8 * season + 10 * weekend + rng.gauss(0, 6)
        intense = max(0.0, 0.55 * moderate + rng.gauss(0, 5))
        calories = 9 * moderate + 11 * intense + rng.gauss(0, 60)

        # santé : le fitness s'améliore sur la période (FC repos qui baisse,
        # VO2max qui monte), avec une saisonnalité et du bruit
        rhr = 60 - 1.2 * year + 1.2 * season + rng.gauss(0, 2.5)
        hr_max = 190 - 1.5 * year + rng.gauss(0, 3)
        bb_max = 55 + 0.35 * (moderate - 26) + rng.gauss(0, 7)
        vo2max = 47 + 1.0 * year + rng.gauss(0, 0.6)
        stress = 32 - 0.25 * (moderate - 26) + rng.gauss(0, 6)
        weight = 78 - 0.3 * year + 0.6 * season + rng.gauss(0, 0.4)

        if d in off:
            # montre portée mais quasi inactive : calories < --min-cal
            moderate = max(0.0, rng.gauss(3, 2))
            intense = 0.0
            calories = max(4.0, rng.gauss(18, 6))

        row = {
            "rhr": round(rhr, 0),
            "hr_max": round(hr_max, 0),
            "stress": round(stress, 0),
            "bb_max": round(bb_max, 0),
            "calories": round(calories, 0),
            "moderate": round(moderate, 0),
            "intense": round(intense, 0),
            "VO2max": round(vo2max, 1),
            # la balance n'est pas utilisée tous les jours
            "weight": round(weight, 3) if (d.weekday() in (0, 2, 4) or rng.random() < 0.1) else None,
        }
        cache[d.isoformat()] = row
    return cache


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=Path("fixtures"), help="dossier de sortie")
    p.add_argument("--years", type=int, default=6, help="durée couverte (années)")
    p.add_argument("--seed", type=int, default=0, help="graine du générateur")
    a = p.parse_args()

    # on commence un lundi et on finit un dimanche : toutes les semaines sont
    # complètes, ce qui maximise le nombre de points à l'échelle hebdomadaire
    start = date(2020, 1, 6)
    start += timedelta(days=(-start.weekday()) % 7)
    days = int(a.years * 365.25)
    days += (-days) % 7

    cache = daily_cache(start, days, a.seed)
    a.out.mkdir(parents=True, exist_ok=True)

    (a.out / "garmin_daily_cache.json").write_text(json.dumps(cache, indent=1))
    monthly(cache).to_csv(a.out / "history.csv", index=False)

    print(f"{a.out/'history.csv'} : {len(cache)} jours, "
          f"{len(monthly(cache))} mois")


if __name__ == "__main__":
    main()
