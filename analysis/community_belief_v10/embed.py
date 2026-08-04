"""로컬 임베딩. 유료 API를 쓰지 않는다.

intfloat/multilingual-e5-small을 CPU에서 돌린다. 모델 가중치는 Task 1에서
1회 내려받았고, 이후에는 HF_HUB_OFFLINE=1로 네트워크를 쓰지 않는다.

belief 텍스트는 차원별로 변하지 않으면 이전 문장이 그대로 유지되므로
(2026-07-31 규칙 변경) 중복이 매우 많다. 텍스트 해시로 중복을 제거해
같은 문장을 두 번 계산하지 않는다.
"""

import hashlib
from pathlib import Path

import numpy as np

import guard

guard.enforce_no_paid_api(offline=True)
guard.assert_no_openai_package()

import paths

MODEL_NAME = "intfloat/multilingual-e5-small"
PREFIX = "query: "
CACHE_DIR = paths.OUT / "embed_cache"
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer(MODEL_NAME, device="cpu")
    return _MODEL


def text_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def encode(texts: list, cache_name: str) -> np.ndarray:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = Path(CACHE_DIR) / f"{cache_name}.npz"

    cache = {}
    if cache_file.exists():
        # allow_pickle은 쓰지 않는다. keys는 문자열 배열, vectors는 float 배열이라
        # 순수 npz로 충분하며, pickle 역직렬화 경로를 열어둘 이유가 없다.
        stored = np.load(cache_file)
        cache = {str(k): v for k, v in zip(stored["keys"], stored["vectors"])}

    keys = [text_key(t) for t in texts]
    missing = sorted({k for k in keys if k not in cache})
    if missing:
        key_to_text = {}
        for key, text in zip(keys, texts):
            key_to_text.setdefault(key, text)
        batch = [PREFIX + key_to_text[k] for k in missing]
        vectors = _model().encode(
            batch, batch_size=64, convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=True)
        for key, vector in zip(missing, vectors):
            cache[key] = vector
        np.savez_compressed(
            cache_file,
            keys=np.array(list(cache.keys())),
            vectors=np.vstack(list(cache.values())))

    return np.vstack([cache[k] for k in keys])
