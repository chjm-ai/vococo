"""声纹识别模块测试(见 claude_hermes/voice/voiceprint.py、
docs/design/voice-companion/03-phase2-实现记录.md"声纹识别"一节)。

覆盖:纯函数(cosine_similarity/match_score/update_profile 的防污染规则)、
声纹参照的存盘/读盘往返、真实模型的冒烟测试(不 mock,真的跑一遍 ONNX 推理,
确认这条产线本身是通的——见 extract_embedding 相关用例)。
"""
from __future__ import annotations

import numpy as np

from claude_hermes.voice import voiceprint


# ── extract_embedding:真实模型冒烟测试,不 mock ────────────────────────────
def test_extract_embedding_returns_unit_vector_for_real_audio():
    sr = 16000
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    wav = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    pcm = (wav * 32767).astype(np.int16).tobytes()

    embedding = voiceprint.extract_embedding(pcm)

    assert embedding is not None
    assert embedding.shape == (256,)
    assert abs(float(np.linalg.norm(embedding)) - 1.0) < 1e-3


def test_extract_embedding_returns_none_for_too_short_audio():
    pcm = np.zeros(100, dtype=np.int16).tobytes()  # 远小于 0.5 秒
    assert voiceprint.extract_embedding(pcm) is None


def test_extract_embedding_returns_none_for_empty_audio():
    assert voiceprint.extract_embedding(b"") is None


# ── cosine_similarity ────────────────────────────────────────────────────
def test_cosine_similarity_identical_vectors():
    a = np.array([1.0, 2.0, 3.0])
    assert abs(voiceprint.cosine_similarity(a, a) - 1.0) < 1e-9


def test_cosine_similarity_orthogonal_vectors():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert abs(voiceprint.cosine_similarity(a, b)) < 1e-9


# ── match_score ──────────────────────────────────────────────────────────
def test_match_score_none_when_profile_empty():
    assert voiceprint.match_score(np.array([1.0, 0.0]), voiceprint.VoiceProfile()) is None


def test_match_score_high_for_similar_embedding():
    profile = voiceprint.VoiceProfile(embeddings=[np.array([1.0, 0.0]), np.array([1.0, 0.0])])
    score = voiceprint.match_score(np.array([1.0, 0.0]), profile)
    assert score > 0.99


def test_match_score_low_for_dissimilar_embedding():
    profile = voiceprint.VoiceProfile(embeddings=[np.array([1.0, 0.0])])
    score = voiceprint.match_score(np.array([0.0, 1.0]), profile)
    assert score < 0.01


# ── update_profile:防污染规则 ─────────────────────────────────────────────
def test_update_profile_accepts_first_sample_unconditionally():
    profile = voiceprint.VoiceProfile()
    updated = voiceprint.update_profile(profile, np.array([1.0, 0.0]), threshold=0.9)
    assert len(updated) == 1


def test_update_profile_rejects_mismatched_sample():
    profile = voiceprint.VoiceProfile(embeddings=[np.array([1.0, 0.0])])
    mismatched = np.array([0.0, 1.0])
    updated = voiceprint.update_profile(profile, mismatched, threshold=0.5)
    assert len(updated) == 1  # 没被采纳,还是原来那一条


def test_update_profile_accepts_matching_sample():
    profile = voiceprint.VoiceProfile(embeddings=[np.array([1.0, 0.0])])
    matching = np.array([1.0, 0.0])
    updated = voiceprint.update_profile(profile, matching, threshold=0.5)
    assert len(updated) == 2


def test_update_profile_caps_at_max_samples():
    profile = voiceprint.VoiceProfile(embeddings=[np.array([1.0, 0.0])] * 20)
    updated = voiceprint.update_profile(profile, np.array([1.0, 0.0]), threshold=0.5)
    assert len(updated) == 20  # 封顶,不是无限增长


# ── 声纹参照持久化:存盘/读盘往返 ───────────────────────────────────────────
def test_save_and_load_profile_roundtrip(isolated):
    profile = voiceprint.VoiceProfile(
        embeddings=[
            np.array([1.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
        ]
    )
    voiceprint.save_profile(profile)
    loaded = voiceprint.load_profile()
    assert len(loaded) == 2


def test_load_profile_empty_when_no_file(isolated):
    loaded = voiceprint.load_profile()
    assert len(loaded) == 0


def test_save_profile_noop_for_empty_profile(isolated):
    voiceprint.save_profile(voiceprint.VoiceProfile())
    assert not voiceprint._profile_path().exists()
