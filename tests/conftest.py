import copy
import json

import pytest

from tracker.config import ROOT, validate
from tracker.profile import profile_for

EXAMPLE = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

# A deliberately different car: no body styles, an AWD must-have, no VIN prefix, a sunroof bonus.
CRV = {
    **copy.deepcopy(EXAMPLE),
    "vehicle": {"label": "CR-V Hybrid", "make": "Honda", "model": "CR-V", "trims": ["Sport Touring", "Sport-L"],
                "other_trims": ["EX-L", "EX", "LX"], "price_range": [10000, 60000], "icon": "🚙"},
    "body": {},
    "features": {
        "awd": {"label": "All-wheel drive", "short": "AWD", "yes": ["awd", "all[- ]wheel drive"],
                "no": ["fwd", "front[- ]wheel drive"], "required": True},
        "sunroof": {"label": "Moonroof", "short": "Roof", "yes": ["moonroof", "sunroof"], "value": 800},
    },
    "criteria": {"min_year": 2023, "max_year": None, "max_miles": 40000, "max_price": 38000},
    "fair_value": {**EXAMPLE["fair_value"], "base": {"2023": 31000, "2024": 33500, "2025": 36000}},
}


def make_cfg(raw, tmp_path=None):
    cfg = copy.deepcopy(raw)
    validate(cfg)
    if tmp_path is not None:
        cfg["db_path"] = str(tmp_path / "t.db")
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(EXAMPLE, tmp_path)


@pytest.fixture
def profile(cfg):
    return profile_for(cfg)


@pytest.fixture
def crv(tmp_path):
    return make_cfg(CRV, tmp_path)
