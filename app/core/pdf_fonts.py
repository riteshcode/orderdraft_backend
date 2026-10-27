"""
PDF ke liye Unicode font. reportlab ka default Helvetica sirf Latin-1 dikhata hai (Ā, ₹, Ω, 中 = kaale box).

  font, bold = pdf_fonts(texts)   # texts = PDF mein jaane wale saare strings
  - DejaVu Sans (repo mein bundled): Latin, accents, Greek, Cyrillic, ₹ € ...
  - Chinese / Japanese / Korean text ho aur server pe CJK font ho (PDF_CJK_FONT ya wqy-zenhei) to wo
"""
import os
import re

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
CJK_RE = re.compile("[぀-ヿ㐀-鿿가-힯豈-﫿]")
CJK_CANDIDATES = [os.getenv("PDF_CJK_FONT", ""), "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                  "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc"]
_registered: dict[str, bool] = {}


def _register(name: str, path: str, **kw) -> bool:
    if name in _registered:
        return _registered[name]
    try:
        pdfmetrics.registerFont(TTFont(name, path, **kw))
        _registered[name] = True
    except Exception:           # font file nahi / format support nahi -> agla option
        _registered[name] = False
    return _registered[name]


def pdf_fonts(texts) -> tuple[str, str]:
    """(regular, bold) font names. Kuch na mile to Helvetica (purana behaviour)."""
    joined = " ".join(t for t in texts if t)
    if CJK_RE.search(joined):
        for path in CJK_CANDIDATES:
            if path and os.path.exists(path) and _register("OD-CJK", path, subfontIndex=0):
                return "OD-CJK", "OD-CJK"
    if _register("OD-Sans", os.path.join(HERE, "DejaVuSans.ttf")) and \
            _register("OD-Sans-Bold", os.path.join(HERE, "DejaVuSans-Bold.ttf")):
        pdfmetrics.registerFontFamily("OD-Sans", normal="OD-Sans", bold="OD-Sans-Bold",
                                      italic="OD-Sans", boldItalic="OD-Sans-Bold")
        return "OD-Sans", "OD-Sans-Bold"
    return "Helvetica", "Helvetica-Bold"
