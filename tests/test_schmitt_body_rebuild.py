from pathlib import Path
import tempfile
import unittest
import json
from PIL import Image, ImageDraw

from tools.books.schmitt_body_rebuild import (
    PAGE_ORDER, annotate_paragraph_starts, clean_reference_marks,
    merge_text, note_separator, page_lines,
)


class SchmittReconstructionTests(unittest.TestCase):
    def test_rule_isolated_from_gutter_and_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'page.png'
            im=Image.new('L',(600,1000),255)
            d=ImageDraw.Draw(im)
            d.line((8,0,8,999),fill=0,width=2)
            d.rectangle((90,350,250,378),fill=0)
            d.line((90,650,175,650),fill=0,width=2)
            im.save(path)
            self.assertEqual(note_separator(path)[0],650)

    def test_indent_breaks_and_quote_lines_do_not_all_break(self):
        xs=[100,100,200,100,100,200,200,200]
        rows=[dict(text='这是超过十五个汉字的连续正文行用于检验原书分段。',x=x,y=100+i*90,right=1400,height=50) for i,x in enumerate(xs)]
        annotate_paragraph_starts(rows)
        self.assertEqual([i for i,r in enumerate(rows) if r['paragraph_start']],[2,5])
        self.assertTrue(all(r['kind']=='body' for r in rows))

    def test_short_sentence_is_not_heading(self):
        rows=[dict(text='这是用于确定正文大小和列位置的完整正文行。',x=100,y=100,right=1400,height=50),dict(text='的著作。',x=100,y=190,right=300,height=85),dict(text='这是正文的下一段而不是一个新的章节标题。',x=200,y=280,right=1400,height=50)]
        annotate_paragraph_starts(rows)
        self.assertEqual(rows[1]['kind'],'body')

    def test_reference_cleanup_keeps_dates_and_prose_numbering(self):
        text='论证[1]仍然成立。[1933]（1）第一命题。'
        cleaned,marks=clean_reference_marks(text,True)
        self.assertEqual(cleaned,'论证仍然成立。[1933]（1）第一命题。')
        self.assertEqual(marks,['[1]'])
        self.assertEqual(clean_reference_marks(text,False)[0],text)
        self.assertEqual(clean_reference_marks('正文[]继续。[1963年版补注]',True)[0],'正文继续。[1963年版补注]')

    def test_same_baseline_uses_x_order_not_small_y_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'page.png'
            Image.new('L',(600,1000),255).save(path)
            record=dict(pdf_page=1,notes=json.dumps(dict(lines=[
                dict(text='作为政治的对立面',score=.99,box=[310,200,550,235]),
                dict(text='文化的',score=.99,box=[100,201,220,235]),
            ])))
            rows,_=page_lines(record,path)
            self.assertEqual(rows[0]['text'],'文化的作为政治的对立面')

    def test_cross_line_join_and_verified_page_cycles(self):
        self.assertEqual(merge_text('国家的','概念'),'国家的概念')
        self.assertEqual(merge_text('Hugo','Krabbe'),'Hugo Krabbe')
        self.assertEqual([PAGE_ORDER['政治的概念'].get(p,p) for p in range(61,77)],
                         [61,62,65,64,63,66,67,68,69,70,73,72,71,74,75,76])


if __name__=='__main__':
    unittest.main()
