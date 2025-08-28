"""
Weekly ECDC Agent – CDTR selector
---------------------------------
Descarga el PDF más reciente del listado del ECDC (Weekly Threats / CDTR),
extrae texto, resume en español y lo envía por email (HTML + texto).

ENV usadas (sensibles como Secrets; otras como Variables):
- SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, RECEIVER_EMAIL, EMAIL_PASSWORD
- BASE_URL (opcional; si no llega usa la de CDTR por defecto)
- PDF_PATTERN (opcional, por defecto r"\.pdf$")
- SUMMARY_SENTENCES (opcional, por defecto 8)
- PDF_INCLUDE / PDF_EXCLUDE (opcional; filtros para afinar los PDFs)
- LOG_LEVEL (p.ej. INFO o DEBUG)
- NO_TRANSLATE=1 para no traducir
- AGENT_STATE_PATH (ruta del fichero de estado; por defecto .agent_state.json)
"""

from __future__ import annotations

import argparse
import datetime as dt
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

# ---- PDF extracción ----
import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None

# ---- Sumario (LexRank) ----
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.lex_rank import LexRankSummarizer


# ---- Utilidades --------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "weekly-ecdc-agent/1.0 (+github actions)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def _normalize(text: str) -> str:
    text = re.sub(r"-\n", "", text)          # une palabras cortadas
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text


class TranslatorClient:
    """Wrapper de traducción (googletrans si está; conmutador NO_TRANSLATE)."""
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


# ---- Config ------------------------------------------------------------------

@dataclass
class Config:
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    pdf_pattern: str = r"\.pdf$"
    summary_sentences: int = 8

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 465
    sender_email: str = "agentia70@gmail.com"
    receiver_email: str = "contra1270@gmail.com"
    ca_file: Optional[str] = None

    state_path: str = os.getenv("AGENT_STATE_PATH", ".agent_state.json")

    # Filtros para acertar el CDTR
    include_regex: str = r"(cdtr|communicable disease threats|weekly threats|weekly\-threats)"
    exclude_regex: str = r"(annual|aer|assessment|guidance|policy|poster|infographic|annex|technical report|strategy)"


# ---- Agente ------------------------------------------------------------------

class WeeklyReportAgent:
    def __init__(self, config: Config, translate: bool = True, dry_run: bool = False) -> None:
        self.config = config
        self.session = make_session()
        self.translator = TranslatorClient()
        if not translate:
            self.translator.disabled = True
        self.dry_run = dry_run

    # Estado anti-duplicado
    def _load_state(self) -> dict:
        try:
            if os.path.exists(self.config.state_path):
                with open(self.config.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_state(self, d: dict) -> None:
        try:
            with open(self.config.state_path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception as e:
            logging.warning("No se pudo guardar el estado: %s", e)

    # --------- Selección robusta del PDF (dos pasadas) ----------
    def fetch_latest_pdf_url(self) -> Optional[str]:
        # 1) Descarga la página/índice
        r = self.session.get(self.config.base_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        pdf_rx = re.compile(self.config.pdf_pattern, re.I)
        inc = re.compile(getattr(self.config, "include_regex", ""), re.I) if getattr(self.config, "include_regex", "") else None
        exc = re.compile(getattr(self.config, "exclude_regex", ""), re.I) if getattr(self.config, "exclude_regex", "") else None

        def _collect(apply_filters: bool) -> List[tuple[str, dt.datetime, int]]:
            cands: List[tuple[str, dt.datetime, int]] = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not pdf_rx.search(href):
                    continue

                text = a.get_text(" ", strip=True)
                parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
                hay = f"{href} {text} {parent_text}"

                if apply_filters:
                    if inc and not inc.search(hay):
                        continue
                    if exc and exc.search(hay):
                        continue

                pdf_url = href if href.startswith("http") else requests.compat.urljoin(self.config.base_url, href)

                # Intento de fecha por patrones conocidos
                date_guess: Optional[dt.datetime] = None
                for rx in [
                    r"(\d{4}-\d{2}-\d{2})",
                    r"(\d{1,2}\s+\w+\s+\d{4})",
                    r"[Ww]eek\s+(\d{1,2})\s+(\d{4})",
                ]:
                    m = re.search(rx, hay)
                    if m:
                        try:
                            if len(m.groups()) == 1:
                                for fmt in ("%Y-%m-%d", "%d %B %Y"):
                                    try:
                                        date_guess = dt.datetime.strptime(m.group(1), fmt)
                                        break
                                    except ValueError:
                                        continue
                            else:
                                week = int(m.group(1)); year = int(m.group(2))
                                date_guess = dt.datetime.fromisocalendar(year, week, 1)
                            if date_guess:
                                break
                        except Exception:
                            pass

                # Fallback a Last-Modified del HEAD
                last_mod: Optional[dt.datetime] = None
                try:
                    h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
                    if "Last-Modified" in h.headers:
                        try:
                            last_mod = dt.datetime.strptime(h.headers["Last-Modified"], "%a, %d %b %Y %H:%M:%S %Z")
                        except Exception:
                            last_mod = None
                except requests.RequestException:
                    pass

                score = date_guess or last_mod or dt.datetime.min
                bonus = 1 if re.search(r"(weekly threats|weekly\-threats|cdtr)", hay, re.I) else 0
                cands.append((pdf_url, score, bonus))
            return cands

        # 1) Con filtros
        filtered = _collect(apply_filters=True)
        logging.debug("Candidatos tras filtro: %d", len(filtered))

        picks = filtered
        if not picks:
            # 2) Sin filtros
            allpdfs = _collect(apply_filters=False)
            logging.debug("Candidatos sin filtro: %d", len(allpdfs))
            picks = allpdfs

        if not picks:
            return None

        picks.sort(key=lambda x: (x[1], x[2]), reverse=True)
        latest_url = picks[0][0]
        logging.debug("Elegido: %s", latest_url)

        # Anti-duplicado
        st = self._load_state()
        if st.get("last_pdf_url") == latest_url:
            logging.info("Último informe ya enviado; no se reenvía.")
            return None
        st["last_pdf_url"] = latest_url
        self._save_state(st)
        return latest_url

    # --------- Descarga / extracción / resumen ----------
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
                    t = p.extract_text() or ""
                    pages.append(t)
            txt = "\n".join(pages)
            if len(txt.strip()) > 200:
                return _normalize(txt)
        except Exception:
            pass
        if pm_extract:
            try:
                txt = pm_extract(pdf_path) or ""
                return _normalize(txt)
            except Exception:
                return ""
        return ""

    def summarize_text(self, text: str) -> str:
        if not text:
            return ""
        # Limitamos coste
        snippet = text[:20000]
        parser = PlaintextParser.from_string(snippet, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sentences = summarizer(parser.document, self.config.summary_sentences)
        return " ".join(str(s) for s in sentences)

    # --------- Email ----------
    @staticmethod
    def _highlight_spain(text_html_escaped: str) -> str:
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text_html_escaped) if s.strip()]
        out: List[str] = []
        keys = ["España", "Spain", "Espana"]
        for s in sents:
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
                <td style="background-color:#f0f0f0;color:#666;padding:15px;text-align:center;font-size:12px;">
                  <p style="margin:0;">© 2025</p>
                </td>
              </tr>
            </table>
          </body>
        </html>
        """

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

    # --------- Pipeline ----------
    def run(self) -> None:
        pdf_url = self.fetch_latest_pdf_url()
        if not pdf_url:
            logging.info("No hay PDF nuevo o no se encontró ninguno.")
            return
        logging.info("PDF seleccionado: %s", pdf_url)

        import tempfile
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
                logging.info("DRY-RUN: no se envía email. Resumen ES (500 chars): %s", summary_es[:500])
            else:
                self.send_email("Resumen del informe semanal", summary_es, html_body=html_content)
                logging.info("Correo enviado correctamente.")
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass


# ---- Config desde entorno (robusto a vacíos) --------------------------------

def build_config_from_env() -> Config:
    def _s(name: str, default: str) -> str:
        v = os.getenv(name)
        return default if v is None or v.strip() == "" else v

    def _i(name: str, default: int) -> int:
        v = os.getenv(name)
        if v is None or v.strip() == "":
            return default
        try:
            return int(v)
        except ValueError:
            logging.warning("Valor inválido para %s='%s'. Se usa %s.", name, v, default)
            return default

    return Config(
        base_url=_s("BASE_URL", Config.base_url),
        pdf_pattern=_s("PDF_PATTERN", Config.pdf_pattern),
        summary_sentences=_i("SUMMARY_SENTENCES", Config.summary_sentences),
        smtp_server=_s("SMTP_SERVER", Config.smtp_server),
        smtp_port=_i("SMTP_PORT", Config.smtp_port),
        sender_email=_s("SENDER_EMAIL", Config.sender_email),
        receiver_email=_s("RECEIVER_EMAIL", Config.receiver_email),
        ca_file=os.getenv("CA_FILE") or None,
        state_path=_s("AGENT_STATE_PATH", Config.state_path),
        include_regex=_s("PDF_INCLUDE", Config.include_regex),
        exclude_regex=_s("PDF_EXCLUDE", Config.exclude_regex),
    )


# ---- Main --------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Weekly ECDC Agent (CDTR)")
    parser.add_argument("--dry-run", action="store_true", help="No envía email; solo logs")
    parser.add_argument("--no-translate", action="store_true", help="No traducir a español")
    args = parser.parse_args(argv)

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(levelname)s %(message)s")

    cfg = build_config_from_env()
    agent = WeeklyReportAgent(cfg, translate=not args.no_translate, dry_run=args.dry_run)
    agent.run()


if __name__ == "__main__":
    main()
