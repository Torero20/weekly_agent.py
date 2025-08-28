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

# PDF
import pdfplumber  # type: ignore
try:
    from pdfminer.high_level import extract_text as pm_extract  # type: ignore
except Exception:
    pm_extract = None

# Sumario
from sumy.nlp.tokenizers import Tokenizer
from sumy.parsers.plaintext import PlaintextParser
from sumy.summarizers.lex_rank import LexRankSummarizer


# ------------------------- Utilidades de red y texto -------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "weekly-ecdc-agent/1.1 (+github actions)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def _normalize(text: str) -> str:
    text = re.sub(r"-\n", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text


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


# ------------------------------ Configuración -------------------------------

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

    include_regex: str = r"(cdtr|communicable[\-\s]disease[\-\s]threats|weekly[\-\s]threats)"
    exclude_regex: str = r"(annual|aer|assessment|guidance|policy|poster|infographic|annex|technical report|strategy)"

    # Patrón directo de PDF semanal (plan A)
    direct_pdf_template: str = os.getenv(
        "DIRECT_PDF_TEMPLATE",
        "https://www.ecdc.europa.eu/sites/default/files/documents/communicable-disease-threats-report-week-{week}-{year}.pdf",
    )


# --------- Auxiliares para encontrar PDFs (fuera de la clase; sin sangrías) --

def _abs_url(href: str, base: str) -> str:
    return href if href.startswith("http") else requests.compat.urljoin(base, href)


def _find_pdfs_in_soup(
    soup: BeautifulSoup,
    page_url: str,
    pdf_pattern: str,
    include_regex: str,
    exclude_regex: str,
    session: requests.Session,
    apply_filters: bool = True,
) -> List[tuple[str, dt.datetime, int]]:
    pdf_rx = re.compile(pdf_pattern, re.I)
    inc = re.compile(include_regex, re.I) if include_regex else None
    exc = re.compile(exclude_regex, re.I) if exclude_regex else None

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

        pdf_url = _abs_url(href, page_url)

        # Fecha por patrones + fallback Last-Modified (HEAD)
        date_guess: Optional[dt.datetime] = None
        for rx in [r"(\d{4}-\d{2}-\d{2})", r"(\d{1,2}\s+\w+\s+\d{4})", r"[Ww]eek\s+(\d{1,2})\s+(\d{4})"]:
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

        last_mod: Optional[dt.datetime] = None
        try:
            h = session.head(pdf_url, timeout=15, allow_redirects=True)
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


# --------------------------------- Agente ------------------------------------

class WeeklyReportAgent:
    def __init__(self, config: Config, translate: bool = True, dry_run: bool = False) -> None:
        self.config = config
        self.session = make_session()
        # IMPORTANTE: algunos sitios exigen Referer para descargar
        self.session.headers["Referer"] = self.config.base_url
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

    # ---------------------- Plan A: HEAD directo por semana -------------------
    def _try_direct_weekly_pdf(self) -> Optional[str]:
        today = dt.date.today()
        year, week, _ = today.isocalendar()
        # prueba 0..6 semanas hacia atrás
        for delta in range(0, 7):
            w = week - delta
            y = year
            # cruces de año
            if w <= 0:
                y -= 1
                # semana ISO 52/53
                last_week_prev_year = dt.date(y, 12, 28).isocalendar()[1]
                w = last_week_prev_year + w  # w es negativo o cero

            url = self.config.direct_pdf_template.format(week=w, year=y)
            try:
                h = self.session.head(url, timeout=12, allow_redirects=True)
                logging.debug("HEAD %s -> %s", url, h.status_code)
                if h.status_code == 200:
                    return url
            except requests.RequestException:
                continue
        return None

    # ---------- Plan B: Selección del PDF por rastreo de páginas --------------
    def fetch_latest_pdf_url(self) -> Optional[str]:
        # Plan A (rápido y robusto)
        direct = self._try_direct_weekly_pdf()
        if direct:
            logging.debug("Plan A: encontrado por HEAD directo -> %s", direct)
            chosen = direct
        else:
            # Plan B: listado + detalle
            r = self.session.get(self.config.base_url, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            filtered = _find_pdfs_in_soup(
                soup, self.config.base_url, self.config.pdf_pattern,
                self.config.include_regex, self.config.exclude_regex, self.session, apply_filters=True
            )
            logging.debug("Candidatos en listado (con filtro): %d", len(filtered))
            picks = filtered
            if not picks:
                allpdfs = _find_pdfs_in_soup(
                    soup, self.config.base_url, self.config.pdf_pattern,
                    self.config.include_regex, self.config.exclude_regex, self.session, apply_filters=False
                )
                logging.debug("Candidatos en listado (sin filtro): %d", len(allpdfs))
                picks = allpdfs

            if picks:
                picks.sort(key=lambda x: (x[1], x[2]), reverse=True)
                chosen = picks[0][0]
                logging.debug("Elegido en listado: %s", chosen)
            else:
                logging.debug("Sin PDFs directos. Explorando páginas de detalle…")
                detail_links: List[tuple[str, Optional[dt.datetime]]] = []
                seen = set()
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if href.lower().endswith(".pdf"):
                        continue
                    url = _abs_url(href, self.config.base_url)
                    if "ecdc.europa.eu" not in url:
                        continue
                    if url in seen:
                        continue

                    txt = (a.get_text(" ", strip=True) or "") + " " + (a.parent.get_text(" ", strip=True) if a.parent else "")
                    if not re.search(r"(weekly|threat|cdtr|communicable|disease|report)", txt, re.I):
                        continue
                    seen.add(url)

                    guessed: Optional[dt.datetime] = None
                    for rx in [r"(\d{4}-\d{2}-\d{2})", r"[Ww]eek\s+(\d{1,2})\s+(\d{4})"]:
                        m = re.search(rx, txt)
                        if m:
                            try:
                                if len(m.groups()) == 1:
                                    guessed = dt.datetime.strptime(m.group(1), "%Y-%m-%d")
                                else:
                                    weekn = int(m.group(1)); yearn = int(m.group(2))
                                    guessed = dt.datetime.fromisocalendar(yearn, weekn, 1)
                                break
                            except Exception:
                                pass
                    detail_links.append((url, guessed))

                detail_links.sort(key=lambda x: x[1] or dt.datetime.min, reverse=True)
                detail_links = detail_links[:12]
                logging.debug("Páginas de detalle a inspeccionar: %d", len(detail_links))

                found: List[tuple[str, dt.datetime, int]] = []
                for url, _d in detail_links:
                    try:
                        rr = self.session.get(url, timeout=30)
                        rr.raise_for_status()
                    except requests.RequestException:
                        continue
                    sub = BeautifulSoup(rr.text, "html.parser")
                    sub_picks = _find_pdfs_in_soup(
                        sub, url, self.config.pdf_pattern,
                        self.config.include_regex, self.config.exclude_regex, self.session, apply_filters=True
                    )
                    if not sub_picks:
                        sub_picks = _find_pdfs_in_soup(
                            sub, url, self.config.pdf_pattern,
                            self.config.include_regex, self.config.exclude_regex, self.session, apply_filters=False
                        )
                    logging.debug("  %s -> PDFs encontrados: %d", url, len(sub_picks))
                    found.extend(sub_picks)

                if not found:
                    return None
                found.sort(key=lambda x: (x[1], x[2]), reverse=True)
                chosen = found[0][0]
                logging.debug("Elegido en detalle: %s", chosen)

        # Anti-duplicado
        st = self._load_state()
        if st.get("last_pdf_url") == chosen:
            logging.info("Último informe ya enviado; no se reenvía.")
            return None
        st["last_pdf_url"] = chosen
        self._save_state(st)
        return chosen

    # ------------------- Descarga / extracción / resumen -------------------
    def download_pdf(self, pdf_url: str, dest_path: str, max_mb: int = 25) -> None:
        """Descarga el PDF comprobando que el servidor devuelve realmente un PDF.
    Si devuelve HTML u otra cosa, reintenta con ?download=1 y falla con mensaje claro."""
    # Comprobación de tamaño (opcional)
    try:
        h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
        clen = h.headers.get("Content-Length")
        if clen and int(clen) > max_mb * 1024 * 1024:
            raise RuntimeError(f"El PDF excede {max_mb} MB ({int(clen)/1024/1024:.1f} MB)")
    except requests.RequestException:
        pass

    headers = {"Accept": "application/pdf", "Referer": self.config.base_url}

    def _download(url: str) -> requests.Response:
        r = self.session.get(url, stream=True, timeout=60, allow_redirects=True, headers=headers)
        r.raise_for_status()
        return r

    # 1º intento
    r = _download(pdf_url)
    ctype = (r.headers.get("Content-Type") or "").lower()

    # Si no parece PDF, probamos con ?download=1
    if "pdf" not in ctype:
        alt = pdf_url + ("&download=1" if "?" in pdf_url else "?download=1")
        logging.debug("Content-Type no es PDF (%s). Reintentando con %s", ctype or "desconocido", alt)
        r = _download(alt)
        ctype = (r.headers.get("Content-Type") or "").lower()

    # Guardamos
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    # Comprobación mágica mínima
    try:
        with open(dest_path, "rb") as f:
            magic = f.read(5)
        if magic != b"%PDF-":
            raise RuntimeError(f"El servidor no devolvió un PDF válido (Content-Type={ctype or 'desconocido'})")
    except Exception:
        # Borramos el archivo roto para no confundir
        try:
            os.unlink(dest_path)
        except OSError:
            pass
        raise


    # ----------------------------- Email ----------------------------------
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

    def send_email(self, subject: str, body: str, html_body: Optional[str] = None) -> bool:
        """Devuelve True si el envío fue bien, False si algo falló (y lo deja en logs)."""
        import smtplib
        from email.message import EmailMessage
        sender = self.config.sender_email
        receiver = self.config.receiver_email
        if not sender or not receiver:
            logging.error("SENDER_EMAIL/RECEIVER_EMAIL no definidos.")
            return False

        password = os.getenv("EMAIL_PASSWORD")
        if not password:
            logging.error("EMAIL_PASSWORD no definido. No se puede enviar por SMTP.")
            return False

        try:
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
            return True
        except Exception as e:
            logging.exception("Fallo enviando email: %s", e)
            return False

    # ------------------------------- Pipeline ------------------------------
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
        try:
            self.download_pdf(pdf_url, pdf_path)
            text = self.extract_text_from_pdf(pdf_path)
        except Exception as e:
            logging.error("No se pudo descargar o leer el PDF: %s", e)
            return

        summary_en = self.summarize_text(text)
        summary_es = self.translator.translate(summary_en, dest="es")
        html_content = self.build_html_email(summary_es, source_url=pdf_url)

        if self.dry_run:
            logging.info("DRY-RUN: no se envía email. Resumen ES (500 chars): %s", summary_es[:500])
        else:
            ok = self.send_email("Resumen del informe semanal", summary_es, html_body=html_content)
            if ok:
                logging.info("Correo enviado correctamente.")
            else:
                logging.warning("No se pudo enviar el correo. Revisa logs SMTP (pero el job no falla).")
    finally:
        try:
            os.unlink(pdf_path)
        except OSError:
            pass



# ---------------------- ENV → Config (robusto a vacíos) ----------------------

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
        direct_pdf_template=_s("DIRECT_PDF_TEMPLATE", Config.direct_pdf_template),
    )


# --------------------------------- Main --------------------------------------

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
