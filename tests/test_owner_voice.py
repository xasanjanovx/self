"""Только его голос (2.0, 26.09.2026): только его голос и посреди разговора, субтитры без чужих письменностей."""
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
    ("hermana", False), ("где ты", False), ("Jes, qalaysan?", False), ("O'zbekiston", False),
    ("नमस्ते", True), ("你好", True), ("مرحبا", True), ("こんにちは", True), ("Γεια", True),
])
def test_foreign_script_subtitles(text, foreign):
    assert phone_live.foreign_script(text) is foreign


def _fake_model(monkeypatch, tmp_path, *, score: float, z: float, emb: np.ndarray, bank=None, z_thr: float = 1.2):
    import json

    monkeypatch.setattr(voiceprint, "_file", lambda uid: tmp_path / f"vp_{uid}.json")
    (tmp_path / "vp_1.json").write_text(json.dumps({"vp": [1.0, 0.0, 0.0], "threshold": 0.4, "z_threshold": z_thr,
                                                    "bank": [list(map(float, b)) for b in bank or []]}))

    async def extractor():
        return object()

    monkeypatch.setattr(voiceprint, "extractor", extractor)
    monkeypatch.setattr(voiceprint, "pcm16k", lambda wav: np.ones(16000, dtype=np.float32))
    monkeypatch.setattr(voiceprint, "trim_silence", lambda x: x)
    monkeypatch.setattr(voiceprint, "_embed_sync", lambda ext, x: emb)
    monkeypatch.setattr(voiceprint, "zscore", lambda vp, e: (score, z))


def _verify() -> dict:
    return asyncio.run(voiceprint.verify(1, b""))


def test_quiet_owner_high_z_low_raw_passes(monkeypatch, tmp_path):
    # 26.09: «Джес, позвони…» — z 2.36 («точно он»), сырая 0.16 → раньше «чужой голос»
    _fake_model(monkeypatch, tmp_path, score=0.16, z=2.36, emb=OWNER)
    assert _verify()["ok"] is True


def test_stranger_low_everything_rejected(monkeypatch, tmp_path):
    _fake_model(monkeypatch, tmp_path, score=0.2, z=0.4, emb=STRANGER, bank=[OWNER])
    res = _verify()
    assert res["ok"] is False and res["bank"] < 0.1


def test_bank_of_his_real_phrases_rescues(monkeypatch, tmp_path):
    # отпечаток не уверен, но запись похожа на его подтверждённые фразы с этого телефона
    _fake_model(monkeypatch, tmp_path, score=0.2, z=1.05, emb=_unit(1, 0.1, 0), bank=[OWNER])
    assert _verify()["ok"] is True


def test_bank_grows_only_with_sure_records_and_is_capped(monkeypatch, tmp_path):
    import json

    _fake_model(monkeypatch, tmp_path, score=0.6, z=2.0, emb=OWNER)
    assert _verify()["sure"] is True
    voiceprint._bank_at.clear()
    for i in range(voiceprint.BANK_MAX + 3):
        voiceprint._bank_at.clear()
        assert voiceprint.bank_add(1, OWNER)
    assert len(json.loads((tmp_path / "vp_1.json").read_text())["bank"]) == voiceprint.BANK_MAX
    assert voiceprint.bank_add(1, OWNER) is False  # не чаще раза в 30 с
