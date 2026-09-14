from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_apparatus import annotate_apparatus, classify_apparatus, load_apparatus
from rag_knowledge_base import (
    RagIndexStaleError, RagKnowledgeBase, build_embedding_index,
    initialize_rag_manifest, vector_index_path_for,
)


class ConstantProvider:
    provider_name = "apparatus-test"
    model = "constant-v1"

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class ApparatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "knowledge_base.jsonl"
        self.rows = [
            {"id": "a" * 40, "title": "目录", "chapter_id": "toc", "chapter_order": 0,
             "content": "欲望机器 欲望机器 欲望机器 001"},
            {"id": "b" * 40, "title": "欲望机器", "chapter_id": "body", "chapter_order": 1,
             "content": "欲望机器是本章讨论的概念。"},
        ]
        self._write()
        self.provider = ConstantProvider()
        build_embedding_index(self.path, self.provider)

    def _write(self):
        self.path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8")

    def test_structural_and_editorial_labels(self):
        for title in ("目录", "目次", "索引", "人名索引", "主题索引", "版权页", "参考文献", "主要参考书目", "封底"):
            with self.subTest(title=title):
                result = classify_apparatus(title, "内容")
                self.assertTrue(result["is_apparatus"])
                self.assertEqual(result["default_weight"], .25)
                self.assertTrue(result["apparatus_kind"])
                self.assertTrue(result["reason"])
        for title in ("出版说明", "关于作者", "关于译者", "译者名词简释"):
            with self.subTest(title=title):
                result = classify_apparatus(title, "内容")
                self.assertTrue(result["is_apparatus"])
                self.assertEqual(result["default_weight"], .7)

    def test_prefixes_and_body_titles(self):
        for title in ("[反俄狄浦斯] 目录", "1. 目录", "[反俄狄浦斯] 1. 目录"):
            with self.subTest(title=title):
                self.assertTrue(classify_apparatus(title, "内容")["is_apparatus"])
        for title in ("目录学研究", "索引理论", "主体与愉悦", "参考文献的历史"):
            with self.subTest(title=title):
                result = classify_apparatus(title, "目录与索引是本段研究对象。")
                self.assertFalse(result["is_apparatus"])
                self.assertEqual(result["default_weight"], 1.0)

    def test_detects_unnamed_leader_page_list_without_matching_prose(self):
        contents = "\n".join(f"第{i}节 欲望机器 ······· {i:03d}" for i in range(1, 9))
        self.assertEqual(classify_apparatus("未命名章节", contents)["apparatus_kind"], "toc")
        self.assertFalse(classify_apparatus("正文", "目录如下：\n欲望机器……001\n接下来详细解释机器概念。")["is_apparatus"])

    def test_sidecar_rejects_missing_ids_and_invalid_annotation_weights(self):
        annotate_apparatus(self.path)
        sidecar = self.path.with_suffix(".apparatus.json")
        valid = json.loads(sidecar.read_text(encoding="utf-8"))
        for mutation in ("missing_id", "negative", "nan", "body_penalty"):
            with self.subTest(mutation=mutation):
                payload = json.loads(json.dumps(valid))
                if mutation == "missing_id":
                    del payload["annotations"][self.rows[0]["id"]]
                elif mutation == "body_penalty":
                    payload["annotations"][self.rows[1]["id"]]["default_weight"] = .25
                else:
                    payload["annotations"][self.rows[0]["id"]]["default_weight"] = -.1 if mutation == "negative" else float("nan")
                sidecar.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_apparatus(self.path, self.rows)

    def test_annotation_preserves_corpus_and_vector_bytes_and_is_idempotent(self):
        before = self.path.read_bytes(), vector_index_path_for(self.path).read_bytes()
        annotate_apparatus(self.path)
        sidecar = self.path.with_suffix(".apparatus.json")
        first = sidecar.read_bytes()
        annotations = load_apparatus(self.path, self.rows)
        self.assertEqual(set(annotations), {row["id"] for row in self.rows})
        self.assertTrue(annotations[self.rows[0]["id"]]["is_apparatus"])
        self.assertFalse(annotations[self.rows[1]["id"]]["is_apparatus"])
        annotate_apparatus(self.path)
        self.assertEqual(first, sidecar.read_bytes())
        self.assertEqual(before, (self.path.read_bytes(), vector_index_path_for(self.path).read_bytes()))

    def test_missing_sidecar_is_compatible(self):
        self.path.with_suffix(".apparatus.json").unlink(missing_ok=True)
        self.assertEqual(load_apparatus(self.path, self.rows), {})
        self.assertTrue(RagKnowledgeBase.open(self.path).retrieve("欲望"))

    def test_changed_corpus_invalidates_annotation(self):
        annotate_apparatus(self.path)
        self.rows[1]["content"] += "新内容"
        self._write()
        with self.assertRaises(ValueError):
            load_apparatus(self.path, self.rows)
        with self.assertRaises(RagIndexStaleError):
            RagKnowledgeBase.open(self.path)
        initialize_rag_manifest(self.path)
        self.assertEqual(set(load_apparatus(self.path, self.rows)), {row["id"] for row in self.rows})
        self.assertTrue(RagKnowledgeBase.open(self.path).retrieve("欲望"))

    def test_invalid_sidecar_is_rejected(self):
        self.path.with_suffix(".apparatus.json").write_text('{"broken": true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_apparatus(self.path, self.rows)
        with self.assertRaises(RagIndexStaleError):
            RagKnowledgeBase.open(self.path)

    def test_all_modes_penalize_before_top_k_and_allow_opt_out(self):
        annotate_apparatus(self.path)
        kb = RagKnowledgeBase.open(self.path)
        for mode in ("lexical", "semantic", "hybrid"):
            with self.subTest(mode=mode):
                kwargs = dict(mode=mode, embedding_provider=self.provider, auto_route=False, per_book_cap=None)
                original = kb.retrieve("欲望机器", top_k=2, apparatus_weight=1.0, **kwargs)
                weighted = kb.retrieve("欲望机器", top_k=2, **kwargs)
                self.assertEqual(weighted[0].id, self.rows[1]["id"])
                self.assertEqual(kb.retrieve("欲望机器", top_k=1, **kwargs)[0].id, self.rows[1]["id"])
                body_before = next(hit for hit in original if hit.id == self.rows[1]["id"])
                body_after = next(hit for hit in weighted if hit.id == self.rows[1]["id"])
                if mode == "hybrid":
                    # Moving apparatus down improves ordinary content's rank;
                    # RRF consequently changes even without a body penalty.
                    self.assertGreaterEqual(body_after.score, body_before.score)
                else:
                    self.assertEqual(body_before.score, body_after.score)
                self.assertLess(next(hit.score for hit in weighted if hit.id == self.rows[0]["id"]),
                                next(hit.score for hit in original if hit.id == self.rows[0]["id"]))
                stripped = kb.retrieve("欲望机器", apparatus_weight=0, **kwargs)
                self.assertEqual([hit.id for hit in stripped], [self.rows[1]["id"]])
        self.assertEqual(kb.retrieve("目录", mode="lexical", apparatus_weight=1, auto_route=False)[0].id, self.rows[0]["id"])

    def test_hybrid_prior_applies_before_channel_candidate_cutoff(self):
        kb = RagKnowledgeBase.open(self.path)
        hits = kb.retrieve("欲望机器", mode="hybrid", embedding_provider=self.provider,
                           top_k=1, candidate_depth=1, auto_route=False)
        self.assertEqual(hits[0].id, self.rows[1]["id"])

    def test_context_reports_apparatus_policy(self):
        annotate_apparatus(self.path)
        kb = RagKnowledgeBase.open(self.path)
        context = kb.retrieve_context("欲望机器", mode="semantic", embedding_provider=self.provider)
        diag = context.diagnostics["apparatus"]
        self.assertEqual(diag["tagged_count"], 1)
        self.assertIsNone(diag["weight_override"])
        self.assertIn(self.rows[0]["id"], diag["penalized_ids"])
        context = kb.retrieve_context("欲望机器", apparatus_weight=1)
        self.assertEqual(context.diagnostics["apparatus"]["weight_override"], 1)
        self.assertEqual(context.diagnostics["apparatus"]["penalized_ids"], [])

    def test_invalid_weight_is_rejected(self):
        annotate_apparatus(self.path)
        kb = RagKnowledgeBase.open(self.path)
        for weight in (-.1, 1.1, float("nan"), float("inf")):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                kb.retrieve("欲望", apparatus_weight=weight)

    def test_negative_cosine_is_not_promoted(self):
        class NegativeQuery(ConstantProvider):
            def embed_query(self, text):
                return [-1.0, 0.0]
        kb = RagKnowledgeBase.open(self.path)
        before = kb.retrieve("欲望", mode="semantic", embedding_provider=NegativeQuery(), apparatus_weight=1)
        after = kb.retrieve("欲望", mode="semantic", embedding_provider=NegativeQuery())
        self.assertLess(next(hit.score for hit in after if hit.id == self.rows[0]["id"]),
                        next(hit.score for hit in before if hit.id == self.rows[0]["id"]))
        self.assertEqual(after[0].id, self.rows[1]["id"])

    def test_cli_annotation_and_recursive_parser(self):
        from knowledge_base_cli import build_parser, main
        args = build_parser().parse_args(["annotate-apparatus", str(self.path.parent), "--recursive"])
        self.assertTrue(args.recursive)
        self.path.with_suffix(".apparatus.json").unlink()
        output = io.StringIO()
        self.assertEqual(main(["annotate-apparatus", str(self.path.parent), "--recursive"], stdout=output), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["libraries"], 1)
        self.assertEqual(report["tagged_chunks"], 1)

    def test_cli_fallback_preserves_weight_override(self):
        from knowledge_base_cli import main
        class BrokenQuery(ConstantProvider):
            def embed_query(self, text):
                raise RuntimeError("test provider unavailable")
        output = io.StringIO()
        with patch("knowledge_base_cli._provider_from_manifest", return_value=BrokenQuery()):
            result = main(["retrieve", str(self.path), "目录", "--mode", "semantic", "--apparatus-weight", "1"], stdout=output)
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["apparatus"]["weight_override"], 1)
        self.assertEqual(report["apparatus"]["penalized_ids"], [])
        self.assertEqual(report["hits"][0]["id"], self.rows[0]["id"])


if __name__ == "__main__":
    unittest.main()
