import math

import pytest

from polysignal.quant import (EwmaVol, evaluate, norm_cdf, p_up,
                              stake_from_edge, taker_fee_per_share)


def test_norm_cdf_basics():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(10) == pytest.approx(1.0, abs=1e-9)
    assert norm_cdf(-10) == pytest.approx(0.0, abs=1e-9)


def test_p_up_symmetry_and_limits():
    # spot at open -> 50/50
    assert p_up(100.0, 100.0, 1e-4, 60) == pytest.approx(0.5)
    # spot above open -> P > .5, symmetric below
    up = p_up(100.1, 100.0, 1e-4, 60)
    dn = p_up(99.9, 100.0 * (100.1 / 100.0) / (99.9 / 100.0) * (99.9 / 100.0), 1e-4, 60)
    assert up > 0.5
    # tau -> 0 collapses to indicator (tie counts as Up, per market rules)
    assert p_up(100.0, 100.0, 1e-4, 0) == 1.0
    assert p_up(99.99, 100.0, 1e-4, 0) == 0.0


def test_ewma_vol_tracks_scale():
    v = EwmaVol(halflife_s=30)
    p = 100.0
    for i in range(300):
        # deterministic alternating 0.05% move ~ sigma 5e-4
        p *= 1.0005 if i % 2 == 0 else 1 / 1.0005
        v.update(p, float(i))
    assert v.ready()
    assert 3e-4 < v.sigma_1s < 7e-4


def test_fee_formula_reads_bps():
    # 1000 bps = 10% of min(p, 1-p): live Gamma value for these markets
    assert taker_fee_per_share(0.5, 1000) == pytest.approx(0.05)
    assert taker_fee_per_share(0.9, 1000) == pytest.approx(0.01)
    assert taker_fee_per_share(0.1, 1000) == pytest.approx(0.01)
    assert taker_fee_per_share(0.5, 0) == 0.0


def test_stake_ladder_and_gate2_cap():
    step = 0.04
    assert stake_from_edge(0.0, 0.06, step) == 0.0
    assert stake_from_edge(0.03, 0.06, step) == 1.0
    assert stake_from_edge(0.06, 0.06, step) == 2.0
    # >2 steps above theta maps to $5 ONLY after GATE 2 — else capped at $2
    assert stake_from_edge(0.10, 0.06, step, gate2_green=False) == 2.0
    assert stake_from_edge(0.10, 0.06, step, gate2_green=True) == 5.0


BASE = dict(s_t=100.05, s_open=100.0, sigma_1s=1e-4, tau_s=60,
            ask_up=0.55, ask_down=0.44, taker_base_fee_bps=1000,
            theta=0.06, buffer=0.02, band_s=(15, 120), ladder_step=0.04)


def test_evaluate_outside_band_passes():
    sig = evaluate(**{**BASE, "tau_s": 10})
    assert sig.side == "PASS" and sig.reason == "outside_time_band"
    sig = evaluate(**{**BASE, "tau_s": 200})
    assert sig.side == "PASS"


def test_evaluate_signal_fires_and_respects_depth():
    # s_t well above open, cheap ask_up -> UP signal
    strong = {**BASE, "s_t": 100.5, "ask_up": 0.60, "ask_down": 0.39}
    sig = evaluate(**strong)
    assert sig.side == "UP" and sig.stake_usd >= 1.0
    # no depth -> PASS
    sig = evaluate(**strong, depth_up_usd=0.0, depth_down_usd=0.0)
    assert sig.side == "PASS" and sig.reason == "insufficient_depth"


def test_evaluate_fee_kills_marginal_edge():
    # marginal raw edge gets eaten by the 10% fee -> PASS
    sig = evaluate(**{**BASE, "s_t": 100.02})
    assert sig.side == "PASS" and sig.reason == "edge_below_theta"
