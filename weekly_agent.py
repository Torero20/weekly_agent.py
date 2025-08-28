#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import ssl
import smtplib
import time
import logging
import tempfile
import datetime as dt
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, List, Tuple

import requests
from bs4 import BeautifulSoup

# PDF: preferimos pdfplumber; si falla, hacemos fallback a pdfminer
import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None  # type: ignore

# Sumario extractivo (no requiere NLTK si usamos el tokenizer de sumy)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lex_rank import LexRankSummarizer

# Traducción opcional (si falla, devolvemos el original)
try:
    from googletrans import Translator  # type: ignore
except Exception:
    Translator = None  # type: ignore


# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

@dataclass
class Config:
    # Página de listados (Plan B)
    base_url: str = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    # Plantilla de URL directa por semana/año (Plan A)
    direct_pdf_template: str = (
        "https://www.ecdc.europa.eu/sites/default/files/documents/"
        "communicable-disease-threats-report-week-{week}-{year}.pdf"
    )

    # Patrón de PDF válido (para Plan B)
    pdf_regex: re.Pattern = re.compile(
        r"/communicable-disease-threats-report-week-(\d+)-(\d{4})\.pdf$"
    )

    # Nº de oraciones del sumario (puede sobreescribirse por env SUMMARY_SENTENCES)
    summary_sentences: int = 12

    # SMTP/Email (rellenado vía GitHub Secrets en el workflow)
    smtp_server: str = os.getenv("SMTP_SERVER", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email: str = os.getenv("SENDER_EMAIL", "")
    receiver_email: str = os.getenv("RECEIVER_EMAIL", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")

    # Bandera para no enviar correo (tests): DRY_RUN=1
    dry_run: bool = os.getenv("DRY_RUN", "0") == "1"

    # Log level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Tamaño máximo opcional (MB) para abortar PDFs inusualmente grandes
    max_pdf_mb: int = 25


# ---------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------

class WeeklyReportAgent:
    """
    Pipeline:
      1) Intenta localizar el PDF de la semana actual (Plan A: URL directa).
      2) Si falla, rastrea la página de listados y localiza el PDF más reciente (Plan B).
      3) Descarga, extrae texto, resume (LexRank), traduce al español (opcional) y envía email.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

        # logging
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(levelname)s %(message)s",
        )

        # Sesión HTTP robusta con reintentos
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8",
            }
        )
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.packages.urllib3.util.retry.Retry(
                total=4,
                backoff_factor=0.6,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["HEAD", "GET"]),
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Traductor opcional
        self.translator = Translator() if Translator is not None else None

    # ------------------------ Localización del PDF ---------------------

    def _try_direct_weekly_pdf(self) -> Optional[str]:
        """
        Plan A: construir la URL del PDF de la semana ISO actual y
        recorrer hacia atrás hasta 6 semanas si hiciera falta (cruces 52/53).
        """
        today = dt.date.today()
        year, week, _ = today.isocalendar()

        for delta in range(0, 7):  # 0..6 semanas atrás
            w = week - delta
            y = year
            if w <= 0:
                # Pasamos al año previo; 28-Dic *siempre* está en la última semana ISO
                y = year - 1
                last_week_prev_year = dt.date(y, 12, 28).isocalendar()[1]
                w = last_week_prev_year + w  # (w es negativo/cero)

            url = self.config.direct_pdf_template.format(week=w, year=y)
            try:
                h = self.session.head(url, timeout=12, allow_redirects=True)
                logging.debug("HEAD %s -> %s", url, h.status_code)
                ct = h.headers.get("Content-Type", "").lower()
                if h.status_code == 200 and "pdf" in ct:
                    return url
            except requests.RequestException:
                continue
        return None

    def _scan_listing_page(self) -> Optional[str]:
        """
        Plan B: rastrear la página de listados y localizar el PDF más reciente.
        Preferimos URLs que cumplan el patrón ...week-XX-YYYY.pdf.
        Si hay varias, seleccionamos por (año, semana) más alto.
        """
        try:
            r = self.session.get(self.config.base_url, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            logging.warning("No se pudo cargar la página de listados: %s", e)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        candidates: List[Tuple[int, int, str]] = []  # (year, week, url)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            # resolver URL relativa
            if not href.startswith("http"):
                href = requests.compat.urljoin(self.config.base_url, href)

            m = self.config.pdf_regex.search(href)
            if m:
                week = int(m.group(1))
                year = int(m.group(2))
                # Verificamos con HEAD que existe y es PDF
                try:
                    h = self.session.head(href, timeout=12, allow_redirects=True)
                    ct = h.headers.get("Content-Type", "").lower()
                    if h.status_code == 200 and "pdf" in ct:
                        candidates.append((year, week, href))
                        logging.debug("Candidato OK: %s (w=%s, y=%s)", href, week, year)
                except requests.RequestException:
                    continue

        if not candidates:
            return None

        # Elegimos el más reciente (por año y semana)
        candidates.sort(reverse=True)  # ordena por (year, week) descendente
        _, _, best = candidates[0]
        return best

    def fetch_latest_pdf_url(self) -> Optional[str]:
        """
        Intenta Plan A; si falla, Plan B. Devuelve URL o None.
        """
        url = self._try_direct_weekly_pdf()
        if url:
            logging.info("PDF directo encontrado: %s", url)
            return url

        url = self._scan_listing_page()
        if url:
            logging.info("PDF por listado encontrado: %s", url)
        else:
            logging.info("No se encontró PDF nuevo.")
        return url

    # --------------------- Descarga / extracción -----------------------

        def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
            """Descarga el PDF verificando tipo y cabecera real. Si el servidor devuelve HTML,
            reintenta automáticamente con ?download=1 (ECDC lo exige a veces)."""
        def _append_download_param(url: str) -> str:
            return url + ("&download=1" if "?" in url else "?download=1")

        def _looks_like_pdf(first_bytes: bytes) -> bool:
            # Un PDF real empieza por %PDF
            return first_bytes.startswith(b"%PDF")

        # 1) HEAD opcional: tamaño
        try:
            h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > max_mb * 1024 * 1024:
                raise RuntimeError(
                    f"El PDF excede {max_mb} MB ({int(clen)/1024/1024:.1f} MB)"
                )
        except requests.RequestException:
            pass

        headers = {
            "Accept": "application/pdf",
            "Referer": self.config.base_url,
            "Cache-Control": "no-cache",
        }

        def _try_get(url: str) -> Tuple[str, Optional[str], bytes]:
            r = self.session.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True)
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            # Leemos los primeros bytes para validar firma %PDF
            chunk_iter = r.iter_content(chunk_size=8192)
            first = next(chunk_iter, b"")
            # Escribimos a disco
            with open(dest_path, "wb") as f:
                if first:
                    f.write(first)
                for chunk in chunk_iter:
                    if chunk:
                        f.write(chunk)
            return ct, r.headers.get("Content-Length"), first

        # 2) Primer intento tal cual
        try:
            ct, clen, first = _try_get(pdf_url)
            logging.debug("GET %s -> Content-Type=%s, len=%s", pdf_url, ct, clen)
            if ("pdf" in (ct or "").lower()) and _looks_like_pdf(first):
                return
            logging.info("Respuesta no-PDF. Reintentando con ?download=1 ...")
        except requests.RequestException as e:
            logging.info("Fallo en GET inicial (%s). Reintentamos con ?download=1 ...", e)

        # 3) Segundo intento con ?download=1
            retry_url = _append_download_param(pdf_url)
            ct2, clen2, first2 = _try_get(retry_url)
            logging.debug("GET %s -> Content-Type=%s, len=%s", retry_url, ct2, clen2)
            if ("pdf" in (ct2 or "").lower()) and _looks_like_pdf(first2):
            return

        # 4) Si seguimos sin PDF, error con diagnóstico
            raise RuntimeError(
            f"No se obtuvo un PDF válido (Content-Type={ct2!r}, firma={first2[:8]!r})."
        )


    # -------------------------- Sumario --------------------------------

    def summarize(self, text: str, sentences: int) -> str:
        """
        Resumen extractivo con LexRank (sumy). No requiere NLTK si usamos
        Tokenizer('english').
        """
        if not text.strip():
            return ""
        sentences = max(1, sentences)
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summarizer = LexRankSummarizer()
        sents = summarizer(parser.document, sentences)
        return " ".join(str(s) for s in sents)

    # ------------------------- Traducción -------------------------------

    def translate_to_spanish(self, text: str) -> str:
        """
        Intenta traducir al español. Si falla o no hay traductor, devuelve el original.
        """
        if not text.strip():
            return text
        if self.translator is None:
            return text
        try:
            return self.translator.translate(text, dest="es").text
        except Exception:
            return text

    # ------------------------- Email -----------------------------------

    def build_html(self, summary_es: str, pdf_url: str) -> str:
        return f"""
        <html>
          <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.5;background:#f7f7f7;padding:18px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:auto;background:#ffffff;border-radius:8px;overflow:hidden;">
              <tr>
                <td style="background:#005ba4;color:#fff;padding:18px 20px;">
                  <h1 style="margin:0;font-size:22px;">Boletín semanal de amenazas sanitarias</h1>
                  <p style="margin:6px 0 0 0;font-size:14px;opacity:.9;">Resumen automático del informe ECDC</p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px;font-size:15px;color:#222;">
                  <p style="margin-top:0;white-space:pre-wrap">{summary_es}</p>
                  <p style="margin-top:18px">
                    Enlace al informe:&nbsp;
                    <a href="{pdf_url}" style="color:#005ba4;text-decoration:underline">{pdf_url}</a>
                  </p>
                </td>
              </tr>
              <tr>
                <td style="background:#f0f0f0;color:#666;padding:12px 16px;text-align:center;font-size:12px;">
                  Generado automáticamente · {dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
                </td>
              </tr>
            </table>
          </body>
        </html>
        """.strip()

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
        with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=context) as server:
            if self.config.email_password:
                server.login(self.config.sender_email, self.config.email_password)
            server.send_message(msg)

    # --------------------------- Run -----------------------------------

    def run(self) -> None:
        # Ajustes por variables de entorno (si están definidas)
        ss_env = os.getenv("SUMMARY_SENTENCES")
        if ss_env and ss_env.strip().isdigit():
            self.config.summary_sentences = int(ss_env.strip())

        pdf_url = self.fetch_latest_pdf_url()
        if not pdf_url:
            logging.info("No hay PDF nuevo o no se encontró ninguno.")
            return

        # Descargar a temporal
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = tmp.name

        try:
            self.download_pdf(pdf_url, tmp_path, max_mb=self.config.max_pdf_mb)
            text = self.extract_text(tmp_path)
        finally:
            # Eliminamos con prudencia (puede estar abierto en sistemas raros)
            for _ in range(3):
                try:
                    os.remove(tmp_path)
                    break
                except Exception:
                    time.sleep(0.2)

        if not text.strip():
            logging.warning("El PDF no contiene texto extraíble.")
            return

        # Resumen (EN) -> traducción (ES)
        summary_en = self.summarize(text, self.config.summary_sentences)
        if not summary_en.strip():
            logging.warning("No se pudo generar resumen.")
            return

        summary_es = self.translate_to_spanish(summary_en)
        html = self.build_html(summary_es, pdf_url)

        subject = "Resumen del informe semanal del ECDC"
        if self.config.dry_run:
            logging.info("DRY_RUN=1: no se envía email. Asunto: %s", subject)
            logging.debug("Resumen ES:\n%s", summary_es)
            return

        self.send_email(subject, summary_es, html)
        logging.info("Correo enviado correctamente.")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main() -> None:
    cfg = Config()
    agent = WeeklyReportAgent(cfg)
    agent.run()


if __name__ == "__main__":
    main()
