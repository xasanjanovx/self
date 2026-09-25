"""ZEKI (26.09.2026): только его голос и посреди разговора, субтитры без чужих письменностей."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from bot import phone_live, voiceprint


def _unit(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


OWNER = _unit(1, 0, 0)
STRANGER = _unit(0, 1, 0)


@pytest.fixture(autouse=True)
def _clean_anchors():
    voiceprint._anchors.clear()
    yield
    voiceprint._anchors.clear()


def _fake_verify(monkeypatch, *, ok: bool, score: float, z: float | None, emb: np.ndarray, threshold: float = 0.8) -> None:
    async def verify(uid, wav):  # noqa: ANN001
        return {"ok": ok, "score": score, "z": z, "threshold": threshold, "_emb": emb}

    monkeypatch.setattr(voiceprint, "verify", verify)


def _is_other() -> dict:
    return asyncio.run(voiceprint.is_other(1, b""))


def test_high_raw_similarity_is_him_even_with_low_z(monkeypatch):
    # 25.09: его фразы (похожесть 0.85, z −0.2) отсекались как чужие — ZEKI молчал
    _fake_verify(monkeypatch, ok=False, score=0.85, z=-0.2, emb=OWNER)
    assert _is_other()["other"] is False


def test_phrase_like_his_wake_word_passes_and_becomes_anchor(monkeypatch):
    voiceprint.remember(1, OWNER)
    _fake_verify(monkeypatch, ok=False, score=0.3, z=0.1, emb=_unit(1, 0.2, 0))
    res = _is_other()
    assert res["other"] is False and res["anchor"] > 0.9 and len(voiceprint.anchors(1)) == 2


def test_stranger_unlike_his_phrases_is_dropped(monkeypatch):
    voiceprint.remember(1, OWNER)
    _fake_verify(monkeypatch, ok=False, score=0.2, z=0.1, emb=STRANGER)
    res = _is_other()
    assert res["other"] is True and res["anchor"] < 0.1
    assert len(voiceprint.anchors(1)) == 1  # чужой голос образцом не становится


def test_without_anchor_falls_back_to_voiceprint(monkeypatch):
    _fake_verify(monkeypatch, ok=False, score=0.2, z=0.1, emb=STRANGER)
    assert _is_other()["other"] is True
    _fake_verify(monkeypatch, ok=False, score=0.4, z=0.9, emb=STRANGER)  # нормированная выше порога — он
    assert _is_other()["other"] is False


def test_short_or_unenrolled_is_never_other(monkeypatch):
    async def short(uid, wav):  # noqa: ANN001
        return {"ok": False, "score": 0.0, "threshold": 0.4, "reason": "short"}

    monkeypatch.setattr(voiceprint, "verify", short)
    assert _is_other()["other"] is False


def test_anchors_expire(monkeypatch):
    voiceprint.remember(1, OWNER)
    at, embs = voiceprint._anchors[1]
    voiceprint._anchors[1] = (at - voiceprint.ANCHOR_TTL_S - 1, embs)
    assert voiceprint.anchors(1) == []


@pytest.mark.parametrize("text, foreign", [
    ("hermana", False), ("где ты", False), ("Zeki, qalaysan?", False), ("O'zbekiston", False),
    ("नमस्ते", True), ("你好", True), ("مرحبا", True), ("こんにちは", True), ("Γεια", True),
])
def test_foreign_script_subtitles(text, foreign):
    assert phone_live.foreign_script(text) is foreign
