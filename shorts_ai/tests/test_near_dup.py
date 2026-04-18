"""Near-dup signal math and confirm_match thresholding.

Pure-function coverage; no DB. Cluster assignment itself needs Postgres
(pgvector HNSW search) and is exercised by integration tests at deploy time.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ingestor import near_dup


@dataclass
class FakeVA:
    video_id: int
    phash_first: int | None = None
    phash_mid: int | None = None
    audio_chromaprint: str | None = None
    visual_embedding: list | None = None
    text_embedding: list | None = None


def test_hamming64_detects_tiny_bit_flip():
    # Flipping one bit = distance 1.
    assert near_dup._hamming64(0, 1) == 1
    # Flipping 5 bits = distance 5.
    assert near_dup._hamming64(0, 0b11111) == 5


def test_hamming64_handles_none():
    assert near_dup._hamming64(None, 1) is None
    assert near_dup._hamming64(1, None) is None


def test_cosine_unit_identical_is_zero():
    v = [1.0, 0.0, 0.0]
    assert near_dup._cosine_unit(v, v) == 0.0


def test_cosine_unit_orthogonal_is_one():
    assert abs(near_dup._cosine_unit([1.0, 0.0], [0.0, 1.0]) - 1.0) < 1e-9


def test_cosine_unit_handles_zero_vector():
    assert near_dup._cosine_unit([0, 0, 0], [1, 0, 0]) is None


def test_confirm_requires_two_strong_signals():
    # Same pHash but nothing else — 1 signal, not enough.
    a = FakeVA(1, phash_first=0b0)
    b = FakeVA(2, phash_first=0b1)
    assert near_dup.compare(a, b).confirming_count() == 1
    assert near_dup.confirm_match(a, b) is False


def test_confirm_with_phash_and_audio_match():
    a = FakeVA(1, phash_first=0b0, audio_chromaprint="fp")
    b = FakeVA(2, phash_first=0b1, audio_chromaprint="fp")
    assert near_dup.confirm_match(a, b) is True


def test_confirm_with_phash_and_visual():
    a = FakeVA(1, phash_first=0, visual_embedding=[1.0, 0.0, 0.0])
    b = FakeVA(2, phash_first=1, visual_embedding=[0.99, 0.1, 0.0])
    assert near_dup.confirm_match(a, b) is True


def test_confirm_with_visual_and_text():
    v = [1.0, 0.0, 0.0]
    v2 = [0.99, 0.01, 0.0]
    a = FakeVA(1, visual_embedding=v, text_embedding=v)
    b = FakeVA(2, visual_embedding=v2, text_embedding=v2)
    assert near_dup.confirm_match(a, b) is True


def test_unrelated_videos_do_not_confirm():
    a = FakeVA(
        1,
        phash_first=0xDEADBEEFCAFEBABE,
        audio_chromaprint="fp-a",
        visual_embedding=[1.0, 0.0, 0.0],
        text_embedding=[1.0, 0.0, 0.0],
    )
    b = FakeVA(
        2,
        phash_first=0x1111111122222222,
        audio_chromaprint="fp-b",
        visual_embedding=[0.0, 1.0, 0.0],
        text_embedding=[0.0, 1.0, 0.0],
    )
    assert near_dup.confirm_match(a, b) is False


def test_same_audio_alone_is_not_enough():
    """Music reels with the same track aren't necessarily the same video."""
    a = FakeVA(1, audio_chromaprint="fp")
    b = FakeVA(2, audio_chromaprint="fp")
    assert near_dup.compare(a, b).confirming_count() == 1
    assert near_dup.confirm_match(a, b) is False
