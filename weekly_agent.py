#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import ssl
import json
import base64
import smtplib
import logging
import datetime as dt
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

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

        # Iconos (círculos de color 16x16) como bytes (no data:), para inline CID
        self.icon_pngs = {
            "icon_green": base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAV0lEQVQokWP8////fwY0"
                "gImJCSYGBgZGRgYGKkZGRv4H4g1gYGB4YGBg2DgQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0m2p8bC9k"
                "AAJm6b1S1xK8kAAAAAElFTkSuQmCC"
            ),
            "icon_blue": base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAWElEQVQokWP8////fwY0"
                "gImJCTYGJgYmBjZGQGBgYGBg2LhQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0i0XcQkQbQJQGgZnE6tqj"
                "AAAXb2F6qf6mQAAAAAElFTkSuQmCC"
            ),
            "icon_red": base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAWElEQVQokWP8////fwY0"
                "gImJCTYGJgYmBjZGQGBgYGBg2LhQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0mNQ2r8kQbQJQGgZu3Zkqg"
                "AAyO6P5rJ4mIcAAAAASUVORK5CYII="
            ),
            "icon_orange": base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAV0lEQVQokWP8////fwY0"
                "gImJCSYGBgZGRgYGKkZGRv4H4g1gYGB4YGBg2DgQkRMQGgQwQGg0gA2gQkQbQAjQGQbwF0GgYkGQZgKkA2gJgB0m7pQk4x5g"
                "AANw2p2s8n0nQAAAAAElFTkSuQmCC"
            ),
        }

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

    # ------------------ HTML email-safe con CIDs ------------------

    def build_email_safe_html(self, pdf_url: str, article_url: str, week, year) -> str:
        period_label = f"Semana {week} · {year}" if week and year else "Último informe ECDC"

        def card(cid, chip_text, title_text, body_html, border_color, bg_color):
            return (
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
                f"style='margin:12px 0;border-left:6px solid {border_color};background:{bg_color};"
                "border-radius:10px'>"
                "<tr><td style='padding:12px 14px'>"
                "<table role='presentation' cellspacing='0' cellpadding='0' width='100%'>"
                "<tr>"
                "<td valign='top' width='20' style='padding-right:8px'>"
                f"<img src='cid:{cid}' width='16' height='16' alt='' style='display:block;border:0;outline:none;'>"
                "</td>"
                "<td>"
                f"<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:{border_color};text-transform:uppercase;margin-bottom:4px'>{chip_text}</div>"
                f"<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>{title_text}</div>"
                f"<div style='font-size:14px;color:#333;opacity:.95'>{body_html}</div>"
                "</td></tr></table>"
                "</td></tr></table>"
            )

        html = (
            "<html><body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#222;'>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px;background:#f5f7fb;'>"
            "<tr><td align='center'>"
            "<table role='presentation' width='760' cellspacing='0' cellpadding='0' style='max-width:760px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.06)'>"
            "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
            "<div style='font-size:22px;font-weight:800'>Boletín semanal de amenazas infecciosas</div>"
            f"<div style='opacity:.95;font-size:13px;margin-top:2px'>{period_label}</div>"
            "</td></tr>"
            "<tr><td style='padding:0 18px'>"
        )

        html += card("icon_green", "Virus del Nilo Occidental",
                     "652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)",
                     "Italia concentra la mayoría de casos;&nbsp;"
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>.",
                     "#2e7d32", "#f0f7f2")

        html += card("icon_red", "Fiebre Crimea-Congo (CCHF)",
                     "Sin nuevos casos esta semana",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 3 casos en 2025</span>; Grecia 2 casos.",
                     "#d32f2f", "#fbf1f1")

        html += card("icon_blue", "Respiratorios",
                     "COVID-19 al alza en detección; Influenza y VRS en niveles bajos",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2.",
                     "#1565c0", "#eef4fb")

        html += (
            "</td></tr>"
            "<tr><td style='padding:6px 18px 4px'>"
            "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
            "<tr><td style='border-left:6px solid #2e7d32;padding:6px 10px;font-size:14px'>"
            "<img src='cid:icon_green' width='12' height='12' alt='' style='vertical-align:middle;margin-right:6px'>"
            "Expansión estacional en 9 países; mortalidad global ~6%."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #ef6c00;padding:6px 10px;font-size:14px'>"
            "<img src='cid:icon_orange' width='12' height='12' alt='' style='vertical-align:middle;margin-right:6px'>"
            "Dengue autóctono en Francia/Italia/Portugal; sin casos en España."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #1565c0;padding:6px 10px;font-size:14px'>"
            "<img src='cid:icon_blue' width='12' height='12' alt='' style='vertical-align:middle;margin-right:6px'>"
            "A(H9N2) esporádico en Asia; riesgo UE/EEE: muy bajo."
            "</td></tr>"
            "</table>"
            "</td></tr>"
            "<tr><td align='center' style='padding:8px 18px 20px'>"
            f"<a href='{article_url or pdf_url}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
            "</td></tr>"
            "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
            f"Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · Fecha (UTC): {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
            "</td></tr>"
            "</table></td></tr></table></body></html>"
        )
        return html

    # ------------------ HTML enriquecido (adjunto correcto) ------------------

    def build_rich_html_attachment(self, week_label: str, gen_date_es: str) -> str:
        # CSS con llaves simples (válido); sin placeholders dobles.
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Resumen Semanal de Amenazas de Enfermedades Transmisibles - {week_label}</title>
<style>
body {{
  font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
  line-height: 1.6; color: #333; max-width: 1000px; margin: 0 auto;
  padding: 20px; background-color: #f9f9f9;
}}
.header {{ text-align: center; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 3px solid #2b6ca3; }}
.header h1 {{ color: #2b6ca3; margin-bottom: 5px; }}
.header .subtitle {{ color: #666; font-size: 1.2em; }}
.header .week {{ background-color: #2b6ca3; color: white; display: inline-block; padding: 5px 10px; border-radius: 4px; margin-top: 10px; }}
.highlight-box {{ background-color: #e8f4ff; border-left: 5px solid #2b6ca3; padding: 15px; margin: 20px 0; border-radius: 4px; }}
.spain-highlight {{ background-color: #fff3cd; border-left: 5px solid #ffc107; padding: 15px; margin: 20px 0; border-radius: 4px; }}
.spain-highlight h3 {{ color: #856404; margin-top: 0; display: flex; align-items: center; }}
.spain-highlight h3:before {{ content: "🇪🇸"; margin-right: 10px; }}
.section {{ margin-bottom: 30px; background-color: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
.section h2 {{ color: #2b6ca3; border-bottom: 2px solid #eaeaea; padding-bottom: 10px; margin-top: 0; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px; margin: 15px 0; }}
.stat-card {{ background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #eaeaea; }}
.stat-card .number {{ font-size: 1.8em; font-weight: bold; color: #2b6ca3; }}
.stat-card .label {{ font-size: 0.9em; color: #666; }}
.key-points {{ background-color: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0; }}
.key-points ul {{ padding-left: 20px; margin: 0; }}
.key-points li {{ margin-bottom: 8px; }}
.footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eaeaea; color: #666; font-size: 0.9em; }}
.tag {{ display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 0.8em; margin-right: 5px; background-color: #e9ecef; color: #495057; }}
.risk-low {{ background-color: #d4edda; color: #155724; }}
.risk-moderate {{ background-color: #fff3cd; color: #856404; }}
.risk-high {{ background-color: #f8d7da; color: #721c24; }}
</style>
</head>
<body>
<div class="header">
  <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
  <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
  <div class="week">{week_label}</div>
</div>

<div class="highlight-box">
  <h2>Resumen Ejecutivo</h2>
  <p>La actividad de virus respiratorios en la UE/EEA se mantiene en niveles bajos o basales tras el verano, con incrementos graduales de SARS-CoV-2 pero con hospitalizaciones y muertes por debajo del mismo período de 2024. Se reportan nuevos casos humanos de gripe aviar A(H9N2) en China y un brote de Ébola en la República Democrática del Congo. Continúa la vigilancia estacional de enfermedades transmitidas por vectores (WNV, dengue, chikungunya, CCHF).</p>
</div>

<!-- Aquí puedes pegar el resto de tu contenido enriquecido -->

<div class="footer">
  <p>Resumen generado el: {gen_date_es}</p>
  <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
  <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
</div>
</body>
</html>
"""
        return html

    # ------------------ Envío email con estructura MIME robusta ------------------

    def send_email(self, subject, plain_text, html_body, inline_images, attachment_html=None, attachment_name="resumen_ecdc.html"):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        # Parse destinatarios
        raw = self.config.receiver_email
        for sep in [";", "\n"]:
            raw = raw.replace(sep, ",")
        to_addresses = [e.strip() for e in raw.split(",") if e.strip()]
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        # Root: mixed
        msg_root = MIMEMultipart('mixed')
        msg_root['Subject'] = subject
        msg_root['From'] = self.config.sender_email
        msg_root['To'] = ", ".join(to_addresses)

        # alternative (plain + related)
        msg_alt = MIMEMultipart('alternative')
        msg_root.attach(msg_alt)

        # plain
        msg_alt.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))

        # related (html + imágenes inline)
        msg_rel = MIMEMultipart('related')
        msg_alt.attach(msg_rel)

        # html
        msg_rel.attach(MIMEText(html_body, 'html', 'utf-8'))

        # imágenes inline
        for cid, binary in inline_images.items():
            img = MIMEImage(binary, name=f"{cid}.png")
            img.add_header('Content-ID', f"<{cid}>")
            img.add_header('Content-Disposition', 'inline', filename=f"{cid}.png")
            msg_rel.attach(img)

        # adjunto opcional: HTML enriquecido
        if attachment_html:
            attach_part = MIMEText(attachment_html, 'html', 'utf-8')
            attach_part.add_header('Content-Disposition', 'attachment', filename=attachment_name)
            msg_root.attach(attach_part)

        logging.info("SMTP: from=%s → to=%s", self.config.sender_email, to_addresses)

        ctx = ssl.create_default_context()
        try:
            if int(self.config.smtp_port) == 465:
                with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx, timeout=30) as s:
                    s.ehlo()
                    if self.config.email_password:
                        s.login(self.config.sender_email, self.config.email_password)
                    s.sendmail(self.config.sender_email, to_addresses, msg_root.as_string())
            else:
                with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=30) as s:
                    s.ehlo()
                    s.starttls(context=ctx)
                    s.ehlo()
                    if self.config.email_password:
                        s.login(self.config.sender_email, self.config.email_password)
                    s.sendmail(self.config.sender_email, to_addresses, msg_root.as_string())
            logging.info("Correo enviado correctamente.")
        except Exception as e:
            logging.exception("Fallo enviando email: %s", e)
            raise

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

        # Cuerpo HTML email-safe (con CIDs)
        email_html = self.build_email_safe_html(pdf_url, article_url, week, year)

        # Adjunto enriquecido
        week_label = f"Semana {week}: fechas según CDTR" if week else "Último informe"
        gen_date_es = dt.datetime.utcnow().strftime("%d de %B de %Y (UTC)")
        rich_html = self.build_rich_html_attachment(week_label, gen_date_es)

        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"
        plain = "Boletín semanal del ECDC. Abre este correo con un cliente que muestre HTML o usa el adjunto."

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto: %s", subject)
            logging.info("HTML body length: %d | adjunto length: %d", len(email_html), len(rich_html))
            return

        try:
            self.send_email(
                subject=subject,
                plain_text=plain,
                html_body=email_html,
                inline_images=self.icon_pngs,   # CIDs: icon_green, icon_blue, icon_red, icon_orange
                attachment_html=rich_html,
                attachment_name="resumen_ecdc.html"
            )
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    cfg = Config()
    WeeklyReportAgent(cfg).run()

