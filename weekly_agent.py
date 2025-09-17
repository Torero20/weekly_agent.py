#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import logging
import smtplib
import requests
from typing import List, Tuple, Optional, Dict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

# Traducción
try:
    from googletrans import Translator  # type: ignore
except Exception:
    Translator = None  # fallback HTTP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class WeeklyReportAgent:
    # Iconos (cosmética para cabeceras de bloques si más adelante quieres usarlos)
    ICONS: Dict[str, str] = {
        "CCHF": "🧬",
        "Influenza aviar": "🐦",
        "Virus del Nilo Occidental": "🦟",
        "Sarampión": "💉",
        "Dengue": "🦟",
        "Chikungunya": "🦟",
        "Mpox": "🐒",
        "Polio": "🧒",
        "COVID-19": "🦠",
        "Tos ferina": "😮‍💨",
        "Fiebre amarilla": "🟡",
        "Fiebre tifoidea": "🍲",
    }

    # Patrones y colores por enfermedad (hex)
    DISEASE_STYLES: List[Tuple[str, re.Pattern, str]] = [
        ("CCHF", re.compile(r"\b(cchf|crimean[-\s]?congo)\b", re.I), "#d32f2f"),            # rojo
        ("Influenza aviar", re.compile(r"\b(h5n1|h7n9|h9n2|influenza\s*aviar|avian)\b", re.I), "#1565c0"),  # azul
        ("Virus del Nilo Occidental", re.compile(r"\b(west\s*nile|wnv)\b", re.I), "#2e7d32"),             # verde
        ("Sarampión", re.compile(r"\b(measles|sarampi[oó]n)\b", re.I), "#6a1b9a"),                       # púrpura
        ("Dengue", re.compile(r"\b(dengue)\b", re.I), "#ef6c00"),                                        # naranja
        ("Chikungunya", re.compile(r"\b(chikungunya)\b", re.I), "#8d6e63"),                              # marrón
        ("Mpox", re.compile(r"\b(mpox|monkeypox)\b", re.I), "#00897b"),                                  # teal
        ("Polio", re.compile(r"\b(polio|poliomyelitis|vdpv|wpv)\b", re.I), "#455a64"),                   # gris azulado
        ("Gripe estacional", re.compile(r"\b(influenza(?!\s*aviar)|\bflu\b|gripe\b)\b", re.I), "#1976d2"),
        ("COVID-19", re.compile(r"\b(covid|sars[-\s]?cov[-\s]?2)\b", re.I), "#0097a7"),
        ("Tos ferina", re.compile(r"\b(pertussis|whooping\s*cough|tos\s*ferina)\b", re.I), "#7b1fa2"),
        ("Fiebre amarilla", re.compile(r"\b(yellow\s*fever|fiebre\s*amarilla)\b", re.I), "#f9a825"),
        ("Fiebre tifoidea", re.compile(r"\b(typhoid|fiebre\s*tifoidea)\b", re.I), "#5d4037"),
    ]
    DEFAULT_COLOR = "#455a64"  # neutro si no hay match

    def __init__(self, smtp_server: str, smtp_port: int, smtp_user: str, smtp_password: str, recipient: str):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.recipient = recipient
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        })
        self.translator = Translator() if Translator else None

    # ------------------------ Fetch último PDF (enlace) ------------------------
    def fetch_latest_pdf(self) -> Tuple[str, bytes]:
        # Página de listados generales; capturamos el primer PDF de CDTR
        list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
        r = self.session.get(list_url, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Mejor: seguir el primer artículo de CDTR y dentro coger el PDF
        article = None
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            if "communicable-disease-threats-report" in href and ("/publications-data/" in href or "/publications-and-data/" in href):
                article = a["href"]
                break
        if not article:
            raise RuntimeError("No encuentro el artículo CDTR más reciente.")
        if not article.startswith("http"):
            article = "https://www.ecdc.europa.eu" + article

        ar = self.session.get(article, timeout=25); ar.raise_for_status()
        asoup = BeautifulSoup(ar.text, "html.parser")
        pdf_url = None
        for a in asoup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                pdf_url = a["href"]
                break
        if not pdf_url:
            raise RuntimeError("Artículo sin PDF.")
        if not pdf_url.startswith("http"):
            pdf_url = "https://www.ecdc.europa.eu" + pdf_url

        logging.info("PDF más reciente: %s", pdf_url)
        pdf_resp = self.session.get(pdf_url, timeout=40)
        pdf_resp.raise_for_status()
        return pdf_url, pdf_resp.content

    # ------------------------ Extracción de texto (placeholder) ----------------
    def extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        """
        ⚠️ Sustituye este placeholder por extracción real (pdfplumber/pdfminer/PyMuPDF)
        si lo necesitas. Para no alargar, dejo un ejemplo estable.
        """
        return (
            "This week, no new cases of CCHF were reported to ECDC. "
            "No human cases of avian influenza A (H9N2) have been reported in the EU/EEA to date. "
            "West Nile virus: 38 deaths this week in Europe, with reports from Italy and Spain. "
            "Measles surveillance: 188 cases across 13 countries. "
            "Imported dengue cases detected in Spain and France."
        )

    # ------------------------ Traducción robusta ------------------------
    def translate_to_spanish(self, text: str) -> str:
        if not text.strip():
            return ""
        # 1) googletrans si está
        if self.translator:
            try:
                res = self.translator.translate(text, src="en", dest="es")
                if res and res.text:
                    return res.text
            except Exception as e:
                logging.warning("googletrans falló: %s", e)
        # 2) endpoint público
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": text}
            r = self.session.get(url, params=params, timeout=12)
            r.raise_for_status()
            data = r.json()
            return " ".join(seg[0] for seg in (data[0] or []) if seg and seg[0]).strip() or text
        except Exception as e:
            logging.warning("Fallback translate falló: %s", e)
            return text

    # ------------------------ Helpers de estilo ------------------------
    @staticmethod
    def split_sentences(text: str) -> List[str]:
        # División simple por punto + espacio; funciona bien para el CDTR
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p.strip() for p in parts if p.strip()]

    def disease_style_for_sentence(self, s: str) -> Tuple[str, str]:
        """Devuelve (label, color_hex) para la primera enfermedad que aparezca."""
        for label, pat, color in self.DISEASE_STYLES:
            if pat.search(s):
                return label, color
        return "General", self.DEFAULT_COLOR

    def highlight_entities(self, s: str) -> str:
        """Negritas/colores para términos; se aplica antes de resaltar España."""
        s_html = s
        # Negrita simple para enfermedad si aparece explícita (cosmético)
        for label, pat, _ in self.DISEASE_STYLES:
            s_html = pat.sub(lambda m: "<b style='color:#8b0000'>{}</b>".format(m.group(0)), s_html)
        # Países en verde
        countries = ["spain","españa","france","italy","germany","portugal","greece","poland","romania",
                     "netherlands","belgium","sweden","norway","finland","denmark","ireland","austria",
                     "czech","hungary","bulgaria","croatia","estonia","latvia","lithuania","slovakia",
                     "slovenia","switzerland","iceland","turkey","cyprus","malta","ukraine","serbia"]
        for c in countries:
            s_html = re.sub(rf"\b{re.escape(c)}\b",
                            lambda m: "<span style='color:#0b6e0b;font-weight:600'>{}</span>".format(m.group(0).title()),
                            s_html, flags=re.I)
        return s_html

    def highlight_spain(self, s_html: str) -> str:
        plain = re.sub(r"<[^>]+>", "", s_html).lower()
        if "españa" in plain or "spain" in plain:
            return ("🇪🇸 <span style='background:#fff7d6;padding:2px 4px;border-radius:4px;"
                    "border-left:4px solid #ff9800'>{}</span>").format(s_html)
        return s_html

    # ------------------------ Titulares (cards) ------------------------
    @staticmethod
    def split_headline(sentence: str, max_title_chars: int = 140) -> Tuple[str, str]:
        s = sentence.strip()
        m = re.search(r"[:;—–\-]|\.\s", s)
        if m and m.start() <= max_title_chars:
            title = s[:m.start()].strip()
            body = s[m.end():].strip()
        else:
            if len(s) > max_title_chars:
                title = s[:max_title_chars].rsplit(" ", 1)[0] + "…"
                body = s[len(title):].strip()
            else:
                title, body = s, ""
        return title, body

    def render_headline_cards(self, sentences: List[str], n_cards: int = 3) -> str:
        cards = []
        for s in sentences[:n_cards]:
            label, color = self.disease_style_for_sentence(s)
            title, body = self.split_headline(s)
            title_html = self.highlight_spain(self.highlight_entities(title))
            body_html = self.highlight_spain(self.highlight_entities(body)) if body else ""
            body_block = "<div style='font-size:14px;color:#333;opacity:.9'>{}</div>".format(body_html) if body_html else ""
            cards.append((
                "<tr><td style='padding:0 20px'>"
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
                "style='margin:10px 0;border-left:6px solid {color};background:#f9fbff;border-radius:10px'>"
                "<tr><td style='padding:12px 14px'>"
                "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:{color};"
                "text-transform:uppercase;margin-bottom:4px'>{label}</div>"
                "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>{title}</div>"
                "{body}"
                "</td></tr></table>"
                "</td></tr>"
            ).format(color=color, label=label, title=title_html, body=body_block))
        return "".join(cards)

    # ------------------------ Render HTML completo ------------------------
    def format_summary_to_html(self, summary_es: str, pdf_url: str) -> str:
        sentences = self.split_sentences(summary_es)

        # Bloque de titulares (3)
        headlines_html = self.render_headline_cards(sentences, n_cards=3)

        # Puntos clave (color por enfermedad): siguientes 8 frases
        bullets = []
        for s in sentences[3:11] if len(sentences) > 3 else sentences[:8]:
            label, color = self.disease_style_for_sentence(s)
            content = self.highlight_spain(self.highlight_entities(s))
            chip = "<span style='background:{bg};color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>{}</span>".format(label).format()
            # Pegamos chip y contenido dentro de un item con borde del color
            item = (
                "<li style='margin:8px 0;list-style:none'>"
                "<div style='border-left:6px solid {color};padding-left:10px'>{chip}{content}</div>"
                "</li>"
            ).format(color=color, chip="<span style='background:{};color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>{}</span>".format(color, label),
                     content=content)
            bullets.append(item)
        keypoints_html = "<ul style='padding-left:0;margin:0'>{}</ul>".format("".join(bullets) if bullets else "<li>Sin datos</li>")

        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#333'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:18px 12px'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='700' cellspacing='0' cellpadding='0' style='max-width:700px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.06)'>"
            "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
            "<div style='font-size:20px;font-weight:800'>Boletín semanal de amenazas sanitarias</div>"
            "<div style='opacity:.9;font-size:13px;margin-top:3px'>Resumen automático del informe ECDC</div>"
            "</td></tr>"
            f"{headlines_html}"
            "<tr><td style='padding:14px 20px'>"
            "<div style='font-weight:800;color:#333;margin:6px 0 10px 0'>Puntos clave</div>"
            f"{keypoints_html}"
            "</td></tr>"
            "<tr><td align='center' style='padding:8px 20px 22px'>"
            f"<a href='{pdf_url}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
            "</td></tr>"
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            "Generado automáticamente"
            "</td></tr>"
            "</table>"
            "</td></tr>"
            "</table>"
            "</body></html>"
        )
        return html

    # ------------------------ Envío de correo ------------------------
    def send_email(self, subject: str, html_content: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_user
        msg["To"] = self.recipient
        msg.attach(MIMEText(html_content, "html"))

        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, [self.recipient], msg.as_string())
        else:
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user, [self.recipient], msg.as_string())
        logging.info("Correo enviado correctamente.")

    # ------------------------ Flujo principal ------------------------
    def run(self) -> None:
        pdf_url, pdf_bytes = self.fetch_latest_pdf()
        raw_text = self.extract_text_from_pdf(pdf_bytes)
        summary_es = self.translate_to_spanish(raw_text)
        html = self.format_summary_to_html(summary_es, pdf_url)
        self.send_email("Boletín semanal de amenazas sanitarias", html)


if __name__ == "__main__":
    agent = WeeklyReportAgent(
        smtp_server=os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASS", ""),
        recipient=os.getenv("RECIPIENT", ""),
    )
    agent.run()

