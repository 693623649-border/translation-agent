from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from rag_knowledge_base import RagHit, RagProviderError
from rag_reranking import OpenAICompatibleReranker, load_query_aliases


def _hit(identifier: str, title: str = "段落", content: str = "内容") -> RagHit:
    return RagHit(
        id=identifier,
        title=title,
        chapter_id="chapter-1",
        chapter_order=1,
        content=content,
        score=0.5,
        retrieval_mode="hybrid",
    )


def _response(scores: Sequence[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"scores": list(scores)}))
            )
        ]
    )


def _response_id_only(entries: Sequence[dict]) -> SimpleNamespace:
    """A model that returns the bare array without the wrapping object."""

    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(list(entries))))
        ]
    )


class _FencedClient:
    """A model that wraps the JSON object in markdown fences."""

    def __init__(self) -> None:
        class _Chat:
            completions = SimpleNamespace(create=self._create)

        self.chat = _Chat

    def _create(self, **kwargs: object) -> object:
        content = "```json\n" + json.dumps({"scores": [{"id": "a", "score": 3}]}) + "\n```"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class _ScriptedClient:
    def __init__(self, responses: Sequence) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

        class _Chat:
            completions = SimpleNamespace(create=self._create)

        self.chat = _Chat

    def _create(self, **kwargs) -> object:
        self.requests.append(kwargs)
        return self.responses.pop(0)


class OpenAICompatibleRerankerTests(unittest.TestCase):
    def test_requires_model_and_positive_limits(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleReranker(model=" ")
        with self.assertRaises(ValueError):
            OpenAICompatibleReranker(model="m", batch_size=0)

    def test_empty_hits_return_immediately(self) -> None:
        reranker = OpenAICompatibleReranker(model="m", client=object())
        self.assertEqual(reranker.rerank("q", []), [])

    def test_reranks_by_reported_scores(self) -> None:
        client = _ScriptedClient(
            [
                _response(
                    [
                        {"id": "a", "score": 1},
                        {"id": "b", "score": 3},
                        {"id": "c", "score": 2},
                    ]
                )
            ]
        )
        reranker = OpenAICompatibleReranker(model="m", client=client)
        hits = [_hit("a"), _hit("b"), _hit("c")]
        reordered = reranker.rerank("问题", hits)
        self.assertEqual([hit.id for hit in reordered], ["b", "c", "a"])

    def test_batches_preserve_every_hit(self) -> None:
        client = _ScriptedClient(
            [
                _response([{"id": "a", "score": 0}, {"id": "b", "score": 3}]),
                _response([{"id": "c", "score": 2}]),
            ]
        )
        reranker = OpenAICompatibleReranker(model="m", client=client, batch_size=2)
        reordered = reranker.rerank("问题", [_hit("a"), _hit("b"), _hit("c")])
        self.assertEqual([hit.id for hit in reordered], ["b", "c", "a"])
        self.assertEqual(len(client.requests), 2)
        second_payload = json.loads(client.requests[1]["messages"][1]["content"])
        self.assertEqual([doc["id"] for doc in second_payload["documents"]], ["c"])

    def test_documents_are_truncated_to_limit(self) -> None:
        client = _ScriptedClient([_response([{"id": "a", "score": 3}])])
        reranker = OpenAICompatibleReranker(
            model="m", client=client, max_document_chars=10
        )
        reranker.rerank("问题", [_hit("a", content="x" * 500)])
        payload = json.loads(client.requests[0]["messages"][1]["content"])
        self.assertEqual(len(payload["documents"][0]["text"]), 10)

    def test_bare_array_and_fenced_responses_are_parsed(self) -> None:
        bare = _ScriptedClient([_response_id_only([{"id": "a", "score": 2}])])
        reranker = OpenAICompatibleReranker(model="m", client=bare)
        hits = reranker.rerank("问题", [_hit("a")])
        self.assertEqual([h.id for h in hits], ["a"])

        fenced_client = _FencedClient()
        reranker = OpenAICompatibleReranker(model="m", client=fenced_client)
        hits = reranker.rerank("问题", [_hit("a")])
        self.assertEqual([h.id for h in hits], ["a"])

    def test_untrusted_identifiers_are_rejected(self) -> None:
        for bad in (
            [{"id": "ghost", "score": 3}],  # invented identifier
            [{"id": "a", "score": 3}, {"id": "a", "score": 1}],  # duplicate
            [{"id": "a", "score": 3}],  # missing candidate
            [{"id": "a", "score": 9}],  # out-of-range score
            [{"id": "a", "score": True}],  # boolean masquerading as number
        ):
            client = _ScriptedClient([_response(bad)])
            reranker = OpenAICompatibleReranker(model="m", client=client)
            with self.assertRaises(RagProviderError):
                reranker.rerank("问题", [_hit("a"), _hit("b")])

    def test_provider_failures_are_wrapped_without_body_leaks(self) -> None:
        class _Boom:
            chat = None

        reranker = OpenAICompatibleReranker(model="m", client=_Boom())
        with self.assertRaises(RagProviderError) as ctx:
            reranker.rerank("问题", [_hit("a")])
        self.assertNotIn("secret", str(ctx.exception))

    def test_missing_client_credential_is_reported(self) -> None:
        reranker = OpenAICompatibleReranker(model="m", api_key_env="NO_SUCH_KEY")
        with self.assertRaises(RagProviderError):
            reranker.rerank("问题", [_hit("a")])


class LoadQueryAliasesTests(unittest.TestCase):
    def test_none_returns_empty_mapping(self) -> None:
        self.assertEqual(load_query_aliases(None), {})

    def test_loads_and_dedupes_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            path.write_text(
                json.dumps(
                    {"交换模式": ["交換様式", "modes of exchange"], "x": ["x", "x"]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_query_aliases(path),
                {"交换模式": ["交換様式", "modes of exchange"], "x": ["x"]},
            )

    def test_rejects_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            not_object = root / "a.json"
            not_object.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_query_aliases(not_object)
            bad_values = root / "b.json"
            bad_values.write_text('{"t": "not-a-list"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_query_aliases(bad_values)
            empty_alias = root / "c.json"
            empty_alias.write_text('{"t": [""]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_query_aliases(empty_alias)


if __name__ == "__main__":
    unittest.main()
