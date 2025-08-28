"""
Weekly ECDC Agent – descarga el PDF más reciente, extrae texto, resume (LexRank),
traduce al español y lo envía por email (HTML + texto). Soporta:
- Búsqueda robusta del PDF (lista + páginas de detalle).
- Evita reenvíos con .agent_state.json.
- pdfplumber -> fallback pdfminer.six.
- NLTK (punkt) se descarga automáticamente.
- SMTP con app password. (También admite DIRECT_PDF_URL para forzar un PDF.)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None

# sumy para resumen
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.lex_rank import LexRankSummarizer


# ---------- Utilidades ----------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "weekly-ecdc-agent/1.0"})
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

def _normalize(text: str) -> str:
    text = re.sub(r"-\n", "", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text

# --- NLTK punkt bootstrap (para sumy) ---
def _ensure_punkt() -> None:
    try:
        import nltk  # type: ignore
        try:
            nltk.data.find("tokenizers/punkt")
        except LookupError:
            nltk.download("punkt", quiet=True)
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError:
            try:
                nltk.download("punkt_tab", quiet=True)
            except Exception:
                pass
    except Exception:
        pass

# Traducción con fallback (puedes desactivar con NO_TRANSLATE=1)
class TranslatorClient:
    def __init__(self) -> None:
        self.disabled = os.getenv("NO_TRANSLATE", "0").lower() in {"1", "true", "yes"}
        try:
            from googletrans import Translator  # type: ignore
            self._gt = Translator()
        except Exception:
            self._gt = None
    def translate(self, text: str, dest: str = "es") -> str:
        if self.disabled or not text:
            return text
        if self._gt:
            try:
                return self._gt.translate(text, dest=dest).text
            except Exception:
                return text
        return text


# ---------- Config ----------
@dataclass
class Config:
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    pdf_pattern: str = r"\.pdf"  # permisivo (no exige terminar en .pdf)
    summary_sentences: int = 8
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465
    sender_email: str = "agentia70@gmail.com"
    receiver_email: str = "contra1270@gmail.com"
    ca_file: Optional[str] = None
    state_path: str = os.getenv("AGENT_STATE_PATH", ".agent_state.json")


# ---------- Agente ----------
class WeeklyReportAgent:
    def __init__(self, config: Config, translate: bool = True, dry_run: bool = False) -> None:
        self.config = config
        self.session = make_session()
        self.translator = TranslatorClient()
        if not translate:
            self.translator.disabled = True
        self.dry_run = dry_run

    # ---- estado (evitar duplicados) ----
    def _load_state(self) -> dict:
        if os.path.exists(self.config.state_path):
            try:
                with open(self.config.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}
    def _save_state(self, d: dict) -> None:
        try:
            with open(self.config.state_path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception as e:
            logging.warning("No se pudo guardar estado: %s", e)

    # ---- extracción robusta de PDFs en HTML ----
    def _extract_pdf_candidates(self, html_text: str, base: str) -> List[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        urls: set[str] = set()
        def add(u: Optional[str]) -> None:
            if not u:
                return
            full = u if u.startswith("http") else requests.compat.urljoin(base, u)
            urls.add(full)
        for a in soup.find_all("a"):
            for attr in ("href", "data-asset-url", "data-file", "data-href"):
                v = a.get(attr)
                if v and (".pdf" in v.lower() or re.search(self.config.pdf_pattern, v, re.IGNORECASE)):
                    add(v)
        for tag in soup.find_all(["link", "source", "meta"]):
            for attr in ("href", "src", "content"):
                v = tag.get(attr)
                if v and ".pdf" in v.lower():
                    add(v)
        for s in soup.find_all("script"):
            txt = (s.string or "") + (s.get_text() or "")
            for m in re.findall(r'["\'](https?://[^"\']*?\.pdf[^"\']*)["\']|["\'](\/[^"\']*?\.pdf[^"\']*)["\']', txt, flags=re.I):
                add(m[0] or m[1])
        goods: List[str] = []
        for u in urls:
            try:
                h = self.session.head(u, timeout=12, allow_redirects=True)
                ct = h.headers.get("Content-Type", "").lower()
                if "pdf" in ct or u.lower().endswith(".pdf"):
                    goods.append(u)
            except requests.RequestException:
                if u.lower().endswith(".pdf"):
                    goods.append(u)
        return sorted(set(goods))

    # ---- localizar PDF (lista + detalle) ----
    def fetch_latest_pdf_url(self) -> Optional[str]:
        r = self.session.get(self.config.base_url, timeout=30)
        r.raise_for_status()
        base_html = r.text
        pdfs = self._extract_pdf_candidates(base_html, self.config.base_url)
        if pdfs:
            latest = pdfs[-1]
            st = self._load_state()
            if st.get("last_pdf_url") == latest:
                logging.info("Último informe ya enviado; no se reenvía.")
                return None
            st["last_pdf_url"] = latest
            self._save_state(st)
            return latest

        soup = BeautifulSoup(base_html, "html.parser")
        detail_urls: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#"):
                continue
            full = href if href.startswith("http") else requests.compat.urljoin(self.config.base_url, href)
            if "ecdc.europa.eu" not in full.lower() or ".pdf" in full.lower():
                continue
            if any(seg in full.lower() for seg in ("/publications", "/publications-data", "/news", "/news-events")):
                detail_urls.append(full)
        seen: set[str] = set()
        for u in detail_urls[:30]:
            if u in seen:
                continue
            seen.add(u)
            try:
                rr = self.session.get(u, timeout=30)
                rr.raise_for_status()
                inner = self._extract_pdf_candidates(rr.text, u)
                if inner:
                    latest = inner[-1]
                    st = self._load_state()
                    if st.get("last_pdf_url") == latest:
                        logging.info("Último informe ya enviado; no se reenvía.")
                        return None
                    st["last_pdf_url"] = latest
                    self._save_state(st)
                    return latest
            except requests.RequestException:
                continue
        return None

    # ---- descarga y extracción de texto ----
    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        try:
            h = self.session.head(pdf_url, timeout=20, allow_redirects=True)
            sz = h.headers.get("Content-Length")
            if sz and int(sz) > max_mb * 1024 * 1024:
                raise RuntimeError("PDF demasiado grande")
        except requests.RequestException:
            pass
        with self.session.get(pdf_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)

    def extract_text_from_pdf(self, pdf_path: str) -> str:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                txt = "\n".join((p.extract_text() or "") for p in pdf.pages)
            if len(txt.strip()) > 200:
                return _normalize(txt)
        except Exception:
            pass
        if pm_extract:
            try:
                return _normalize(pm_extract(pdf_path) or "")
            except Exception:
                return ""
        return ""

    # ---- resumen (¡ojo a la sangría: 4 espacios en def, 8 en cuerpo!) ----
    def summarize_text(self, text: str) -> str:
        if not text:
            return ""
        _ensure_punkt()
        snippet = text[:20000]
        parser = PlaintextParser.from_string(snippet, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sentences = summarizer(parser.document, self.config.summary_sentences)
        return " ".join(str(s) for s in sentences)

    # ---- email HTML ----
    @staticmethod
    def _highlight_spain(text_escaped_html: str) -> str:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text_escaped_html) if s.strip()]
        keys = ["España", "Spain", "Espana"]
        out = []
        for s in sentences:
            if any(k.lower() in s.lower() for k in keys):
                out.append('<span style="background-color:#eaf2fa;border-left:4px solid #005ba4;padding-left:4px;">' + s + "</span>")
            else:
                out.append(s)
        return " ".join(out)

    def build_html_email(self, summary_es: str, source_url: str) -> str:
        safe = html.escape(summary_es)
        highlighted = self._highlight_spain(safe)
        paragraphs = [p.strip() for p in highlighted.split("\n") if p.strip()]
        paragraph_html = "".join(f'<p style="margin:0 0 12px 0;">{p}</p>' for p in paragraphs)
        return f"""
        <html>
          <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.4;background-color:#f7f7f7;padding:20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:auto;background-color:#ffffff;border-radius:8px;overflow:hidden;">
              <tr><td style="background-color:#005ba4;color:#ffffff;padding:20px;">
                <h1 style="margin:0;font-size:24px;">Boletín semanal de amenazas sanitarias</h1>
                <p style="margin:0;font-size:14px;">Resumen del informe semanal</p>
              </td></tr>
              <tr><td style="padding:20px;">
                {paragraph_html}
                <p style="margin-top:20px;">Más detalles en el <a href="{html.escape(source_url)}" style="color:#005ba4;text-decoration:underline;">PDF original</a>.</p>
              </td></tr>
            </table>
          </body>
        </html>
        """

    # ---- envío por SMTP ----
    def send_email(self, subject: str, body: str, html_body: Optional[str] = None) -> None:
        import smtplib
        from email.message import EmailMessage
        sender = self.config.sender_email
        receiver = self.config.receiver_email
        if not sender or not receiver:
            raise ValueError("Debes definir SENDER_EMAIL y RECEIVER_EMAIL")
        password = os.getenv("EMAIL_PASSWORD")
        if not password:
            raise ValueError("EMAIL_PASSWORD no definido (SMTP)")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = receiver
        msg.set_content(body or "(Sin texto)")
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        context = ssl.create_default_context(cafile=self.config.ca_file) if self.config.ca_file else ssl.create_default_context()
        with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=context) as server:
            server.login(sender, password)
            server.send_message(msg)

    # ---- pipeline ----
    def run(self) -> None:
        # Permite forzar PDF con variable DIRECT_PDF_URL
        override = os.getenv("DIRECT_PDF_URL")
        if override and override.lower().endswith(".pdf"):
            pdf_url = override
        else:
            pdf_url = self.fetch_latest_pdf_url()
        if not pdf_url:
            logging.info("No hay PDF nuevo o no se encontró ninguno.")
            return
        logging.info("PDF seleccionado: %s", pdf_url)

        import tempfile, os as _os
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_path = tmp.name
        tmp.close()
        try:
            self.download_pdf(pdf_url, pdf_path)
            text = self.extract_text_from_pdf(pdf_path)
            summary_en = self.summarize_text(text)
            summary_es = self.translator.translate(summary_en, dest="es")
            html_content = self.build_html_email(summary_es, source_url=pdf_url)
            if self.dry_run:
                logging.info("DRY-RUN activado: no se envía email.\nResumen ES (500 chars): %s", summary_es[:500])
            else:
                self.send_email("Resumen del informe semanal", summary_es, html_body=html_content)
                logging.info("Correo enviado correctamente.")
        finally:
            try:
                _os.unlink(pdf_path)
            except OSError:
                pass


# ---------- CLI ----------
def build_config_from_env() -> Config:
    return Config(
        base_url=os.getenv("BASE_URL", Config.base_url),
        pdf_pattern=os.getenv("PDF_PATTERN", Config.pdf_pattern),
        summary_sentences=int(os.getenv("SUMMARY_SENTENCES", str(Config.summary_sentences))),
        smtp_server=os.getenv("SMTP_SERVER", Config.smtp_server),
        smtp_port=int(os.getenv("SMTP_PORT", str(Config.smtp_port))),
        sender_email=os.getenv("SENDER_EMAIL", Config.sender_email),
        receiver_email=os.getenv("RECEIVER_EMAIL", Config.receiver_email),
        ca_file=os.getenv("CA_FILE") or None,
        state_path=os.getenv("AGENT_STATE_PATH", Config.state_path),
    )

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-translate", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(levelname)s %(message)s")
    cfg = build_config_from_env()
    agent = WeeklyReportAgent(cfg, translate=not args.no_translate, dry_run=args.dry_run)
    agent.run()

if __name__ == "__main__":
    main()
