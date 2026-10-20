#!/usr/bin/env python3
"""
Analyse activité physique <-> santé à partir de history.csv (produit par
garmin_history.py).

  - corrélations de Kendall (tau-b) avec p-values et correction FDR
    (Benjamini-Hochberg), car on teste beaucoup de paires à la fois ;
  - décalage d'un mois : l'activité du mois M agit-elle sur la santé de M+1 ?
  - tendances de fond de chaque indicateur (Kendall vs temps + pente de Theil-Sen) ;
  - comparaison des mois très actifs / peu actifs ;
  - heatmap enregistrée dans correlations.png, détails dans correlations.csv.

Usage :
    python analysis.py                      # lit history.csv
    python analysis.py --csv autre.csv --min-days 25 --alpha 0.10

Dépendances : pandas, numpy, scipy, matplotlib
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

# activité (ramenée par jour, car les mois n'ont pas la même durée)
ACTIVITY = {
    "moderate": "Activité modérée (min/j)",
    "intense": "Activité intense (min/j)",
    "total": "Total pondéré (min/j)",
    "calories": "Calories actives (kcal/j)",
    "steps": "Pas (nb/j)",
}
# indicateurs de santé / forme
HEALTH = {
    "rhr": "FC repos",
    "VO2max": "VO2max",
    "weight": "Poids",
    "stress": "Stress",
    "bb_max": "Body Battery max",
    "sleep_score": "Score sommeil",
    "sleep_hours": "Sommeil (h)",
}
# sens "favorable" (+1 : plus haut = mieux, -1 : plus bas = mieux)
GOOD_DIRECTION = {"rhr": -1, "VO2max": +1, "weight": -1, "stress": -1, "bb_max": +1,
                  "sleep_score": +1, "sleep_hours": +1}
MIN_PAIRS = 12  # en dessous, une corrélation n'a pas de sens


# ----------------------------------------------------------------- données
def load(path: Path, min_days: int) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["month"] = pd.PeriodIndex(df["month"], freq="M")
    df = df.sort_values("month").set_index("month")
    n0 = len(df)
    df = df[df["days"] >= min_days]  # mois partiel (en cours) exclu
    if len(df) < n0:
        print(f"({n0 - len(df)} mois incomplet(s) exclu(s), < {min_days} jours)")
    # une série mensuelle doit être continue pour que le décalage d'un mois ait un sens
    full = pd.period_range(df.index.min(), df.index.max(), freq="M")
    df = df.reindex(full)
    for c in ACTIVITY:
        if c in df:
            df[c] = df[c] / df["days"]
    return df


# ------------------------------------------------------------ statistiques
def bh_fdr(p: pd.Series) -> pd.Series:
    """Correction de Benjamini-Hochberg (q-values)."""
    p = p.astype(float)
    order = p.sort_values().index
    m = len(p)
    ranked = p.loc[order].values * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    return pd.Series(np.clip(q, 0, 1), index=order).reindex(p.index)


def kendall(x: pd.Series, y: pd.Series):
    d = pd.concat([x, y], axis=1).dropna()
    if len(d) < MIN_PAIRS or d.iloc[:, 0].nunique() < 3 or d.iloc[:, 1].nunique() < 3:
        return np.nan, np.nan, len(d)
    tau, p = stats.kendalltau(d.iloc[:, 0], d.iloc[:, 1])
    return tau, p, len(d)


def correlations(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for lag in (0, 1):
        for a in ACTIVITY:
            for h in HEALTH:
                if a not in df or h not in df:
                    continue
                # lag=1 : activité du mois M  vs  santé du mois M+1
                tau, p, n = kendall(df[a], df[h].shift(-lag))
                rows.append(dict(lag=lag, activity=a, health=h, tau=tau, p=p, n=n))
    out = pd.DataFrame(rows).dropna(subset=["p"]).reset_index(drop=True)
    out["q"] = bh_fdr(out["p"])
    return out


def trends(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    t = pd.Series(np.arange(len(df)), index=df.index, dtype=float)
    for h in list(HEALTH) + list(ACTIVITY):
        if h not in df:
            continue
        d = pd.concat([t, df[h]], axis=1).dropna()
        if len(d) < MIN_PAIRS:
            continue
        tau, p = stats.kendalltau(d.iloc[:, 0], d.iloc[:, 1])
        slope = stats.theilslopes(d.iloc[:, 1], d.iloc[:, 0])[0] * 12  # par an
        rows.append(dict(variable=h, tau=tau, p=p, slope_per_year=slope, n=len(d)))
    return pd.DataFrame(rows)


def tercile_contrast(df: pd.DataFrame, activity="total") -> pd.DataFrame:
    """Santé du mois suivant : tiers le plus actif vs tiers le moins actif."""
    a = df[activity]
    lo, hi = a.quantile([1 / 3, 2 / 3])
    rows = []
    for h in HEALTH:
        nxt = df[h].shift(-1)
        low = nxt[a <= lo].dropna()
        high = nxt[a >= hi].dropna()
        if len(low) < 5 or len(high) < 5:
            continue
        _, p = stats.mannwhitneyu(high, low, alternative="two-sided")
        rows.append(dict(health=h, low_median=low.median(), high_median=high.median(),
                         diff=high.median() - low.median(), p=p, n_low=len(low), n_high=len(high)))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- sorties
def heatmap(corr: pd.DataFrame, path: Path):
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, lag, title in zip(axes, (0, 1), ("Même mois", "Activité M → santé M+1")):
        sub = corr[corr.lag == lag]
        tau = sub.pivot(index="activity", columns="health", values="tau").reindex(
            index=list(ACTIVITY), columns=list(HEALTH))
        q = sub.pivot(index="activity", columns="health", values="q").reindex(
            index=list(ACTIVITY), columns=list(HEALTH))
        im = ax.imshow(tau.values, cmap="RdBu_r", vmin=-0.6, vmax=0.6)
        ax.set_xticks(range(len(HEALTH)), [HEALTH[c] for c in tau.columns], rotation=30, ha="right")
        ax.set_yticks(range(len(ACTIVITY)), [ACTIVITY[r] for r in tau.index])
        ax.set_title(title)
        for i in range(tau.shape[0]):
            for j in range(tau.shape[1]):
                v = tau.values[i, j]
                if np.isnan(v):
                    continue
                star = "*" if q.values[i, j] < 0.05 else ""
                ax.text(j, i, f"{v:+.2f}{star}", ha="center", va="center", fontsize=9)
    fig.colorbar(im, ax=axes, shrink=0.8, label="tau de Kendall")
    fig.suptitle("Activité physique vs santé  (* = significatif après correction FDR, q < 0.05)")
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def verdict(h: str, tau: float) -> str:
    return "favorable" if np.sign(tau) == GOOD_DIRECTION[h] else "défavorable"


def report(df, corr, trend, terc, alpha):
    print(f"\n=== PÉRIODE : {df.index.min()} → {df.index.max()}  "
          f"({df['days'].notna().sum()} mois exploitables) ===")

    print("\n--- 1. TENDANCES DE FOND (Kendall vs temps) ---")
    for _, r in trend.iterrows():
        name = {**HEALTH, **ACTIVITY}[r.variable]
        sig = "significative" if r.p < 0.05 else "non significative"
        print(f"  {name:28s} tau={r.tau:+.2f}  p={r.p:.3f}  {r.slope_per_year:+.2f}/an  ({sig})")

    print(f"\n--- 2. CORRÉLATIONS ACTIVITÉ ↔ SANTÉ  (seuil FDR q < {alpha}) ---")
    sig = corr[corr.q < alpha].copy()
    if sig.empty:
        print("  Aucune corrélation ne survit à la correction pour tests multiples.")
        print("  Meilleurs candidats (à interpréter avec prudence, p non corrigé) :")
        sig = corr.reindex(corr.tau.abs().sort_values(ascending=False).index).head(5)
    else:
        sig = sig.reindex(sig.tau.abs().sort_values(ascending=False).index)
    for _, r in sig.iterrows():
        when = "même mois" if r.lag == 0 else "mois suivant"
        print(f"  {ACTIVITY[r.activity]:26s} → {HEALTH[r.health]:16s} ({when:12s}) "
              f"tau={r.tau:+.2f}  p={r.p:.3f}  q={r.q:.3f}  n={int(r.n)}  [{verdict(r.health, r.tau)}]")

    if not terc.empty:
        print("\n--- 3. MOIS TRÈS ACTIFS vs PEU ACTIFS → santé du mois suivant (médianes) ---")
        for _, r in terc.iterrows():
            print(f"  {HEALTH[r.health]:16s} peu actif {r.low_median:7.1f} | très actif "
                  f"{r.high_median:7.1f} | écart {r['diff']:+.1f}  (p={r.p:.3f})")

    print("\n--- LIMITES ---")
    print("  * Corrélation ≠ causalité : un mois très actif peut aussi être un mois en forme,")
    print("    ou un mois d'été (confusion saisonnière).")
    print("  * Données mensuelles : peu de points (n≈12-60), donc faible puissance statistique.")
    print("  * Les mois consécutifs sont liés entre eux (autocorrélation) : les p-values sont")
    print("    plutôt optimistes. Privilégiez les effets cohérents entre plusieurs indicateurs.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="history.csv", type=Path)
    ap.add_argument("--min-days", type=int, default=20, help="jours minimum pour garder un mois")
    ap.add_argument("--alpha", type=float, default=0.10, help="seuil FDR pour le rapport")
    ap.add_argument("--out", default=".", type=Path, help="dossier de sortie")
    a = ap.parse_args()

    df = load(a.csv, a.min_days)
    corr = correlations(df)
    trend = trends(df)
    terc = tercile_contrast(df)

    a.out.mkdir(exist_ok=True, parents=True)
    corr.to_csv(a.out / "correlations.csv", index=False)
    heatmap(corr, a.out / "correlations.png")
    report(df, corr, trend, terc, a.alpha)
    print(f"\nFichiers : {a.out / 'correlations.csv'}, {a.out / 'correlations.png'}")


if __name__ == "__main__":
    main()
