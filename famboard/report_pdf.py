"""Render the attention report as a PDF styled like the dashboard (dark theme,
accent headings, level-coloured tags), with device detail: name, MAC, owner,
type. Pure-Python (fpdf2), no system dependencies."""
from __future__ import annotations

from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# dashboard palette (style.css :root), as RGB
BG = (11, 16, 22)
PANEL2 = (27, 36, 48)
FG = (233, 238, 245)
MUTED = (143, 160, 179)
ACCENT = (92, 124, 250)
ACCENT_TEXT = (186, 200, 255)
LINE = (40, 52, 66)
LEVEL_COLOR = {"high": (240, 62, 62), "concern": (230, 126, 34), "watch": (143, 160, 179)}
LEVEL_LABEL = {"high": "ALTA", "concern": "DA ATTENZIONARE", "watch": "OSSERVAZIONE"}

_TRANS = str.maketrans({"–": "-", "—": "-", "·": "-", "’": "'", "‘": "'",
                        "“": '"', "”": '"', "⚠": "!", "🧠": "", "🌙": "",
                        "€": "EUR", "…": "..."})


def _san(s: str) -> str:
    """fpdf core fonts are latin-1 only — normalise then drop anything else."""
    return (s or "").translate(_TRANS).encode("latin-1", "replace").decode("latin-1")


class _PDF(FPDF):
    def header(self):  # paint the dark page background
        self.set_fill_color(*BG)
        self.rect(0, 0, self.w, self.h, style="F")

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", size=7)
        self.set_text_color(*MUTED)
        self.cell(0, 6, _san(f"famboard - pagina {self.page_no()}"), align="C")


def _need(pdf: _PDF, h: float) -> None:
    if pdf.get_y() + h > pdf.h - 15:
        pdf.add_page()


def _subject_card(pdf: _PDF, W: float, s: dict) -> None:
    _need(pdf, 18)
    x, y = 14, pdf.get_y()
    lc = LEVEL_COLOR.get(s["level"], MUTED)
    pdf.set_fill_color(*PANEL2)
    pdf.rect(x, y, W, 9, style="F", round_corners=True, corner_radius=1.5)

    tag = LEVEL_LABEL.get(s["level"], s["level"])
    pdf.set_font("Helvetica", "B", 8)
    tw = pdf.get_string_width(_san(tag)) + 6
    pdf.set_fill_color(*lc)
    pdf.rect(x + 2.5, y + 2, tw, 5, style="F", round_corners=True, corner_radius=1)
    pdf.set_xy(x + 2.5, y + 2)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(tw, 5, _san(tag), align="C")

    kind = (" (minore)" if s.get("person_kind") == "minor"
            else " (dispositivo)" if s["kind"] == "device" else " (adulto)")
    pdf.set_xy(x + 5 + tw, y)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*FG)
    pdf.cell(W - tw - 32, 9, _san(s["name"] + kind))
    pdf.set_xy(x, y)
    pdf.set_text_color(*lc)
    pdf.cell(W - 3, 9, _san(f"punteggio {s['score']}"), align="R")
    pdf.set_y(y + 11)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    for r in s["reasons"]:
        _need(pdf, 5)
        pdf.set_x(x + 2)
        pdf.multi_cell(W - 4, 4.6, _san("- " + r))

    if s.get("devices"):
        _need(pdf, 7)
        pdf.ln(1)
        pdf.set_x(x + 2)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*ACCENT)
        pdf.cell(0, 5, _san("Dispositivi"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        owner = s["name"] if s["kind"] == "person" else "non assegnato"
        for d in s["devices"]:
            _need(pdf, 5)
            extra = [v for v in (d.get("device_type"), owner, d.get("vendor")) if v]
            pdf.set_x(x + 4)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*FG)
            pdf.cell(78, 4.6, _san(d["label"]), new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*MUTED)
            pdf.cell(0, 4.6, _san(f"[{d['mac']}]  {'  -  '.join(extra)}"),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(2)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.2)
    pdf.line(x, pdf.get_y(), x + W, pdf.get_y())
    pdf.ln(3)


def render_attention_pdf(report: dict, flagged: list[dict] | None = None) -> bytes:
    subs = flagged if flagged is not None else report["subjects"]
    pdf = _PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_left_margin(14)
    pdf.set_right_margin(14)
    pdf.add_page()
    W = pdf.w - 28

    pdf.set_xy(14, 15)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*FG)
    pdf.cell(0, 10, _san("famboard"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 7, _san("Situazioni da attenzionare"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    when = datetime.fromtimestamp(report["generated"]).strftime("%d/%m/%Y %H:%M")
    pdf.cell(0, 5, _san(f"{when}  -  ultimi {report['days']} giorni"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    if report.get("ai_summary"):
        pdf.set_x(14)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*ACCENT_TEXT)
        pdf.multi_cell(W, 5, _san(report["ai_summary"]))
        pdf.ln(2)

    if not subs:
        pdf.set_x(14)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 6, _san("Nessuna situazione sopra soglia nel periodo."))
    for s in subs:
        _subject_card(pdf, W, s)

    pdf.set_y(-20)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(W, 3.5, _san(
        "Il DNS rivela i domini contattati, non i contenuti: usa questo come "
        "spunto per un confronto, non come prova."))
    return bytes(pdf.output())
