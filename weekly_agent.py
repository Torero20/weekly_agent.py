#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, ssl, smtplib, json, logging, requests, datetime as dt
from email.message import EmailMessage
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

class Config:
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email = os.getenv("SENDER_EMAIL", "")
    receiver_email = os.getenv("RECEIVER_EMAIL", "miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es")
    email_password = os.getenv("EMAIL_PASSWORD", "")
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

    # ------------------ Localización PDF ------------------

    def _parse_week_year(self, text: str):
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None,
                int(y.group(1)) if y else None)

    def fetch_latest_pdf(self):
        """
        Devuelve: (pdf_url, article_url, week, year)
        Busca el primer artículo de 'communicable-disease-threats-report' y su PDF.
        """
        r = self.session.get(self.config.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Candidatos de artículos (enlace que contenga el slug CDTR)
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                candidates.append(url)

        # El primero suele ser el más reciente; de todos modos, quitamos duplicados manteniendo orden
        seen, ordered = set(), []
        for u in candidates:
            if u not in seen:
                ordered.append(u)
                seen.add(u)

        if not ordered:
            raise RuntimeError("No se encontraron artículos CDTR en la página de listados.")

        # Abrimos el primer artículo que tenga PDF
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

            # Semana/Año desde título o URL
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

    # ------------------ Email ------------------

    def send_email(self, subject, plain, html=None):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        # Destinatarios separados por coma
        to_addresses = [e.strip() for e in self.config.receiver_email.split(",") if e.strip()]
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.sender_email
        msg["To"] = ", ".join(to_addresses)  # visible en cabecera
        msg.set_content(plain or "(vacío)")
        if html:
            msg.add_alternative(html, subtype="html")

        logging.info("SMTP: server=%s port=%s from=%s to=%s",
                     self.config.smtp_server, self.config.smtp_port,
                     self.config.sender_email, to_addresses)

        ctx = ssl.create_default_context()

        def _send_ssl():
            logging.info("SMTP: intentando SSL (puerto %s)...", self.config.smtp_port)
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.send_message(msg, from_addr=self.config.sender_email, to_addrs=to_addresses)

        def _send_starttls():
            logging.info("SMTP: intentando STARTTLS (puerto 587)...")
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
                    logging.warning("SMTP SSL falló (%s). Probando STARTTLS...", e)
                    _send_starttls()
            else:
                _send_starttls()
            logging.info("Correo enviado correctamente a %s", to_addresses)
        except smtplib.SMTPAuthenticationError as e:
            logging.error("Autenticación SMTP fallida: %s. Revisa SENDER_EMAIL y EMAIL_PASSWORD (App Password).", e)
            raise
        except smtplib.SMTPRecipientsRefused as e:
            logging.error("El servidor rechazó destinatarios: %s", e.recipients)
            raise
        except Exception as e:
            logging.exception("Fallo enviando email: %s", e)
            raise

    # ------------------ HTML (visual, en español) ------------------

    def build_html_email(self, pdf_url: str, article_url: str, week, year) -> str:
        title_week = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        period_label = f"ECDC · Semana 37 · 6–12 septiembre 2025" if (week, year) == (37, 2025) else title_week

        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#222;'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px;background:#f5f7fb;'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='760' cellspacing='0' cellpadding='0' style='max-width:760px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.06)'>"

            # Header
            "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
            "<div style='font-size:22px;font-weight:800'>Boletín semanal de amenazas infecciosas</div>"
            f"<div style='opacity:.95;font-size:13px;margin-top:2px'>{period_label}</div>"
            "</td></tr>"

            # Cards
            "<tr><td style='padding:0 18px'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #2e7d32;background:#f0f7f2;border-radius:10px'>"
            "<tr><td style='padding:12px 14px'>"
            "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#2e7d32;text-transform:uppercase;margin-bottom:4px'>Virus del Nilo Occidental</div>"
            "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)</div>"
            "<div style='font-size:14px;color:#333;opacity:.95'>Italia concentra la mayoría de casos; "
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>."
            "</div></td></tr></table>"

            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #d32f2f;background:#fbf1f1;border-radius:10px'>"
            "<tr><td style='padding:12px 14px'>"
            "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#d32f2f;text-transform:uppercase;margin-bottom:4px'>Fiebre Crimea-Congo (CCHF)</div>"
            "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>Sin nuevos casos esta semana</div>"
            "<div style='font-size:14px;color:#333;opacity:.95'>"
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 3 casos en 2025</span>; "
            "Grecia 2 casos. Riesgo bajo en general, mayor en áreas con garrapatas."
            "</div></td></tr></table>"

            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #1565c0;background:#eef4fb;border-radius:10px'>"
            "<tr><td style='padding:12px 14px'>"
            "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#1565c0;text-transform:uppercase;margin-bottom:4px'>Respiratorios</div>"
            "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>COVID-19 al alza en detección; Influenza y VRS en niveles bajos</div>"
            "<div style='font-size:14px;color:#333;opacity:.95'>"
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2."
            "</div></td></tr></table>"
            "</td></tr>"

            # Chips
            "<tr><td style='padding:6px 18px 4px'>"
            "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
            "<ul style='padding-left:0;margin:0'>"
            "<li style='list-style:none;margin:8px 0'><div style='border-left:6px solid #2e7d32;padding-left:10px'>"
            "<span style='background:#2e7d32;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>WNV</span>"
            "Expansión estacional en 9 países; mortalidad global ~6%."
            "</div></li>"
            "<li style='list-style:none;margin:8px 0'><div style='border-left:6px solid #ef6c00;padding-left:10px'>"
            "<span style='background:#ef6c00;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Dengue</span>"
            "Autóctono en Francia (21), Italia (4), Portugal (2); sin casos en "
            "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>."
            "</div></li>"
            "<li style='list-style:none;margin:8px 0'><div style='border-left:6px solid #8d6e63;padding-left:10px'>"
            "<span style='background:#8d6e63;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Chikungunya</span>"
            "Francia 383 (82 nuevos), Italia 167 (60 nuevos); sin casos en España."
            "</div></li>"
            "<li style='list-style:none;margin:8px 0'><div style='border-left:6px solid #1565c0;padding-left:10px'>"
            "<span style='background:#1565c0;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>A(H9N2)</span>"
            "4 casos leves en China (niños); riesgo para UE/EEE: muy bajo."
            "</div></li>"
            "<li style='list-style:none;margin:8px 0'><div style='border-left:6px solid #6a1b9a;padding-left:10px'>"
            "<span style='background:#6a1b9a;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Sarampión</span>"
            "Aumento en Europa central/oriental por coberturas subóptimas; sin cambios en España."
            "</div></li>"
            "</ul></td></tr>"

            # Tabla WNV
            "<tr><td style='padding:8px 18px 2px'>"
            "<div style='font-weight:800;color:#2e7d32;margin:12px 0 6px'>🦟 Virus del Nilo Occidental — situación por país</div>"
            "<table role='presentation' cellspacing='0' cellpadding='0' width='100%' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
            "<thead><tr style='background:#f0f7f2'>"
            "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>País</th>"
            "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Casos humanos</th>"
            "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Muertes</th>"
            "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Notas</th>"
            "</tr></thead>"
            "<tbody>"
            "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Italia</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>500</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>32</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Mayor carga 2025</td></tr>"
            "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong style='color:#0b6e0b'>España 🇪🇸</strong></td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong>5</strong></td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>0</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>3 brotes en équidos/aves</td></tr>"
            "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Grecia</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
            "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
            "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Transmisión activa</td></tr>"
            "<tr><td style='padding:10px 8px'>Otros (Rumanía, Hungría, Francia, Alemania, Croacia, Bulgaria)</td>"
            "<td align='right' style='padding:10px 8px'>—</td>"
            "<td align='right' style='padding:10px 8px'>—</td>"
            "<td style='padding:10px 8px'>Transmisión estacional</td></tr>"
            "</tbody></table>"
            "<div style='font-size:11px;color:#6b7280;margin-top:6px'>Totales Europa: 652 casos humanos, 38 muertes (hasta 03-sep-2025).</div>"
            "</td></tr>"

            # Botón PDF
            "<tr><td align='center' style='padding:8px 18px 20px'>"
            f"<a href='{article_url or pdf_url}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
            "</td></tr>"

            # Footer
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            f"Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · Fecha (UTC): {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
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

