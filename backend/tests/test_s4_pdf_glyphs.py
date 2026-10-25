"""Production has no Liberation fonts, so PDFs fall back to reportlab's Vera. Every literal character the trade-document
renderer prints must exist in Vera, or it renders as an empty box (live HAWB showed "HEL □ JFK")."""
import os
import re

import reportlab
from reportlab.pdfbase.ttfonts import TTFontFile

HERE = os.path.dirname(__file__)
SOURCE = os.path.join(HERE, "..", "services", "tradedocs", "pdf.py")


def test_trade_document_literals_exist_in_fallback_font():
    cmap = TTFontFile(os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")).charToGlyph
    text = open(SOURCE, encoding="utf-8").read()
    strings = "".join(re.findall(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'', text))
    missing = sorted({ch for ch in strings if ord(ch) > 127 and ord(ch) not in cmap})
    assert not missing, f"Characters missing from the Vera fallback font: {missing}"
