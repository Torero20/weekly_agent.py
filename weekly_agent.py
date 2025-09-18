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

    # ------------------ HTML email-safe (sin imágenes, con “círculos” CSS) ------------------

    def build_email_safe_html(self, pdf_url: str, article_url: str, week, year) -> str:
        period_label = f"Semana {week} · {year}" if week and year else "Último informe ECDC"

        def circle(color):
            return (f"<span style='display:inline-block;width:12px;height:12px;border-radius:50%;"
                    f"background:{color};vertical-align:middle;margin-right:6px'></span>")

        def card(color, chip_text, title_text, body_html, border_color, bg_color):
            return (
                "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' "
                f"style='margin:12px 0;border-left:6px solid {border_color};background:{bg_color};"
                "border-radius:10px'>"
                "<tr><td style='padding:12px 14px'>"
                "<table role='presentation' cellspacing='0' cellpadding='0' width='100%'>"
                "<tr>"
                "<td valign='top' width='20' style='padding-right:8px'>"
                f"{circle(color)}"
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

        html += card("#2e7d32", "Virus del Nilo Occidental",
                     "652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)",
                     "Italia concentra la mayoría de casos;&nbsp;"
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>.",
                     "#2e7d32", "#f0f7f2")

        html += card("#d32f2f", "Fiebre Crimea-Congo (CCHF)",
                     "Sin nuevos casos esta semana",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 3 casos en 2025</span>; Grecia 2 casos.",
                     "#d32f2f", "#fbf1f1")

        html += card("#1565c0", "Respiratorios",
                     "COVID-19 al alza en detección; Influenza y VRS en niveles bajos",
                     "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2.",
                     "#1565c0", "#eef4fb")

        html += (
            "</td></tr>"
            "<tr><td style='padding:6px 18px 4px'>"
            "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
            "<table role='presentation' width='100%' cellspacing='0' cellpadding='0'>"
            "<tr><td style='border-left:6px solid #2e7d32;padding:6px 10px;font-size:14px'>"
            f"{circle('#2e7d32')}Expansión estacional en 9 países; mortalidad global ~6%."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #ef6c00;padding:6px 10px;font-size:14px'>"
            f"{circle('#ef6c00')}Dengue autóctono en Francia/Italia/Portugal; sin casos en España."
            "</td></tr>"
            "<tr><td style='border-left:6px solid #1565c0;padding:6px 10px;font-size:14px'>"
            f"{circle('#1565c0')}A(H9N2) esporádico en Asia; riesgo UE/EEE: muy bajo."
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

    # ------------------ HTML enriquecido (adjunto) - NUEVO FORMATO ------------------

    def build_rich_html_attachment(self, week_label: str, gen_date_es: str) -> str:
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resumen Semanal ECDC - {week_label}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }}
        body {{
            background-color: #f5f7fa;
            color: #333;
            line-height: 1.6;
            padding: 20px;
            max-width: 1200px;
            margin: 0 auto;
        }}
        .header {{
            text-align: center;
            padding: 20px;
            background: linear-gradient(135deg, #2b6ca3 0%, #1a4e7a 100%);
            color: white;
            border-radius: 10px;
            margin-bottom: 25px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        }}
        .header h1 {{
            font-size: 2.2rem;
            margin-bottom: 10px;
        }}
        .header .subtitle {{
            font-size: 1.2rem;
            margin-bottom: 15px;
            opacity: 0.9;
        }}
        .header .week {{
            background-color: rgba(255, 255, 255, 0.2);
            display: inline-block;
            padding: 8px 16px;
            border-radius: 30px;
            font-weight: 600;
        }}
        .container {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}
        @media (max-width: 900px) {{
            .container {{
                grid-template-columns: 1fr;
            }}
        }}
        .card {{
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.05);
            transition: transform 0.3s ease;
        }}
        .card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 6px 12px rgba(0, 0, 0, 0.1);
        }}
        .card h2 {{
            color: #2b6ca3;
            border-bottom: 2px solid #eaeaea;
            padding-bottom: 10px;
            margin-bottom: 15px;
            font-size: 1.4rem;
        }}
        .spain-card {{
            border-left: 5px solid #c60b1e;
            background-color: #fff9f9;
        }}
        .spain-card h2 {{
            color: #c60b1e;
            display: flex;
            align-items: center;
        }}
        .spain-card h2:before {{
            content: "🇪🇸";
            margin-right: 10px;
        }}
        .stat-grid {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            margin: 15px 0;
        }}
        .stat-box {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
            border: 1px solid #eaeaea;
        }}
        .stat-box .number {{
            font-size: 1.8rem;
            font-weight: bold;
            color: #2b6ca3;
            margin-bottom: 5px;
        }}
        .stat-box .label {{
            font-size: 0.9rem;
            color: #666;
        }}
        .spain-stat .number {{
            color: #c60b1e;
        }}
        .key-points {{
            background-color: #e8f4ff;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
        }}
        .key-points h3 {{
            margin-bottom: 10px;
            color: #2b6ca3;
        }}
        .key-points ul {{
            padding-left: 20px;
        }}
        .key-points li {{
            margin-bottom: 8px;
        }}
        .risk-tag {{
            display: inline-block;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
            margin-top: 10px;
        }}
        .risk-low {{
            background-color: #d4edda;
            color: #155724;
        }}
        .risk-moderate {{
            background-color: #fff3cd;
            color: #856404;
        }}
        .risk-high {{
            background-color: #f8d7da;
            color: #721c24;
        }}
        .full-width {{
            grid-column: 1 / -1;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #eaeaea;
            color: #666;
            font-size: 0.9rem;
        }}
        .topic-list {{
            list-style-type: none;
        }}
        .topic-list li {{
            padding: 8px 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .topic-list li:last-child {{
            border-bottom: none;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
        <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
        <div class="week">{week_label}</div>
    </div>

    <div class="container">
        <div class="card full-width">
            <h2>Resumen Ejecutivo</h2>
            <p>La actividad de virus respiratorios en la UE/EEA se mantiene en niveles bajos o basales tras el verano, con incrementos graduales de SARS-CoV-2 pero con hospitalizaciones y muertes por debajo del mismo período de 2024. Se reportan nuevos casos humanos de gripe aviar A(H9N2) en China y un brote de Ébola en la República Democrática del Congo. Continúa la vigilancia estacional de enfermedades transmitidas por vectores (WNV, dengue, chikungunya, CCHF).</p>
        </div>

        <div class="card spain-card full-width">
            <h2>Datos Destacados para España</h2>
            <div class="stat-grid">
                <div class="stat-box spain-stat">
                    <div class="number">5</div>
                    <div class="label">Casos de Virus del Nilo Occidental</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">3</div>
                    <div class="label">Casos de Fiebre Hemorrágica de Crimea-Congo</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">1</div>
                    <div class="label">Brote aviar de WNV (Almería)</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">0</div>
                    <div class="label">Nuevos casos de CCHF esta semana</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Virus Respiratorios en la UE/EEA</h2>
            <div class="key-points">
                <h3>Puntos Clave:</h3>
                <ul>
                    <li>Positividad de SARS-CoV-2 en atención primaria: <strong>22.3%</strong></li>
                    <li>Positividad de SARS-CoV-2 en hospitalarios: <strong>10%</strong></li>
                    <li>Actividad de influenza: <strong>2.1%</strong> en atención primaria</li>
                    <li>Actividad de VRS: <strong>1.2%</strong> en atención primaria</li>
                </ul>
            </div>
            <p><strong>Evaluación de riesgo:</strong> Aumento gradual de SARS-CoV-2 pero con impacto sanitario limitado.</p>
            <div class="risk-tag risk-low">RIESGO BAJO</div>
        </div>

        <div class="card">
            <h2>Gripe Aviar A(H9N2) - China</h2>
            <p>Se reportaron 4 nuevos casos en niños en China (9 de septiembre).</p>
            <div class="key-points">
                <h3>Datos Globales:</h3>
                <ul>
                    <li>Total de casos desde 1998: <strong>177 casos</strong> en 10 países</li>
                    <li>Casos en 2025: <strong>26 casos</strong> (todos en China)</li>
                    <li>Tasa de letalidad: <strong>1.13%</strong> (2 muertes)</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">RIESGO MUY BAJO para UE/EEA</div>
        </div>

        <div class="card">
            <h2>Virus del Nilo Occidental (WNV)</h2>
            <div class="key-points">
                <h3>Datos Europeos:</h3>
                <ul>
                    <li><strong>652</strong> casos autóctonos en humanos</li>
                    <li><strong>38</strong> muertes (tasa de letalidad: 6%)</li>
                    <li><strong>9</strong> países reportando casos humanos</li>
                </ul>
            </div>
            <p><strong>Países más afectados:</strong> Italia (500), Grecia (69), Serbia (33), Francia (20)</p>
        </div>

        <div class="card">
            <h2>Otras Enfermedades Transmitidas por Vectores</h2>
            <div class="stat-grid">
                <div class="stat-box">
                    <div class="number">21</div>
                    <div class="label">Dengue (Francia)</div>
                </div>
                <div class="stat-box">
                    <div class="number">4</div>
                    <div class="label">Dengue (Italia)</div>
                </div>
                <div class="stat-box">
                    <div class="number">383</div>
                    <div class="label">Chikungunya (Francia)</div>
                </div>
                <div class="stat-box">
                    <div class="number">167</div>
                    <div class="label">Chikungunya (Italia)</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Ébola - República Democrática del Congo</h2>
            <div class="key-points">
                <h3>Datos del Brote:</h3>
                <ul>
                    <li><strong>68</strong> casos sospechosos</li>
                    <li><strong>16</strong> muertes (tasa de letalidad: 23.5%)</li>
                    <li>Confirmado como cepa Zaire de Ébola</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">RIESGO MUY BAJO para UE/EEA</div>
        </div>

        <div class="card">
            <h2>Sarampión - Vigilancia Mensual</h2>
            <div class="key-points">
                <h3>Datos de la UE/EEA (Julio 2025):</h3>
                <ul>
                    <li><strong>188 casos</strong> reportados en julio</li>
                    <li><strong>13 países</strong> reportaron casos</li>
                    <li><strong>8 muertes</strong> en los últimos 12 meses</li>
                    <li><strong>83.3%</strong> de casos no vacunados</li>
                </ul>
            </div>
            <p><strong>Tendencia:</strong> Disminución general de casos.</p>
        </div>

        <div class="card full-width">
            <h2>Temas Adicionales del Informe</h2>
            <ul class="topic-list">
                <li><strong>Malaria - Grecia:</strong> 2 casos con probable transmisión local y 1 caso con origen indeterminado</li>
                <li><strong>Vigilancia estacional:</strong> Se mantiene la vigilancia de dengue, chikungunya y fiebre hemorrágica de Crimea-Congo</li>
                <li><strong>Eventos en monitorización activa:</strong> Incluyen polio, rabia, fiebre de Lassa y variantes de SARS-CoV-2</li>
            </ul>
        </div>
    </div>

    <div class="footer">
        <p>Resumen generado el: {gen_date_es}</p>
        <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
        <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
    </div>
</body>
</html>
"""
        return html

    # ------------------ Envío email (multipart/alternative + adjunto) ------------------

    def send_email(self, subject, plain_text, html_body, attachment_html=None, attachment_name="resumen_ecdc.html"):
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

        # Root: mixed si hay adjunto; si no, alternative
        root_kind = 'mixed' if attachment_html else 'alternative'
        msg_root = MIMEMultipart(root_kind)
        msg_root['Subject'] = subject
        msg_root['From'] = self.config.sender_email
        msg_root['To'] = ", ".join(to_addresses)

        if root_kind == 'mixed':
            # Cuerpo alternative dentro de mixed
            msg_alt = MIMEMultipart('alternative')
            msg_root.attach(msg_alt)
            msg_alt.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            msg_alt.attach(MIMEText(html_body, 'html', 'utf-8'))

            # Adjunto HTML enriquecido
            attach_part = MIMEText(attachment_html, 'html', 'utf-8')
            attach_part.add_header('Content-Disposition', 'attachment', filename=attachment_name)
            msg_root.attach(attach_part)
        else:
            msg_root.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            msg_root.attach(MIMEText(html_body, 'html', 'utf-8'))

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

        # Cuerpo HTML (círculos CSS, sin imágenes)
        email_html = self.build_email_safe_html(pdf_url, article_url, week, year)

        # Adjunto enriquecido
        week_label = f"Semana {week}: fechas según CDTR" if week else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())
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
