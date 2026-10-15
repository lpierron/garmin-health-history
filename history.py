#!/usr/bin/env python3
"""
Télécharge l'historique Garmin Connect et produit un tableau mensuel
(history.csv + activity.xlsx) avec les colonnes utilisées par votre analyse :
month, days, weight, rhr, hr_max, bb_max, VO2max, max_VO2max, stress,
calories, moderate, intense, total (= moderate + 2 * intense).

Installation :
    pip install garminconnect pandas openpyxl

Identifiants (jamais écrits dans le code ni sur disque) :
    export GARMIN_EMAIL="votre@email"
    export GARMIN_PASSWORD="votre_mot_de_passe"
  Sinon le script les demande au clavier (mot de passe masqué).

Usage :
    python garmin_history.py --start 2020-08-01
    python garmin_history.py --start 2024-01-01 --end 2026-10-15
"""
import argparse
import getpass
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from garminconnect import Garmin

TOKEN_DIR = Path.home() / ".garmin_tokens"   # jetons de session (pas le mot de passe)
CACHE_FILE = Path("garmin_daily_cache.json")  # données journalières déjà téléchargées


def login() -> Garmin:
    email = os.environ.get("GARMIN_EMAIL") or input("Email Garmin : ")
    # 1) essai avec les jetons enregistrés
    try:
        api = Garmin()
        api.login(str(TOKEN_DIR))
        return api
    except Exception:
        pass
    # 2) connexion classique
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Mot de passe Garmin : ")
    api = Garmin(email, password, prompt_mfa=lambda: input("Code MFA : "))
    api.login()
    TOKEN_DIR.mkdir(exist_ok=True, mode=0o700)
    api.garth.dump(str(TOKEN_DIR))
    return api


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def fetch_day(api: Garmin, d: str) -> dict:
    s = safe(api.get_stats, d) or {}
    row = {
        "rhr": s.get("restingHeartRate"),
        "hr_max": s.get("maxHeartRate"),
        "stress": s.get("averageStressLevel"),
        "bb_max": s.get("bodyBatteryHighestValue"),
        "calories": s.get("activeKilocalories"),
        "moderate": s.get("moderateIntensityMinutes"),
        "intense": s.get("vigorousIntensityMinutes"),
    }
    mm = safe(api.get_max_metrics, d)
    if mm:
        try:
            row["VO2max"] = mm[0]["generic"]["vo2MaxPreciseValue"]
        except (KeyError, IndexError, TypeError):
            row["VO2max"] = None
    bc = safe(api.get_body_composition, d, d)
    if bc and bc.get("totalAverage", {}).get("weight"):
        row["weight"] = bc["totalAverage"]["weight"] / 1000  # g -> kg
    return row


def download(api: Garmin, start: date, end: date) -> dict:
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    d, n = start, 0
    while d <= end:
        key = d.isoformat()
        # on retélécharge toujours les 3 derniers jours (données incomplètes)
        if key not in cache or d >= end - timedelta(days=3):
            cache[key] = fetch_day(api, key)
            n += 1
            if n % 25 == 0:
                CACHE_FILE.write_text(json.dumps(cache))
                print(f"  {key} ...")
            time.sleep(0.4)  # ménage le serveur Garmin
        d += timedelta(days=1)
    CACHE_FILE.write_text(json.dumps(cache))
    return cache


def monthly(cache: dict) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(cache, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.apply(pd.to_numeric, errors="coerce")
    g = df.groupby(df.index.to_period("M"))
    out = pd.DataFrame({
        "days": g.size(),
        "weight": g["weight"].mean(),
        "rhr": g["rhr"].mean(),
        "hr_max": g["hr_max"].max(),
        "bb_max": g["bb_max"].max(),
        "VO2max": g["VO2max"].mean(),
        "max_VO2max": g["VO2max"].max(),
        "stress": g["stress"].mean(),
        "calories": g["calories"].sum(),
        "moderate": g["moderate"].sum(),
        "intense": g["intense"].sum(),
    })
    out["total"] = out["moderate"] + 2 * out["intense"]
    out.index = out.index.astype(str)
    out.index.name = "month"
    return out.round(1).reset_index()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2020-08-01")
    p.add_argument("--end", default=date.today().isoformat())
    a = p.parse_args()
    start = datetime.strptime(a.start, "%Y-%m-%d").date()
    end = datetime.strptime(a.end, "%Y-%m-%d").date()

    api = login()
    print("Connecté. Téléchargement (long la première fois, ensuite en cache)...")
    cache = download(api, start, end)
    table = monthly(cache)
    table.to_csv("history.csv", index=False)
    table.to_excel("activity.xlsx", index=False)
    print(table.tail(6).to_string(index=False))
    print("\nFichiers créés : history.csv, activity.xlsx")


if __name__ == "__main__":
    main()
