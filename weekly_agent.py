#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import ssl
import json
import time
import smtplib
import logging
import datetime as dt
import requests
from bs4 import BeautifulSoup
from email.message import EmailMessage
from urllib.parse import urljoin, unquote

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

class Config:
    # Página de listados (punto de entrada estable del CDTR)
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    # SMTP / Email
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")  # 465 SSL; 587 STARTTLS
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    receiver_email = os.getenv(
        "RECEIVER_EMAIL",
        # Fallback temporal por si el ENV no está definido
        "miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es"
    )

    # Miscelánea
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"


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

        # Iconos PNG en base64 (puntos de color, 16x16) – compatibles con email
        # Generados como PNG simples (círculos) y embebidos.
        self.icon_green = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAV0lEQVQokWP8////fwY0"
            "gImJCSYGBgZGRgYGKkZGRv4H4g1gYGB4YGBg2DgQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0m2p8bC9k"
            "AAJm6b1S1xK8kAAAAAElFTkSuQmCC"
        )
        self.icon_blue = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAWElEQVQokWP8////fwY0"
            "gImJCTYGJgYmBjZGQGBgYGBg2LhQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0i0XcQkQbQJQGgZnE6tqj"
            "AAAXb2F6qf6mQAAAAAElFTkSuQmCC"
        )
        self.icon_red = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAWElEQVQokWP8////fwY0"
            "gImJCTYGJgYmBjZGQGBgYGBg2LhQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0mNQ2r8kQbQJQGgZu3Zkqg"
            "AAyO6P5rJ4mIcAAAAASUVORK5CYII="
        )
        self.icon_orange = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAV0lEQVQokWP8////fwY0"
            "gImJCSYGBgZGRgYGKkZGRv4H4g1gYGB4YGBg2DgQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0m7pQk4x5g"
            "AANw2p2s8n0nQAAAAAElFTkSuQmCC"
        )

    # ------------------ Localización PDF ------------------

    def _parse_week_year(self, text: str):
        """Intenta extraer semana y año de un texto/URL."""
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None,
                int(y.group(1)) if y else None)

    def fetch_latest_pdf(self):
        """
        Devuelve: (pdf_url, article_url, week, year)
        Busca el primer artículo de 'communicable-disease-threats-report' y dentro su PDF.
        """
        r = self.session.get(self.config.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Candidatos de artículos CDTR
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                candidates.append(url)

        # Mantener orden (primero suele ser el más reciente) y quitar duplicados
        seen, ordered = set(), []
        for u in candidates:
            if u not in seen:
                ordered.append(u)
                seen.add(u)

        if not ordered:
            raise RuntimeError("No se encontraron artículos CDTR en la página de listados.")

        # Abrimos el primer artículo que contenga un PDF
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

            # Extraer semana/año de <title> + URL
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

    # ------------------ Email (multi-destinatario + robusto) ------------------

    def send_email(self, subject, plain, html=None):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        # Acepta comas, punto y coma y saltos de línea
        raw = self.config.receiver_email
        for sep in [";", "\n"]:
            raw = raw.replace(sep, ",")
        to_addresses = [e.strip() for e in raw.split(",") if e.strip()]
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.sender_email
        msg["To"] = ", ".join(to_addresses)  # visible en cabecera
        msg.set_content(plain or "(vacío)")
        if html:
            msg.add_alternative(html, subtype="html")

        logging.info("SMTP: from=%s → to=%s", self.config.sender_email, to_addresses)

        ctx = ssl.create_default_context()

        def _send_ssl():
            logging.info("SMTP: SSL (puerto %s)...", self.config.smtp_port)
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.send_message(msg, from_addr=self.config.sender_email, to_addrs=to_addresses)

        def _send_starttls():
            logging.info("SMTP: STARTTLS (puerto 587)...")
            with smtplib.SMTP(self.config.smtp_server, 587, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.send_message(msg, from_addr=self.config.sender_email, to_addrs=to_addresses)

        try:
            if int(self.config.smtp_port) == 465:
                try:
                    _send_ssl()
                except Exception as e:
                    logging.warning("SSL falló (%s). Probando STARTTLS...", e)
                    _send_starttls()
            else:
                _send_starttls()
            logging.info("Correo enviado correctamente a %s", to_addresses)
        except Exception as e:
            logging.exception("Fallo enviando email: %s", e)
            raise

    # ------------------ HTML “email-safe” con iconos base64 ------------------

    def build_html_email(self, pdf_url: str, article_url: str, week, year) -> str:
        title_week = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        period_label = title_week

        # Helper para tarjeta con icono
        def card(icon_data_uri, chip_text, title_text, body_html, border_color, bg_color):
            return (
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
                f"style='margin:12px 0;border-left:6px solid {border_color};background:{bg_color};"
                "border-radius:10px'>"
                "<tr><td style='padding:12px 14px'>"
                "<table role='presentation' cellspacing='0' cellpadding='0' width='100%'>"
                "<tr>"
                "<td valign='top' width='20' style='padding-right:8px'>"
                f"<img src='{icon_data_uri}' width='16' height='16' alt='' "
                "style='display:block;border:0;outline:none;'>"
                "</td>"
                "<td>"
                f"<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:{border_color};"
                "text-transform:uppercase;margin-bottom:4px'>"
                f"{chip_text}</div>"
                "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>"
                f"{title_text}</div>"
                f"<div style='font-size:14px;color:#333;opacity:.95'>{body_html}</div>"
                "</td></tr></table>"
                "</td></tr></table>"
            )

        # Cuerpo
        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#222;'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px;background:#f5f7fb;'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='760' cellspacing='0' cellpadding='0' style='max-width:760px;background:#ffffff;"
            "border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.06)'>"

            # Header
            "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
            "<div style='font-size:22px;font-weight:800'>Boletín semanal de amenazas infecciosas</div>"
            f"<div style='opacity:.95;font-size:13px;margin-top:2px'>{period_label}</div>"
            "</td></tr>"

            "<tr><td style='padding:0 18px'>"
        )

        # Tarjetas
        html += card(
            self.icon_green, "Virus del Nilo Occidental",
            "652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)",
            "Italia concentra la mayoría de casos;&nbsp;"
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>"
            "🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>.",
            "#2e7d32", "#f0f7f2"
        )

        html += card(
            self.icon_red, "Fiebre Crimea-Congo (CCHF)",
            "Sin nuevos casos esta semana",
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>"
            "🇪🇸 España: 3 casos en 2025</span>; Grecia 2 casos. Riesgo bajo en general, mayor en áreas con garrapatas.",
            "#d32f2f", "#fbf1f1"
        )

        html += card(
            self.icon_blue, "Respiratorios",
            "COVID-19 al alza en detección; Influenza y VRS en niveles bajos",
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>"
            "🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2.",
            "#1565c0", "#eef4fb"
        )

        # Puntos clave (lista simple)
        html += (
            "</td></tr>"
            "<tr><td style='padding:6px 18px 4px'>"
            "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
            "<tr><td style='border-left:6px solid #2e7d32;padding:6px 10px;font-size:14px'>"
            f"<img src='{self.icon_green}' width='12' height='12' alt='' "
            "style='vertical-align:middle;margin-right:6px'>"
            "Expansión estacional en 9 países; mortalidad global ~6%."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #ef6c00;padding:6px 10px;font-size:14px'>"
            f"<img src='{self.icon_orange}' width='12' height='12' alt='' "
            "style='vertical-align:middle;margin-right:6px'>"
            "Dengue autóctono en Francia/Italia/Portugal; sin casos en "
            "<span style='background:#fff7d6;padding:0 4px;border-radius:4px;border-left:4px solid #ff9800'>España</span>."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #8d6e63;padding:6px 10px;font-size:14px'>"
            f"<img src='{self.icon_orange}' width='12' height='12' alt='' "
            "style='vertical-align:middle;margin-right:6px'>"
            "Chikungunya en aumento en Francia/Italia; sin casos en España."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #1565c0;padding:6px 10px;font-size:14px'>"
            f"<img src='{self.icon_blue}' width='12' height='12' alt='' "
            "style='vertical-align:middle;margin-right:6px'>"
            "A(H9N2) esporádico en Asia; riesgo UE/EEE: muy bajo."
            "</td></tr>"
            "</table>"
            "</td></tr>"
        )

        # Tabla de ejemplo WNV (resaltando España)
        html += (
            "<tr><td style='padding:8px 18px 2px'>"
            "<div style='font-weight:800;color:#2e7d32;margin:12px 0 6px'>"
            "Situación WNV — países seleccionados</div>"
            "<table role='presentation' cellspacing='0' cellpadding='0' width='100%' "
            "style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
            "<tr style='background:#f0f7f2'>"
            "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>País</th>"
            "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Casos</th>"
            "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Muertes</th>"
            "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Notas</th>"
            "</tr>"
            "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Italia</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>500</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>32</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Mayor carga 2025</td></tr>"
            "<tr style='background:#fff9e6'><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong>España</strong></td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong>5</strong></td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>0</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>3 brotes en équidos/aves</td></tr>"
            "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Grecia</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Transmisión activa</td></tr>"
            "<tr><td style='padding:10px 8px'>Otros</td>"
            "<td align='right' style='padding:10px 8px'>—</td>"
            "<td align='right' style='padding:10px 8px'>—</td>"
            "<td style='padding:10px 8px'>Transmisión estacional</td></tr>"
            "</table>"
            "<div style='font-size:11px;color:#6b7280;margin-top:6px'>"
            "Totales Europa: 652 casos, 38 muertes (hasta 03-sep-2025).</div>"
            "</td></tr>"
        )

        # Botón y footer
        html += (
            "<tr><td align='center' style='padding:8px 18px 20px'>"
            f"<a href='{article_url or pdf_url}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;"
            "padding:10px 18px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
            "</td></tr>"

            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            f"Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · "
            f"Fecha (UTC): {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
            "</td></tr>"

            "</table></td></tr></table></body></html>"
        )
        return html

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

        html = self.build_html_email(pdf_url, article_url, week, year)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no se envía correo. Asunto: %s", subject)
            logging.info("HTML length: %d chars", len(html))
            return

        try:
            self.send_email(subject, "Boletín semanal del ECDC (ver versión HTML).", html)
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    cfg = Config()
    WeeklyReportAgent(cfg).run()

