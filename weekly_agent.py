#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import ssl
import json
import time
import smtplib
import logging
import tempfile
import datetime as dt
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

# Silenciar pdfminer (pdfplumber lo usa bajo mano a veces)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfminer.pdfinterp").setLevel(logging.ERROR)

# PDF
import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None  # type: ignore

# Sumario
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

# Traducción (opcional)
try:
    from googletrans import Translator  # type: ignore
except Exception:
    Translator = None  # type: ignore


# ----------------------------- Utilidades -----------------------------

def ensure_nltk_resources() -> bool:
    """Asegura recursos NLTK para sumy (si no, habrá fallback)."""
    try:
        import nltk
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
        # Algunas versiones piden 'punkt_tab'
        try:
            nltk.data.find("tokenizers/punkt_tab/english.pickle")
        except LookupError:
            try:
                nltk.download("punkt_tab", quiet=True)
            except Exception:
                pass
        return True
    except Exception:
        return False


def simple_extractive_summary(text: str, n_sentences: int) -> str:
    """Fallback sencillo sin NLTK: puntúa frases por frecuencia de palabras."""
    import re as _re
    from collections import Counter
    n_sentences = max(1, n_sentences)
    text = (text or "").strip()
    if not text:
        return ""
    sentences = _re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    sentences = sentences[:250] or sentences
    words = _re.findall(r"[A-Za-zÀ-ÿ']{3,}", text.lower())
    if not words:
        return " ".join(sentences[:n_sentences])
    freqs = Counter(words)
    def score(s: str) -> int:
        return sum(freqs.get(w, 0) for w in _re.findall(r"[A-Za-zÀ-ÿ']{3,}", s.lower()))
    ranked = sorted(sentences, key=score, reverse=True)
    return " ".join(ranked[:n_sentences])


# ----------------------------- Config --------------------------------

@dataclass
class Config:
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    direct_pdf_template: str = (
        "https://www.ecdc.europa.eu/sites/default/files/documents/"
        "communicable-disease-threats-report-week-{week}-{year}.pdf"
    )
    summary_sentences: int = int(os.getenv("SUMMARY_SENTENCES", "12") or "12")

    smtp_server: str = os.getenv("SMTP_SERVER", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email: str = os.getenv("SENDER_EMAIL", "")
    receiver_email: str = os.getenv("RECEIVER_EMAIL", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")

    state_path: str = os.getenv("STATE_PATH", "./.weekly_agent_state.json")
    force_send: bool = os.getenv("FORCE_SEND", "0") == "1"

    dry_run: bool = os.getenv("DRY_RUN", "0") == "1"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    max_pdf_mb: int = 25

    highlight_country_names: List[str] = None

    def __post_init__(self):
        if self.highlight_country_names is None:
            self.highlight_country_names = ["españa", "spain"]


# ----------------------------- Agente --------------------------------

class WeeklyReportAgent:
    # Patrones y colores por enfermedad
    DISEASE_STYLES: List[Tuple[str, re.Pattern, str]] = [
        ("CCHF", re.compile(r"\b(cchf|crimean[-\s]?congo)\b", re.I), "#d32f2f"),
        ("Influenza aviar", re.compile(r"\b(h5n1|h7n9|h9n2|influenza\s*aviar|avian)\b", re.I), "#1565c0"),
        ("Virus del Nilo Occidental", re.compile(r"\b(west\s*nile|wnv)\b", re.I), "#2e7d32"),
        ("Sarampión", re.compile(r"\b(measles|sarampi[oó]n)\b", re.I), "#6a1b9a"),
        ("Dengue", re.compile(r"\b(dengue)\b", re.I), "#ef6c00"),
        ("Chikungunya", re.compile(r"\b(chikungunya)\b", re.I), "#8d6e63"),
        ("Mpox", re.compile(r"\b(mpox|monkeypox)\b", re.I), "#00897b"),
        ("Polio", re.compile(r"\b(polio|poliomyelitis|vdpv|wpv)\b", re.I), "#455a64"),
        ("Gripe estacional", re.compile(r"\b(influenza(?!\s*aviar)|\bflu\b|gripe\b)\b", re.I), "#1976d2"),
        ("COVID-19", re.compile(r"\b(covid|sars[-\s]?cov[-\s]?2)\b", re.I), "#0097a7"),
        ("Tos ferina", re.compile(r"\b(pertussis|whooping\s*cough|tos\s*ferina)\b", re.I), "#7b1fa2"),
        ("Fiebre amarilla", re.compile(r"\b(yellow\s*fever|fiebre\s*amarilla)\b", re.I), "#f9a825"),
        ("Fiebre tifoidea", re.compile(r"\b(typhoid|fiebre\s*tifoidea)\b", re.I), "#5d4037"),
    ]
    DEFAULT_COLOR = "#455a64"

    def __init__(self, config: Config) -> None:
        self.config = config
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8",
        })
        from urllib3.util.retry import Retry
        adapter = requests.adapters.HTTPAdapter(
            max_retries=Retry(total=4, backoff_factor=0.6,
                              status_forcelist=(429, 500, 502, 503, 504),
                              allowed_methods=frozenset(["HEAD", "GET"]))
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.translator = Translator() if Translator is not None else None

    # ------------------------ Estado ------------------------

    def _load_state(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.config.state_path):
                with open(self.config.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.warning("No se pudo leer el estado: %s", e)
        return {}

    def _save_state(self, data: Dict[str, Any]) -> None:
        try:
            with open(self.config.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("No se pudo guardar el estado: %s", e)

    # ------------------------ Helpers ------------------------

    @staticmethod
    def _parse_week_year_from_text(s: str) -> Tuple[Optional[int], Optional[int]]:
        s = unquote(s or "").lower()
        mw = re.search(r"week(?:[\s_\-]?)(\d{1,2})", s)
        wy = int(mw.group(1)) if mw else None
        my = re.search(r"(20\d{2})", s)
        yy = int(my.group(1)) if my else None
        return wy, yy

    # ------------------------ Localización del PDF ------------------------

    def _try_direct_weekly_pdf(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        today = dt.date.today()
        year, week, _ = today.isocalendar()
        for delta in range(0, 7):
            w = week - delta
            y = year
            if w <= 0:
                y = year - 1
                w = dt.date(y, 12, 28).isocalendar()[1] + w
            for wk in (str(w), str(w).zfill(2)):
                url = self.config.direct_pdf_template.format(week=wk, year=y)
                try:
                    h = self.session.head(url, timeout=12, allow_redirects=True)
                    if h.status_code == 200 and "pdf" in h.headers.get("Content-Type", "").lower():
                        return url, int(wk), y
                except requests.RequestException:
                    continue
        return None

    def _scan_listing_page(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        def fetch_pdf_from_article(url: str) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
            try:
                r = self.session.get(url, timeout=20); r.raise_for_status()
            except requests.RequestException:
                return None, None, None, None
            soup = BeautifulSoup(r.text, "html.parser")

            published_iso = None
            meta = soup.find("meta", {"property": "article:published_time"}) or soup.find("time", {"itemprop": "datePublished"})
            if meta and meta.get("content"):
                published_iso = meta["content"]
            elif meta and meta.get("datetime"):
                published_iso = meta["datetime"]

            pdf_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    if not href.startswith("http"):
                        href = requests.compat.urljoin(url, href)
                    pdf_url = href
                    break

            title = soup.title.get_text(strip=True) if soup.title else ""
            w, y = self._parse_week_year_from_text(" ".join([title, url, pdf_url or ""]))
            return pdf_url, w, y, published_iso

        try:
            r = self.session.get(self.config.base_url, timeout=20); r.raise_for_status()
        except requests.RequestException as e:
            logging.warning("Listado inaccesible: %s", e); return None

        soup = BeautifulSoup(r.text, "html.parser")
        candidates: List[Tuple[int, int, str, Optional[str]]] = []
        arts, pdfs = [], []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            if not href.startswith("http"):
                href = requests.compat.urljoin(self.config.base_url, href)
            l = href.lower()
            if l.endswith(".pdf") and "communicable-disease-threats-report" in l:
                pdfs.append(href)
            elif "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                arts.append(href)

        arts = arts[:15]
        for art in arts:
            pdf_url, w, y, pub = fetch_pdf_from_article(art)
            if not pdf_url:
                continue
            try:
                h = self.session.head(pdf_url, timeout=12, allow_redirects=True)
                ok = (h.status_code == 200 and "pdf" in h.headers.get("Content-Type", "").lower())
            except requests.RequestException:
                ok = True
            if ok:
                if y is None: y = dt.date.today().year
                w = w or 0
                candidates.append((y, w, pdf_url, pub))

        for href in pdfs:
            w, y = self._parse_week_year_from_text(href)
            if y is None: y = dt.date.today().year
            candidates.append((y, w or 0, href, None))

        if not candidates:
            return None

        # Orden por (año, semana). Si semana es 0, desempatar por fecha publicación.
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        if candidates[0][1] == 0:
            def key_pub(iso: Optional[str]) -> float:
                try:
                    from datetime import datetime
                    return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    return 0.0
            candidates.sort(key=lambda t: key_pub(t[3]), reverse=True)

        y, w, pdf, _ = candidates[0]
        return pdf, (w if w != 0 else None), y

    def fetch_latest_pdf(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        a = self._try_direct_weekly_pdf()
        b = self._scan_listing_page()
        def key(t: Optional[Tuple[str, Optional[int], Optional[int]]]) -> Tuple[int, int]:
            if not t: return (0, 0)
            _, w, y = t; return (y or 0, w or 0)
        best = max([x for x in (a, b) if x], key=key, default=None)
        if best:
            url, w, y = best
            logging.info("Seleccionado PDF: %s (semana=%s, año=%s)", url, w, y)
        return best

    # ------------------------ Descarga / extracción ------------------------

    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        def looks_like_pdf(b: bytes) -> bool: return b.startswith(b"%PDF")
        try:
            h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > max_mb * 1024 * 1024:
                raise RuntimeError("PDF demasiado grande")
        except requests.RequestException:
            pass
        headers = {"Accept": "application/pdf", "Referer": self.config.base_url, "Cache-Control": "no-cache"}
        def _get(url: str) -> Tuple[str, bytes]:
            r = self.session.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True)
            r.raise_for_status()
            it = r.iter_content(8192)
            first = next(it, b"")
            with open(dest_path, "wb") as f:
                if first: f.write(first)
                for chunk in it:
                    if chunk: f.write(chunk)
            return r.headers.get("Content-Type", ""), first
        try:
            ct, first = _get(pdf_url)
            if "pdf" in (ct or "").lower() and looks_like_pdf(first):
                return
        except requests.RequestException:
            pass
        retry_url = pdf_url + ("&download=1" if "?" in pdf_url else "?download=1")
        ct2, first2 = _get(retry_url)
        if not ("pdf" in (ct2 or "").lower() and looks_like_pdf(first2)):
            raise RuntimeError("No se obtuvo un PDF válido (content-type o firma).")

    def extract_text(self, pdf_path: str) -> str:
        try:
            parts = []
            with pdfplumber.open(pdf_path) as pdf:
                for p in pdf.pages:
                    parts.append(p.extract_text() or "")
            return "\n".join(parts)
        except Exception as e:
            logging.warning("Fallo con pdfplumber: %s; probando pdfminer...", e)
            if pm_extract:
                try:
                    return pm_extract(pdf_path)
                except Exception as pm_e:
                    logging.error("Fallo pdfminer: %s", pm_e)
                    return ""
            return ""

    # ------------------------ Resumen / Traducción ------------------------

    def summarize(self, text: str, sentences: int) -> str:
        if not text.strip():
            return ""
        if ensure_nltk_resources():
            try:
                parser = PlaintextParser.from_string(text, Tokenizer("english"))
                sents = LexRankSummarizer()(parser.document, max(1, sentences))
                out = " ".join(str(s) for s in sents)
                if out.strip():
                    return out
            except Exception as e:
                logging.warning("LexRank falló, uso fallback: %s", e)
        return simple_extractive_summary(text, sentences)

    def translate_to_spanish(self, text: str) -> str:
        if not text.strip():
            return ""
        # 1) googletrans
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

    # ------------------------ Helpers de formato ------------------------

    @staticmethod
    def split_sentences(text: str) -> List[str]:
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p.strip() for p in parts if p.strip()]

    def disease_style_for_sentence(self, s: str) -> Tuple[str, str]:
        for label, pat, color in self.DISEASE_STYLES:
            if pat.search(s):
                return label, color
        return "General", self.DEFAULT_COLOR

    def highlight_entities(self, s: str) -> str:
        s_html = s
        # Negrita en nombre de enfermedad capturada (cosmético)
        for _, pat, _ in self.DISEASE_STYLES:
            s_html = pat.sub(lambda m: "<b style='color:#8b0000'>{}</b>".format(m.group(0)), s_html)
        # Países en verde (básico)
        countries = [
            "spain","españa","france","italy","germany","portugal","greece","poland","romania",
            "netherlands","belgium","sweden","norway","finland","denmark","ireland","austria",
            "czech","hungary","bulgaria","croatia","estonia","latvia","lithuania","slovakia",
            "slovenia","switzerland","iceland","turkey","cyprus","malta","ukraine","serbia","bosnia"
        ]
        for c in countries:
            s_html = re.sub(
                rf"\b{re.escape(c)}\b",
                lambda m: "<span style='color:#0b6e0b;font-weight:600'>{}</span>".format(m.group(0).title()),
                s_html,
                flags=re.I
            )
        return s_html

    def highlight_priority_country(self, s_html: str) -> str:
        plain = re.sub(r"<[^>]+>", "", s_html).lower()
        if any(k in plain for k in self.config.highlight_country_names):
            return ("🇪🇸 <span style='background:#fff7d6;padding:2px 4px;border-radius:4px;"
                    "border-left:4px solid #ff9800'>{}</span>").format(s_html)
        return s_html

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
            title_html = self.highlight_priority_country(self.highlight_entities(title))
            body_html = self.highlight_priority_country(self.highlight_entities(body)) if body else ""
            body_block = "<div style='font-size:14px;color:#333;opacity:.9'>{}</div>".format(body_html) if body_html else ""
            card = (
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
            ).format(color=color, label=label, title=title_html, body=body_block)
            cards.append(card)
        return "".join(cards)

    # ------------------------ HTML del correo ------------------------

    def format_summary_to_html(self, summary_es: str, pdf_url: str, week: Optional[int], year: Optional[int]) -> str:
        sentences = self.split_sentences(summary_es)

        headlines_html = self.render_headline_cards(sentences, n_cards=3)

        bullets = []
        kp_range = sentences[3:11] if len(sentences) > 3 else sentences[:8]
        for s in kp_range:
            label, color = self.disease_style_for_sentence(s)
            content = self.highlight_priority_country(self.highlight_entities(s))
            chip = "<span style='background:{color};color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>{label}</span>".format(
                color=color, label=label
            )
            item = (
                "<li style='margin:8px 0;list-style:none'>"
                "<div style='border-left:6px solid {color};padding-left:10px'>{chip}{content}</div>"
                "</li>"
            ).format(color=color, chip=chip, content=content)
            bullets.append(item)
        keypoints_html = "<ul style='padding-left:0;margin:0'>{}</ul>".format("".join(bullets) if bullets else "<li>Sin datos</li>")

        title_week = "Semana {} · {}".format(week, year) if week and year else "Último informe ECDC"

        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#333'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:18px 12px'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='700' cellspacing='0' cellpadding='0' style='max-width:700px;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.06)'>"
            "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
            "<div style='font-size:20px;font-weight:800'>Boletín semanal de amenazas sanitarias</div>"
            "<div style='opacity:.9;font-size:13px;margin-top:3px'>{title_week}</div>"
            "</td></tr>"
            "{headlines}"
            "<tr><td style='padding:14px 20px'>"
            "<div style='font-weight:800;color:#333;margin:6px 0 10px 0'>Puntos clave</div>"
            "{keypoints}"
            "</td></tr>"
            "<tr><td align='center' style='padding:8px 20px 22px'>"
            "<a href='{pdf}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
            "</td></tr>"
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            "Generado automáticamente · {utc}"
            "</td></tr>"
            "</table>"
            "</td></tr>"
            "</table>"
            "</body></html>"
        ).format(
            title_week=title_week,
            headlines=headlines_html,
            keypoints=keypoints_html,
            pdf=pdf_url,
            utc=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        )
        return html

    # ------------------------ Envío ------------------------

    def send_email(self, subject: str, plain: str, html: Optional[str]) -> None:
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.sender_email
        msg["To"] = self.config.receiver_email
        msg.set_content(plain or "(vacío)")
        if html:
            msg.add_alternative(html, subtype="html")

        ctx = ssl.create_default_context()
        if self.config.smtp_port == 465:
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx) as s:
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.send_message(msg)
        logging.info("Correo enviado correctamente.")

    # ------------------------ Run ------------------------

    def run(self) -> None:
        found = self.fetch_latest_pdf()
        if not found:
            logging.info("No hay PDF nuevo."); return
        pdf_url, week, year = found

        state = self._load_state()
        if state.get("last_pdf_url") == pdf_url and not self.config.force_send:
            logging.info("PDF ya enviado anteriormente. No se reenvía.")
            return

        tmp_path = ""
        text = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name
            self.download_pdf(pdf_url, tmp_path, max_mb=self.config.max_pdf_mb)
            text = self.extract_text(tmp_path) or ""
        finally:
            if tmp_path:
                for _ in range(3):
                    try:
                        os.remove(tmp_path)
                        break
                    except Exception:
                        time.sleep(0.2)

        if not text.strip():
            logging.warning("PDF sin texto extraíble."); return

        summary_en = self.summarize(text, self.config.summary_sentences)
        if not summary_en.strip():
            logging.warning("No se pudo generar resumen."); return

        summary_es = self.translate_to_spanish(summary_en)

        html = self.format_summary_to_html(summary_es, pdf_url, week, year)
        subject = "ECDC CDTR – Semana {} ({})".format(week, year) if week and year else "Resumen del informe semanal del ECDC"

        # Plain: solo titulares (3) + link
        sentences = self.split_sentences(summary_es)
        plain_lines = ["Boletín semanal de amenazas sanitarias"]
        if week and year:
            plain_lines.append("Semana {} · {}".format(week, year))
        plain_lines.append("")
        for s in sentences[:3]:
            t, _ = self.split_headline(s)
            plain_lines.append("- " + t)
        plain_lines.append("")
        plain_lines.append("Informe completo: {}".format(pdf_url))
        plain = "\n".join(plain_lines)

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no se envía email. Asunto: %s", subject)
            logging.debug("PLAIN:\n%s", plain)
            return

        self.send_email(subject, plain, html)

        state.update({
            "last_pdf_url": pdf_url,
            "last_week": week,
            "last_year": year,
            "last_sent_utc": dt.datetime.utcnow().isoformat() + "Z",
        })
        self._save_state(state)


# ----------------------------- main ----------------------------------

def main() -> None:
    cfg = Config()
    WeeklyReportAgent(cfg).run()

if __name__ == "__main__":
    main()
