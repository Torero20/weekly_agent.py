#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import ssl
import json
import time
import smtplib
import logging
import tempfile
import datetime as dt
from typing import Dict, List, Tuple, Optional
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

# PDF: extractor principal y respaldo
try:
    import pdfplumber  # type: ignore
except Exception:
    pdfplumber = None  # type: ignore

try:
    from PyPDF2 import PdfReader  # type: ignore
except Exception:
    PdfReader = None  # type: ignore

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# =====================================================================
# Configuración
# =====================================================================

class Config:
    # Página de listados del ECDC (CDTR)
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    # SMTP / email (rellenar vía .env o secretos del runner)
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")  # 465 SSL; 587 STARTTLS
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    receiver_email = os.getenv("RECEIVER_EMAIL", "")  # múltiples: coma, ; o saltos de línea

    # Otros
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"

    # Tamaño máximo del PDF (MB) por seguridad
    max_pdf_mb = int(os.getenv("MAX_PDF_MB", "30"))


# =====================================================================
# Utilidades
# =====================================================================

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year} (UTC)"


def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# =====================================================================
# Agente
# =====================================================================

class WeeklyReportAgent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # --------------------------------------------------------------
    # Localización del artículo y PDF
    # --------------------------------------------------------------
    def _parse_week_year(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None,
                int(y.group(1)) if y else None)

    def fetch_latest_article_and_pdf(self) -> Tuple[str, str, Optional[int], Optional[int]]:
        """Devuelve (article_url, pdf_url, week, year)."""
        r = self.session.get(self.cfg.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Candidatos: enlaces a "communicable-disease-threats-report-...-week-XX"
        candidates: List[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                candidates.append(url)

        if not candidates:
            raise RuntimeError("No se encontraron artículos CDTR en la página de listados.")

        # Recorremos por orden de aparición (la página ya ordena por recencia)
        for article_url in candidates:
            ar = self.session.get(article_url, timeout=30)
            if ar.status_code != 200:
                continue
            asoup = BeautifulSoup(ar.text, "html.parser")

            # En el artículo suele existir un enlace directo a PDF (primer <a> .pdf)
            pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a:
                # A veces el PDF usa espacios codificados u otros sufijos; probamos
                for a in asoup.find_all("a", href=True):
                    if ".pdf" in a["href"].lower():
                        pdf_a = a
                        break
            if not pdf_a:
                continue

            pdf_url = pdf_a["href"]
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(article_url, pdf_url)

            # Semana/año
            t = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
            week, year = self._parse_week_year(t)
            logging.info("Artículo CDTR: %s", article_url)
            logging.info("PDF CDTR: %s (semana=%s, año=%s)", pdf_url, week, year)
            return article_url, pdf_url, week, year

        raise RuntimeError("No se logró localizar un PDF dentro de los artículos candidatos.")

    # --------------------------------------------------------------
    # Estado (para no reenviar el mismo PDF)
    # --------------------------------------------------------------
    def _load_state(self) -> Dict:
        if not os.path.exists(self.cfg.state_file):
            return {}
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, pdf_url: str) -> None:
        state = {"last_pdf_url": pdf_url, "ts": dt.datetime.utcnow().isoformat()}
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    # --------------------------------------------------------------
    # Descarga y extracción de texto del PDF
    # --------------------------------------------------------------
    def _download_pdf(self, pdf_url: str) -> str:
        # Pre-chequeo tamaño
        try:
            h = self.session.head(pdf_url, timeout=15, allow_redirects=True)
            clen = h.headers.get("Content-Length")
            if clen and int(clen) > self.cfg.max_pdf_mb * 1024 * 1024:
                raise RuntimeError(f"El PDF excede {self.cfg.max_pdf_mb} MB.")
        except requests.RequestException:
            pass

        r = self.session.get(pdf_url, timeout=60, stream=True)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        with tmp as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return tmp.name

    def _extract_text_pdf(self, path: str) -> str:
        # 1) pdfplumber (si está)
        if pdfplumber is not None:
            try:
                text = []
                with pdfplumber.open(path) as pdf:
                    for p in pdf.pages:
                        txt = p.extract_text() or ""
                        # Normalizamos cortes de línea
                        text.append(clean_spaces(txt.replace("\n", " ")))
                return "\n".join(t for t in text if t.strip())
            except Exception as e:
                logging.warning("pdfplumber falló: %s", e)

        # 2) PyPDF2
        if PdfReader is not None:
            try:
                reader = PdfReader(path)
                parts = []
                for page in reader.pages:
                    try:
                        txt = page.extract_text() or ""
                    except Exception:
                        txt = ""
                    if txt:
                        parts.append(clean_spaces(txt.replace("\n", " ")))
                return "\n".join(parts)
            except Exception as e:
                logging.warning("PyPDF2 falló: %s", e)

        return ""

    # --------------------------------------------------------------
    # Resumen heurístico en español
    # --------------------------------------------------------------
    DISEASES: Dict[str, Dict] = {
        "RESP":  {"pat": r"(SARS\-CoV\-2|COVID|respiratory|influenza|RSV)", "title": "Respiratorios"},
        "WNV":   {"pat": r"(West Nile|WNV)", "title": "Virus del Nilo Occidental"},
        "CCHF":  {"pat": r"(Crimean\-Congo|CCHF)", "title": "Fiebre Crimea-Congo (CCHF)"},
        "DENG":  {"pat": r"\bdengue\b", "title": "Dengue"},
        "CHIK":  {"pat": r"\bchikungunya\b", "title": "Chikungunya"},
        "EBOV":  {"pat": r"\bEbola\b", "title": "Ébola"},
        "MEAS":  {"pat": r"\bmeasles\b", "title": "Sarampión"},
        "NIPAH": {"pat": r"\bNipah\b", "title": "Nipah"},
        "RAB":   {"pat": r"\brabies\b", "title": "Rabia"},
    }

    SIMPLE_EN2ES = [
        (r"\bcases?\b", "casos"),
        (r"\bdeaths?\b", "muertes"),
        (r"\bfatalit(y|ies)\b", "letalidad"),
        (r"\bfatality rate\b", "tasa de letalidad"),
        (r"\bprobable\b", "probable"),
        (r"\bconfirmed\b", "confirmados"),
        (r"\bnew\b", "nuevos"),
        (r"\bthis week\b", "esta semana"),
        (r"\bweek\b", "semana"),
        (r"\bEurope\b", "Europa"),
        (r"\bEU\/EEA\b", "UE/EEE"),
        (r"\bcountry\b", "país"),
        (r"\bcountries\b", "países"),
        (r"\bHospitalizations?\b", "hospitalizaciones"),
        (r"\binfections?\b", "infecciones"),
        (r"\btransmission\b", "transmisión"),
        (r"\btravellers?\b", "viajeros"),
        (r"\bvector\b", "vector"),
        (r"\btrend\b", "tendencia"),
    ]

    def _split_sentences(self, text: str) -> List[str]:
        # Segmentador robusto para texto de PDF (puntos + cortes)
        raw = re.sub(r"\s+", " ", text).strip()
        # Separamos por . ; : si siguen de espacio y mayúscula o número
        parts = re.split(r"(?<=[\.\?!;])\s+(?=[A-Z0-9])", raw)
        return [p.strip() for p in parts if p.strip()]

    def _pick_scored_sentences(self, sentences: List[str], regex: str, maxn: int = 3) -> List[str]:
        pat = re.compile(regex, re.I)
        # scoring: priorizamos frases con % o números + palabras clave (cases, deaths)
        out = []
        scored: List[Tuple[int, str]] = []
        for s in sentences:
            if pat.search(s):
                score = 0
                if re.search(r"\d+(\.\d+)?\s*%", s): score += 3
                if re.search(r"\b\d{1,4}\b", s):     score += 2
                if re.search(r"\b(cases?|deaths?|hospital|fatal|outbreak)\b", s, re.I): score += 1
                scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, s in scored[:maxn]:
            out.append(s)
        return out

    def _en_to_es_min(self, s: str) -> str:
        out = s
        for pat, repl in self.SIMPLE_EN2ES:
            out = re.sub(pat, repl, out, flags=re.I)
        # Limpieza de espacios y ajustes menores
        out = out.replace("  ", " ").strip()
        # Cambiamos coma inglesa en % si aparece
        out = re.sub(r"(\d+),(\d+)%", r"\1.\2%", out)
        return out

    def build_summary(self, text: str) -> Dict[str, List[str]]:
        sents = self._split_sentences(text)
        summary: Dict[str, List[str]] = {}
        for key, meta in self.DISEASES.items():
            found = self._pick_scored_sentences(sents, meta["pat"], maxn=3)
            if not found:
                continue
            # “traducción” mínima al vuelo
            es_found = [self._en_to_es_min(f) for f in found]
            summary[meta["title"]] = es_found
        return summary

    # --------------------------------------------------------------
    # HTML final (inline CSS; botón PDF; bloques de color)
    # --------------------------------------------------------------
    def _chip(self, text: str) -> str:
        return f"<span style='display:inline-block;background:#174ea6;color:#fff;padding:6px 12px;border-radius:999px;font-size:12px'>{text}</span>"

    def _bullet(self, color: str, body: str) -> str:
        return (
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
            "style='margin:8px 0;border-left:5px solid {c};background:#f9fafb;border-radius:8px'>"
            "<tr><td style='padding:10px 12px;font-size:14px;color:#222'>"
            "<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:{c};vertical-align:middle;margin-right:8px'></span>"
            "{b}"
            "</td></tr></table>"
        ).format(c=color, b=body)

    def _card(self, title: str, body_html: str, border: str, bg: str) -> str:
        return (
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
            f"style='margin:10px 0;border-left:6px solid {border};background:{bg};border-radius:12px'>"
            "<tr><td style='padding:12px 14px'>"
            f"<div style='font-weight:700;color:#0b5cab;font-size:15px;margin-bottom:6px'>{title}</div>"
            f"<div style='font-size:14px;color:#222;line-height:1.45'>{body_html}</div>"
            "</td></tr></table>"
        )

    def build_html(self, week: Optional[int], year: Optional[int],
                   pdf_url: str, article_url: str,
                   summary: Dict[str, List[str]]) -> str:

        week_label = f"Semana {week} · {year}" if (week and year) else "Último informe ECDC"
        # Paleta por tema
        palette = {
            "Virus del Nilo Occidental": ("#2e7d32", "#eef8f0"),
            "Fiebre Crimea-Congo (CCHF)": ("#d32f2f", "#fdeeee"),
            "Respiratorios": ("#1565c0", "#eef4fb"),
            "Dengue": ("#ef6c00", "#fff6e9"),
            "Chikungunya": ("#6a1b9a", "#f5ecfb"),
            "Ébola": ("#374151", "#f3f4f6"),
            "Sarampión": ("#8a5800", "#fff6e9"),
            "Nipah": ("#4b5563", "#f3f4f6"),
            "Rabia": ("#00897b", "#e8f5f3"),
        }
        color_dot = {"green":"#2e7d32", "orange":"#ef6c00", "blue":"#1565c0"}

        html = (
            "<html><body style='margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;color:#222'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='860' cellspacing='0' cellpadding='0' style='max-width:860px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 6px 18px rgba(0,0,0,.08)'>"
            "<tr><td style='padding:26px 26px 12px;background:linear-gradient(180deg,#0c4a93,#0b5cab);color:#fff'>"
            "<div style='font-size:26px;font-weight:900;letter-spacing:.2px'>Resumen Semanal de Amenazas de Enfermedades<br/>Transmisibles</div>"
            "<div style='opacity:.9;margin-top:8px;font-size:13px'>Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>"
            f"<div style='margin-top:14px'>{self._chip(week_label)}</div>"
            "<div style='margin-top:16px'>"
            f"<a href='{pdf_url}' style='display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;font-weight:800;padding:10px 16px;border-radius:8px'>Abrir / Descargar PDF del informe</a>"
            "</div>"
            "<div style='margin-top:8px;font-size:12px;opacity:.9'>"
            "Si el botón no funciona, copia y pega este enlace en tu navegador:<br/>"
            f"<span style='word-break:break-all;color:#dbeafe'>{pdf_url}</span>"
            "</div>"
            "</td></tr>"
            "<tr><td style='padding:18px 26px'>"
        )

        # Bloques por tema (si existen frases)
        # Orden agradable
        order = [
            "Virus del Nilo Occidental",
            "Fiebre Crimea-Congo (CCHF)",
            "Respiratorios",
            "Dengue",
            "Chikungunya",
            "Ébola",
            "Sarampión",
            "Nipah",
            "Rabia",
        ]
        for topic in order:
            if topic not in summary:
                continue
            border, bg = palette.get(topic, ("#0b5cab", "#eef4fb"))
            bullets = "".join(self._bullet(border, f) for f in summary[topic])
            html += self._card(topic, bullets, border, bg)

        # Mini “puntos clave” automáticos (si no hubo nada, mostramos el título del PDF)
        if not summary:
            html += self._card(
                "Puntos clave de la semana (auto-extraídos del ECDC)",
                self._bullet(color_dot["green"], "Communicable disease threats report (última semana)"),
                "#2e7d32", "#eef8f0"
            )

        html += (
            "</td></tr>"
            "<tr><td style='padding:0 26px 20px' align='center'>"
            f"<a href='{article_url}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:800'>Página del informe (ECDC)</a>"
            "</td></tr>"
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            f"Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · Fecha: {fecha_es(dt.datetime.utcnow())}"
            "</td></tr>"
            "</table>"
            "</td></tr></table>"
            "</body></html>"
        )
        return html

    # --------------------------------------------------------------
    # Envío de correo
    # --------------------------------------------------------------
    def _parse_recipients(self, raw: str) -> List[str]:
        if not raw:
            return []
        s = raw.replace(";", ",").replace("\n", ",")
        emails = [e.strip() for e in s.split(",") if e.strip()]
        return emails

    def send_email(self, subject: str, html: str) -> None:
        to_addrs = self._parse_recipients(self.cfg.receiver_email)
        if not self.cfg.sender_email or not to_addrs:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.cfg.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.cfg.sender_email
        msg['To'] = ", ".join(to_addrs)

        # Sólo HTML (sin versión de texto plano para evitar “doble contenido” arriba)
        msg.attach(MIMEText(html, 'html', 'utf-8'))

        logging.info("SMTP: from=%s → to=%s", self.cfg.sender_email, to_addrs)
        ctx = ssl.create_default_context()

        if self.cfg.dry_run:
            logging.info("DRY_RUN=1: no se envía (asunto: %s).", subject)
            return

        if int(self.cfg.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.cfg.smtp_server, self.cfg.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.cfg.email_password:
                    s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.cfg.email_password:
                    s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addrs, msg.as_string())

        logging.info("Correo enviado correctamente.")

    # --------------------------------------------------------------
    # Run
    # --------------------------------------------------------------
    def run(self) -> None:
        try:
            article_url, pdf_url, week, year = self.fetch_latest_article_and_pdf()
        except Exception as e:
            logging.exception("No se pudo localizar el CDTR más reciente: %s", e)
            return

        # Anti-duplicados
        state = self._load_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("PDF ya enviado anteriormente, no se vuelve a enviar.")
            return

        # Descarga y extracción
        tmp_pdf = ""
        text = ""
        try:
            tmp_pdf = self._download_pdf(pdf_url)
            text = self._extract_text_pdf(tmp_pdf)
        except Exception as e:
            logging.exception("Error descargando/extrayendo el PDF: %s", e)
        finally:
            # limpieza best-effort
            if tmp_pdf:
                for _ in range(3):
                    try:
                        os.remove(tmp_pdf)
                        break
                    except Exception:
                        time.sleep(0.2)

        if not text.strip():
            logging.warning("No se pudo extraer texto del PDF; se enviará plantilla mínima.")

        # Resumen heurístico
        try:
            summary = self.build_summary(text) if text else {}
        except Exception as e:
            logging.exception("Error generando el resumen: %s", e)
            summary = {}

        # HTML final
        html = self.build_html(week, year, pdf_url, article_url, summary)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"

        # Envío
        try:
            self.send_email(subject, html)
            self._save_state(pdf_url)
        except Exception as e:
            logging.exception("Fallo enviando el email: %s", e)


# =====================================================================
# main
# =====================================================================

def main() -> None:
    cfg = Config()
    WeeklyReportAgent(cfg).run()

if __name__ == "__main__":
    main()
