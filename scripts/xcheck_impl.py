"""Cross-implementation consistency check: Python engine (source of truth)
vs the app/cloud JS math, on identical scenarios.

Random but SEEDED scenarios spanning the real operating envelope, plus
adversarial cases hugging the theta threshold. The JS erf approximation
(Abramowitz-Stegun 7.1.26) differs from math.erf by <=7e-8 — a decision may
legitimately flip only when the edge sits within that hair of a boundary,
so such knife-edge cases are tolerated (they are 6 orders of magnitude
below the 1-cent price grid).

Usage: python scripts/xcheck_impl.py   (requires node; run from repo root)
"""
import json
import math
import os
import random
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from polysignal.quant import EwmaVol, evaluate, norm_cdf, p_up  # noqa: E402

APPROX_TOL = 1e-6  # decision-boundary tolerance for the JS erf approximation

rng = random.Random(20260717)
THETA, BUFFER, STEP, FEE_BPS = 0.06, 0.02, 0.04, 1000.0
BAND = (15.0, 120.0)

scenarios = []
for _ in range(4000):
    s_open = rng.uniform(20_000, 120_000)
    move = rng.gauss(0, s_open * rng.uniform(1e-5, 3e-3))
    sigma = rng.uniform(2e-6, 3e-4)          # realistic per-second vol range
    tau = rng.uniform(5, 290)                 # includes out-of-band
    ask_up = round(rng.uniform(0.01, 0.99), 2) if rng.random() > 0.05 else None
    ask_down = round(1 - ask_up + rng.gauss(0, 0.02), 2) if ask_up and rng.random() > 0.1 \
        else (round(rng.uniform(0.01, 0.99), 2) if rng.random() > 0.05 else None)
    if ask_down is not None and not (0 < ask_down < 1):
        ask_down = None
    scenarios.append(dict(sT=s_open + move, sOpen=s_open, sigma=sigma, tau=tau,
                          askUp=ask_up, askDown=ask_down, feeBps=FEE_BPS,
                          theta=THETA, buffer=BUFFER, band=list(BAND), step=STEP))

# adversarial: edges engineered within ±2e-4 of theta and of the ladder steps
for target in (THETA, THETA + STEP, THETA + 2 * STEP):
    for eps in (-2e-4, -1e-5, 0.0, 1e-5, 2e-4):
        ask = 0.50
        fee = (FEE_BPS / 10_000) * 0.5
        # choose pf so that edge = pf - ask - fee - buffer = target + eps
        pf_target = target + eps + ask + fee + BUFFER
        if not (0.001 < pf_target < 0.999):
            continue
        # invert: pf = Phi(z) -> z, then pick sT giving that z
        # use bisection on z
        lo, hi = -6, 6
        for _ in range(80):
            mid = (lo + hi) / 2
            if norm_cdf(mid) < pf_target:
                lo = mid
            else:
                hi = mid
        z = (lo + hi) / 2
        sigma, tau, s_open = 1e-4, 60.0, 60_000.0
        s_t = s_open * math.exp(z * sigma * math.sqrt(tau))
        scenarios.append(dict(sT=s_t, sOpen=s_open, sigma=sigma, tau=tau,
                              askUp=ask, askDown=None, feeBps=FEE_BPS,
                              theta=THETA, buffer=BUFFER, band=list(BAND), step=STEP))

# EWMA series: identical price paths through both implementations
ewma_series = []
for _ in range(50):
    p = rng.uniform(30_000, 90_000)
    t = 0.0
    series = []
    for _ in range(400):
        t += rng.choice([1.0, 1.0, 1.0, 2.0, 5.0, 0.5])
        p *= math.exp(rng.gauss(0, 1e-4))
        series.append([p, t])
    ewma_series.append(series)

phi_xs = [x / 100 for x in range(-600, 601)]

payload = json.dumps(dict(
    scenarios=scenarios, ewma_series=ewma_series, phi_xs=phi_xs))
js = subprocess.run(
    ["node", "xcheck_js_impl.mjs"], input=payload, capture_output=True,
    text=True, cwd=os.path.join(REPO, "scripts"))
if js.returncode != 0:
    print("JS FAILED:", js.stderr[:2000])
    sys.exit(1)
jsout = json.loads(js.stdout)

# --- compare decisions ---
mismatch, knife_edge = [], 0
boundaries = (THETA, THETA + STEP, THETA + 2 * STEP)
for sc, jd in zip(scenarios, jsout["decisions"]):
    py = evaluate(s_t=sc["sT"], s_open=sc["sOpen"], sigma_1s=sc["sigma"],
                  tau_s=sc["tau"], ask_up=sc["askUp"], ask_down=sc["askDown"],
                  taker_base_fee_bps=sc["feeBps"], theta=sc["theta"],
                  buffer=sc["buffer"], band_s=tuple(sc["band"]),
                  ladder_step=sc["step"], gate2_green=False)
    if py.side != jd["side"] or abs(py.stake_usd - jd["stake"]) > 1e-9:
        edge = py.edge if py.edge else jd.get("edge") or 0.0
        if any(abs(edge - b) < APPROX_TOL for b in boundaries):
            knife_edge += 1  # legitimate erf-approximation boundary case
        else:
            mismatch.append((sc, dict(py=(py.side, py.stake_usd, py.edge)), jd))

print(f"decision scenarios: {len(scenarios)}  real mismatches: {len(mismatch)}  "
      f"tolerated knife-edge (<1e-6 of a boundary): {knife_edge}")
for m in mismatch[:5]:
    print("MISMATCH:", json.dumps(m, default=str)[:400])

# --- compare EWMA ---
worst = 0.0
for series, js_sigma in zip(ewma_series, jsout["ewma"]):
    v = EwmaVol(90, 1e-6)
    for p, t in series:
        v.update(p, t)
    rel = abs(v.sigma_1s - js_sigma) / max(v.sigma_1s, 1e-12)
    worst = max(worst, rel)
print(f"EWMA series: {len(ewma_series)}  worst relative sigma diff: {worst:.3e}")

# --- phi approximation error (JS Abramowitz-Stegun vs Python math.erf) ---
werr = max(abs(norm_cdf(x) - j) for x, j in zip(phi_xs, jsout["phi"]))
print(f"phi sweep [-6,6]: worst |Python-JS| = {werr:.3e} "
      f"(threshold flip needs ~1e-2 near theta -> {'OK' if werr < 1e-5 else 'RISK'})")

ok = not mismatch and worst < 1e-9 and werr < 1e-5
print("XCHECK:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
