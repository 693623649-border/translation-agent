"""Retrieval runtime and embedding sidecars for published knowledge bases.

``knowledge_base.jsonl`` remains the canonical, verifier-owned document corpus.
This module adds the two missing RAG layers without changing that stable schema:

* a small discovery manifest with a lexical retrieval fallback;
* an optional vector sidecar produced by an injected embedding provider.

No network client is embedded here.  A future API adapter only needs to
implement :class:`EmbeddingProvider`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol


RAG_SCHEMA_VERSION = 1
RAG_MANIFEST_KIND = "translation-agent.rag-knowledge-base"
RAG_INDEX_KIND = "translation-agent.rag-embedding-index"
KNOWLEDGE_FIELDS = (
    "id",
    "title",
    "chapter_id",
    "chapter_order",
    "content",
)
_KNOWLEDGE_FIELD_SET = frozenset(KNOWLEDGE_FIELDS)
_ROW_ID = re.compile(r"[0-9a-f]{40}")
_TOKEN_PART = re.compile(
    r"[a-z0-9_]+|"
    r"[\u3400-\u9fff]+|"
    r"[\u3040-\u30ff]+|"
    r"[\uac00-\ud7af]+",
    flags=re.I,
)


class RagError(ValueError):
    """Base class for invalid RAG corpora, indexes, or provider output."""


class RagFormatError(RagError):
    """The canonical corpus or one of its sidecars is malformed."""


class RagIndexStaleError(RagError):
    """A sidecar was built from different knowledge-base bytes."""


class RagEmbeddingUnavailableError(RagError):
    """Semantic retrieval was requested before a vector index was built."""


class RagProviderError(RagError):
    """An embedding provider returned invalid or incompatible data."""


class EmbeddingProvider(Protocol):
    """Vendor-neutral seam for the embedding API supplied later.

    ``provider_name`` and ``model`` are persisted with the vectors so a query
    cannot silently mix embeddings from incompatible providers or models.
    """

    provider_name: str
    model: str

    def embed_documents(
        self,
        texts: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        """Embed a batch of document texts in the same order."""

    def embed_query(self, text: str) -> Sequence[float]:
        """Embed one retrieval query."""


def _is_embedding_param_error(exc: BaseException) -> bool:
    """Detect per-request payload rejections (Zhipu code 1210 and peers)."""

    if isinstance(exc, RagProviderError):
        message = str(exc)
        status = getattr(exc.__cause__, "status_code", None)
        code = getattr(exc.__cause__, "code", None)
    else:
        message = str(exc)
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
    if status == 400:
        return True
    if isinstance(code, (int, str)) and str(code) == "1210":
        return True
    return "1210" in message or "Error code: 400" in message


class ZhipuEmbeddingProvider:
    """智谱 ``embedding-3`` adapter through its OpenAI-compatible API."""

    provider_name = "zhipu"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4/"
    supported_dimensions = frozenset({256, 512, 1024, 2048})
    max_batch_size = 64
    min_shrink_chars = 256
    shrink_factor = 0.8

    def __init__(
        self,
        *,
        api_key_env: str = "ZHIPU_API_KEY",
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        timeout: float = 60.0,
        client: Any | None = None,
    ) -> None:
        self.api_key_env = api_key_env
        self.base_url = (
            base_url
            or os.getenv("ZHIPU_EMBEDDING_BASE_URL")
            or self.default_base_url
        ).rstrip("/") + "/"
        self.model = (
            model
            or os.getenv("ZHIPU_EMBEDDING_MODEL")
            or "embedding-3"
        )
        configured_dimensions: object = dimensions
        if configured_dimensions is None:
            configured_dimensions = os.getenv(
                "ZHIPU_EMBEDDING_DIMENSIONS",
                "2048",
            )
        try:
            self.dimensions = int(configured_dimensions)
        except (TypeError, ValueError) as exc:
            raise ValueError("Zhipu embedding dimensions must be an integer") from exc
        if self.dimensions not in self.supported_dimensions:
            raise ValueError(
                "Zhipu embedding-3 dimensions must be one of "
                f"{sorted(self.supported_dimensions)}"
            )
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        self.timeout = float(timeout)
        self._client = client

    def _openai_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = os.getenv(self.api_key_env, "").strip()
        if not api_key:
            raise RagProviderError(
                f"Missing Zhipu API key; set the {self.api_key_env} environment variable"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - installation boundary.
            raise RagProviderError(
                "Zhipu embeddings require openai>=1; install translation-agent[legacy]"
            ) from exc
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )
        return self._client

    def _embed_request(self, values: list[str]) -> list[tuple[float, ...]]:
        try:
            response = self._openai_client().embeddings.create(
                model=self.model,
                input=values,
                dimensions=self.dimensions,
            )
        except RagProviderError:
            raise
        except Exception as exc:
            raise RagProviderError(f"Zhipu embedding request failed: {exc}") from exc
        data = getattr(response, "data", None)
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise RagProviderError("Zhipu embedding response has no data array")
        by_index: dict[int, tuple[float, ...]] = {}
        for item in data:
            index = getattr(item, "index", None)
            embedding = getattr(item, "embedding", None)
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(values)
                or index in by_index
            ):
                raise RagProviderError("Zhipu embedding response contains invalid indices")
            by_index[index] = _validate_vector(
                embedding,
                expected_dimensions=self.dimensions,
                label=f"Zhipu embedding {index}",
            )
        if list(sorted(by_index)) != list(range(len(values))):
            raise RagProviderError(
                "Zhipu embedding response count does not match the request"
            )
        return [by_index[index] for index in range(len(values))]

    def _embed_single(self, text: str) -> tuple[float, ...]:
        """Embed one document, shrinking token-dense inputs that exceed the
        API's token budget.  Truncation keeps the one-vector-per-row mapping;
        the unrepresented tail only weakens that row's retrieval signal."""

        current = text
        while True:
            try:
                return self._embed_request([current])[0]
            except RagProviderError as exc:
                if (
                    not _is_embedding_param_error(exc)
                    or len(current) <= self.min_shrink_chars
                ):
                    raise
                current = current[: int(len(current) * self.shrink_factor)]

    def _embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        values = list(texts)
        if not values:
            raise RagProviderError("Zhipu embedding input cannot be empty")
        if len(values) > self.max_batch_size:
            raise RagProviderError(
                f"Zhipu embedding-3 accepts at most {self.max_batch_size} inputs per request"
            )
        if any(not isinstance(text, str) or not text.strip() for text in values):
            raise RagProviderError("Zhipu embedding inputs must be non-empty strings")
        try:
            return self._embed_request(values)
        except RagProviderError as exc:
            # One oversized document poisons the whole batch (e.g. an
            # ASCII-heavy index chunk over the token budget); embed the
            # remainder individually so a single bad chunk cannot fail
            # an otherwise valid batch.
            if len(values) == 1 or not _is_embedding_param_error(exc):
                raise
        return [self._embed_single(text) for text in values]

    def embed_documents(
        self,
        texts: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> Sequence[float]:
        return self._embed([text])[0]


@dataclass(frozen=True)
class RagEmbeddingMetadata:
    provider_name: str
    model: str
    dimensions: int
    chunk_count: int
    documents_sha256: str
    index_sha256: str
    index_path: Path


@dataclass(frozen=True)
class RagHit:
    id: str
    title: str
    chapter_id: str
    chapter_order: int
    content: str
    score: float
    retrieval_mode: str
    book: str = ""
    channels: str = ""
    source_content: str = ""
    duplicate_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RagContext:
    query: str
    hits: tuple[RagHit, ...]
    text: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class Reranker(Protocol):
    def rerank(self, query: str, hits: Sequence[RagHit]) -> Sequence[RagHit]: ...


def manifest_path_for(knowledge_base_path: Path | str) -> Path:
    path = Path(knowledge_base_path)
    return path.with_suffix(".rag.json")


def vector_index_path_for(knowledge_base_path: Path | str) -> Path:
    path = Path(knowledge_base_path)
    return path.with_suffix(".vectors.jsonl")


def metadata_sidecar_path_for(knowledge_base_path: Path | str) -> Path:
    """Optional per-chunk metadata sidecar path (book/author/language)."""

    path = Path(knowledge_base_path)
    return path.with_suffix(".meta.jsonl")


def load_metadata_sidecar(
    knowledge_base_path: Path | str,
) -> dict[str, dict[str, str]]:
    """Load the optional ``knowledge_base.meta.jsonl`` sidecar.

    The five-field corpus contract stays untouched; routing metadata
    (``book_id``, ``book_title``, ``author``, ``language``, ...) lives beside
    it keyed by chunk id.  Unknown fields are preserved; a missing file is a
    valid empty sidecar.
    """

    path = metadata_sidecar_path_for(knowledge_base_path)
    if not path.is_file():
        return {}
    sidecar: dict[str, dict[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RagFormatError(f"Cannot read metadata sidecar {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RagFormatError(
                f"Invalid JSON at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
            raise RagFormatError(
                f"Invalid sidecar row at {path}:{line_number}; "
                "expected an object with a non-empty 'id'"
            )
        row_id = str(raw["id"])
        if _ROW_ID.fullmatch(row_id) is None:
            raise RagFormatError(f"Invalid sidecar row id at {path}:{line_number}")
        if row_id in sidecar:
            raise RagFormatError(
                f"Duplicate sidecar row id at {path}:{line_number}: {row_id}"
            )
        sidecar[row_id] = {
            str(key): str(value) for key, value in raw.items()
        }
    return sidecar


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def load_knowledge_rows(path: Path | str) -> list[dict[str, Any]]:
    """Load and validate the verifier-owned five-field JSONL corpus."""

    source = Path(path)
    if not source.is_file():
        raise RagFormatError(f"Knowledge base does not exist: {source}")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RagFormatError(f"Cannot read knowledge base {source}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RagFormatError(
                f"Invalid JSON at {source}:{line_number}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != _KNOWLEDGE_FIELD_SET:
            actual = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
            raise RagFormatError(
                f"Invalid fields at {source}:{line_number}; "
                f"expected {list(KNOWLEDGE_FIELDS)}, got {actual}"
            )
        row_id = raw["id"]
        chapter_order = raw["chapter_order"]
        if not isinstance(row_id, str) or _ROW_ID.fullmatch(row_id) is None:
            raise RagFormatError(f"Invalid row id at {source}:{line_number}")
        if row_id in seen_ids:
            raise RagFormatError(f"Duplicate row id at {source}:{line_number}: {row_id}")
        for field in ("title", "chapter_id", "content"):
            if not isinstance(raw[field], str) or not raw[field].strip():
                raise RagFormatError(
                    f"Field {field!r} must be a non-empty string at "
                    f"{source}:{line_number}"
                )
        if not isinstance(chapter_order, int) or isinstance(chapter_order, bool):
            raise RagFormatError(
                f"Field 'chapter_order' must be an integer at "
                f"{source}:{line_number}"
            )
        seen_ids.add(row_id)
        rows.append({field: raw[field] for field in KNOWLEDGE_FIELDS})
    if not rows:
        raise RagFormatError(f"Knowledge base contains no chunks: {source}")
    return rows


def _pending_manifest(
    knowledge_base_path: Path,
    *,
    chunk_count: int,
    documents_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": RAG_SCHEMA_VERSION,
        "kind": RAG_MANIFEST_KIND,
        "documents": {
            "path": knowledge_base_path.name,
            "sha256": documents_sha256,
            "chunk_count": chunk_count,
            "fields": list(KNOWLEDGE_FIELDS),
        },
        "retrieval": {
            "lexical": {
                "status": "ready",
                "algorithm": "okapi-bm25",
            },
            "embedding": {
                "status": "awaiting_provider",
                "index_path": vector_index_path_for(knowledge_base_path).name,
                "provider": None,
                "model": None,
                "dimensions": None,
                "documents_sha256": None,
                "index_sha256": None,
            },
        },
    }


def _read_manifest_file(knowledge_base_path: Path) -> dict[str, Any]:
    path = manifest_path_for(knowledge_base_path)
    if not path.is_file():
        raise RagFormatError(f"RAG manifest does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RagFormatError(f"Cannot read RAG manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RagFormatError(f"RAG manifest must be a JSON object: {path}")
    return value


def _provider_identity(provider: EmbeddingProvider) -> tuple[str, str]:
    provider_name = getattr(provider, "provider_name", None)
    model = getattr(provider, "model", None)
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise RagProviderError("Embedding provider_name must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise RagProviderError("Embedding model must be a non-empty string")
    return provider_name.strip(), model.strip()


def _validate_vector(
    raw: Sequence[float],
    *,
    expected_dimensions: int | None = None,
    label: str,
) -> tuple[float, ...]:
    if isinstance(raw, (str, bytes)):
        raise RagProviderError(f"{label} is not a numeric vector")
    try:
        values = tuple(raw)
    except TypeError as exc:
        raise RagProviderError(f"{label} is not a sequence") from exc
    if not values:
        raise RagProviderError(f"{label} is empty")
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RagProviderError(f"{label} contains a non-numeric value")
        converted = float(value)
        if not math.isfinite(converted):
            raise RagProviderError(f"{label} contains a non-finite value")
        vector.append(converted)
    if expected_dimensions is not None and len(vector) != expected_dimensions:
        raise RagProviderError(
            f"{label} has {len(vector)} dimensions; expected {expected_dimensions}"
        )
    if not any(vector):
        raise RagProviderError(f"{label} is a zero vector")
    return tuple(vector)


def _load_embedding_index(
    knowledge_base_path: Path,
    *,
    expected_documents_sha256: str,
    expected_ids: Sequence[str],
) -> tuple[RagEmbeddingMetadata, dict[str, tuple[float, ...]]]:
    index_path = vector_index_path_for(knowledge_base_path)
    if not index_path.is_file():
        raise RagFormatError(f"Embedding index does not exist: {index_path}")
    try:
        lines = [
            line
            for line in index_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError) as exc:
        raise RagFormatError(f"Cannot read embedding index {index_path}: {exc}") from exc
    if not lines:
        raise RagFormatError(f"Embedding index is empty: {index_path}")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise RagFormatError(f"Invalid embedding index header: {index_path}") from exc
    if not isinstance(header, dict):
        raise RagFormatError(f"Embedding index header must be an object: {index_path}")
    if (
        header.get("schema_version") != RAG_SCHEMA_VERSION
        or header.get("kind") != RAG_INDEX_KIND
    ):
        raise RagFormatError(f"Unsupported embedding index schema: {index_path}")
    documents_sha256 = header.get("documents_sha256")
    if documents_sha256 != expected_documents_sha256:
        raise RagIndexStaleError(
            f"Embedding index was built from different knowledge-base bytes: {index_path}"
        )
    provider_name = header.get("provider")
    model = header.get("model")
    dimensions = header.get("dimensions")
    chunk_count = header.get("chunk_count")
    if not isinstance(provider_name, str) or not provider_name:
        raise RagFormatError(f"Embedding index provider is invalid: {index_path}")
    if not isinstance(model, str) or not model:
        raise RagFormatError(f"Embedding index model is invalid: {index_path}")
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0:
        raise RagFormatError(f"Embedding index dimensions are invalid: {index_path}")
    if chunk_count != len(expected_ids):
        raise RagFormatError(f"Embedding index chunk count is invalid: {index_path}")

    vectors: dict[str, tuple[float, ...]] = {}
    for line_number, line in enumerate(lines[1:], start=2):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RagFormatError(
                f"Invalid vector JSON at {index_path}:{line_number}"
            ) from exc
        if not isinstance(item, dict) or set(item) != {"id", "embedding"}:
            raise RagFormatError(
                f"Invalid vector row at {index_path}:{line_number}"
            )
        row_id = item["id"]
        if not isinstance(row_id, str) or row_id in vectors:
            raise RagFormatError(
                f"Invalid or duplicate vector id at {index_path}:{line_number}"
            )
        try:
            vectors[row_id] = _validate_vector(
                item["embedding"],
                expected_dimensions=dimensions,
                label=f"embedding at {index_path}:{line_number}",
            )
        except RagProviderError as exc:
            raise RagFormatError(str(exc)) from exc
    if list(vectors) != list(expected_ids):
        raise RagFormatError(
            f"Embedding index ids do not match the knowledge-base chunk order: {index_path}"
        )
    return (
        RagEmbeddingMetadata(
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            chunk_count=chunk_count,
            documents_sha256=documents_sha256,
            index_sha256=_sha256_file(index_path),
            index_path=index_path,
        ),
        vectors,
    )


def read_rag_manifest(knowledge_base_path: Path | str) -> dict[str, Any]:
    """Read a manifest and prove it still describes the current corpus/index."""

    source = Path(knowledge_base_path)
    rows = load_knowledge_rows(source)
    documents_sha256 = _sha256_file(source)
    manifest = _read_manifest_file(source)
    if (
        manifest.get("schema_version") != RAG_SCHEMA_VERSION
        or manifest.get("kind") != RAG_MANIFEST_KIND
    ):
        raise RagFormatError(f"Unsupported RAG manifest schema: {manifest_path_for(source)}")
    documents = manifest.get("documents")
    retrieval = manifest.get("retrieval")
    if not isinstance(documents, dict) or not isinstance(retrieval, dict):
        raise RagFormatError(f"RAG manifest sections are invalid: {manifest_path_for(source)}")
    if (
        documents.get("path") != source.name
        or documents.get("sha256") != documents_sha256
        or documents.get("chunk_count") != len(rows)
        or documents.get("fields") != list(KNOWLEDGE_FIELDS)
    ):
        raise RagIndexStaleError(
            f"RAG manifest does not describe the current knowledge base: "
            f"{manifest_path_for(source)}"
        )
    lexical = retrieval.get("lexical")
    embedding = retrieval.get("embedding")
    if not isinstance(lexical, dict) or lexical.get("status") != "ready":
        raise RagFormatError(f"Lexical retrieval is not ready: {manifest_path_for(source)}")
    if not isinstance(embedding, dict):
        raise RagFormatError(f"Embedding manifest is invalid: {manifest_path_for(source)}")
    status = embedding.get("status")
    if status not in {"awaiting_provider", "ready"}:
        raise RagFormatError(f"Unknown embedding status {status!r}")
    if embedding.get("index_path") != vector_index_path_for(source).name:
        raise RagFormatError(f"Embedding index path is invalid: {manifest_path_for(source)}")
    if status == "ready":
        expected_index_sha256 = embedding.get("index_sha256")
        if (
            not isinstance(expected_index_sha256, str)
            or not expected_index_sha256
        ):
            raise RagFormatError(
                f"Embedding index checksum is invalid: {manifest_path_for(source)}"
            )
        index_path = vector_index_path_for(source)
        if not index_path.is_file():
            raise RagFormatError(f"Embedding index does not exist: {index_path}")
        actual_index_sha256 = _sha256_file(index_path)
        if actual_index_sha256 != expected_index_sha256:
            raise RagIndexStaleError(
                f"Embedding index checksum does not match the RAG manifest: "
                f"{index_path}"
            )
        metadata, _vectors = _load_embedding_index(
            source,
            expected_documents_sha256=documents_sha256,
            expected_ids=[str(row["id"]) for row in rows],
        )
        if (
            embedding.get("provider") != metadata.provider_name
            or embedding.get("model") != metadata.model
            or embedding.get("dimensions") != metadata.dimensions
            or embedding.get("documents_sha256") != metadata.documents_sha256
            or embedding.get("index_sha256") != metadata.index_sha256
        ):
            raise RagIndexStaleError(
                f"Embedding manifest and index disagree: {manifest_path_for(source)}"
            )
    return manifest


def rag_manifest_is_current(knowledge_base_path: Path | str) -> bool:
    try:
        read_rag_manifest(knowledge_base_path)
    except (OSError, RagError):
        return False
    return True


def initialize_rag_manifest(knowledge_base_path: Path | str) -> dict[str, Any]:
    """Create or refresh RAG discovery metadata after publishing JSONL.

    A ready embedding index is retained when the canonical JSONL bytes did not
    change.  Any corpus change marks semantic retrieval as awaiting the future
    provider while lexical retrieval remains immediately usable.
    """

    source = Path(knowledge_base_path)
    rows = load_knowledge_rows(source)
    documents_sha256 = _sha256_file(source)
    try:
        current = read_rag_manifest(source)
    except RagError:
        current = None
    if current is not None:
        return current
    manifest = _pending_manifest(
        source,
        chunk_count=len(rows),
        documents_sha256=documents_sha256,
    )
    _atomic_write_text(manifest_path_for(source), _json_text(manifest))
    return manifest


def _embedding_document(row: Mapping[str, Any]) -> str:
    return f"{row['title']}\n\n{row['content']}"


def build_embedding_index(
    knowledge_base_path: Path | str,
    provider: EmbeddingProvider,
    *,
    batch_size: int = 64,
) -> RagEmbeddingMetadata:
    """Embed all canonical chunks and atomically publish the vector sidecar."""

    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")
    source = Path(knowledge_base_path)
    manifest = initialize_rag_manifest(source)
    rows = load_knowledge_rows(source)
    documents_sha256 = _sha256_file(source)
    provider_name, model = _provider_identity(provider)
    existing_embedding = manifest["retrieval"]["embedding"]
    requested_dimensions = getattr(provider, "dimensions", None)
    if (
        existing_embedding["status"] == "ready"
        and existing_embedding["provider"] == provider_name
        and existing_embedding["model"] == model
        and (
            requested_dimensions is None
            or existing_embedding["dimensions"] == requested_dimensions
        )
    ):
        metadata, _vectors = _load_embedding_index(
            source,
            expected_documents_sha256=documents_sha256,
            expected_ids=[str(row["id"]) for row in rows],
        )
        return metadata
    cache_path = source.with_suffix(".embedding-cache.json")
    cache: dict[str, Any] = {}
    try:
        loaded_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if loaded_cache.get("provider") == provider_name and loaded_cache.get("model") == model and isinstance(loaded_cache.get("dimensions"), int) and (requested_dimensions is None or loaded_cache["dimensions"] == requested_dimensions):
            payload = loaded_cache.get("vectors", {})
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            if digest == loaded_cache.get("sha256"):
                cache = loaded_cache
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    dimensions: int | None = requested_dimensions or cache.get("dimensions")
    by_digest: dict[str, tuple[float, ...]] = {}
    for digest, vector in cache.get("vectors", {}).items():
        try:
            by_digest[digest] = _validate_vector(vector, expected_dimensions=dimensions, label="cached embedding")
        except RagError:
            continue
    texts = [_embedding_document(row) for row in rows]
    digests = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    missing = list(dict.fromkeys(digest for digest in digests if digest not in by_digest))
    text_by_digest = dict(zip(digests, texts))
    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        try:
            raw_vectors = list(provider.embed_documents([text_by_digest[digest] for digest in batch]))
        except Exception as exc:
            raise RagProviderError(f"Embedding provider failed for document batch {start // batch_size + 1}: {exc}") from exc
        if len(raw_vectors) != len(batch):
            raise RagProviderError(f"Embedding provider returned {len(raw_vectors)} vectors for {len(batch)} documents")
        for digest, raw_vector in zip(batch, raw_vectors, strict=True):
            vector = _validate_vector(raw_vector, expected_dimensions=dimensions, label="document embedding")
            if dimensions is None:
                dimensions = len(vector)
            by_digest[digest] = vector
    vectors = [by_digest[digest] for digest in digests]
    cache_vectors = {digest: list(by_digest[digest]) for digest in set(digests)}
    _atomic_write_text(cache_path, _json_text({"provider": provider_name, "model": model, "dimensions": dimensions, "vectors": cache_vectors, "sha256": hashlib.sha256(json.dumps(cache_vectors, sort_keys=True).encode()).hexdigest()}))
    if dimensions is None:
        raise RagProviderError("Embedding provider produced no vectors")

    index_path = vector_index_path_for(source)
    header = {
        "schema_version": RAG_SCHEMA_VERSION,
        "kind": RAG_INDEX_KIND,
        "documents_sha256": documents_sha256,
        "provider": provider_name,
        "model": model,
        "dimensions": dimensions,
        "chunk_count": len(rows),
    }
    index_lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    index_lines.extend(
        json.dumps(
            {"id": row["id"], "embedding": list(vector)},
            ensure_ascii=False,
            sort_keys=True,
        )
        for row, vector in zip(rows, vectors, strict=True)
    )
    _atomic_write_text(index_path, "\n".join(index_lines) + "\n")
    index_sha256 = _sha256_file(index_path)

    manifest = _pending_manifest(
        source,
        chunk_count=len(rows),
        documents_sha256=documents_sha256,
    )
    manifest["retrieval"]["embedding"] = {
        "status": "ready",
        "index_path": index_path.name,
        "provider": provider_name,
        "model": model,
        "dimensions": dimensions,
        "documents_sha256": documents_sha256,
        "index_sha256": index_sha256,
    }
    _atomic_write_text(manifest_path_for(source), _json_text(manifest))
    return RagEmbeddingMetadata(
        provider_name=provider_name,
        model=model,
        dimensions=dimensions,
        chunk_count=len(rows),
        documents_sha256=documents_sha256,
        index_sha256=index_sha256,
        index_path=index_path,
    )


def zhipu_embedding_enabled(
    requested: bool | None,
    *,
    api_key_env: str = "ZHIPU_API_KEY",
) -> bool:
    """Resolve explicit enable/disable or automatic key-based activation."""

    if requested is not None:
        return requested
    return bool(os.getenv(api_key_env, "").strip())


def maybe_build_zhipu_embedding_index(
    knowledge_base_path: Path | str,
    *,
    requested: bool | None = None,
    batch_size: int = 64,
) -> RagEmbeddingMetadata | None:
    """Build an idempotent embedding-3 index when configured or requested."""

    if not zhipu_embedding_enabled(requested):
        return None
    return build_embedding_index(
        knowledge_base_path,
        ZhipuEmbeddingProvider(),
        batch_size=batch_size,
    )


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


_ROUTING_VARIANTS = str.maketrans(
    {
        "與": "与",
        "國": "国",
        "學": "学",
        "體": "体",
        "書": "书",
        "臺": "台",
        "宮": "宫",
        "終": "终",
        "爭": "争",
        "論": "论",
    }
)


def _normalized_route_text(value: str) -> str:
    """Normalize stable book/author labels for conservative query routing."""

    normalized = _normalized_text(value).translate(_ROUTING_VARIANTS)
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _book_route_aliases(value: str) -> set[str]:
    normalized = _normalized_route_text(value)
    aliases = {normalized} if len(normalized) >= 4 else set()
    if normalized.endswith("论") and len(normalized) > 4:
        aliases.add(normalized[:-1])
    without_particle = normalized.replace("的", "")
    if len(without_particle) >= 4:
        aliases.add(without_particle)
    return aliases


def _author_route_aliases(value: str) -> set[str]:
    names = re.split(r"[、,，;/；&]+", value)
    return {
        normalized
        for name in names
        if len(normalized := _normalized_route_text(name)) >= 2
    }


def _tokens(value: str) -> list[str]:
    output: list[str] = []
    for match in _TOKEN_PART.finditer(_normalized_text(value)):
        part = match.group(0)
        if part.isascii():
            output.append(part)
            continue
        characters = list(part)
        output.extend(characters)
        output.extend(
            "".join(characters[index : index + 2])
            for index in range(len(characters) - 1)
        )
    return output


def _lexical_scores(
    rows: Sequence[Mapping[str, Any]],
    query: str,
    cached_terms: Mapping[str, Counter] | None = None,
    statistics_cache: dict | None = None,
) -> list[float]:
    query_terms = Counter(_tokens(query))
    if not query_terms:
        return [0.0] * len(rows)
    document_terms = [
        cached_terms[str(row["id"])] if cached_terms is not None else Counter(_tokens(f"{row['title']} {row['title']} {row['content']}"))
        for row in rows
    ]
    cache_key = tuple(str(row["id"]) for row in rows)
    statistics = statistics_cache.get(cache_key) if statistics_cache is not None else None
    if statistics is None:
        document_lengths = [sum(terms.values()) for terms in document_terms]
        average_length = sum(document_lengths) / max(len(document_lengths), 1)
        document_frequency = Counter(term for terms in document_terms for term in terms)
        statistics = (document_lengths, average_length, document_frequency)
        if statistics_cache is not None:
            if len(statistics_cache) >= 16:
                statistics_cache.clear()
            statistics_cache[cache_key] = statistics
    document_lengths, average_length, document_frequency = statistics
    document_count = len(rows)
    k1 = 1.5
    b = 0.75
    compact_query = re.sub(r"\s+", "", _normalized_text(query))
    scores: list[float] = []
    for row, terms, length in zip(
        rows,
        document_terms,
        document_lengths,
        strict=True,
    ):
        score = 0.0
        for term, query_frequency in query_terms.items():
            frequency = terms.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (document_count - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            denominator = frequency + k1 * (
                1.0 - b + b * length / max(average_length, 1.0)
            )
            score += (
                inverse_document_frequency
                * frequency
                * (k1 + 1.0)
                / denominator
                * query_frequency
            )
        compact_document = re.sub(
            r"\s+",
            "",
            _normalized_text(f"{row['title']} {row['content']}"),
        )
        if compact_query and compact_query in compact_document:
            score += 1.0
        scores.append(score)
    return scores


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


class RagKnowledgeBase:
    """Validated corpus with lexical or embedding-backed retrieval."""

    def __init__(
        self,
        knowledge_base_path: Path,
        rows: Sequence[Mapping[str, Any]],
        *,
        manifest: Mapping[str, Any],
        embedding_metadata: RagEmbeddingMetadata | None,
        vectors: Mapping[str, Sequence[float]],
        metadata_sidecar: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.knowledge_base_path = knowledge_base_path
        self._rows = tuple(dict(row) for row in rows)
        self.manifest = dict(manifest)
        self.embedding_metadata = embedding_metadata
        self._vectors = {
            row_id: tuple(vector)
            for row_id, vector in vectors.items()
        }
        self._sidecar = dict(metadata_sidecar or {})
        self._lexical_statistics: dict = {}
        self._lexical_terms = {str(row["id"]): Counter(_tokens(f"{row['title']} {row['title']} {row['content']}")) for row in self._rows}

    @classmethod
    def open(cls, knowledge_base_path: Path | str) -> "RagKnowledgeBase":
        source = Path(knowledge_base_path)
        rows = load_knowledge_rows(source)
        manifest = read_rag_manifest(source)
        metadata_sidecar = load_metadata_sidecar(source)
        unknown_metadata_ids = set(metadata_sidecar) - {
            str(row["id"]) for row in rows
        }
        if unknown_metadata_ids:
            preview = ", ".join(sorted(unknown_metadata_ids)[:5])
            raise RagIndexStaleError(
                "Metadata sidecar contains ids absent from the knowledge base: "
                f"{preview}"
            )
        embedding = manifest["retrieval"]["embedding"]
        metadata: RagEmbeddingMetadata | None = None
        vectors: dict[str, tuple[float, ...]] = {}
        if embedding["status"] == "ready":
            metadata, vectors = _load_embedding_index(
                source,
                expected_documents_sha256=_sha256_file(source),
                expected_ids=[str(row["id"]) for row in rows],
            )
        return cls(
            source,
            rows,
            manifest=manifest,
            embedding_metadata=metadata,
            vectors=vectors,
            metadata_sidecar=metadata_sidecar,
        )

    @property
    def chunk_count(self) -> int:
        return len(self._rows)

    @property
    def embedding_ready(self) -> bool:
        return self.embedding_metadata is not None

    def chunk_metadata(self, row_id: str) -> dict[str, str]:
        """Return the sidecar metadata row for a chunk (empty when absent)."""

        return dict(self._sidecar.get(str(row_id), {}))

    def infer_query_routes(
        self,
        query: str,
    ) -> dict[str, frozenset[str]]:
        """Infer conservative book routes from explicit title/author mentions.

        Author matches are converted to their associated books. This keeps
        comparative queries inclusive: a query naming one book and a different
        author searches both source sets instead of intersecting them to zero.
        """

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        normalized_query = _normalized_route_text(query)
        matched_title_books: set[str] = set()
        matched_authors: set[str] = set()
        books_by_author: dict[str, set[str]] = {}
        seen_books: set[tuple[str, str]] = set()
        for row in self._rows:
            row_id = str(row["id"])
            metadata = self._sidecar.get(row_id, {})
            book = self._book_key(row)
            book_title = str(metadata.get("book_title") or book).strip()
            identity = (book, book_title)
            if identity not in seen_books:
                seen_books.add(identity)
                if any(
                    alias in normalized_query
                    for alias in _book_route_aliases(book_title)
                ):
                    matched_title_books.add(book)
            author = str(metadata.get("author") or "").strip()
            for alias in _author_route_aliases(author):
                books_by_author.setdefault(alias, set()).add(book)

        author_books: set[str] = set()
        for author_alias, books in books_by_author.items():
            if author_alias in normalized_query:
                matched_authors.add(author_alias)
                author_books.update(books)
        comparison_query = any(
            marker in normalized_query
            for marker in ("比较", "对比", "对照", "异同", "versus", "vs")
        )
        matched_books = set(matched_title_books)
        if not matched_title_books or comparison_query:
            matched_books.update(author_books)
        return {
            "book_ids": frozenset(matched_books),
            "authors": frozenset(matched_authors),
        }

    def _book_key(self, row: Mapping[str, Any]) -> str:
        """Resolve the owning book label used for routing and result caps.

        Sidecar ``book_title`` (or ``book_id``) wins; otherwise fall back to
        the aggregate convention of a ``chapter_id`` book prefix or a
        ``[书名]`` title prefix.
        """

        meta = self._sidecar.get(str(row["id"]), {})
        for field in ("book_title", "book_id"):
            value = str(meta.get(field) or "").strip()
            if value:
                return value
        chapter_id = str(row.get("chapter_id") or "")
        if ":" in chapter_id:
            prefix = chapter_id.split(":", 1)[0].strip()
            if prefix:
                return prefix
        title = str(row.get("title") or "")
        bracket = re.match(r"^\[([^\]]+)\]", title)
        if bracket:
            return bracket.group(1).strip()
        return chapter_id or "unknown-book"

    def _routing_allows(
        self,
        row: Mapping[str, Any],
        *,
        book_ids: set[str] | None,
        authors: set[str] | None,
        languages: set[str] | None,
    ) -> bool:
        if book_ids is not None:
            meta = self._sidecar.get(str(row["id"]), {})
            keys = {
                self._book_key(row),
                str(meta.get("book_id") or "").strip(),
                str(meta.get("book_title") or "").strip(),
            } - {""}
            if not keys & book_ids:
                return False
        if authors is not None or languages is not None:
            meta = self._sidecar.get(str(row["id"]), {})
            if authors is not None:
                author = str(meta.get("author") or "").strip()
                if not author or not any(
                    wanted and (wanted == author or wanted in author or author in wanted)
                    for wanted in authors
                ):
                    return False
            if languages is not None:
                language = str(meta.get("language") or "").strip()
                if language not in languages:
                    return False
        return True

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        chapter_ids: set[str] | frozenset[str] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        mode: str | None = "hybrid",
        book_ids: set[str] | frozenset[str] | None = None,
        authors: set[str] | frozenset[str] | None = None,
        languages: set[str] | frozenset[str] | None = None,
        per_book_cap: int | None = 3,
        candidate_depth: int = 60,
        auto_route: bool = True,
        aliases: Mapping[str, Sequence[str]] | None = None,
        reranker: Reranker | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> list[RagHit]:
        """Return ranked chunks using lexical, semantic, or hybrid ranking.

        ``mode=None`` keeps the legacy contract: BM25 without a provider and
        cosine similarity with one.  ``mode="hybrid"`` runs both channels,
        fuses them with Reciprocal Rank Fusion, labels each hit with the
        channels that recalled it, and degrades gracefully to pure lexical
        retrieval when no embedding index/provider is available.
        """

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if mode not in (None, "lexical", "semantic", "hybrid"):
            raise ValueError("mode must be 'lexical', 'semantic', or 'hybrid'")
        if candidate_depth <= 0:
            raise ValueError("candidate_depth must be positive")
        diag = diagnostics if diagnostics is not None else {}
        diag.clear()
        expansions = [query]
        for term, alternatives in (aliases or {}).items():
            if term and _normalized_text(term) in _normalized_text(query):
                for alternative in alternatives:
                    if isinstance(alternative, str) and alternative.strip():
                        expanded = re.sub(re.escape(term), lambda _: alternative.strip(), query, flags=re.IGNORECASE)
                        if expanded not in expansions and len(expansions) < 5:
                            expansions.append(expanded)
        lexical_query = " ".join(expansions)
        inferred = self.infer_query_routes(query) if auto_route and book_ids is None and authors is None else {"book_ids": frozenset()}
        diag.update(requested_mode=mode, query_variants=expansions, routing={"policy": "soft" if inferred["book_ids"] else "explicit" if book_ids is not None or authors is not None else "global", "preferred_books": sorted(inferred["book_ids"])}, fallback_reason=None)
        requested_top_k = top_k
        final_cap = per_book_cap
        top_k = max(top_k, candidate_depth)
        per_book_cap = None
        rows = [
            row
            for row in self._rows
            if chapter_ids is None or row["chapter_id"] in chapter_ids
        ]
        if book_ids is not None or authors is not None or languages is not None:
            rows = [
                row
                for row in rows
                if self._routing_allows(
                    row,
                    book_ids=set(book_ids) if book_ids is not None else None,
                    authors=set(authors) if authors is not None else None,
                    languages=set(languages) if languages is not None else None,
                )
            ]
        diag["filtered_count"] = len(rows)
        if not rows:
            diag.update(effective_mode="lexical" if embedding_provider is None else mode, candidate_count=0, result_count=0)
            return []
        if per_book_cap and len({self._book_key(row) for row in rows}) <= 1:
            per_book_cap = None

        effective_mode = mode
        if effective_mode is None:
            effective_mode = "semantic" if embedding_provider is not None else "lexical"

        semantic_scores: list[float] | None = None
        if effective_mode in ("semantic", "hybrid"):
            if embedding_provider is None:
                # Legacy graceful degradation: requesting semantic ranking
                # without an attached provider keeps keyword recall.
                effective_mode = "lexical"
                diag["fallback_reason"] = "embedding_provider_unavailable"
            elif self.embedding_metadata is None and effective_mode == "hybrid":
                effective_mode = "lexical"
                diag["fallback_reason"] = "embedding_index_unavailable"
            elif self.embedding_metadata is None:
                raise RagEmbeddingUnavailableError(
                    "Semantic retrieval requires an embedding index; "
                    "attach an EmbeddingProvider and build the index first"
                )
            else:
                metadata = self.embedding_metadata
                provider_name, model = _provider_identity(embedding_provider)
                if (
                    provider_name != metadata.provider_name
                    or model != metadata.model
                ):
                    raise RagProviderError(
                        "Query embedding provider/model does not match the stored index: "
                        f"expected {metadata.provider_name}/{metadata.model}, "
                        f"got {provider_name}/{model}"
                    )
                try:
                    raw_query_vector = embedding_provider.embed_query(query)
                except Exception as exc:
                    raise RagProviderError(f"Embedding provider failed for query: {exc}") from exc
                query_vector = _validate_vector(
                    raw_query_vector,
                    expected_dimensions=metadata.dimensions,
                    label="query embedding",
                )
                semantic_scores = [
                    _cosine_similarity(query_vector, self._vectors[str(row["id"])])
                    for row in rows
                ]

        if effective_mode == "hybrid" and semantic_scores is not None:
            if candidate_depth < top_k:
                raise ValueError(
                    "candidate_depth must be greater than or equal to top_k "
                    "for hybrid retrieval"
                )
            hits = self._hybrid_rank(
                rows,
                lexical_query,
                semantic_scores,
                top_k=top_k,
                candidate_depth=candidate_depth,
                per_book_cap=per_book_cap,
            )
        else:
            scores = (
                semantic_scores
                if semantic_scores is not None
                else _lexical_scores(rows, lexical_query, self._lexical_terms, self._lexical_statistics)
            )
            ranked = sorted(
                (
                    (score, row)
                    for score, row in zip(scores, rows, strict=True)
                    if semantic_scores is not None or score > 0.0
                ),
                key=lambda item: (
                    -item[0],
                    int(item[1]["chapter_order"]),
                    str(item[1]["id"]),
                ),
            )
            if per_book_cap:
                ranked = self._apply_per_book_cap(ranked, top_k, per_book_cap)
            else:
                ranked = ranked[:top_k]
            hits = [
                RagHit(
                    id=str(row["id"]),
                    title=str(row["title"]),
                    chapter_id=str(row["chapter_id"]),
                    chapter_order=int(row["chapter_order"]),
                    content=str(row["content"]),
                    score=float(score),
                    retrieval_mode=effective_mode,
                    book=self._book_key(row),
                )
                for score, row in ranked
            ]
        diag.update(effective_mode=effective_mode, candidate_count=len(hits), reranker=type(reranker).__name__ if reranker else None)
        deduplicated: dict[str, RagHit] = {}
        for hit in hits:
            key = re.sub(r"\s+", "", _normalized_text(hit.content))
            if key in deduplicated:
                first = deduplicated[key]
                deduplicated[key] = replace(first, duplicate_sources=first.duplicate_sources + (hit.id,))
            else:
                deduplicated[key] = hit
        hits = list(deduplicated.values())
        preferred = inferred["book_ids"]
        if preferred:
            hits.sort(key=lambda hit: -(hit.score * (1.15 if hit.book in preferred else 1.0)))
        if reranker is not None:
            original = {hit.id: hit for hit in hits}
            try:
                reordered = list(reranker.rerank(query, tuple(hits)))
            except Exception as exc:
                diag["reranker_fallback_reason"] = f"{type(exc).__name__}: {exc}"
                reordered = hits
            if len(reordered) != len(original) or {hit.id for hit in reordered} != set(original):
                raise RagProviderError("Reranker must return every candidate exactly once")
            hits = [replace(original[hit.id], score=float(hit.score)) for hit in reordered]
        if len({self._book_key(row) for row in rows}) <= 1:
            final_cap = None
        selected: list[RagHit] = []
        counts: Counter = Counter()
        for hit in hits:
            if final_cap and counts[hit.book] >= final_cap:
                continue
            selected.append(hit)
            counts[hit.book] += 1
            if len(selected) >= requested_top_k:
                break
        diag.update(deduplicated_count=len(hits), candidate_ids=[hit.id for hit in hits], result_count=len(selected), result_ids=[hit.id for hit in selected])
        return selected

    def _hybrid_rank(
        self,
        rows: Sequence[Mapping[str, Any]],
        query: str,
        semantic_scores: Sequence[float],
        *,
        top_k: int,
        candidate_depth: int,
        per_book_cap: int | None,
    ) -> list[RagHit]:
        """Fuse lexical and semantic candidate lists with RRF."""

        lexical_ranked = sorted(
            (
                (score, index)
                for index, score in enumerate(_lexical_scores(rows, query, self._lexical_terms, self._lexical_statistics))
                if score > 0.0
            ),
            key=lambda item: (-item[0], int(rows[item[1]]["chapter_order"]), str(rows[item[1]]["id"])),
        )[:candidate_depth]
        semantic_ranked = sorted(
            range(len(rows)),
            key=lambda index: (
                -semantic_scores[index],
                int(rows[index]["chapter_order"]),
                str(rows[index]["id"]),
            ),
        )[:candidate_depth]
        rrf_k = 60.0
        fused: dict[int, float] = {}
        channels: dict[int, set[str]] = {}
        for rank, (_score, index) in enumerate(lexical_ranked, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (rrf_k + rank)
            channels.setdefault(index, set()).add("lexical")
        for rank, index in enumerate(semantic_ranked, start=1):
            fused[index] = fused.get(index, 0.0) + 1.0 / (rrf_k + rank)
            channels.setdefault(index, set()).add("semantic")
        ordered = sorted(
            fused,
            key=lambda index: (
                -fused[index],
                int(rows[index]["chapter_order"]),
                str(rows[index]["id"]),
            ),
        )
        picked: list[int] = []
        if per_book_cap:
            book_counts: dict[str, int] = {}
            for index in ordered:
                book = self._book_key(rows[index])
                if book_counts.get(book, 0) >= per_book_cap:
                    continue
                book_counts[book] = book_counts.get(book, 0) + 1
                picked.append(index)
                if len(picked) >= top_k:
                    break
        else:
            picked = ordered[:top_k]
        return [
            RagHit(
                id=str(rows[index]["id"]),
                title=str(rows[index]["title"]),
                chapter_id=str(rows[index]["chapter_id"]),
                chapter_order=int(rows[index]["chapter_order"]),
                content=str(rows[index]["content"]),
                score=float(fused[index]),
                retrieval_mode="hybrid",
                book=self._book_key(rows[index]),
                channels="+".join(sorted(channels.get(index, set()))),
            )
            for index in picked
        ]

    def _apply_per_book_cap(
        self,
        ranked: list[tuple[float, Mapping[str, Any]]],
        top_k: int,
        per_book_cap: int,
    ) -> list[tuple[float, Mapping[str, Any]]]:
        picked: list[tuple[float, Mapping[str, Any]]] = []
        book_counts: dict[str, int] = {}
        for item in ranked:
            book = self._book_key(item[1])
            if book_counts.get(book, 0) >= per_book_cap:
                continue
            book_counts[book] = book_counts.get(book, 0) + 1
            picked.append(item)
            if len(picked) >= top_k:
                break
        return picked

    def retrieve_context(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_chars: int = 12_000,
        chapter_ids: set[str] | frozenset[str] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        mode: str | None = "hybrid",
        book_ids: set[str] | frozenset[str] | None = None,
        authors: set[str] | frozenset[str] | None = None,
        languages: set[str] | frozenset[str] | None = None,
        per_book_cap: int | None = 3,
        candidate_depth: int = 60,
        auto_route: bool = True,
        aliases: Mapping[str, Sequence[str]] | None = None,
        reranker: Reranker | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> RagContext:
        """Return citation-labelled text ready to augment a generation prompt."""

        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        diag = diagnostics if diagnostics is not None else {}
        hits = self.retrieve(
            query,
            top_k=top_k,
            chapter_ids=chapter_ids,
            embedding_provider=embedding_provider,
            mode=mode,
            book_ids=book_ids,
            authors=authors,
            languages=languages,
            per_book_cap=per_book_cap,
            candidate_depth=candidate_depth,
            auto_route=auto_route,
            aliases=aliases,
            reranker=reranker,
            diagnostics=diag,
        )
        included: list[RagHit] = []
        blocks: list[str] = []
        prefixes = [" ".join([f"[KB:{hit.id}]", *([f"[{hit.book}]"] if hit.book else []), hit.title, *([f"({hit.channels})"] if hit.channels else [])]) + "\n" for hit in hits]
        while hits and sum(map(len, prefixes)) + 2 * (len(hits) - 1) + len(hits) > max_chars:
            hits.pop()
            prefixes.pop()
        remaining = max_chars - sum(map(len, prefixes)) - max(0, 2 * (len(hits) - 1))
        truncated: list[str] = []
        for index, (hit, prefix) in enumerate(zip(hits, prefixes)):
            allowance = remaining // (len(hits) - index)
            content = hit.content
            if len(content) > allowance:
                terms = sorted(set(_tokens(query)), key=len, reverse=True)
                location = next((content.lower().find(term) for term in terms if term in content.lower()), 0)
                start = max(0, location - allowance // 3)
                content = content[start:start + max(0, allowance - 2)]
                content = ("…" if start else "") + content + "…"
                content = content[:allowance]
                truncated.append(hit.id)
            remaining -= len(content)
            blocks.append(prefix + content)
            included.append(replace(hit, content=content, source_content=hit.source_content or hit.content))
        diag.update(context_included_ids=[hit.id for hit in included], context_truncated_ids=truncated, context_chars=len("\n\n".join(blocks)), context_omitted_count=diag.get("result_count", 0)-len(included))
        return RagContext(query=query, hits=tuple(included), text="\n\n".join(blocks), diagnostics=dict(diag))



__all__ = [
    "EmbeddingProvider",
    "Reranker",
    "RagContext",
    "RagEmbeddingMetadata",
    "RagEmbeddingUnavailableError",
    "RagError",
    "RagFormatError",
    "RagHit",
    "RagIndexStaleError",
    "RagKnowledgeBase",
    "RagProviderError",
    "ZhipuEmbeddingProvider",
    "build_embedding_index",
    "initialize_rag_manifest",
    "load_knowledge_rows",
    "load_metadata_sidecar",
    "manifest_path_for",
    "maybe_build_zhipu_embedding_index",
    "metadata_sidecar_path_for",
    "rag_manifest_is_current",
    "read_rag_manifest",
    "vector_index_path_for",
    "zhipu_embedding_enabled",
]
