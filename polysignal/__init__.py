"""POLYSIGNAL — Polymarket BTC 5-minute Up/Down fair-value signal engine.

No AI predicts direction here. The core is a deterministic fair-value model
(random walk, no drift) compared against the CLOB quote; everything else is
plumbing, risk rules and delivery. See POLYSIGNAL master plan.
"""

__version__ = "0.1.0"
