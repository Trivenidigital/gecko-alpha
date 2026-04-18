"""Social-velocity research tier (LunarCrush, etc.).

Top-level package houses the vendor-agnostic shared layer:

* :mod:`scout.social.models`       -- ``ResearchAlert``, ``BaselineState``,
                                      ``SpikeKind`` enum.
* :mod:`scout.social.baselines`    -- EWMA baseline cache + DB checkpoint.

Vendor-specific code lives under ``scout.social.<vendor>.*`` -- today only
``scout.social.lunarcrush``. A future Santiment integration would add a
sibling package without touching the shared layer.
"""
