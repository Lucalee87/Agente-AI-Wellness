"""Verifica la configurazione reale di MIFIT (config/centri.yaml)."""

from datetime import date
from pathlib import Path

import pytest

from app import centers
from app.voice import prompt

REAL = str(Path(__file__).resolve().parent.parent / "config" / "centri.yaml")


@pytest.fixture
def mifit(monkeypatch):
    cfg = centers.load_config(REAL)
    monkeypatch.setattr(centers, "load_config", lambda path=None: cfg)
    return cfg


def test_casoria_slots(mifit):
    c = mifit.centri["casoria"]
    lun, mer, ven = date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 16)
    hours = lambda d: [s.strftime("%H:%M") for s in centers.slots_for_day(c, d)]
    assert hours(lun) == ["15:00", "15:40", "16:20", "17:00", "17:40", "18:20"]
    assert hours(mer) == ["09:00", "09:40", "10:20", "17:30", "18:10", "18:50", "19:30"]
    assert hours(ven) == ["16:00", "16:40", "17:20", "18:00", "18:40", "19:20"]
    assert hours(date(2026, 10, 13)) == []  # martedì: niente valutazioni


def test_prompt_rules(mifit):
    lead = {"id": 1, "nome": "Maria Esposito", "source": "meta", "centro_id": None}
    first = prompt.first_message(lead)
    assert "Vittoria" in first and "MIFIT" in first and "La chiamo" in first and "Maria," in first
    sp = prompt.system_prompt(lead)
    assert "Non dire MAI prezzi" in sp
    assert "Dai del Lei" in sp
    assert "MIFIT Casoria (id `casoria`)" in sp  # unico centro: già selezionato
