"""Embedding functions. An embedder turns a list of texts into a list of vectors."""

import hashlib
import math
import re
from collections.abc import Callable

Embedder = Callable[[list[str]], list[list[float]]]

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def local_embedder() -> Embedder:
    """all-MiniLM-L6-v2 running on CPU through ONNX. Downloads about 80 MB on first use."""
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    model = ONNXMiniLM_L6_V2()
    return lambda texts: [[float(x) for x in vector] for vector in model(texts)]


def hashing_embedder(dimensions: int = 256) -> Embedder:
    """Deterministic bag-of-words vectors. No model and no download: used in tests and CI."""

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * dimensions
            for token in tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "little") % dimensions] += 1.0
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
        return vectors

    return embed
