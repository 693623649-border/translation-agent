import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import fitz
from PIL import Image
from book_pipeline import PageRecord, save_page_record
from tools.chinese_pdf_kb_plugin import reader_ingest as v


class ChineseIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "book.pdf"
        with fitz.open() as doc:
            for _ in range(2): doc.new_page()
            doc.save(self.source)
        self.ws = self.root / "work"
        self.args = dict(source=str(self.source), workspace=str(self.ws), title="测试书", author="作者")
        v.prepare(self.args)

    def populate(self, text="这是一段正文，保留日期[1932]和解释[原文说明]。"):
        job = self.root / "job"
        (job / "images").mkdir(parents=True)
        v.write(job / "local_paddleocr_job.json", dict(source_pdf=dict(sha256=v.sha(self.source))))
        manifest = v.read(self.ws / v.MANIFEST)
        for page in (1, 2):
            image = job / "images" / f"page_{page:04d}.jpg"
            Image.new("RGB", (1000, 1400), "white").save(image)
            manifest["page_images"][str(page)] = str(image)
            lines = [dict(text="页眉", score=.99, box=[20, 10, 80, 30]), dict(text=text if page == 1 else "这是下一页延续的正文。", score=.99, box=[50, 100, 850, 125]), dict(text="这是保留在档案中的注释。", score=.99, box=[50, 1200, 850, 1225])]
            save_page_record(self.ws, PageRecord(page, "\n".join(x["text"] for x in lines), notes=json.dumps(dict(reading_direction="horizontal", ordering_version=3, lines=lines))))
        v.write(self.ws / v.MANIFEST, manifest)
        self.plan = dict(sections=[dict(id="chapter", title="第一章", pdf_pages=[1, 2])], excluded_pages=[], page_overrides=[dict(pdf_page=p, header_bottom_y=40, note_boundary_y=1100, note="源图可见页下注线") for p in (1, 2)])
        self.review()

    def review(self):
        v.write(self.ws / "reconstruction-plan.json", self.plan)
        state = v.snapshot(self.ws)[0]
        v.write(self.ws / "source-review.json", dict(source_sha256=state["source_sha256"], plan_sha256=v.sha(self.ws / "reconstruction-plan.json"), ocr_sha256=state["ocr_sha256"], reviewer="fixture", note="合成几何图检查", checked_pages=[dict(pdf_page=p, note="版面检查") for p in (1, 2)], low_confidence_lines=[]))

    def run_stage(self, stage):
        return v.run(dict(workspace=str(self.ws), stage=stage))

    def test_cross_page_paragraph_and_archive_coverage(self):
        self.populate()
        self.run_stage("reconstruct")
        paragraphs = [json.loads(x) for x in (self.ws / "正文段落.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(paragraphs), 1)
        self.assertIn("[1932]", paragraphs[0]["text"])
        self.assertIn("[原文说明]", paragraphs[0]["text"])
        self.assertEqual(len(paragraphs[0]["source_lines"]), 2)
        self.assertNotIn("注释", (self.ws / "source.md").read_text(encoding="utf-8"))
        self.assertEqual(len((self.ws / "audit/cleaning-ledger.jsonl").read_text(encoding="utf-8").splitlines()), 6)
        self.assertTrue(v.snapshot(self.ws)[0]["reconstructed"])

    def test_duplicate_missing_page_blocks(self):
        self.populate()
        self.plan["sections"][0]["pdf_pages"] = [1, 1]
        self.review()
        with self.assertRaisesRegex(ValueError, "exactly once"): self.run_stage("reconstruct")

    def test_stale_review_and_image_block(self):
        self.populate()
        self.plan["page_overrides"][0]["note_boundary_y"] = 1000
        v.write(self.ws / "reconstruction-plan.json", self.plan)
        with self.assertRaisesRegex(ValueError, "plan_sha256"): self.run_stage("reconstruct")
        self.review()
        image = Path(v.read(self.ws / v.MANIFEST)["page_images"]["1"])
        Image.new("RGB", (1000, 1400), "black").save(image)
        with self.assertRaisesRegex(ValueError, "ocr_sha256"): self.run_stage("reconstruct")

    def test_unknown_glyph_and_overlong_paragraph_block(self):
        self.populate("这是一段含□字的正文。")
        with self.assertRaisesRegex(ValueError, "Unknown glyph"): self.run_stage("reconstruct")

    def test_budget_preserves_unsplit_paragraph(self):
        self.populate("正文。" * 100)
        self.plan["chunk_chars"] = 200
        self.review()
        with self.assertRaisesRegex(ValueError, "Paragraph exceeds"): self.run_stage("reconstruct")

    def test_exact_correction_requires_image(self):
        self.populate("这是一段含□字的正文。")
        state = v.snapshot(self.ws)[0]
        self.plan["corrections"] = [dict(pdf_page=1, line_index=1, before="这是一段含□字的正文。", after="这是一段含口字的正文。", reason="源图核对", evidence_image_sha256=state["geometry_review"][0]["image_sha256"])]
        self.review()
        self.run_stage("reconstruct")
        self.assertIn("含口字", (self.ws / "source.md").read_text(encoding="utf-8"))

    def test_publish_is_real_offline_and_five_fields(self):
        self.populate()
        self.run_stage("reconstruct")
        self.run_stage("publish")
        self.assertTrue(v.snapshot(self.ws)[0]["published"])
        v.corpus_gate(self.ws)
        self.assertTrue((self.ws / "测试书.docx").is_file())
        (self.ws / "source.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Fresh publication"): self.run_stage("verify")

    def test_no_register_without_word_render(self):
        self.populate()
        with patch.object(v, "execute") as execute:
            with self.assertRaisesRegex(ValueError, "Fresh Word"): self.run_stage("register")
            execute.assert_not_called()

    def test_exact_page_ranges_and_dirty_workspace(self):
        self.assertEqual(v.ranges([1, 2, 5], 5), [[1, 2], [5, 5]])
        for pages in ([1, 1], [True], [], [0], [3]):
            with self.assertRaises(ValueError): v.ranges(pages, 2)
        dirty = self.root / "dirty"
        dirty.mkdir(); (dirty / "keep").write_text("keep")
        with self.assertRaisesRegex(ValueError, "nonempty"): v.prepare({**self.args, "workspace": str(dirty)})

    def test_quote_continuation_and_noisy_baseline_fragments(self):
        self.populate()
        path=self.ws/'pages/page_0001.json'
        record=json.loads(path.read_text(encoding='utf-8'))
        rows=[dict(text='页眉',score=.99,box=[20,10,80,30]),
              dict(text='引文的第一行继续讨论文学。',score=.99,box=[100,100,800,125]),
              dict(text='引文的第二行仍属于同一段。',score=.99,box=[100,140,800,165]),
              dict(text='普通段落另起。',score=.99,box=[50,180,500,205]),
              dict(text='作为政治的对立面',score=.99,box=[310,240,850,265]),
              dict(text='文化的',score=.99,box=[100,241,220,266]),
              dict(text='注释。',score=.99,box=[50,1200,500,1225])]
        record['text']='\n'.join(r['text'] for r in rows)
        record['notes']=json.dumps(dict(lines=rows,ordering_version=3,reading_direction='horizontal'))
        v.write(path,record)
        self.plan['page_overrides'][0].update(column_left=50,paragraph_start_line_indices=[3])
        self.review();self.run_stage('reconstruct')
        ps=[json.loads(s) for s in (self.ws/'正文段落.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(ps[0]['text'],'引文的第一行继续讨论文学。引文的第二行仍属于同一段。')
        self.assertTrue(any('文化的作为政治的对立面' in p['text'] for p in ps))
        self.assertTrue(all(p['paragraph_id'] and p['pdf_pages'] for p in ps))

    def test_introduced_ellipsis_is_blocked_and_original_kept(self):
        self.populate('原文保留……这样的省略。')
        self.run_stage('reconstruct')
        self.assertIn('……',(self.ws/'source.md').read_text(encoding='utf-8'))
        original='原文保留……这样的省略。'
        image_hash=v.snapshot(self.ws)[0]['geometry_review'][0]['image_sha256']
        self.plan['corrections']=[dict(pdf_page=1,line_index=1,before=original,after=original+'省略……',reason='fixture',evidence_image_sha256=image_hash)]
        self.review()
        with self.assertRaisesRegex(ValueError,'Introduced ellipsis'):self.run_stage('reconstruct')
        self.assertFalse(v.snapshot(self.ws)[0]['reconstructed'])

    def test_sanitized_word_filename_and_chapter_files_bind_receipt(self):
        manifest=v.read(self.ws/v.MANIFEST);manifest['title']='测试书（正文版）:续篇';v.write(self.ws/v.MANIFEST,manifest)
        self.populate();self.run_stage('reconstruct');self.run_stage('publish')
        self.assertTrue((self.ws/v.docx_name(manifest)).is_file())
        self.assertTrue(v.snapshot(self.ws)[0]['published'])
        next((self.ws/'chapters').glob('*.md')).write_text('changed',encoding='utf-8')
        self.assertFalse(v.snapshot(self.ws)[0]['published'])

    def test_metadata_edit_invalidates_current_receipts(self):
        self.populate();self.run_stage('reconstruct');self.run_stage('publish')
        manifest=v.read(self.ws/v.MANIFEST);manifest['author']='不同作者';v.write(self.ws/v.MANIFEST,manifest)
        self.assertFalse(v.snapshot(self.ws)[0]['reconstructed'])
        self.assertFalse(v.snapshot(self.ws)[0]['published'])

    def test_verify_and_register_receipts_expire_on_layout_change(self):
        self.populate();self.run_stage('reconstruct');self.run_stage('publish')
        def execute(command,timeout):
            if '-DocxPath' in command:
                target=Path(command[command.index('-OutputDirectory')+1]);target.mkdir(parents=True,exist_ok=True)
                with fitz.open() as pdf:
                    page=pdf.new_page();page.insert_text((50,80),'Synthetic Word rendering fixture')
                    pdf.save(target/'测试书.pdf')
            else:
                v.write(self.ws/'knowledge_base.rag.json',dict(fixture=True))
                (self.ws/'knowledge_base.vectors.jsonl').write_text('{}\n',encoding='utf-8')
        with patch.object(v,'execute',side_effect=execute):
            self.run_stage('verify');state=v.snapshot(self.ws)[0];r=state['verified']
            v.write(self.ws/'layout-review.json',dict(artifact_sha256=state['artifact_sha256'],rendered_pdf=r['rendered_pdf'],rendered_pdf_sha256=r['rendered_pdf_sha256'],reviewer='fixture',note='Synthetic fixture only',pages=[dict(pdf_page=1,note='fixture')]))
            self.run_stage('register')
        self.assertTrue(v.snapshot(self.ws)[0]['registration_current'])
        (self.ws/'layout-review.json').write_text('changed',encoding='utf-8')
        self.assertFalse(v.snapshot(self.ws)[0]['registration_current'])


if __name__ == "__main__":
    unittest.main()
