import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import embed


def test_text_key_is_stable_and_short():
    assert embed.text_key("가나다") == embed.text_key("가나다")
    assert embed.text_key("가나다") != embed.text_key("가나라")
    assert len(embed.text_key("가나다")) == 16


def test_encode_returns_normalized_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    vectors = embed.encode(["삼성전자 주가가 올랐다", "메모리 업황이 좋다"], "t1")
    assert vectors.shape == (2, 384)
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_duplicate_texts_get_identical_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    vectors = embed.encode(["같은 문장", "다른 문장", "같은 문장"], "t2")
    assert np.allclose(vectors[0], vectors[2])


def test_cache_reuse_avoids_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    first = embed.encode(["캐시 테스트"], "t3")
    assert (tmp_path / "t3.npz").exists()

    def _model_should_not_be_called():
        raise AssertionError(
            "encode() invoked the model on a cache hit; "
            "cache-lookup path is broken")

    monkeypatch.setattr(embed, "_model", _model_should_not_be_called)

    second = embed.encode(["캐시 테스트"], "t3")
    assert np.allclose(first, second)


def test_duplicates_reach_model_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE_DIR", tmp_path)
    calls = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            calls.append(list(texts))
            return np.tile(
                np.arange(384, dtype=np.float32), (len(texts), 1))

    monkeypatch.setattr(embed, "_model", lambda: FakeModel())

    vectors = embed.encode(["A", "B", "A"], "t4")

    assert len(calls) == 1, "model should be invoked exactly once for the batch"
    assert set(calls[0]) == {embed.PREFIX + "A", embed.PREFIX + "B"}
    assert len(calls[0]) == 2, "duplicate 'A' must not reach the model twice"
    assert vectors.shape == (3, 384)
    assert np.allclose(vectors[0], vectors[2])
