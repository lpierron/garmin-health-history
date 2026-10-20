#!/usr/bin/env python3
"""
Télécharge l'historique Garmin Connect et produit un tableau mensuel
(history.csv + activity.xlsx) avec les colonnes utilisées par votre analyse :
month, days, weight, rhr, hr_max, bb_max, VO2max, max_VO2max, stress,
calories, steps, moderate, intense, sleep_score, sleep_hours, pred_5k,
pred_half, total (= moderate + 2 * intense).

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
    python history.py --start 2020-08-01
    python history.py --start 2024-01-01 --end 2026-10-15
    python history.py --start 2020-08-01 --all   # + pas, sommeil, prédictions

Les indicateurs optionnels (pas, sommeil, prédictions de course) sont
récupérés par tranches de dates (1 appel par 28 jours, ou par 365 jours pour
les prédictions) et ne remplissent que les jours où la valeur manque.
"""

import argparse
import contextlib
import getpass
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd
from garminconnect import Garmin
from tqdm.auto import tqdm

from units import fmt_hm, fmt_hms

try:
    from dotenv import load_dotenv

    # .env à côté du script, quel que soit le dossier d'où on lance la commande
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    print("(python-dotenv non installé : le fichier .env est ignoré)")

TOKEN_DIR = Path.home() / ".garmin_tokens"  # jetons de session (pas le mot de passe)
CACHE_FILE = Path("garmin_daily_cache.json")  # données journalières déjà téléchargées
VERBOSE = False  # journalise les erreurs d'appel API (--verbose)

# l'API limite la taille des plages demandées : 28 jours pour les endpoints
# « stats » (pas, sommeil), 365 jours pour les prédictions de course
CHUNK_DAYS = 28
RACE_CHUNK_DAYS = 365

# ------------------------------------------------- synthèse de la semaine
COLS_DAILY = [
    "rhr",
    "hr_max",
    "stress",
    "bb_max",
    "calories",
    "moderate",
    "intense",
    "weight",
    "VO2max",
    "steps",
    "sleep_score",
    "sleep_hours",
    "pred_5k",
    "pred_half",
]
MOIS = ["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août", "sept", "oct", "nov", "déc"]
# sens "favorable" (+1 : plus haut = mieux, -1 : plus bas = mieux) ; l'activité
# et la FC max n'ont pas de sens favorable défini (plus n'est pas toujours mieux)
SENS = {
    "rhr": -1,
    "stress": -1,
    "bb_max": +1,
    "weight": -1,
    "VO2max": +1,
    "steps": +1,
    "sleep_score": +1,
    "sleep_hours": +1,
    "pred_5k": -1,
    "pred_half": -1,
}
MIN_CAL = 50  # calories actives min. pour considérer un jour « actif »
# indicateur -> (libellé affiché, nombre de décimales, agrégation)
#   "somme" : cumulé sur la semaine (activité, comme dans history.csv)
#   "moyenne" : niveau moyen sur la semaine (santé, pas)
SPEC = {
    "moderate": ("Modérée (min)", 0, "somme"),
    "intense": ("Intense (min)", 0, "somme"),
    "total": ("Total pondéré (min)", 0, "somme"),
    "calories": ("Calories actives (kcal)", 0, "somme"),
    "steps": ("Pas (par jour)", 0, "moyenne"),
    "rhr": ("FC repos (bpm)", 0, "moyenne"),
    "hr_max": ("FC max (bpm)", 0, "moyenne"),
    "stress": ("Stress (pts)", 0, "moyenne"),
    "bb_max": ("Body Battery max (pts)", 0, "moyenne"),
    "VO2max": ("VO2max (ml/kg/min)", 1, "moyenne"),
    "sleep_score": ("Score sommeil", 0, "moyenne"),
    "sleep_hours": ("Sommeil", 0, "moyenne"),
    "weight": ("Poids (kg)", 1, "moyenne"),
    "pred_5k": ("Prédiction 5K", 0, "moyenne"),
    "pred_half": ("Prédiction semi", 0, "moyenne"),
}
# indicateurs affichés sous forme de durée plutôt que de nombre
FORMATS = {"sleep_hours": "hm", "pred_5k": "hms", "pred_half": "hms"}
ORDRE = [
    "moderate",
    "intense",
    "total",
    "calories",
    "steps",
    "rhr",
    "hr_max",
    "stress",
    "bb_max",
    "VO2max",
    "sleep_score",
    "sleep_hours",
    "pred_5k",
    "pred_half",
    "weight",
]


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
            api.login(str(TOKEN_DIR))  # versions récentes : sauvegarde les jetons ici
        except TypeError:
            api.login()  # anciennes versions
    except Exception as e:
        if "429" in str(e) or "TooMany" in type(e).__name__:
            raise SystemExit(
                "Garmin bloque temporairement la connexion (erreur 429).\n"
                "Ne relancez pas en boucle : attendez 1 à 2 heures (parfois 24 h), "
                "ou essayez depuis un autre réseau (partage de connexion du téléphone)."
            ) from e
        raise
    # anciennes versions uniquement (garth) : sauvegarde des jetons
    garth = getattr(api, "garth", None)
    if garth is not None:
        garth.dump(str(TOKEN_DIR))
    return api


def safe(fn, *args):
    """Appelle fn(*args) ; renvoie None si l'appel échoue.

    Les erreurs sont silencieuses par défaut (le script ne doit pas s'arrêter
    sur un jour manquant) mais journalisées avec --verbose.
    """
    try:
        return fn(*args)
    except Exception as e:
        if VERBOSE:
            name = getattr(fn, "__name__", str(fn))
            print(f"  [api] {name} a échoué : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _put(row: dict, key: str, value) -> None:
    """Écrit une valeur seulement si elle est connue.

    Un appel API qui échoue (réseau, 429, champ absent) renvoie None : on ne
    doit alors pas écraser la valeur déjà présente dans le cache.
    """
    if value is not None:
        row[key] = value


def _rows(data) -> list:
    """Normalise une réponse d'API en liste de lignes.

    Selon l'endpoint, l'API renvoie soit une liste directe, soit un objet
    contenant la liste sous une clé (« userPredictionVos », « individualStats »).
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("userPredictionVos", "individualStats", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _chunks(start: date, end: date, days: int):
    """Tranches [a, b] inclusives couvrant [start, end) (borne 'end' exclusive)."""
    if end <= start:
        return
    last = end - timedelta(days=1)
    a = start
    while a <= last:
        b = min(a + timedelta(days=days - 1), last)
        yield a, b
        a = b + timedelta(days=1)


def _needed_chunks(start: date, end: date, days: int, missing: set[str]):
    """Tranches contenant au moins un jour manquant (évite les appels inutiles)."""
    for a, b in _chunks(start, end, days):
        d = a
        while d <= b:
            if d.isoformat() in missing:
                yield a, b
                break
            d += timedelta(days=1)


def _parse_steps_rows(rows) -> dict[str, int]:
    """Réponse « stats/pas » -> {date ISO: nombre de pas}."""
    out: dict[str, int] = {}
    for row in rows or []:
        d = row.get("calendarDate")
        n = row.get("totalSteps")
        if d and n is not None:
            out[d] = n
    return out


def _parse_sleep_rows(rows) -> dict[str, dict]:
    """Réponse « sleep/daily » -> {date ISO: {"sleep_score":…, "sleep_hours":…}}."""
    out: dict[str, dict] = {}
    for row in rows or []:
        d = row.get("calendarDate")
        if not d:
            continue
        v = row.get("values") or {}
        score = v.get("sleepScore")
        dur = v.get("totalSleepTimeInSeconds")
        out[d] = {
            "sleep_score": score,
            "sleep_hours": round(dur / 3600, 2) if dur else None,
        }
    return out


def _parse_race_rows(rows) -> dict[str, dict]:
    """Réponse « racepredictions » -> {date ISO: {"pred_5k":…, "pred_half":…}}.

    L'API renvoie les temps en secondes (time5K, time10K, timeHalfMarathon,
    timeMarathon) ; on ne garde que le 5K et le semi, convertis en minutes.
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        d = row.get("calendarDate")
        if not d:
            continue
        t5 = row.get("time5K")
        th = row.get("timeHalfMarathon")
        if t5 is None and th is None:
            continue
        out[d] = {
            "pred_5k": round(t5 / 60, 1) if t5 else None,
            "pred_half": round(th / 60, 1) if th else None,
        }
    return out


def _merge(cache: dict, data: dict, cols) -> int:
    """Écrit dans le cache les valeurs absentes uniquement.

    Renvoie le nombre de valeurs écrites. Les jours absents du cache sont
    ignorés, et une valeur déjà connue n'est jamais écrasée.
    """
    n = 0
    for d, vals in data.items():
        row = cache.get(d)
        if row is None:
            continue
        # une valeur peut être un scalaire (nombre de pas) ou un dict
        # (sommeil, prédictions de course)
        items = vals.items() if isinstance(vals, dict) else [(cols[0], vals)]
        for c, v in items:
            if c not in cols or v is None:
                continue
            if row.get(c) is None:
                row[c] = v
                n += 1
    return n


class DayApi(Protocol):
    """Partie de l'API Garmin utilisée pour récupérer une journée.

    Décrire un protocole (plutôt que la classe `Garmin`) permet de tester
    `fetch_day()` avec un faux objet, sans Garmin ni réseau.
    """

    def get_stats(self, cdate: str) -> dict: ...

    def get_max_metrics(self, cdate: str) -> dict: ...

    def get_body_composition(self, startdate: str, enddate: str | None = None) -> dict: ...


def fetch_day(api: DayApi, d: str, existing: dict | None = None) -> dict:
    """Récupère les indicateurs quotidiens d'une date.

    `existing` (entrée du cache) sert de base : une valeur déjà connue n'est
    jamais écrasée par None (un appel API qui échoue ne doit pas perdre une
    donnée).
    """
    s = safe(api.get_stats, d) or {}
    row = dict(existing) if existing else {}
    _put(row, "rhr", s.get("restingHeartRate"))
    _put(row, "hr_max", s.get("maxHeartRate"))
    _put(row, "stress", s.get("averageStressLevel"))
    _put(row, "bb_max", s.get("bodyBatteryHighestValue"))
    _put(row, "calories", s.get("activeKilocalories"))
    _put(row, "moderate", s.get("moderateIntensityMinutes"))
    _put(row, "intense", s.get("vigorousIntensityMinutes"))
    # les pas sont déjà dans la réponse « stats » : aucun appel supplémentaire
    _put(row, "steps", s.get("totalSteps"))
    mm = safe(api.get_max_metrics, d)
    if mm:
        # garder la valeur en cache si l'appel échoue
        with contextlib.suppress(KeyError, IndexError, TypeError):
            _put(row, "VO2max", mm[0]["generic"]["vo2MaxPreciseValue"])
    bc = safe(api.get_body_composition, d, d)
    if bc and bc.get("totalAverage", {}).get("weight"):
        row["weight"] = bc["totalAverage"]["weight"] / 1000  # g -> kg
    return row


def fetch_steps_range(api: Garmin, chunks) -> dict[str, int]:
    """Nombre de pas quotidiens (1 appel API par tranche de 28 jours)."""
    out: dict[str, int] = {}
    for a, b in chunks:
        data = safe(api.get_daily_steps, a.isoformat(), b.isoformat())
        out.update(_parse_steps_rows(_rows(data)))
    return out


def fetch_sleep_range(api: Garmin, chunks) -> dict[str, dict]:
    """Score et durée du sommeil (1 appel API par tranche de 28 jours)."""
    out: dict[str, dict] = {}
    for a, b in chunks:
        data = safe(api.get_sleep_daily, a.isoformat(), b.isoformat())
        out.update(_parse_sleep_rows(_rows(data)))
    return out


def fetch_race_predictions(api: Garmin, chunks) -> dict[str, dict]:
    """Prédictions de course 5K / semi (1 appel API par tranche de 365 jours)."""
    out: dict[str, dict] = {}
    for a, b in chunks:
        data = safe(api.get_race_predictions, a.isoformat(), b.isoformat(), "daily")
        out.update(_parse_race_rows(_rows(data)))
    return out


def _need_refresh(entry: dict | None) -> bool:
    """Une journée absente du cache doit être téléchargée.

    Les indicateurs optionnels (pas, sommeil, prédictions) ne passent pas par
    là : ils sont récupérés par tranches, hors de la boucle journalière.
    """
    return entry is None


def download(
    api: Garmin,
    start: date,
    end: date,
    with_steps: bool = False,
    with_sleep: bool = False,
    with_races: bool = False,
) -> dict:
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    total = max(0, (end - start).days)  # la borne 'end' est exclusive
    n = 0
    with tqdm(
        total=total, unit="j", desc="Téléchargement", disable=None, dynamic_ncols=True
    ) as bar:
        bar.set_postfix_str("0 téléchargés")
        d = start
        while d < end:
            key = d.isoformat()
            # on retélécharge toujours les 3 derniers jours (données incomplètes)
            # et les journées absentes du cache
            if _need_refresh(cache.get(key)) or d >= end - timedelta(days=3):
                cache[key] = fetch_day(api, key, existing=cache.get(key))
                n += 1
                bar.set_postfix_str(f"{n} téléchargés")
                if n % 25 == 0:
                    CACHE_FILE.write_text(json.dumps(cache))
                time.sleep(0.4)  # ménage le serveur Garmin
            d += timedelta(days=1)
            bar.update(1)

        # Indicateurs optionnels : récupérés par tranches de dates (1 appel par
        # 28 jours pour les pas et le sommeil, 1 par 365 jours pour les
        # prédictions de course) et fusionnés dans le cache. Seules les
        # tranches incomplètes sont interrogées, et une valeur déjà connue
        # n'est jamais écrasée.
        for flag, label, days, fetcher, cols, is_missing in (
            (
                with_steps,
                "pas",
                CHUNK_DAYS,
                fetch_steps_range,
                ("steps",),
                lambda e: e.get("steps") is None,
            ),
            (
                with_sleep,
                "sommeil",
                CHUNK_DAYS,
                fetch_sleep_range,
                ("sleep_score", "sleep_hours"),
                lambda e: e.get("sleep_score") is None or e.get("sleep_hours") is None,
            ),
            (
                with_races,
                "prédictions de course",
                RACE_CHUNK_DAYS,
                fetch_race_predictions,
                ("pred_5k", "pred_half"),
                lambda e: e.get("pred_5k") is None and e.get("pred_half") is None,
            ),
        ):
            if not flag:
                continue
            missing = {
                d.isoformat()
                for d in _daterange(start, end)
                if is_missing(cache.get(d.isoformat(), {}))
            }
            if not missing:
                continue
            bar.set_postfix_str(label)
            data = fetcher(api, _needed_chunks(start, end, days, missing))
            written = _merge(cache, data, cols)
            bar.set_postfix_str(f"{label} : {written} jour(s)")
            CACHE_FILE.write_text(json.dumps(cache))

    CACHE_FILE.write_text(json.dumps(cache))
    return cache


def _daterange(start: date, end: date):
    """Génère chaque date de start (inclu) à end (exclu)."""
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)


def monthly(cache: dict) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(cache, orient="index")
    df.index = pd.to_datetime(df.index)
    # colonnes fixes : un indicateur absent du cache devient NaN (jamais une erreur)
    df = df.reindex(columns=COLS_DAILY).apply(pd.to_numeric, errors="coerce")
    g = df.groupby(df.index.to_period("M"))
    out = pd.DataFrame(
        {
            "days": g.size(),
            "weight": g["weight"].mean(),
            "rhr": g["rhr"].mean(),
            "hr_max": g["hr_max"].max(),
            "bb_max": g["bb_max"].mean(),
            "VO2max": g["VO2max"].mean(),
            "max_VO2max": g["VO2max"].max(),
            "stress": g["stress"].mean(),
            # min_count=1 : un mois sans aucune mesure vaut NaN (et non 0),
            # pour ne pas confondre « aucun relevé » et « valeur nulle »
            "calories": g["calories"].sum(min_count=1),
            # les pas sont ramenés au jour : la moyenne du mois (pas/j)
            "steps": g["steps"].mean(),
            "moderate": g["moderate"].sum(min_count=1),
            "intense": g["intense"].sum(min_count=1),
            "sleep_score": g["sleep_score"].mean(),
            "sleep_hours": g["sleep_hours"].mean(),
            "pred_5k": g["pred_5k"].mean(),
            "pred_half": g["pred_half"].mean(),
        }
    )
    out["total"] = out["moderate"] + 2 * out["intense"]
    out.index = out.index.astype(str)
    out.index.name = "month"
    out = out.reset_index()
    # arrondi à 1 décimale, sauf les durées (sommeil, prédictions) qui
    # gardent leur précision pour l'affichage « hh:mm » / « hh:mm:ss », et
    # les pas, déjà exprimés par jour
    dur = ["sleep_hours", "pred_5k", "pred_half"]
    num = [c for c in out.columns if c not in dur and c != "steps"]
    out[num] = out[num].round(1)
    return out


def display_table(table: pd.DataFrame) -> pd.DataFrame:
    """Version « lisible » du tableau mensuel (ce qui est écrit sur disque).

    - les pas sont des pas/j (nombre entier) ;
    - le sommeil est formaté « hh:mm » ;
    - les prédictions de course sont formatées « hh:mm:ss ».
    """
    out = table.copy()
    if "steps" in out.columns:
        out["steps"] = out["steps"].round(0).astype("Int64")
    if "sleep_hours" in out.columns:
        out["sleep_hours"] = out["sleep_hours"].map(fmt_hm)
    for c in ("pred_5k", "pred_half"):
        if c in out.columns:
            out[c] = out[c].map(fmt_hms)
    return out


# ------------------------------------------------------ synthèse hebdomadaire
def _daily_frame(cache: dict) -> pd.DataFrame:
    """Cache journalier -> DataFrame indexé par la date.

    Les colonnes absentes du cache deviennent NaN (une journée sans balance
    n'a pas de poids, etc.). `total` = moderate + 2 * intense, comme dans
    `monthly()`.
    """
    if not cache:
        return pd.DataFrame(columns=[*COLS_DAILY, "total"], index=pd.DatetimeIndex([]))
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


def _fmt(v, dec: int, fmt: str | None = None) -> str:
    """Formate une valeur selon l'unité de l'indicateur.

    fmt = "hm"  -> durée « hh:mm » (sommeil)
    fmt = "hms" -> durée « hh:mm:ss » (prédictions de course)
    sinon        -> nombre à `dec` décimales
    """
    if fmt == "hm":
        return fmt_hm(v) or "-"
    if fmt == "hms":
        return fmt_hms(v) or "-"
    return _num(v, dec)


def _fmt_ecart(v, dec: int, fmt: str | None = None) -> str:
    """Écart signé entre la semaine et la référence."""
    if pd.isna(v):
        return "-"
    if fmt == "hm":
        return fmt_hm(v, signed=True)
    if fmt == "hms":
        return fmt_hms(v, signed=True)
    return ("+" if v >= 0 else "") + _num(v, dec)


def _ecart_affiche(d, dec: int, fmt: str | None = None) -> float:
    """Écart ramené à l'unité d'affichage (pour judge du sens)."""
    if pd.isna(d):
        return float("nan")
    if fmt in ("hm", "hms"):
        return round(d * 60)
    return round(d, dec)


def synthese_semaine(cache: dict, jours: int = 7, semaines_ref: int = 12) -> str:
    """Synthèse des `jours` derniers jours, comparés aux `semaines_ref` semaines
    précédentes. Retourne un texte (affiché par `main`, testable sans Garmin)."""
    df = _daily_frame(cache)
    if df.empty:
        return "  (aucune donnée journalière dans le cache)"

    n = min(jours, len(df))
    week = df.iloc[-n:]  # la fenêtre observée
    ref = df.iloc[-(n + semaines_ref * 7) : -n]  # la période de référence
    n_blocs = len(ref) // 7
    blocs = [ref.iloc[i : i + 7] for i in range(0, n_blocs * 7, 7)]

    L = [f"=== SYNTHÈSE DE LA SEMAINE ({_date(week.index.min())} → {_date(week.index.max())}) ==="]
    L.append(
        f"  Fenêtre : {n} {_pl(n, 'dernier jour', 'derniers jours')} "
        f"({_date_court(week.index.min())} → {_date_court(week.index.max())})"
    )
    if n_blocs:
        ref_label = (
            "de la semaine précédente" if n_blocs == 1 else f"des {n_blocs} semaines précédentes"
        )
        L.append(
            f"  Référence : moyenne hebdomadaire {ref_label} "
            f"({_date_court(ref.index.min())} → {_date_court(ref.index.max())})"
        )
    else:
        L.append("  Référence : pas d'historique suffisant pour comparer")
    L.append(
        f"  Couverture : {int((week['calories'] >= MIN_CAL).sum())} j actifs / {n}"
        f" | poids : {_mesures(int(week['weight'].notna().sum()))}"
        f" | VO2max : {_mesures(int(week['VO2max'].notna().sum()))}"
        f" | pas : {_mesures(int(week['steps'].notna().sum()))}"
        f" | sommeil : {_mesures(int(week['sleep_score'].notna().sum()))}"
        f" | 5K : {_mesures(int(week['pred_5k'].notna().sum()))}"
        f" | semi : {_mesures(int(week['pred_half'].notna().sum()))}"
    )

    L.append("")
    L.append(
        "  ACTIVITÉ : cumul de la semaine (pas : moyenne/jour) | SANTÉ : moyenne de la semaine"
    )
    L.append(f"  {'INDICATEUR':<26s}{'SEMAINE':>11s}{'RÉFÉRENCE':>11s}{'ÉCART':>11s}   SENS")
    fav = unfav = n_stable = hors = 0
    non_comparables = []
    wvs, refs = {}, {}
    for c in ORDRE:
        lab, dec, mode = SPEC[c]
        fmt = FORMATS.get(c)
        wv = _agg(week[c], mode)
        nw = int(week[c].notna().sum())
        nr = int(ref[c].notna().sum())
        # référence : moyenne des blocs hebdomadaires (somme ou moyenne)
        bv = [_agg(b[c], mode) for b in blocs]
        bv = [v for v in bv if not pd.isna(v)]
        rv = sum(bv) / len(bv) if bv else float("nan")
        d = wv - rv
        d_r = _ecart_affiche(d, dec, fmt)
        if d_r == 0:
            d_r = 0.0  # normalise -0.0 (sinon on afficherait « +-0 »)
        # comparable : assez de mesures des deux côtés
        comparable = bool(nw >= 2 and nr >= 5 and not pd.isna(d))
        # hors de la plage des semaines de référence ?
        # (3 blocs minimum : en dessous, la plage est trop poorre pour conclure)
        hors_plage = bool(comparable and n_blocs >= 3 and bv and (wv < min(bv) or wv > max(bv)))
        sens = "—"
        if comparable and c in SENS:
            # le sens est celui de l'écart arrondi : un écart nul est « stable »
            sens = (
                "favorable"
                if d_r * SENS[c] > 0
                else "défavorable"
                if d_r * SENS[c] < 0
                else "stable"
            )
            fav += 1 if sens == "favorable" else 0
            unfav += 1 if sens == "défavorable" else 0
            n_stable += 1 if sens == "stable" else 0
        if hors_plage:
            sens += " (hors plage)"
            hors += 1
        if c in SENS and not comparable:
            non_comparables.append(f"{lab.split(' (')[0]} ({_mesures(nw)})")
        wvs[c], refs[c] = wv, rv
        L.append(
            f"  {lab:<26s}{_fmt(wv, dec, fmt):>11s}{_fmt(rv, dec, fmt):>11s}"
            f"{_fmt_ecart(d, dec, fmt):>11s}   {sens}"
        )

    if non_comparables and n_blocs:
        L.append("")
        L.append(f"  Non comparable(s) : {', '.join(non_comparables)}.")

    # --- verdict
    L.append("")
    L.append("  VERDICT")
    n_comp = fav + unfav + n_stable
    if n_comp:
        L.append(
            f"    Santé : {fav} en amélioration, {unfav} en dégradation, "
            f"{n_stable} {_pl(n_stable, 'stable', 'stables')} "
            f"(sur {n_comp} comparables)."
        )
    else:
        L.append("    Santé : aucun indicateur comparable sur la période.")
    if n_blocs:
        dt = wvs["total"] - refs["total"]
        seuil = n  # 1 min/j d'écart = seuil de « stable »
        tend = "en hausse" if dt > seuil else "en baisse" if dt < -seuil else "stable"
        L.append(f"    Activité : charge pondérée {tend} ({dt:+.0f} min/sem vs référence).")
        if hors:
            L.append(
                f"    {hors} écart(s) hors de la plage des {n_blocs} dernières "
                f"{_pl(n_blocs, 'semaine')}."
            )
    L.append("    Semaine isolée : à lire comme un instantané, pas comme une tendance.")
    return "\n".join(L)


def main():
    global VERBOSE
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2020-08-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument(
        "--steps",
        action="store_true",
        help="récupère aussi le nombre de pas quotidiens (1 appel API par tranche de 28 jours)",
    )
    p.add_argument(
        "--sleep",
        action="store_true",
        help="récupère aussi le score et la durée du sommeil (1 appel API par tranche de 28 jours)",
    )
    p.add_argument(
        "--races",
        action="store_true",
        help="récupère aussi les prédictions de course (5K, semi-marathon) "
        "(1 appel API par tranche d'année)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="récupère tous les indicateurs optionnels (pas, sommeil, courses)",
    )
    p.add_argument("--verbose", action="store_true", help="journalise les erreurs d'appel API")
    a = p.parse_args()
    start = datetime.strptime(a.start, "%Y-%m-%d").date()
    end = datetime.strptime(a.end, "%Y-%m-%d").date()
    VERBOSE = a.verbose

    api = login()
    print("Connecté. Téléchargement (long la première fois, ensuite en cache)...")
    cache = download(
        api,
        start,
        end,
        with_steps=a.steps or a.all,
        with_sleep=a.sleep or a.all,
        with_races=a.races or a.all,
    )
    table = display_table(monthly(cache))
    table.to_csv("history.csv", index=False)
    table.to_excel("activity.xlsx", index=False)
    print(table.tail(6).to_string(index=False))
    print()
    print(synthese_semaine(cache))
    print("\nFichiers créés : history.csv, activity.xlsx")


if __name__ == "__main__":
    main()
