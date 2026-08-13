"""Offline E5 embedding and BGE reranking adapters for the recall demo."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.model_store import (
    InstalledModels,
    ModelStoreError,
    load_installed_models,
)


class EmbeddingRuntime(Protocol):
    dimension: int

    def embed_query(self, text: str) -> tuple[float, ...]: ...

    def embed_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]: ...


class RerankerRuntime(Protocol):
    def score(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]: ...


@dataclass(frozen=True)
class ModelRuntimeBundle:
    profile_digest: str
    embedding: EmbeddingRuntime
    reranker: RerankerRuntime


class DemoRuntimeError(RuntimeError):
    """A sanitized local model runtime validation failure."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_transformers_runtime(
    profile: DemoRetrievalProfile,
    installed: InstalledModels,
    *,
    device: str | None = None,
) -> ModelRuntimeBundle:
    """Load both verified snapshots with networking and remote code disabled."""

    verified = _revalidate_installed(profile, installed)
    try:
        import torch
        from transformers import (
            AutoModel,
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None

    if device:
        selected_device = device
    else:
        try:
            mps_available = torch.backends.mps.is_available()
        except DemoRuntimeError:
            raise
        except Exception:
            raise DemoRuntimeError("runtime_load_failed") from None
        selected_device = "mps" if mps_available else "cpu"
    local_only = {"local_files_only": True, "trust_remote_code": False}

    embedding_path = str(verified.embedding_path)
    try:
        embedding_tokenizer = AutoTokenizer.from_pretrained(
            embedding_path, **local_only
        )
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None
    try:
        embedding_model = AutoModel.from_pretrained(embedding_path, **local_only)
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None
    try:
        embedding_model = embedding_model.to(selected_device).eval()
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None

    reranker_path = str(verified.reranker_path)
    try:
        reranker_tokenizer = AutoTokenizer.from_pretrained(
            reranker_path, **local_only
        )
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None
    try:
        reranker_model = AutoModelForSequenceClassification.from_pretrained(
            reranker_path, **local_only
        )
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None
    try:
        reranker_model = reranker_model.to(selected_device).eval()
    except DemoRuntimeError:
        raise
    except Exception:
        raise DemoRuntimeError("runtime_load_failed") from None

    return ModelRuntimeBundle(
        profile_digest=profile.digest,
        embedding=_E5Runtime(
            torch=torch,
            tokenizer=embedding_tokenizer,
            model=embedding_model,
            device=selected_device,
            max_tokens=profile.embedding.max_tokens,
            dimension=profile.embedding_dimension,
        ),
        reranker=_BgeRerankerRuntime(
            torch=torch,
            tokenizer=reranker_tokenizer,
            model=reranker_model,
            device=selected_device,
            max_tokens=profile.reranker.max_tokens,
        ),
    )


class _E5Runtime:
    def __init__(
        self,
        *,
        torch,
        tokenizer,
        model,
        device: str,
        max_tokens: int,
        dimension: int,
    ) -> None:
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._device = device
        self._max_tokens = max_tokens
        self.dimension = dimension

    def embed_query(self, text: str) -> tuple[float, ...]:
        vectors = self._embed((f"query: {text}",))
        return vectors[0]

    def embed_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        return self._embed(tuple(f"passage: {text}" for text in texts))

    def _embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        encoded = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self._max_tokens,
            return_tensors="pt",
        )
        model_inputs = {
            name: tensor.to(self._device) for name, tensor in encoded.items()
        }
        with self._torch.inference_mode():
            output = self._model(**model_inputs)
            hidden = output.last_hidden_state
            attention = model_inputs["attention_mask"].unsqueeze(-1)
            attention = attention.expand(hidden.size()).float()
            pooled = (hidden * attention).sum(dim=1) / attention.sum(dim=1).clamp(
                min=1e-9
            )
            normalized = self._torch.nn.functional.normalize(pooled, p=2, dim=1)
        values = tuple(
            tuple(float(value) for value in row)
            for row in normalized.detach().cpu().tolist()
        )
        if len(values) != len(texts) or any(
            not _valid_embedding(vector, self.dimension) for vector in values
        ):
            raise DemoRuntimeError("embedding_output_invalid")
        return values


class _BgeRerankerRuntime:
    def __init__(
        self,
        *,
        torch,
        tokenizer,
        model,
        device: str,
        max_tokens: int,
    ) -> None:
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._device = device
        self._max_tokens = max_tokens

    def score(self, query: str, documents: tuple[str, ...]) -> tuple[float, ...]:
        if not documents:
            return ()
        encoded = self._tokenizer(
            [query] * len(documents),
            list(documents),
            padding=True,
            truncation=True,
            max_length=self._max_tokens,
            return_tensors="pt",
        )
        model_inputs = {
            name: tensor.to(self._device) for name, tensor in encoded.items()
        }
        with self._torch.inference_mode():
            output = self._model(**model_inputs)
            logits = output.logits.reshape(-1).detach().cpu().tolist()
        scores = tuple(float(value) for value in logits)
        if len(scores) != len(documents) or any(
            not math.isfinite(value) for value in scores
        ):
            raise DemoRuntimeError("reranker_output_invalid")
        return scores


def _valid_embedding(vector: tuple[float, ...], dimension: int) -> bool:
    if len(vector) != dimension or any(not math.isfinite(value) for value in vector):
        return False
    norm = math.sqrt(sum(value * value for value in vector))
    return math.isfinite(norm) and abs(norm - 1.0) <= 1e-4


def _revalidate_installed(
    profile: DemoRetrievalProfile, installed: InstalledModels
) -> InstalledModels:
    paths = (
        installed.embedding_path,
        installed.reranker_path,
        installed.install_manifest_path,
    )
    if installed.profile_digest != profile.digest or any(
        not path.is_absolute() for path in paths
    ):
        raise DemoRuntimeError("installed_models_invalid")
    install_root = installed.embedding_path.parent
    if (
        installed.embedding_path.name != "embedding"
        or installed.reranker_path != install_root / "reranker"
        or installed.install_manifest_path != install_root / "model-install.json"
        or install_root.parent.name != "installs"
        or install_root.parent.parent.name != "models"
        or install_root.parent.parent.parent.name != "recall-demo"
    ):
        raise DemoRuntimeError("installed_models_invalid")
    state_root = install_root.parent.parent.parent.parent
    try:
        revalidated = load_installed_models(profile, state_root)
    except ModelStoreError:
        raise DemoRuntimeError("installed_models_invalid") from None
    if revalidated != installed:
        raise DemoRuntimeError("installed_models_invalid")
    return revalidated
