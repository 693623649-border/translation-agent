"""Guard the text-only stages against inventing repairs for bad OCR order."""

import unittest

from book_pipeline import (
    ChatOCRProofreader,
    ChatTranslator,
    PROOFREAD_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
)


class RecordingClient:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    def chat_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.output


class OCRPromptFidelityTests(unittest.TestCase):
    def test_translation_preserves_source_evidence_and_partial_sentences(self):
        client = RecordingClient("跨页残句（原文缺损）")
        source = "先の列\n次の列（原文缺损）"
        translated = ChatTranslator(client).translate(
            source, source_language="ja", target_language="简体中文"
        )
        self.assertEqual(translated, client.output)
        prompt = client.prompts[0]
        self.assertEqual(prompt.split("\n原文：\n", 1)[1], source)
        for requirement in (
            "不总结、不删减、不扩写",
            "严格按提供的源文顺序翻译",
            "不得擅自调整列序、行序",
            "不得补造原文",
            "不把视觉换行当成作者段落",
            "保留跨页半句",
            "无法可靠辨认的局部紧邻标（原文缺损）",
            "原有（原文缺损）标注必须保留",
        ):
            self.assertIn(requirement, prompt)
        for unsafe in ("宁可意译连贯", "依上下文推断复原", "仅当整段完全无法辨认"):
            self.assertNotIn(unsafe, prompt)

    def test_proofreading_is_not_image_based_reconstruction(self):
        source = "# 舊題\n\n思つてゐる（原文缺损）"
        client = RecordingClient(source)
        result = ChatOCRProofreader(client).proofread(source, language="ja")
        self.assertEqual(result, source)
        prompt = client.prompts[0]
        self.assertEqual(prompt.split("\nOCR 原文：\n", 1)[1], source)
        for requirement in (
            "不能替代原页图像证据",
            "不能据此确认或改写书名、章节标题",
            "保留历史假名遣和旧字",
            "不得猜补漏字",
            "严格保留提供的源文顺序",
            "跨页半句原样保留",
            "保留诗歌分行",
            "紧邻标注（原文缺损）",
        ):
            self.assertIn(requirement, prompt)
        self.assertNotIn("明显的阅读顺序错误", prompt)

    def test_unsafe_prompt_versions_are_invalidated(self):
        self.assertNotEqual(TRANSLATION_PROMPT_VERSION, "book-translation-v5")
        self.assertNotEqual(PROOFREAD_PROMPT_VERSION, "book-ocr-proofread-ja-v1")


if __name__ == "__main__":
    unittest.main()
