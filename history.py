#!/usr/bin/env python3
"""
Télécharge l'historique Garmin Connect et produit un tableau mensuel
(history.csv + activity.xlsx) avec les colonnes utilisées par votre analyse :
month, days, weight, rhr, hr_max, bb_max, VO2max, max_VO2max, stress,
calories, moderate, intense, total (= moderate + 2 * intense).

Installation :
    pip install garminconnect pandas openpyxl python-dotenv tqdm
  ou avec uv :
    uv run --with garminconnect --with pandas --with openpyxl --with python-dotenv --with tqdm history.py

Identifiants (jamais écrits dans le code) :
  1. Fichier .env à côté du script (voir .env.example) :
         GARMIN_EMAIL=votre@email
         GARMIN_PASSWORD=votre_mot_de_passe
  2. ou variables d'environnement (export GARMIN_EMAIL=...)
  3. sinon le script les demande au clavier (mot de passe masqué).
  Les variables déjà définies dans l'environnement priment sur le .env.

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
from tqdm.auto import tqdm

try:
    from dotenv import load_dotenv
    # .env à côté du script, quel que soit le dossier d'où on lance la commande
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    print("(python-dotenv non installé : le fichier .env est ignoré)")

TOKEN_DIR = Path.home() / ".garmin_tokens"   # jetons de session (pas le mot de passe)
CACHE_FILE = Path("garmin_daily_cache.json")  # données journalières déjà téléchargées

# ------------------------------------------------- synthèse de la semaine
COLS_DAILY = ["rhr", "hr_max", "stress", "bb_max", "calories",
              "moderate", "intense", "weight", "VO2max"]
MOIS = ["janv", "févr", "mars", "avr", "mai", "juin",
        "juil", "août", "sept", "oct", "nov", "déc"]
# sens "favorable" (+1 : plus haut = mieux, -1 : plus bas = mieux) ; l'activité
# et la FC max n'ont pas de sens favorable défini (plus n'est pas toujours mieux)
SENS = {"rhr": -1, "stress": -1, "bb_max": +1, "weight": -1, "VO2max": +1}
MIN_CAL = 50   # calories actives min. pour considérer un jour « actif »
# indicateur -> (libellé affiché, nombre de décimales, agrégation)
#   "somme" : cumulé sur la semaine (activité, comme dans history.csv)
#   "moyenne" : niveau moyen sur la semaine (santé)
SPEC = {
    "moderate": ("Modérée (min)", 0, "somme"),
    "intense": ("Intense (min)", 0, "somme"),
    "total": ("Total pondéré (min)", 0, "somme"),
    "calories": ("Calories actives (kcal)", 0, "somme"),
    "rhr": ("FC repos (bpm)", 0, "moyenne"),
    "hr_max": ("FC max (bpm)", 0, "moyenne"),
    "stress": ("Stress (pts)", 0, "moyenne"),
    "bb_max": ("Body Battery max (pts)", 0, "moyenne"),
    "VO2max": ("VO2max (ml/kg/min)", 1, "moyenne"),
    "weight": ("Poids (kg)", 1, "moyenne"),
}
ORDRE = ["moderate", "intense", "total", "calories",
         "rhr", "hr_max", "stress", "bb_max", "VO2max", "weight"]


def login() -> Garmin:
    TOKEN_DIR.mkdir(exist_ok=True, mode=0o700)

    # 1) essai avec les jetons enregistrés (aucun appel de connexion => pas de 429)
    try:
        api = Garmin()
        api.login(str(TOKEN_DIR))
        return api
    except Exception:
        pass

    # 2) connexion avec identifiants
    email = os.environ.get("GARMIN_EMAIL") or input("Email Garmin : ")
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Mot de passe Garmin : ")
    api = Garmin(email, password, prompt_mfa=lambda: input("Code MFA : "))
    try:
        try:
            api.login(str(TOKEN_DIR))   # versions récentes : sauvegarde les jetons ici
        except TypeError:
            api.login()                  # anciennes versions
    except Exception as e:
        if "429" in str(e) or "TooMany" in type(e).__name__:
            raise SystemExit(
                "Garmin bloque temporairement la connexion (erreur 429).\n"
                "Ne relancez pas en boucle : attendez 1 à 2 heures (parfois 24 h), "
                "ou essayez depuis un autre réseau (partage de connexion du téléphone)."
            )
        raise
    # anciennes versions uniquement (garth) : sauvegarde des jetons
    if hasattr(api, "garth"):
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
    total = max(0, (end - start).days)   # la borne 'end' est exclusive
    n = 0
    with tqdm(total=total, unit="j", desc="Téléchargement",
              disable=None, dynamic_ncols=True) as bar:
        bar.set_postfix_str("0 téléchargés")
        d = start
        while d < end:
            key = d.isoformat()
            # on retélécharge toujours les 3 derniers jours (données incomplètes)
            if key not in cache or d >= end - timedelta(days=3):
                cache[key] = fetch_day(api, key)
                n += 1
                bar.set_postfix_str(f"{n} téléchargés")
                if n % 25 == 0:
                    CACHE_FILE.write_text(json.dumps(cache))
                time.sleep(0.4)  # ménage le serveur Garmin
            d += timedelta(days=1)
            bar.update(1)
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
        "bb_max": g["bb_max"].mean(),
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


# ------------------------------------------------------ synthèse hebdomadaire
def _daily_frame(cache: dict) -> pd.DataFrame:
    """Cache journalier -> DataFrame indexé par la date.

    Les colonnes absentes du cache deviennent NaN (une journée sans balance
    n'a pas de poids, etc.). `total` = moderate + 2 * intense, comme dans
    `monthly()`.
    """
    if not cache:
        return pd.DataFrame(columns=COLS_DAILY + ["total"],
                            index=pd.DatetimeIndex([]))
    df = pd.DataFrame.from_dict(cache, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().apply(pd.to_numeric, errors="coerce")
    df = df.reindex(columns=COLS_DAILY)
    df["total"] = df["moderate"] + 2 * df["intense"]
    return df


def _date(ts) -> str:
    return f"{ts.day} {MOIS[ts.month - 1]} {ts.year}"


def _date_court(ts) -> str:
    return f"{ts.day} {MOIS[ts.month - 1]}"


def _num(v, dec: int = 0) -> str:
    if v is None or pd.isna(v):
        return "-"
    return f"{v:.{dec}f}"


def _pl(n: int, singulier: str, pluriel: str | None = None) -> str:
    """Accorde un nom au nombre (1 semaine / 3 semaines)."""
    if n == 1:
        return singulier
    return pluriel or singulier + "s"


def _agg(s: pd.Series, mode: str):
    """Agrège une série selon le mode : somme cumulée ou moyenne."""
    return s.sum(min_count=1) if mode == "somme" else s.mean()


def _mesures(n: int) -> str:
    """Accorde « mesure » au nombre (0 mesure / 3 mesures)."""
    return f"{n} mesure" if n <= 1 else f"{n} mesures"


def synthese_semaine(cache: dict, jours: int = 7, semaines_ref: int = 12) -> str:
    """Synthèse des `jours` derniers jours, comparés aux `semaines_ref` semaines
    précédentes. Retourne un texte (affiché par `main`, testable sans Garmin)."""
    df = _daily_frame(cache)
    if df.empty:
        return "  (aucune donnée journalière dans le cache)"

    n = min(jours, len(df))
    week = df.iloc[-n:]                       # la fenêtre observée
    ref = df.iloc[-(n + semaines_ref * 7):-n]  # la période de référence
    n_blocs = len(ref) // 7
    blocs = [ref.iloc[i:i + 7] for i in range(0, n_blocs * 7, 7)]

    L = [f"=== SYNTHÈSE DE LA SEMAINE ({_date(week.index.min())} → "
         f"{_date(week.index.max())}) ==="]
    L.append(f"  Fenêtre : {n} {_pl(n, 'dernier jour', 'derniers jours')} "
             f"({_date_court(week.index.min())} → {_date_court(week.index.max())})")
    if n_blocs:
        ref_label = ("de la semaine précédente" if n_blocs == 1
                     else f"des {n_blocs} semaines précédentes")
        L.append(f"  Référence : moyenne hebdomadaire {ref_label} "
                 f"({_date_court(ref.index.min())} → {_date_court(ref.index.max())})")
    else:
        L.append("  Référence : pas d'historique suffisant pour comparer")
    L.append(f"  Couverture : {int((week['calories'] >= MIN_CAL).sum())} j actifs / {n}"
             f" | poids : {_mesures(int(week['weight'].notna().sum()))}"
             f" | VO2max : {_mesures(int(week['VO2max'].notna().sum()))}")

    L.append("")
    L.append("  ACTIVITÉ : cumul de la semaine | SANTÉ : moyenne de la semaine")
    L.append(f"  {'INDICATEUR':<26s}{'SEMAINE':>10s}{'RÉFÉRENCE':>10s}"
             f"{'ÉCART':>9s}   SENS")
    fav = unfav = n_stable = hors = 0
    non_comparables = []
    wvs, refs = {}, {}
    for c in ORDRE:
        lab, dec, mode = SPEC[c]
        wv = _agg(week[c], mode)
        nw = int(week[c].notna().sum())
        nr = int(ref[c].notna().sum())
        # référence : moyenne des blocs hebdomadaires (somme ou moyenne)
        bv = [_agg(b[c], mode) for b in blocs]
        bv = [v for v in bv if not pd.isna(v)]
        rv = sum(bv) / len(bv) if bv else float("nan")
        d = wv - rv
        d_r = round(d, dec) if not pd.isna(d) else float("nan")
        if d_r == 0:
            d_r = 0.0   # normalise -0.0 (sinon on afficherait « +-0 »)
        # comparable : assez de mesures des deux côtés
        comparable = bool(nw >= 2 and nr >= 5 and not pd.isna(d))
        # hors de la plage des semaines de référence ?
        # (3 blocs minimum : en dessous, la plage est trop poorre pour conclure)
        hors_plage = bool(comparable and n_blocs >= 3 and bv
                          and (wv < min(bv) or wv > max(bv)))
        sens = "—"
        if comparable and c in SENS:
            # le sens est celui de l'écart arrondi : un écart nul est « stable »
            sens = ("favorable" if d_r * SENS[c] > 0
                    else "défavorable" if d_r * SENS[c] < 0 else "stable")
            fav += 1 if sens == "favorable" else 0
            unfav += 1 if sens == "défavorable" else 0
            n_stable += 1 if sens == "stable" else 0
        if hors_plage:
            sens += " (hors plage)"
            hors += 1
        if c in SENS and not comparable:
            non_comparables.append(f"{lab.split(' (')[0]} ({_mesures(nw)})")
        wvs[c], refs[c] = wv, rv
        L.append(f"  {lab:<26s}{_num(wv, dec):>10s}{_num(rv, dec):>10s}"
                 f"{('+' if d_r >= 0 else '') + _num(d_r, dec):>9s}   {sens}")

    if non_comparables and n_blocs:
        L.append("")
        L.append(f"  Non comparable(s) : {', '.join(non_comparables)}.")

    # --- verdict
    L.append("")
    L.append("  VERDICT")
    n_comp = fav + unfav + n_stable
    if n_comp:
        L.append(f"    Santé : {fav} en amélioration, {unfav} en dégradation, "
                 f"{n_stable} {_pl(n_stable, 'stable', 'stables')} "
                 f"(sur {n_comp} comparables).")
    else:
        L.append("    Santé : aucun indicateur comparable sur la période.")
    if n_blocs:
        dt = wvs["total"] - refs["total"]
        seuil = n          # 1 min/j d'écart = seuil de « stable »
        tend = "en hausse" if dt > seuil else "en baisse" if dt < -seuil else "stable"
        L.append(f"    Activité : charge pondérée {tend} "
                 f"({dt:+.0f} min/sem vs référence).")
        if hors:
            L.append(f"    {hors} écart(s) hors de la plage des {n_blocs} dernières "
                     f"{_pl(n_blocs, 'semaine')}.")
    L.append("    Semaine isolée : à lire comme un instantané, pas comme une tendance.")
    return "\n".join(L)


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
    print()
    print(synthese_semaine(cache))
    print("\nFichiers créés : history.csv, activity.xlsx")


if __name__ == "__main__":
    main()