#!/usr/bin/env python3
"""
Unités et mise en forme des durées (partagé par history.py, analysis.py et
analysis_fine.py).

Deux usages :

  - `fmt_hms` / `fmt_hm` : affichage lisible d'une durée
    (prédictions de course en « hh:mm:ss », sommeil en « hh:mm ») ;
  - `parse_hms` / `parse_hm` : lecture inverse, pour relire un tableau
    mensuel dont les durées ont été formatées.

Aucune dépendance : ce module doit rester importable sans pandas.
"""
import math


def _is_na(v) -> bool:
    """True si la valeur est absente (None, NaN, chaîne vide)."""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == "" or v.strip().lower() == "nan"
    try:
        return math.isnan(v)
    except (TypeError, ValueError):
        return False


def _round_half_up(x: float) -> int:
    """Arrondi au supérieur le plus proche (0.5 -> 1), comme on l'attend
    pour une durée affichée."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def fmt_hms(minutes, signed: bool = False) -> str:
    """Minutes -> « hh:mm:ss » (chaîne vide si la valeur est absente)."""
    if _is_na(minutes):
        return ""
    total = _round_half_up(float(minutes) * 60)
    sign = "-" if total < 0 else ("+" if signed else "")
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def fmt_hm(hours, signed: bool = False) -> str:
    """Heures -> « hh:mm » (chaîne vide si la valeur est absente)."""
    if _is_na(hours):
        return ""
    total = _round_half_up(float(hours) * 60)
    sign = "-" if total < 0 else ("+" if signed else "")
    total = abs(total)
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def parse_hms(value) -> float:
    """« hh:mm:ss » -> minutes. Un nombre est renvoyé tel quel."""
    if _is_na(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return float("nan")
    while len(nums) < 3:
        nums.append(0)
    h, m, s = nums[:3]
    return h * 60 + m + s / 60


def parse_hm(value) -> float:
    """« hh:mm » -> heures. Un nombre est renvoyé tel quel."""
    if _is_na(value):
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    parts = str(value).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return float("nan")
    while len(nums) < 2:
        nums.append(0)
    h, m = nums[:2]
    return h + m / 60
