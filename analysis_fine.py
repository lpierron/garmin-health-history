#!/usr/bin/env python3
"""
Analyse fine activité physique <-> santé, à l'échelle JOURNALIÈRE et
HEBDOMADAIRE, avec correction saisonnière.  Lit garmin_daily_cache.json
(produit par garmin_history.py).

Méthode
-------
1. Nettoyage : jours « montre non portée » (calories actives < --min-cal)
   mis à vide (le poids, mesuré par la balance, est conservé).
2. Correction : chaque série est ramenée à des écarts « résiduels » après
   retrait (régression) de
      - une tendance lente (spline linéaire, un nœud par an),
      - la saisonnalité annuelle (2 harmoniques, calées sur le calendrier),
      - l'effet jour de semaine (échelle journalière seulement).
   On ne compare donc plus « l'été » à « l'hiver » ni 2019 à 2026, mais les
   variations de court terme autour du régime habituel de la période.
3. Corrélation de Kendall (tau-b) entre activité et santé, au même instant et
   avec décalages (santé du lendemain / de la semaine suivante...).
4. P-values par SUBSTITUTION CIRCULAIRE : on décale circulairement une série
   (>= 30 jours / 8 semaines) pour casser le lien avec l'autre tout en gardant
   son autocorrélation. L'écart-type du tau sous cette hypothèse nulle donne
   un z puis la p-value. C'est indispensable : jours et semaines consécutifs
   se ressemblent, la p-value « naïve » de Kendall est donc beaucoup trop
   optimiste (elle figure dans le CSV, colonne p_naive, pour comparaison).
5. Correction des tests multiples (Benjamini-Hochberg, FDR) par échelle de temps.

Usage :
    python analysis_fine.py
    python analysis_fine.py --cache garmin_daily_cache.json --out resultats --alpha 0.05

Dépendances : pandas, numpy, scipy, matplotlib  (+ analysis.py à côté)
"""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from scipy import stats

from analysis import bh_fdr  # correction FDR déjà écrite dans analysis.py

# ------------------------------------------------------------ vocabulaire
ACT_LABEL = {
    "moderate": "Activité modérée",
    "intense": "Activité intense",
    "total": "Total pondéré",
    "calories": "Calories actives",
    "load7": "Charge 7 jours",
    "load4w": "Charge 4 semaines",
    "steps": "Pas",
}
HEALTH_LABEL = {
    "rhr": "FC repos",
    "stress": "Stress",
    "bb_max": "Body Battery max",
    "weight": "Poids",
    "VO2max": "VO2max",
    "sleep_score": "Score sommeil",
    "sleep_hours": "Sommeil",
}
UNITS = {"rhr": "bpm", "stress": "pts", "bb_max": "pts", "weight": "kg", "VO2max": "ml/kg/min",
         "total": "min/j", "moderate": "min/j", "intense": "min/j", "calories": "kcal/j",
         "steps": "pas", "sleep_score": "pts", "sleep_hours": "h"}
GOOD = {"rhr": -1, "stress": -1, "bb_max": +1, "weight": -1, "VO2max": +1,
        "sleep_score": +1, "sleep_hours": +1}
DAILY_ACT = ["moderate", "intense", "total", "calories", "load7", "steps"]
DAILY_HEALTH = ["rhr", "stress", "bb_max", "sleep_score", "sleep_hours"]
WEEKLY_ACT = ["moderate", "intense", "total", "calories", "load4w", "steps"]
WEEKLY_HEALTH = ["rhr", "stress", "bb_max", "weight", "VO2max", "sleep_score", "sleep_hours"]
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
          "juil.", "août", "sept.", "oct.", "nov.", "déc."]

# palette de référence (dataviz) : divergent bleu <-> gris <-> rouge
SURF, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#dcdbd6"
BLUE, ORANGE, RED, MID = "#2a78d6", "#eb6834", "#e34948", "#f0efec"
DIVERGING = LinearSegmentedColormap.from_list("div", [BLUE, MID, RED])


# ---------------------------------------------------------------- données
def load_daily(path: Path, min_cal: float):
    raw = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame.from_dict(raw, orient="index")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().apply(pd.to_numeric, errors="coerce").asfreq("D")
    df = df.iloc[:-1]  # dernier jour du cache : souvent incomplet
    df["total"] = df["moderate"] + 2 * df["intense"]
    not_worn = df["calories"] < min_cal
    n_off = int(not_worn.sum())
    watch_cols = [c for c in df.columns if c != "weight"]
    df.loc[not_worn, watch_cols] = np.nan
    return df, n_off


def to_weekly(daily: pd.DataFrame, min_valid: int) -> pd.DataFrame:
    g = daily.resample("W-SUN")  # semaines lundi -> dimanche, datées du dimanche
    mean, cnt, size = g.mean(), g.count(), g.size()
    for c in mean.columns:
        need = 1 if c in ("weight", "VO2max") else min_valid
        mean[c] = mean[c].where(cnt[c] >= need)
    mean = mean[size == 7]  # semaines incomplètes (début/fin) écartées
    return mean


# ------------------------------------------------- correction saisonnière
def _basis(index: pd.DatetimeIndex, weekday: bool, K: int, knot_days: int):
    t = np.asarray((index - index[0]).days, dtype=float)
    cols = [np.ones_like(t), t / 365.25]
    for k in range(1, int(t.max() // knot_days) + 1):
        cols.append(np.maximum(0.0, t - k * knot_days) / 365.25)
    n_trend = len(cols)
    ph = 2 * np.pi * (index.dayofyear.values - 1) / 365.25
    for k in range(1, K + 1):
        cols += [np.sin(k * ph), np.cos(k * ph)]
    if weekday:
        dow = index.dayofweek.values
        cols += [(dow == d).astype(float) for d in range(1, 7)]
    return np.column_stack(cols), n_trend, 2 * K


def deseason(s: pd.Series, weekday=False, K=2, knot_days=365):
    """Retourne (résidus, info saisonnière)."""
    X, n_tr, n_h = _basis(s.index, weekday, K, knot_days)
    y = s.values.astype(float)
    m = ~np.isnan(y)
    if m.sum() < max(40, 3 * X.shape[1]):
        return s * np.nan, None
    beta, *_ = np.linalg.lstsq(X[m], y[m], rcond=None)
    resid = pd.Series(y - X @ beta, index=s.index)
    # profil saisonnier moyen sur une année calendaire
    doy = np.arange(1, 366)
    ph = 2 * np.pi * (doy - 1) / 365.25
    prof = np.zeros_like(ph)
    for k in range(1, K + 1):
        b_s, b_c = beta[n_tr + 2 * (k - 1)], beta[n_tr + 2 * (k - 1) + 1]
        prof += b_s * np.sin(k * ph) + b_c * np.cos(k * ph)
    seas_obs = X[m][:, n_tr:n_tr + n_h] @ beta[n_tr:n_tr + n_h]
    month_of = lambda d: (pd.Timestamp("2001-01-01") + pd.Timedelta(days=int(d) - 1)).month
    info = dict(amp=float(prof.max() - prof.min()),
                peak=month_of(doy[prof.argmax()]), trough=month_of(doy[prof.argmin()]),
                share=float(np.var(seas_obs) / np.var(y[m])))
    return resid, info


# ------------------------------------------------------------ corrélations
def tau_of(x, y, min_pairs):
    m = ~(np.isnan(x) | np.isnan(y))
    n = int(m.sum())
    if n < min_pairs or np.ptp(x[m]) == 0 or np.ptp(y[m]) == 0:
        return np.nan, np.nan, n
    r = stats.kendalltau(x[m], y[m])
    return float(r[0]), float(r[1]), n


def lagged(a, lag):
    return a if lag == 0 else np.concatenate([a[lag:], np.full(lag, np.nan)])


def correlate(scale, acts_raw, acts_adj, hl_raw, hl_adj, lags, min_shift, n_shifts, rng, min_pairs):
    n = len(next(iter(acts_adj.values())))
    shifts = rng.integers(min_shift, n - min_shift, size=n_shifts)
    rows = []
    for lag in lags:
        for a in acts_adj:
            for h in hl_adj:
                xa, xr = acts_adj[a].values, acts_raw[a].values
                ya, yr = lagged(hl_adj[h].values, lag), lagged(hl_raw[h].values, lag)
                tau, p_naive, npairs = tau_of(xa, ya, min_pairs)
                if np.isnan(tau):
                    continue
                tau_raw = tau_of(xr, yr, min_pairs)[0]
                null = np.array([tau_of(xa, np.roll(ya, s), min_pairs)[0] for s in shifts])
                sd = np.nanstd(null, ddof=1)
                p = 2 * stats.norm.sf(abs(tau) / sd) if sd > 0 else np.nan
                rows.append(dict(scale=scale, lag=lag, activity=a, health=h, tau=tau,
                                 tau_raw=tau_raw, p=p, p_naive=p_naive, n=npairs, null_sd=sd))
    out = pd.DataFrame(rows).dropna(subset=["p"]).reset_index(drop=True)
    out["q"] = bh_fdr(out["p"])
    return out


def trend_table(weekly_raw: pd.DataFrame, cols, n_boot, rng, block=13):
    """Pente de Theil-Sen par an + IC95% par bootstrap par blocs (13 semaines)."""
    rows = []
    for c in cols:
        y = weekly_raw[c].values.astype(float)
        t = np.arange(len(y)) / 52.1775
        m = ~np.isnan(y)
        if m.sum() < 60:
            continue
        slope, icpt, *_ = stats.theilslopes(y[m], t[m])
        fit = icpt + slope * t
        e = y - fit
        bs = []
        nb = int(np.ceil(len(y) / block))
        for _ in range(n_boot):
            starts = rng.integers(0, len(y) - block + 1, size=nb)
            idx = np.concatenate([np.arange(s, s + block) for s in starts])[:len(y)]
            yb = fit + e[idx]
            mb = ~np.isnan(yb)
            bs.append(stats.theilslopes(yb[mb], t[mb])[0])
        lo, hi = np.percentile(bs, [2.5, 97.5])
        rows.append(dict(variable=c, slope_per_year=slope, lo=lo, hi=hi, n=int(m.sum())))
    return pd.DataFrame(rows)


def tercile_contrast(act_adj: pd.Series, hl_adj: dict, lag=1):
    lo, hi = act_adj.quantile([1 / 3, 2 / 3])
    rows = []
    for h, s in hl_adj.items():
        nxt = pd.Series(lagged(s.values, lag), index=s.index)
        low, high = nxt[act_adj <= lo].dropna(), nxt[act_adj >= hi].dropna()
        if len(low) < 15 or len(high) < 15:
            continue
        p = stats.mannwhitneyu(high, low, alternative="two-sided")[1]
        rows.append(dict(health=h, low=low.median(), high=high.median(),
                         diff=high.median() - low.median(), p=p, n_low=len(low), n_high=len(high)))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ figures
def _style():
    plt.rcParams.update({
        "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
        "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.edgecolor": GRID, "axes.spines.top": False, "axes.spines.right": False,
        "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "regular",
    })


def heat_panels(corr, scale, lags, acts, health, lag_unit, title, path):
    _style()
    fig, axes = plt.subplots(1, len(lags), figsize=(3.6 * len(lags) + 1.6, 3.9), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, lag in zip(axes, lags):
        sub = corr[(corr.scale == scale) & (corr.lag == lag)]
        tau = sub.pivot(index="activity", columns="health", values="tau").reindex(index=acts, columns=health)
        q = sub.pivot(index="activity", columns="health", values="q").reindex(index=acts, columns=health)
        im = ax.imshow(np.ma.masked_invalid(tau.values), cmap=DIVERGING, vmin=-0.5, vmax=0.5, aspect="auto")
        ax.set_xticks(range(len(health)), [HEALTH_LABEL[h] for h in health], rotation=30, ha="right")
        ax.set_yticks(range(len(acts)), [ACT_LABEL[a] for a in acts])
        ax.set_title("même " + lag_unit if lag == 0 else f"santé à +{lag} {lag_unit}")
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.tick_params(length=0)
        for i in range(len(acts)):
            for j in range(len(health)):
                v, qq = tau.values[i, j], q.values[i, j]
                if np.isnan(v):
                    continue
                mark = "*" if qq < 0.05 else ("+" if qq < 0.10 else "")
                ax.text(j, i, f"{v:+.2f}{mark}", ha="center", va="center", fontsize=8.5, color=INK)
    cb = fig.colorbar(im, ax=axes, shrink=0.8, pad=0.02)
    cb.set_label("tau de Kendall (écarts corrigés)", color=INK2)
    cb.outline.set_visible(False)
    fig.suptitle(title + "\n* q<0.05   + q<0.10  (FDR, p-values par substitution circulaire)",
                 fontsize=10, y=1.04)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def seasonality_figure(weekly_raw, path):
    _style()
    cols = ["rhr", "stress", "bb_max", "weight", "VO2max", "total"]
    names = {**HEALTH_LABEL, "total": "Activité (total pondéré)"}
    fig, axes = plt.subplots(2, 3, figsize=(10.5, 5.6), sharex=True)
    for ax, c in zip(axes.ravel(), cols):
        s = weekly_raw[c]
        piv = s.groupby([s.index.year, s.index.month]).mean().unstack(0)
        for yr in piv.columns:
            ax.plot(piv.index, piv[yr], color=GRID, lw=1)
        ax.plot(piv.index, piv.mean(axis=1), color=BLUE, lw=2)
        ax.set_title(f"{names[c]} ({UNITS[c]})", loc="left")
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_xticks(range(1, 13), [m[0].upper() for m in MONTHS])
        ax.tick_params(length=0)
    fig.suptitle("Saisonnalité : moyenne par mois (bleu) et chaque année (gris)", fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def raw_vs_adj_figure(corr, path):
    _style()
    fig, ax = plt.subplots(figsize=(4.8, 4.6))
    lim = 0.6
    ax.plot([-lim, lim], [-lim, lim], color=GRID, lw=1)
    ax.axhline(0, color=GRID, lw=0.6)
    ax.axvline(0, color=GRID, lw=0.6)
    for scale, col, lab in (("weekly", BLUE, "hebdomadaire"), ("daily", ORANGE, "journalier")):
        d = corr[corr.scale == scale]
        ax.scatter(d.tau_raw, d.tau, s=36, color=col, edgecolor=SURF, linewidth=1.2, label=lab)
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_xlabel("tau brut (sans correction)")
    ax.set_ylabel("tau corrigé (tendance + saison + jour de semaine)")
    ax.set_title("Effet de la correction sur chaque paire", loc="left")
    ax.legend(frameon=False, loc="upper left")
    ax.tick_params(length=0)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------ rapport
def verdict(h, tau):
    return "favorable" if np.sign(tau) == GOOD[h] else "défavorable"


def print_pairs(df, alpha, top=8):
    sig = df[df.q < alpha]
    if sig.empty:
        print(f"  Aucune corrélation ne survit à la correction (FDR q < {alpha}).")
        print("  Meilleurs candidats (prudence) :")
        sig = df.reindex(df.p.sort_values().index).head(top)
    else:
        sig = sig.reindex(sig.tau.abs().sort_values(ascending=False).index)
    unit = {"daily": "j", "weekly": "sem."}
    for _, r in sig.iterrows():
        when = "même période" if r.lag == 0 else f"santé à +{int(r.lag)} {unit[r.scale]}"
        print(f"  {ACT_LABEL[r.activity]:18s} → {HEALTH_LABEL[r.health]:17s} ({when:16s}) "
              f"tau={r.tau:+.2f} (brut {r.tau_raw:+.2f})  p={r.p:.4f}  q={r.q:.3f}  "
              f"n={int(r.n)}  [{verdict(r.health, r.tau)}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="garmin_daily_cache.json", type=Path)
    ap.add_argument("--out", default=".", type=Path)
    ap.add_argument("--min-cal", type=float, default=50, help="calories actives min./jour (sinon montre non portée)")
    ap.add_argument("--min-days-week", type=int, default=4, help="jours valides min. pour garder une semaine")
    ap.add_argument("--shifts", type=int, default=400, help="substitutions circulaires par paire")
    ap.add_argument("--boot", type=int, default=300, help="rééchantillons pour l'IC des tendances")
    ap.add_argument("--alpha", type=float, default=0.10, help="seuil FDR du rapport")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    a.out.mkdir(parents=True, exist_ok=True)

    # --- données
    daily, n_off = load_daily(a.cache, a.min_cal)
    weekly = to_weekly(daily, a.min_days_week)
    daily["load7"] = daily["total"].rolling(7, min_periods=5).mean()
    weekly["load4w"] = weekly["total"].rolling(4, min_periods=3).mean()

    print(f"=== DONNÉES ===\nPériode : {daily.index.min().date()} → {daily.index.max().date()}"
          f"  ({len(daily)} jours, {len(weekly)} semaines complètes)")
    print(f"Jours « montre non portée » exclus (calories < {a.min_cal:.0f}) : {n_off}")
    cov = (daily[["rhr", "stress", "bb_max", "weight", "VO2max", "total",
                  "steps", "sleep_score", "sleep_hours"]].notna().mean() * 100).round(0)
    print("Couverture journalière : " + ", ".join(f"{k} {int(v)} %" for k, v in cov.items()))

    # --- correction
    def adjust(df, weekday, cols):
        raw, adj, info = {}, {}, {}
        for c in cols:
            raw[c] = df[c]
            adj[c], info[c] = deseason(df[c], weekday=weekday)
        return raw, adj, info

    d_cols = sorted(set(DAILY_ACT + DAILY_HEALTH))
    w_cols = sorted(set(WEEKLY_ACT + WEEKLY_HEALTH))
    d_raw, d_adj, _ = adjust(daily, True, d_cols)
    w_raw, w_adj, w_info = adjust(weekly, False, w_cols)
    seas_cols = ["rhr", "stress", "bb_max", "weight", "VO2max", "total", "intense",
                 "calories", "steps", "sleep_score", "sleep_hours"]
    _, _, seas_info = adjust(weekly, False, seas_cols)

    # --- 1. saisonnalité
    print("\n=== 1. SAISONNALITÉ (hebdomadaire, après retrait de la tendance) ===")
    print(f"  {'':18s}{'amplitude':>14s}  {'pic':>6s}  {'creux':>6s}  part de variance")
    for c in seas_cols:
        i = seas_info.get(c)
        if i is None:
            continue
        name = HEALTH_LABEL.get(c) or ACT_LABEL.get(c)
        print(f"  {name:18s}{i['amp']:9.1f} {UNITS[c]:<6s}{MONTHS[i['peak'] - 1]:>7s}{MONTHS[i['trough'] - 1]:>8s}"
              f"   {i['share'] * 100:4.0f} %")

    # --- 2. tendances
    print("\n=== 2. TENDANCES DE FOND (pente de Theil-Sen / an, IC95 % bootstrap par blocs) ===")
    tr = trend_table(weekly, WEEKLY_HEALTH + ["total", "intense", "calories", "steps"],
                     a.boot, rng)
    for _, r in tr.iterrows():
        name = HEALTH_LABEL.get(r.variable) or ACT_LABEL[r.variable]
        excl = (r.lo > 0) or (r.hi < 0)
        flag = "tendance nette" if excl else "indéterminée"
        print(f"  {name:18s} {r.slope_per_year:+7.2f} {UNITS[r.variable]}/an   [{r.lo:+.2f} ; {r.hi:+.2f}]   {flag}")

    # --- 3/4. corrélations
    print(f"\n(calcul des p-values : {a.shifts} substitutions circulaires par paire...)")
    cw = correlate("weekly", {c: w_raw[c] for c in WEEKLY_ACT}, {c: w_adj[c] for c in WEEKLY_ACT},
                   {c: w_raw[c] for c in WEEKLY_HEALTH}, {c: w_adj[c] for c in WEEKLY_HEALTH},
                   lags=(0, 1, 2), min_shift=8, n_shifts=a.shifts, rng=rng, min_pairs=40)
    cd = correlate("daily", {c: d_raw[c] for c in DAILY_ACT}, {c: d_adj[c] for c in DAILY_ACT},
                   {c: d_raw[c] for c in DAILY_HEALTH}, {c: d_adj[c] for c in DAILY_HEALTH},
                   lags=(0, 1), min_shift=30, n_shifts=a.shifts, rng=rng, min_pairs=200)
    corr = pd.concat([cw, cd], ignore_index=True)
    corr.to_csv(a.out / "correlations_fine.csv", index=False)

    print(f"\n=== 3. CORRÉLATIONS HEBDOMADAIRES CORRIGÉES ({len(cw)} paires) ===")
    print_pairs(cw, a.alpha)
    print(f"\n=== 4. CORRÉLATIONS JOURNALIÈRES CORRIGÉES ({len(cd)} paires) ===")
    print_pairs(cd, a.alpha)

    # --- 5. effet de la correction
    print("\n=== 5. EFFET DE LA CORRECTION SAISONNIÈRE ===")
    for scale, df in (("hebdomadaire", cw), ("journalier", cd)):
        naive = int((df.p_naive < 0.05).sum())
        corrected = int((df.q < 0.05).sum())
        flips = int((np.sign(df.tau) != np.sign(df.tau_raw)).sum())
        print(f"  {scale:13s}: |tau| médian brut {df.tau_raw.abs().median():.2f} → corrigé {df.tau.abs().median():.2f} ;"
              f" « significatifs » (p naïve < 0.05) {naive}/{len(df)} → après correction complète {corrected}/{len(df)} ;"
              f" signe inversé : {flips}")

    # --- 6. dose-réponse hebdo
    print("\n=== 6. SEMAINES TRÈS ACTIVES vs PEU ACTIVES → santé de la semaine suivante ===")
    print("  (écarts corrigés ; médiane du tiers le plus actif moins celle du tiers le moins actif)")
    terc = tercile_contrast(w_adj["total"], {h: w_adj[h] for h in WEEKLY_HEALTH}, lag=1)
    for _, r in terc.iterrows():
        print(f"  {HEALTH_LABEL[r.health]:17s} écart {r['diff']:+6.2f} {UNITS[r.health]:<9s} "
              f"(p={r.p:.3f}, n={int(r.n_low)}/{int(r.n_high)})  [{verdict(r.health, r['diff'])}]")
    print("  p-values de cette section : naïves (semaines supposées indépendantes) → indicatives.")

    # --- limites
    print("\n=== LIMITES ===")
    print("  * Corrélation ≠ causalité : on observe des co-variations, pas l'effet d'une intervention.")
    print("  * L'activité et la santé réagissent aussi à des tiers : maladie, voyage, sommeil, alcool,")
    print("    météo. Une semaine malade = activité basse ET FC de repos haute (biais vers « défavorable »).")
    print("  * Changements de montre/firmware Garmin : sauts artificiels de FC repos, stress, VO2max,")
    print("    en partie absorbés par la tendance lente, mais pas toujours.")
    print("  * Poids et VO2max sont des mesures irrégulières (balance, sorties de course) : semaines peu fiables.")
    print("  * ~100 paires testées : quelques résultats « limites » (q entre 0.05 et 0.10) sont attendus par hasard.")

    # --- figures
    heat_panels(corr, "weekly", (0, 1, 2), WEEKLY_ACT, WEEKLY_HEALTH, "sem.",
                "Activité hebdomadaire vs santé (corrigé saison + tendance)", a.out / "correlations_weekly.png")
    heat_panels(corr, "daily", (0, 1), DAILY_ACT, DAILY_HEALTH, "jour",
                "Activité journalière vs santé (corrigé saison + tendance + jour de semaine)",
                a.out / "correlations_daily.png")
    seasonality_figure(weekly, a.out / "seasonality.png")
    raw_vs_adj_figure(corr, a.out / "raw_vs_adjusted.png")
    print(f"\nFichiers dans {a.out}/ : correlations_fine.csv, correlations_weekly.png, "
          "correlations_daily.png, seasonality.png, raw_vs_adjusted.png")


if __name__ == "__main__":
    main()
