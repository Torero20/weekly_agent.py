#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import ssl
import smtplib
import time
import json
import logging
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfminer.pdfinterp").setLevel(logging.ERROR)
import tempfile
import datetime as dt
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

# PDF
import pdfplumber  # type: ignore
try:
    from pdfminer_high_level import extract_text as pm_extract  # type: ignore
except Exception:
    try:
        from pdfminer.high_level import extract_text as pm_extract  # type: ignore
    except Exception:
        pm_extract = None  # type: ignore

# Sumario
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

# Traducción opcional
try:
    from googletrans import Translator  # type: ignore
except Exception:
    Translator = None  # type: ignore


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

def _ensure_nltk_resources() -> bool:
    """Garantiza recursos NLTK (punkt/punkt_tab) si están disponibles; si falla, devolvemos False."""
    try:
        import nltk
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
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


def _simple_extractive_summary(text: str, n_sentences: int) -> str:
    """Fallback sin NLTK: segmenta por puntuación y puntúa por frecuencia de palabras."""
    import re
    from collections import Counter
    n_sentences = max(1, n_sentences)
    text = (text or "").strip()
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    sentences = sentences[:250] or sentences
    words = re.findall(r"[A-Za-zÀ-ÿ']{3,}", text.lower())
    if not words:
        return " ".join(sentences[:n_sentences])
    freqs = Counter(words)
    def score(s: str) -> int:
        return sum(freqs.get(w, 0) for w in re.findall(r"[A-Za-zÀ-ÿ']{3,}", s.lower()))
    ranked = sorted(sentences, key=score, reverse=True)
    return " ".join(ranked[:n_sentences])


# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

@dataclass
class Config:
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    direct_pdf_template: str = (
        "https://www.ecdc.europa.eu/sites/default/files/documents/"
        "communicable-disease-threats-report-week-{week}-{year}.pdf"
    )
    pdf_regex: re.Pattern = re.compile(
        r"/communicable-disease-threats-report-week-(\d{1,2})-(\d{4})\.pdf$"
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


# ---------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------

class WeeklyReportAgent:
    """
    1) Localiza el último PDF (URL directa o dentro de la página de artículo).
    2) Descarga, extrae, resume (LexRank con fallback), traduce (opcional).
    3) Genera correo con HTML visual (puntos clave + secciones por enfermedad) y lo envía.
    4) Evita duplicados guardando el último PDF enviado.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8",
        })
        from urllib3.util.retry import Retry
        adapter = requests.adapters.HTTPAdapter(
            max_retries=Retry(
                total=4,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["HEAD", "GET"]),
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.translator = Translator() if Translator is not None else None

    # ------------------------ Estado -----------------------------------

    def _load_state(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.config.state_path):
                with open(self.config.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logging.warning("No se pudo leer state_path (%s): %s", self.config.state_path, e)
        return {}

    def _save_state(self, data: Dict[str, Any]) -> None:
        try:
            with open(self.config.state_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.warning("No se pudo guardar state_path (%s): %s", self.config.state_path, e)

    # ------------------------ Helpers ----------------------------------

    @staticmethod
    def _parse_week_year_from_text(s: str) -> Tuple[Optional[int], Optional[int]]:
        # Normaliza: lowercase + decodifica %20 -> espacio, etc.
        s = unquote(s or "").lower()
        mw = re.search(r"week(?:[\s_\-]?)(\d{1,2})", s)
        wy = int(mw.group(1)) if mw else None
        my = re.search(r"(20\d{2})", s)
        yy = int(my.group(1)) if my else None
        return wy, yy

    # ------------------------ Localización del PDF ---------------------

    def _try_direct_weekly_pdf(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        today = dt.date.today()
        year, week, _ = today.isocalendar()
        for delta in range(0, 7):
            w = week - delta
            y = year
            if w <= 0:
                y = year - 1
                last_week_prev_year = dt.date(y, 12, 28).isocalendar()[1]
                w = last_week_prev_year + w
            for wk in (str(w), str(w).zfill(2)):
                url = self.config.direct_pdf_template.format(week=wk, year=y)
                try:
                    h = self.session.head(url, timeout=12, allow_redirects=True)
                    logging.debug("HEAD %s -> %s", url, getattr(h, "status_code", "?"))
                    ct = h.headers.get("Content-Type", "").lower()
                    if h.status_code == 200 and "pdf" in ct:
                        return url, int(wk), y
                except requests.RequestException:
                    continue
        return None

    def _scan_listing_page(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        """
        Rastrea el listado, entra en artículos, encuentra el PDF y extrae week/year.
        """
        def fetch_pdf_from_article(url: str) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
            try:
                r = self.session.get(url, timeout=20)
                r.raise_for_status()
            except requests.RequestException:
                return None, None, None, None
            soup = BeautifulSoup(r.text, "html.parser")
            # Fecha (opcional)
            published_iso = None
            meta_pub = soup.find("meta", {"property": "article:published_time"}) or soup.find("time", {"itemprop": "datePublished"})
            if meta_pub and meta_pub.get("content"):
                published_iso = meta_pub["content"]
            elif meta_pub and meta_pub.get("datetime"):
                published_iso = meta_pub["datetime"]
            # Primer PDF interno
            pdf_url = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    if not href.startswith("http"):
                        href = requests.compat.urljoin(url, href)
                    pdf_url = href
                    break
            full_text = " ".join([
                soup.title.get_text(strip=True) if soup.title else "",
                url,
                pdf_url or ""
            ])
            w, y = self._parse_week_year_from_text(full_text)
            return pdf_url, w, y, published_iso

        try:
            r = self.session.get(self.config.base_url, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            logging.warning("No se pudo cargar la página de listados: %s", e)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        candidates: List[Tuple[int, int, str, Optional[str]]] = []
        article_links: List[str] = []
        direct_pdfs: List[str] = []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            if not href.startswith("http"):
                href = requests.compat.urljoin(self.config.base_url, href)
            lch = href.lower()
            if lch.endswith(".pdf") and "communicable-disease-threats-report" in lch:
                direct_pdfs.append(href)
            elif "communicable-disease-threats-report" in lch and ("/publications-data/" in lch or "/publications-and-data/" in lch):
                article_links.append(href)

        article_links = article_links[:15]  # limitar carga

        # a) artículos
        for art in article_links:
            pdf_url, w, y, published_iso = fetch_pdf_from_article(art)
            if w is None or y is None:
                w2, y2 = self._parse_week_year_from_text(art)
                if w is None:
                    w = w2
                if y is None:
                    y = y2
            if not pdf_url:
                continue
            ok = True
            try:
                h = self.session.head(pdf_url, timeout=12, allow_redirects=True)
                ct = h.headers.get("Content-Type", "").lower()
                if h.status_code != 200 or "pdf" not in ct:
                    ok = False
            except requests.RequestException:
                ok = True
            if ok:
                if y is None:
                    y = dt.date.today().year
                w = w or 0
                logging.debug("Artículo %s -> PDF %s | week=%s year=%s", art, pdf_url, w, y)
                candidates.append((y, w, pdf_url, published_iso))

        # b) PDFs directos del listado
        for href in direct_pdfs:
            w, y = self._parse_week_year_from_text(href)
            if y is None:
                y = dt.date.today().year
            w = w or 0
            candidates.append((y, w, href, None))

        if not candidates:
            return None

        # Orden principal
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)

        # Si el top no trae week, desempatar por fecha publicación
        top_year, top_week, _, _ = candidates[0]
        if top_week == 0:
            def published_key(iso: Optional[str]) -> float:
                try:
                    from datetime import datetime
                    return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    return 0.0
            candidates.sort(key=lambda t: published_key(t[3]), reverse=True)

        best_y, best_w, best_pdf, _ = candidates[0]
        best_w = best_w if best_w != 0 else None
        return best_pdf, best_w, best_y

    def fetch_latest_pdf(self) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
        a = self._try_direct_weekly_pdf()
        b = self._scan_listing_page()
        def key(t: Optional[Tuple[str, Optional[int], Optional[int]]]) -> Tuple[int, int]:
            if not t:
                return (0, 0)
            _, w, y = t
            return (y or 0, w or 0)
        best = max([x for x in (a, b) if x], key=key, default=None)
        if best:
            url, w, y = best
            logging.info("%s seleccionado: %s (week=%s, year=%s)",
                         "PDF directo" if best == a else "PDF de artículo", url, w, y)
        return best

    # --------------------- Descarga / extracción -----------------------

    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        def _append_download_param(url: str) -> str:
            return url + ("&download=1" if "?" in url else "?download=1")
        def _looks_like_pdf(first_bytes: bytes) -> bool:
            return first_bytes.startswith(b"%PDF")
        try:
            h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > max_mb * 1024 * 1024:
                raise RuntimeError(f"El PDF excede {max_mb} MB ({int(clen)/1024/1024:.1f} MB)")
        except requests.RequestException:
            pass
        headers = {"Accept": "application/pdf", "Referer": self.config.base_url, "Cache-Control": "no-cache"}
        def _try_get(url: str) -> Tuple[str, Optional[str], bytes]:
            r = self.session.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            it = r.iter_content(chunk_size=8192)
            first = next(it, b"")
            with open(dest_path, "wb") as f:
                if first:
                    f.write(first)
                for chunk in it:
                    if chunk:
                        f.write(chunk)
            return ct, r.headers.get("Content-Length"), first
        try:
            ct, clen, first = _try_get(pdf_url)
            logging.debug("GET %s -> Content-Type=%s, len=%s", pdf_url, ct, clen)
            if ("pdf" in (ct or "").lower()) and _looks_like_pdf(first):
                return
            logging.info("Respuesta no-PDF. Reintentando con ?download=1 ...")
        except requests.RequestException as e:
            logging.info("GET inicial falló (%s). Reintentamos con ?download=1 ...", e)
        retry_url = _append_download_param(pdf_url)
        ct2, clen2, first2 = _try_get(retry_url)
        logging.debug("GET %s -> Content-Type=%s, len=%s", retry_url, ct2, clen2)
        if ("pdf" in (ct2 or "").lower()) and _looks_like_pdf(first2):
            return
        raise RuntimeError(f"No se obtuvo un PDF válido (Content-Type={ct2!r}, firma={first2[:8]!r}).")

    def extract_text(self, pdf_path: str) -> str:
        try:
            parts = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    parts.append(page.extract_text() or "")
            return "\n".join(parts)
        except Exception as e:
            logging.warning("Fallo pdfplumber: %s. Probando pdfminer...", e)
            if pm_extract:
                try:
                    return pm_extract(pdf_path)
                except Exception as pm_e:
                    logging.error("Fallo pdfminer: %s", pm_e)
                    return ""
            logging.error("pdfminer no disponible.")
            return ""

    # -------------------------- Sumario --------------------------------

    def summarize(self, text: str, sentences: int) -> str:
        if not text.strip():
            return ""
        if _ensure_nltk_resources():
            try:
                parser = PlaintextParser.from_string(text, Tokenizer("english"))
                summarizer = LexRankSummarizer()
                sents = summarizer(parser.document, max(1, sentences))
                out = " ".join(str(s) for s in sents)
                if out.strip():
                    return out
            except Exception as e:
                logging.warning("LexRank falló; uso fallback: %s", e)
        else:
            logging.info("NLTK no disponible; uso fallback.")
        return _simple_extractive_summary(text, sentences)

    # --------------------- Formato visual del correo -------------------

    DISEASE_PATTERNS: Dict[str, re.Pattern] = {
        "CCHF": re.compile(r"\b(crimean[-\s]?congo|cchf)\b", re.I),
        "Influenza aviar": re.compile(r"\bavian|influenza\s+avian|h5n1|h7n9|h9n2\b", re.I),
        "Virus del Nilo Occidental": re.compile(r"\bwest nile|wnv\b", re.I),
        "Sarampión": re.compile(r"\bmeasles\b", re.I),
        "Dengue": re.compile(r"\bdengue\b", re.I),
        "Chikungunya": re.compile(r"\bchikungunya\b", re.I),
        "Mpox": re.compile(r"\bmpox|monkeypox\b", re.I),
        "Polio": re.compile(r"\b(polio|poliomyelitis|vdpv|wpv)\b", re.I),
        "Gripe estacional": re.compile(r"\b(influenza(?!\s*avian)|flu)\b", re.I),
        "COVID-19": re.compile(r"\bcovid|sars[-\s]?cov[-\s]?2\b", re.I),
        "Tos ferina": re.compile(r"\b(pertussis|whooping\s+cough)\b", re.I),
        "Fiebre amarilla": re.compile(r"\byellow fever\b", re.I),
        "Fiebre tifoidea": re.compile(r"\btyphoid\b", re.I),
    }

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        # Segmentar en frases para poblar viñetas/secciones
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        return [p.strip() for p in parts if p.strip()]

    def _highlight_entities(self, s: str) -> str:
        # Negritas en nombres de enfermedades y países (heurístico simple para países comunes EU/OMS)
        s_html = s
        for name, pat in self.DISEASE_PATTERNS.items():
            s_html = pat.sub(lambda m: f"<strong style='color:#8b0000'>{m.group(0)}</strong>", s_html)

        country_list = [
            "spain","france","italy","germany","portugal","greece","poland","romania","netherlands","belgium",
            "sweden","norway","finland","denmark","ireland","uk","united kingdom","austria","czech","hungary",
            "bulgaria","croatia","estonia","latvia","lithuania","slovakia","slovenia","switzerland","iceland",
            "turkey","cyprus","malta","ukraine","russia","georgia","moldova","serbia","bosnia","albania",
            "montenegro","north macedonia"
        ]
        for c in country_list:
            s_html = re.sub(rf"\b{re.escape(c)}\b", lambda m: f"<span style='color:#0b6e0b;font-weight:600'>{m.group(0).title()}</span>", s_html, flags=re.I)
        return s_html

    def format_summary_to_html(self, summary_es: str) -> Tuple[str, str]:
        """
        Devuelve (html_keypoints, html_by_disease)
        - html_keypoints: lista <li> con primeras N frases
        - html_by_disease: bloques por enfermedad con las frases que la mencionan
        """
        sentences = self._split_sentences(summary_es)
        # Key points = primeras 6 frases (o todas si menos)
        keypoints = sentences[:6]
        lis = []
        for s in keypoints:
            lis.append(f"<li style='margin:6px 0'>{self._highlight_entities(s)}</li>")
        html_keypoints = "\n".join(lis) if lis else "<li>Sin datos destacados.</li>"

        # Agrupar por enfermedad
        buckets: Dict[str, List[str]] = {k: [] for k in self.DISEASE_PATTERNS.keys()}
        others: List[str] = []
        for s in sentences:
            placed = False
            for name, pat in self.DISEASE_PATTERNS.items():
                if pat.search(s):
                    buckets[name].append(self._highlight_entities(s))
                    placed = True
            if not placed:
                others.append(self._highlight_entities(s))

        sections = []
        for name, items in buckets.items():
            if not items:
                continue
            items_html = "".join(f"<li style='margin:4px 0'>{it}</li>" for it in items[:6])
            sections.append(
                f"""
                <tr>
                  <td style="padding:12px 14px;border-bottom:1px solid #eee">
                    <div style="font-weight:700;color:#333;margin-bottom:6px">{name}</div>
                    <ul style="padding-left:18px;margin:0">{items_html}</ul>
                  </td>
                </tr>
                """.strip()
            )
        if others:
            items_html = "".join(f"<li style='margin:4px 0'>{it}</li>" for it in others[:6])
            sections.append(
                f"""
                <tr>
                  <td style="padding:12px 14px;border-bottom:1px solid #eee">
                    <div style="font-weight:700;color:#333;margin-bottom:6px">Otros</div>
                    <ul style="padding-left:18px;margin:0">{items_html}</ul>
                  </td>
                </tr>
                """.strip()
            )
        html_by_disease = "\n".join(sections) if sections else ""
        return f"<ul style='padding-left:18px;margin:0'>{html_keypoints}</ul>", html_by_disease

    # ------------------------- Construcción HTML -----------------------

    def build_html(self, summary_es: str, pdf_url: str, week: Optional[int], year: Optional[int]) -> str:
        keypoints_html, by_disease_html = self.format_summary_to_html(summary_es)
        title_week = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        # Layout basado en tablas con estilos inline -> compatibilidad Gmail/Outlook
        return f"""
        <html>
          <body style="margin:0;padding:0;background:#f5f7fb">
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fb;padding:18px 12px">
              <tr>
                <td align="center">
                  <table role="presentation" width="680" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06)">
                    <tr>
                      <td style="background:#0b5cab;color:#fff;padding:18px 20px">
                        <div style="font-size:20px;font-weight:700">Boletín semanal de amenazas sanitarias</div>
                        <div style="opacity:.9;font-size:14px;margin-top:4px">{title_week}</div>
                      </td>
                    </tr>

                    <tr>
                      <td style="padding:18px 20px">
                        <div style="font-weight:700;color:#333;margin-bottom:8px">Puntos clave</div>
                        {keypoints_html}
                      </td>
                    </tr>

                    {"<tr><td style='padding:0 20px 6px'><div style='height:1px;background:#eee'></div></td></tr>" if by_disease_html else ""}

                    {f"""
                    <tr>
                      <td style="padding:6px 20px 14px">
                        <div style="font-weight:700;color:#333;margin-bottom:8px">Detalle por enfermedad</div>
                        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #f0f0f0;border-radius:8px;overflow:hidden">
                          {by_disease_html}
                        </table>
                      </td>
                    </tr>
                    """ if by_disease_html else ""}

                    <tr>
                      <td align="center" style="padding:8px 20px 22px">
                        <a href="{pdf_url}" style="display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:700">Abrir informe completo (PDF)</a>
                      </td>
                    </tr>

                    <tr>
                      <td style="background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center">
                        Generado automáticamente · {dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """.strip()

    # ------------------------- Envío -----------------------------------

    def send_email(self, subject: str, plain: str, html: Optional[str] = None) -> None:
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

        context = ssl.create_default_context()
        try:
            if self.config.smtp_port == 465:
                logging.debug("SMTP: SSL (465) a %s ...", self.config.smtp_server)
                with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=context) as server:
                    server.ehlo()
                    if self.config.email_password:
                        server.login(self.config.sender_email, self.config.email_password)
                    server.send_message(msg)
            else:
                logging.debug("SMTP: STARTTLS (%s) a %s ...", self.config.smtp_server, self.config.smtp_port)
                with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    if self.config.email_password:
                        server.login(self.config.sender_email, self.config.email_password)
                    server.send_message(msg)
            logging.info("Correo enviado correctamente.")
        except Exception as e:
            logging.exception("Error enviando email: %s", e)
            raise

    # --------------------------- Run -----------------------------------

    def run(self) -> None:
        found = self.fetch_latest_pdf()
        if not found:
            logging.info("No hay PDF nuevo o no se encontró ninguno.")
            return

        pdf_url, week, year = found

        # Evitar duplicados
        state = self._load_state()
        last_url = state.get("last_pdf_url")
        if last_url == pdf_url and not self.config.force_send:
            logging.info("PDF ya enviado previamente y FORCE_SEND!=1. No se reenvía. (%s)", pdf_url)
            return

        tmp_path = ""
        text = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = tmp.name

            try:
                self.download_pdf(pdf_url, tmp_path, max_mb=self.config.max_pdf_mb)
            except Exception as e:
                logging.exception("Fallo descargando el PDF: %s", e)
                return

            try:
                text = self.extract_text(tmp_path) or ""
            except Exception as e:
                logging.exception("Fallo extrayendo texto: %s", e)
                text = ""
        finally:
            if tmp_path:
                for _ in range(3):
                    try:
                        os.remove(tmp_path)
                        break
                    except Exception:
                        time.sleep(0.2)

        if not text.strip():
            logging.warning("El PDF no contiene texto extraíble.")
            return

        try:
            summary_en = self.summarize(text, self.config.summary_sentences)
        except Exception as e:
            logging.exception("Fallo generando el resumen: %s", e)
            return
        if not summary_en.strip():
            logging.warning("No se pudo generar resumen.")
            return

        try:
            summary_es = self.translate_to_spanish(summary_en)
        except Exception as e:
            logging.exception("Fallo traduciendo, envío el original en inglés: %s", e)
            summary_es = summary_en

        # Construir HTML visual + asunto
        html = self.build_html(summary_es, pdf_url, week, year)
        subject = f"ECDC CDTR – Week {week} ({year})" if week and year else "Resumen del informe semanal del ECDC"

        # Plain text (para clientes que no muestren HTML): usa puntos clave
        plain_sentences = self._split_sentences(summary_es)[:8]
        plain = "Boletín semanal de amenazas sanitarias\n" + \
                (f"Semana {week} · {year}\n\n" if week and year else "\n") + \
                "\n- ".join([""] + plain_sentences) + \
                f"\n\nInforme completo: {pdf_url}"

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no se envía email. Asunto: %s", subject)
            logging.debug("Resumen ES (plain):\n%s", plain)
            return

        try:
            self.send_email(subject, plain, html)
        except Exception as e:
            logging.exception("Fallo enviando el email: %s", e)
            return

        # Guardar estado
        state.update({
            "last_pdf_url": pdf_url,
            "last_week": week,
            "last_year": year,
            "last_sent_utc": dt.datetime.utcnow().isoformat() + "Z",
        })
        self._save_state(state)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main() -> None:
    cfg = Config()
    agent = WeeklyReportAgent(cfg)
    agent.run()

if __name__ == "__main__":
    main()
