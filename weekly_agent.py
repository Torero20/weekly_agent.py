#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import ssl
import json
import smtplib
import logging
import datetime as dt
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

class Config:
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")  # 465 SSL; 587 STARTTLS
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    receiver_email = os.getenv(
        "RECEIVER_EMAIL",
        "miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es"
    )

    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year} (UTC)"


# ---------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------

class WeeklyReportAgent:
    def __init__(self, config: Config):
        self.config = config
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # ------------------ Localización PDF ------------------

    def _parse_week_year(self, text: str):
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None,
                int(y.group(1)) if y else None)

    def fetch_latest_pdf(self):
        r = self.session.get(self.config.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                candidates.append(url)

        seen, ordered = set(), []
        for u in candidates:
            if u not in seen:
                ordered.append(u)
                seen.add(u)

        if not ordered:
            raise RuntimeError("No se encontraron artículos CDTR.")

        for article_url in ordered:
            ar = self.session.get(article_url, timeout=30)
            if ar.status_code != 200:
                continue
            asoup = BeautifulSoup(ar.text, "html.parser")
            pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a:
                continue
            pdf_url = pdf_a["href"]
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(article_url, pdf_url)
            t = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
            week, year = self._parse_week_year(t)
            logging.info("PDF más reciente: %s (semana=%s, año=%s)", pdf_url, week, year)
            return pdf_url, article_url, week, year

        raise RuntimeError("No se encontró PDF en los artículos candidatos.")

    # ------------------ Estado (anti-duplicados) ------------------

    def _load_last_state(self):
        if not os.path.exists(self.config.state_file):
            return {}
        try:
            with open(self.config.state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_last_state(self, pdf_url):
        state = {"last_pdf_url": pdf_url, "timestamp": dt.datetime.utcnow().isoformat()}
        with open(self.config.state_file, "w") as f:
            json.dump(state, f)

    # ------------------ HTML email-safe (TABLAS + inline styles) ------------------

    def build_email_body(self, week_label: str, gen_date_es: str, pdf_url: str, article_url: str) -> str:
        # Helpers
        def circle(color):
            return (f"<span style='display:inline-block;width:10px;height:10px;"
                    f"border-radius:50%;background:{color};vertical-align:middle;margin-right:6px'></span>")

        def card(header_color, badge_text, title_text, body_html, bg):
            return (
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
                f"style='margin:12px 0;border-left:6px solid {header_color};background:{bg};"
                "border-radius:10px'><tr><td style='padding:12px 14px'>"
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
                "<tr>"
                "<td valign='top' width='20' style='padding-right:8px'>"
                f"{circle(header_color)}"
                "</td>"
                "<td>"
                f"<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:{header_color};text-transform:uppercase;margin-bottom:4px'>{badge_text}</div>"
                f"<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>{title_text}</div>"
                f"<div style='font-size:14px;color:#333;opacity:.95'>{body_html}</div>"
                "</td></tr></table>"
                "</td></tr></table>"
            )

        # Wrapper
        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#222;'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px;background:#f5f7fb;'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='760' cellspacing='0' cellpadding='0' style='max-width:760px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.06)'>"
            "<tr><td style='background:#0b5cab;color:#fff;padding:22px'>"
            "<div style='font-size:28px;font-weight:800;line-height:1.2'>Resumen Semanal de Amenazas de Enfermedades Transmisibles</div>"
            "<div style='opacity:.95;font-size:14px;margin-top:6px'>Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>"
            f"<div style='margin-top:10px;display:inline-block;background:rgba(255,255,255,.2);padding:6px 12px;border-radius:30px;font-weight:700'>{week_label}</div>"
            "</td></tr>"
            "<tr><td style='padding:16px 18px 0'>"
            # CTA botón PDF
            f"<div style='text-align:center;margin:4px 0 16px'><a href='{pdf_url}' target='_blank' "
            "style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:12px 18px;"
            "border-radius:8px;font-weight:700'>Abrir / Descargar PDF del informe</a></div>"
            f"<div style='text-align:center;font-size:12px;color:#6b7280;margin:-6px 0 10px'>"
            "Si el botón no funciona, copia y pega este enlace en tu navegador:<br>"
            f"<span style='word-break:break-all'>{pdf_url}</span></div>"
        )

        # Tarjetas principales
        html += card("#2e7d32", "Virus del Nilo Occidental",
                     "652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)",
                     "Italia concentra la mayoría de casos;&nbsp;"
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;"
                     "border-left:4px solid #ff9800'>🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>.",
                     "#f0f7f2")

        html += card("#d32f2f", "Fiebre Crimea-Congo (CCHF)",
                     "Sin nuevos casos esta semana",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>"
                     "🇪🇸 España: 3 casos en 2025</span>; Grecia 2 casos.",
                     "#fbf1f1")

        html += card("#1565c0", "Respiratorios",
                     "COVID-19 al alza en detección; Influenza y VRS en niveles bajos",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>"
                     "🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2.",
                     "#eef4fb")

        # Puntos clave (tablas)
        html += (
            "</td></tr>"
            "<tr><td style='padding:0 18px 10px'>"
            "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
            "<tr><td style='border-left:6px solid #2e7d32;padding:8px 10px;font-size:14px'>"
            f"{circle('#2e7d32')}Expansión estacional en 9 países; mortalidad global ~6%."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #ef6c00;padding:8px 10px;font-size:14px'>"
            f"{circle('#ef6c00')}Dengue autóctono en Francia/Italia/Portugal; sin casos en España."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #1565c0;padding:8px 10px;font-size:14px'>"
            f"{circle('#1565c0')}A(H9N2) esporádico en Asia; riesgo UE/EEE: muy bajo."
            "</td></tr>"
            "</table>"
            "</td></tr>"
            # Pie
            "<tr><td style='padding:8px 18px 20px;text-align:center'>"
            f"<a href='{article_url}' target='_blank' "
            "style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;"
            "padding:10px 16px;border-radius:8px;font-weight:700'>Página del informe (ECDC)</a>"
            "</td></tr>"
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            f"Generado automáticamente · Fuente: ECDC (CDTR) · Fecha: {gen_date_es}"
            "</td></tr>"
            "</table></td></tr></table></body></html>"
        )
        return html

    # ------------------ Envío email (multipart/alternative) ------------------

    def send_email(self, subject, plain_text, html_body):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        raw = self.config.receiver_email
        for sep in [";", "\n"]:
            raw = raw.replace(sep, ",")
        to_addresses = [e.strip() for e in raw.split(",") if e.strip()]
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.config.sender_email
        msg['To'] = ", ".join(to_addresses)

        minimal_plain = plain_text or "Ver versión HTML."
        msg.attach(MIMEText(minimal_plain, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        logging.info("SMTP: from=%s → to=%s", self.config.sender_email, to_addresses)

        ctx = ssl.create_default_context()
        if int(self.config.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.sendmail(self.config.sender_email, to_addresses, msg.as_string())
        else:
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.sendmail(self.config.sender_email, to_addresses, msg.as_string())
        logging.info("Correo enviado correctamente.")

    # ------------------ Run ------------------

    def run(self):
        try:
            pdf_url, article_url, week, year = self.fetch_latest_pdf()
        except Exception as e:
            logging.exception("No se pudo localizar el PDF más reciente: %s", e)
            return

        # Anti-duplicados
        state = self._load_last_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado previamente, no se reenvía.")
            return

        week_label = f"Semana {week}: fechas según CDTR" if week else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())
        html_body = self.build_email_body(week_label, gen_date_es, pdf_url, article_url)

        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"
        plain = ""  # parte texto mínima

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto: %s | HTML length=%d", subject, len(html_body))
            logging.info("PDF URL: %s | Article URL: %s", pdf_url, article_url)
            return

        try:
            self.send_email(subject=subject, plain_text=plain, html_body=html_body)
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    cfg = Config()
    WeeklyReportAgent(cfg).run()


