"""
Weekly ECDC Agent
-----------------
Descarga el PDF más reciente del ECDC (lista de Weekly Threats), extrae texto,
genera un resumen en español y lo envía por email (HTML + texto).

- Selección ROBUSTA del PDF (busca en la lista y dentro de las noticias).
- Evita duplicados con .agent_state.json (se cachea en GitHub Actions).
- Extracción: pdfplumber -> fallback pdfminer.six.
- Resumen: LexRank (sumy) con NLTK (auto-descarga 'punkt').
- Traducción: googletrans (se puede desactivar).
- Email: SMTP/SSL con contraseña de aplicación (secrets).

ENV necesarios (en GitHub Secrets):
  SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, RECEIVER_EMAIL, EMAIL_PASSWORD

ENV opcionales (Variables del repo):
  BASE_URL, PDF_PATTERN, SUMMARY_SENTENCES, CA_FILE, LOG_LEVEL, NO_TRANSLATE,
  AGENT_STATE_PATH, DIRECT_PDF_URL
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Carga .env si existe (para pruebas locales)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# PDF extracción
import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None

# Sumario (LexRank)
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.lex_rank import LexRankSummarizer


# ---------- Utilidades ----------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "weekly-ecdc-agent/1.0 (+contact@example.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def _normalize(text: str) -> str:
    text = re.sub(r"-\n", "", text)             # une palabras cortadas por guión
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
        # Algunas versiones nuevas usan 'punkt_tab'
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError:
            try:
                nltk.download("punkt_tab", quiet=True)
            except Exception:
                pass
    except Exception:
        pass


# Traducción con fallback
class TranslatorClient:
    def __init__(self) -> None:
        self._gt = None
        self.disabled = os.getenv("NO_TRANSLATE", "0").lower() in {"1", "true", "yes"}
        try:
            from googletrans import Translator as GT  # type: ignore
            self._gt = GT()
        except Exception:
            self._gt = None

    def translate(self, text: str, dest: str = "es") -> str:
        if self.disabled:
            return text
        if self._gt:
            try:
                return self._gt.translate(text, dest=dest).text
            except Exception:
                pass
        return text


# ---------- Config ----------
@dataclass
class Config:
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    # Permisivo: acepta .pdf en cualquier parte de la URL
    pdf_pattern: str = r"\.pdf"
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

    # Estado para evitar duplicados
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
            logging.warning("No se pudo guardar el estado: %s", e)

    # Extractor robusto de enlaces a PDF en HTML
    def _extract_pdf_candidates(self, html_text: str, base: str) -> List[str]:
        soup = BeautifulSoup(html_text, "html.parser")
        urls: set[str] = set()

        def add(u: Optional[str]) -> None:
            if not u:
                return
            u = u.strip()
            full = u if u.startswith("http") else requests.compat.urljoin(base, u)
            urls.add(full)

        # <a> y atributos frecuentes en CMS
        for a in soup.find_all("a"):
            for attr in ("href", "data-asset-url", "data-file", "data-href"):
                v = a.get(attr)
                if not v:
                    continue
                if ".pdf" in v.lower() or re.search(self.config.pdf_pattern, v, re.IGNORECASE):
                    add(v)

        # Otras etiquetas que pueden contener rutas
        for tag in soup.find_all(["link", "source", "meta"]):
            for attr in ("href", "src", "content"):
                v = tag.get(attr)
                if v and ".pdf" in v.lower():
                    add(v)

        # Rutas de PDF embebidas en <script>
        for s in soup.find_all("script"):
            txt = (s.string or "") + (s.get_text() or "")
            for m in re.findall(r'["\'](https?://[^"\']*?\.pdf[^"\']*)["\']|["\'](\/[^"\']*?\.pdf[^"\']*)["\']',
                                txt, flags=re.I):
                cand = m[0] or m[1]
                add(cand)

        # Validación por HEAD Content-Type (cuando se puede)
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

    # Localiza el PDF más reciente (lista + páginas de detalle)
    def fetch_latest_pdf_url(self) -> Optional[str]:
        # 1) Página base
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

        # 2) Explorar páginas de detalle (publications/news)
        soup = BeautifulSoup(base_html, "html.parser")
        detail_urls: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#"):
                continue
            full = href if href.startswith("http") else requests.compat.urljoin(self.config.base_url, href)
            if "ecdc.europa.eu" not in full.lower():
                continue
            if ".pdf" in full.lower():
                continue
            if any(seg in full.lower() for seg in ("/publications", "/publications-data", "/news", "/news-events")):
                detail_urls.append(full)

        seen: set[str] = set()
        for u in detail_urls[:30]:  # revisa hasta 30 páginas
            if u in seen:
                continue
            seen.add(u)
            try:
                rr = self.session.get(u, timeout=30)
                rr.raise_for_status()
                inner_pdfs = self._extract_pdf_candidates(rr.text, u)
                if inner_pdfs:
                    latest = inner_pdfs[-1]
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

    # Descarga, extracción, resumen
    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        try:
            h = self.session.head(pdf_url, timeout=20, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > max_mb * 1024 * 1024:
                raise RuntimeError(f"El PDF excede {max_mb} MB ({int(clen)/1024/1024:.1f} MB)")
        except requests.RequestException:
            pass

        with self.session.get(pdf_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

    def extract_text_from_pdf(self, pdf_path: str) -> str:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages: List[str] = []
                for p in pdf.pages:
                    pages.append(p.extract_text() or "")
            txt = "\n".join(pages)
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

    def summarize_text(self, text: str) -> str:
        if not text:
            return ""
        _ensure_punkt()
        snippet = text[:20000]  # limita coste
        parser = PlaintextParser.from_string(snippet, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sentences = summarizer(parser.document, self.config.summary_sentences)
        return " ".join(str(s) for s in sentences)

    # Email HTML
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
              <tr>
                <td style="background-color:#005ba4;color:#ffffff;padding:20px;">
                  <h1 style="margin:0;font-size:24px;">Boletín semanal de amenazas sanitarias</h1>
                  <p style="margin:0;font-size:14px;">Resumen del informe semanal</p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px;">
                  {paragraph_html}
                  <p style="margin-top:20px;">Más detalles en el <a href="{html.escape(source_url)}" style="color:#005ba4;text-decoration:underline;">PDF original</a>.</p>
                </td>
              </tr>
              <tr>
                <td style="background-color:#f0f0f0;color:#666666;padding:15px;text-align:center;font-size:12px;">
                  <p style="margin:0;">© 2025.</p>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """

    # Envío por SMTP (app password)
    def send_email(self, subject: str, body: str, html_body: Optional[str] = None) -> None:
        import smtplib, ssl
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

    # Pipeline
    def run(self) -> None:
        # Permite forzar un PDF concreto vía variable DIRECT_PDF_URL
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
                logging.info("DRY-RUN activado: no se envía email.\nResumen ES (primeros 500 chars): %s", summary_es[:500])
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


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Weekly ECDC Agent")
    parser.add_argument("--dry-run", action="store_true", help="No envía email; solo imprime logs")
    parser.add_argument("--no-translate", action="store_true", help="No traducir (mantener inglés)")
    args = parser.parse_args(argv)

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(levelname)s %(message)s")

    cfg = build_config_from_env()
    agent = WeeklyReportAgent(cfg, translate=not args.no_translate, dry_run=args.dry_run)
    agent.run()


if __name__ == "__main__":
    main()
