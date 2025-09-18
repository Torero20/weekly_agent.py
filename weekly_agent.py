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

    # ------------------ HTML enriquecido (CUERPO del correo) ------------------

    def build_rich_html_body(self, week_label: str, gen_date_es: str, pdf_url: str, article_url: str) -> str:
        # CTA común (botón compatible con la mayoría de clientes)
        cta = f"""
        <div style="text-align:center;margin:18px 0 6px">
          <a href="{pdf_url}" target="_blank"
             style="display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;
                    padding:12px 18px;border-radius:8px;font-weight:700">
            Abrir / Descargar PDF del informe
          </a>
        </div>
        <div style="text-align:center;font-size:12px;color:#6b7280;margin-top:4px">
          Si el botón no funciona, copia y pega este enlace en tu navegador:<br>
          <span style="word-break:break-all">{pdf_url}</span>
        </div>
        """

        # === PLANTILLA RICA (incluye CTA al PDF y fuente) ===
        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Resumen Semanal ECDC - {week_label}</title>
<style>
* {{
  margin:0; padding:0; box-sizing:border-box;
  font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
}}
body {{ background:#f5f7fa; color:#333; line-height:1.6; padding:20px; max-width:1200px; margin:0 auto; }}
.header {{
  text-align:center; padding:20px;
  background:linear-gradient(135deg,#2b6ca3 0%,#1a4e7a 100%); color:#fff;
  border-radius:10px; margin-bottom:25px; box-shadow:0 4px 12px rgba(0,0,0,.1);
}}
.header h1 {{ font-size:2.2rem; margin-bottom:10px; }}
.header .subtitle {{ font-size:1.2rem; margin-bottom:15px; opacity:.9; }}
.header .week {{
  background:rgba(255,255,255,.2); display:inline-block; padding:8px 16px;
  border-radius:30px; font-weight:600;
}}
.container {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
@media (max-width:900px) {{ .container {{ grid-template-columns:1fr; }} }}
.card {{ background:#fff; border-radius:10px; padding:20px; box-shadow:0 4px 8px rgba(0,0,0,.05); }}
.card h2 {{ color:#2b6ca3; border-bottom:2px solid #eaeaea; padding-bottom:10px; margin-bottom:15px; font-size:1.4rem; }}
.spain-card {{ border-left:5px solid #c60b1e; background:#fff9f9; }}
.spain-card h2 {{ color:#c60b1e; display:flex; align-items:center; }}
.spain-card h2:before {{ content:"🇪🇸"; margin-right:10px; }}
.stat-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:15px; margin:15px 0; }}
.stat-box {{ background:#f8f9fa; padding:15px; border-radius:8px; text-align:center; border:1px solid #eaeaea; }}
.stat-box .number {{ font-size:1.8rem; font-weight:bold; color:#2b6ca3; margin-bottom:5px; }}
.stat-box .label {{ font-size:.9rem; color:#666; }}
.spain-stat .number {{ color:#c60b1e; }}
.key-points {{ background:#e8f4ff; padding:15px; border-radius:8px; margin:15px 0; }}
.key-points h3 {{ margin-bottom:10px; color:#2b6ca3; }}
.key-points ul {{ padding-left:20px; }}
.key-points li {{ margin-bottom:8px; }}
.risk-tag {{ display:inline-block; padding:5px 12px; border-radius:20px; font-size:.85rem; font-weight:600; margin-top:10px; }}
.risk-low {{ background:#d4edda; color:#155724; }}
.risk-moderate {{ background:#fff3cd; color:#856404; }}
.risk-high {{ background:#f8d7da; color:#721c24; }}
.full-width {{ grid-column:1 / -1; }}
.footer {{ text-align:center; margin-top:30px; padding-top:20px; border-top:1px solid #eaeaea; color:#666; font-size:.9rem; }}
.topic-list {{ list-style-type:none; }}
.topic-list li {{ padding:8px 0; border-bottom:1px solid #f0f0f0; }}
.topic-list li:last-child {{ border-bottom:none; }}
</style>
</head>
<body>
  <div class="header">
    <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
    <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
    <div class="week">{week_label}</div>
  </div>

  {cta}

  <div class="container">
    <div class="card full-width">
      <h2>Resumen Ejecutivo</h2>
      <p>La actividad de virus respiratorios en la UE/EEA se mantiene en niveles bajos o basales tras el verano, con incrementos graduales de SARS-CoV-2 pero con hospitalizaciones y muertes por debajo del mismo período de 2024. Se reportan nuevos casos humanos de gripe aviar A(H9N2) en China y un brote de Ébola en la República Democrática del Congo. Continúa la vigilancia estacional de enfermedades transmitidas por vectores (WNV, dengue, chikungunya, CCHF).</p>
    </div>

    <div class="card spain-card full-width">
      <h2>Datos Destacados para España</h2>
      <div class="stat-grid">
        <div class="stat-box spain-stat"><div class="number">5</div><div class="label">Casos de Virus del Nilo Occidental</div></div>
        <div class="stat-box spain-stat"><div class="number">3</div><div class="label">Casos de Fiebre Hemorrágica de Crimea-Congo</div></div>
        <div class="stat-box spain-stat"><div class="number">1</div><div class="label">Brote aviar de WNV (Almería)</div></div>
        <div class="stat-box spain-stat"><div class="number">0</div><div class="label">Nuevos casos de CCHF esta semana</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Virus Respiratorios en la UE/EEA</h2>
      <div class="key-points"><h3>Puntos Clave:</h3>
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
      <div class="key-points"><h3>Datos Globales:</h3>
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
      <div class="key-points"><h3>Datos Europeos:</h3>
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
        <div class="stat-box"><div class="number">21</div><div class="label">Dengue (Francia)</div></div>
        <div class="stat-box"><div class="number">4</div><div class="label">Dengue (Italia)</div></div>
        <div class="stat-box"><div class="number">383</div><div class="label">Chikungunya (Francia)</div></div>
        <div class="stat-box"><div class="number">167</div><div class="label">Chikungunya (Italia)</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Ébola - República Democrática del Congo</h2>
      <div class="key-points"><h3>Datos del Brote:</h3>
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
      <div class="key-points"><h3>Datos de la UE/EEA (Julio 2025):</h3>
        <ul>
          <li><strong>188 casos</strong> reportados en julio</li>
          <li><strong>13 países</strong> reportaron casos</li>
          <li><strong>8</strong> muertes en los últimos 12 meses</li>
          <li><strong>83.3%</strong> de casos no vacunados</li>
        </ul>
      </div>
      <p><strong>Tendencia:</strong> Disminución general de casos.</p>
    </div>

    <div class="card full-width">
      <h2>Temas Adicionales del Informe</h2>
      <ul class="topic-list">
        <li><strong>Malaria - Grecia:</strong> 2 casos con probable transmisión local y 1 caso con origen indeterminado</li>
        <li><strong>Vigilancia estacional:</strong> Seguimiento de dengue, chikungunya y CCHF</li>
        <li><strong>Monitorización activa:</strong> Polio, rabia, fiebre de Lassa y variantes de SARS-CoV-2</li>
      </ul>
    </div>
  </div>

  <div class="footer">
    <p>Resumen generado el: {gen_date_es}</p>
    <p>Fuente: <a href="{article_url}" target="_blank" style="color:#0b5cab">Página del informe (ECDC)</a></p>
  </div>
</body>
</html>
"""

    # ------------------ Envío email (solo multipart/alternative, SIN adjuntos) ------------------

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
        rich_html_body = self.build_rich_html_body(week_label, gen_date_es, pdf_url, article_url)

        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"
        plain = ""  # parte texto mínima

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto: %s | HTML length=%d", subject, len(rich_html_body))
            logging.info("PDF URL: %s | Article URL: %s", pdf_url, article_url)
            return

        try:
            self.send_email(subject=subject, plain_text=plain, html_body=rich_html_body)
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    cfg = Config()
    WeeklyReportAgent(cfg).run()


