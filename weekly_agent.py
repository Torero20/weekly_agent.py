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

# --------------------------- Config ---------------------------------

class Config:
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")

    # Varias direcciones separadas por coma, punto y coma o saltos de línea
    receiver_email = os.getenv(
        "RECEIVER_EMAIL",
        "miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es"
    )

    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"

# --------------------------- Util -----------------------------------

MESES_ES = {
    1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
    7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"
}
def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month,'mes')} de {dt_utc.year} (UTC)"

# --------------------------- Agent ----------------------------------

class WeeklyReportAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # ---- localizar último artículo CDTR y su PDF ----
    def _parse_week_year(self, text: str):
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None, int(y.group(1)) if y else None)

    def fetch_latest_pdf(self):
        r = self.session.get(self.cfg.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                if url not in candidates:
                    candidates.append(url)

        if not candidates:
            raise RuntimeError("No se encontraron artículos CDTR en el listado.")

        for article_url in candidates:
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
            title_text = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
            week, year = self._parse_week_year(title_text)
            logging.info("Artículo CDTR: %s", article_url)
            logging.info("PDF CDTR: %s (semana=%s, año=%s)", pdf_url, week, year)
            return pdf_url, article_url, week, year

        raise RuntimeError("No se encontró PDF en los artículos candidatos.")

    # ---- estado anti-duplicados ----
    def _load_last_state(self):
        if not os.path.exists(self.cfg.state_file):
            return {}
        try:
            with open(self.cfg.state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_last_state(self, pdf_url):
        state = {"last_pdf_url": pdf_url, "timestamp": dt.datetime.utcnow().isoformat()}
        with open(self.cfg.state_file, "w") as f:
            json.dump(state, f)

    # ---- HTML extendido (cuerpo del correo) ----
    def build_full_html(self, week_label: str, pdf_url: str, gen_date_es: str) -> str:
        # Nota: contenido de ejemplo con las cifras clave que venimos utilizando.
        # Si más adelante parseamos el PDF, se rellenan dinámicamente.
        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Resumen Semanal ECDC - {week_label}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Tahoma,Verdana,sans-serif; }}
body {{ background:#f5f7fa; color:#333; line-height:1.6; padding:20px; max-width:1200px; margin:0 auto; }}
.header {{ text-align:center; padding:22px; background:linear-gradient(135deg,#2b6ca3 0%,#1a4e7a 100%); color:#fff;
          border-radius:10px; margin-bottom:22px; box-shadow:0 4px 12px rgba(0,0,0,.1); }}
.header h1 {{ font-size:2.0rem; margin-bottom:8px; }}
.header .subtitle {{ font-size:1.05rem; opacity:.9; }}
.header .week {{ margin-top:10px; display:inline-block; background:rgba(255,255,255,.2); padding:6px 14px; border-radius:22px; font-weight:600; }}

.section {{ background:#fff; border-radius:10px; padding:16px 18px; box-shadow:0 2px 8px rgba(0,0,0,.06); margin-bottom:16px; }}
.section h2 {{ color:#2b6ca3; border-bottom:2px solid #eaeaea; padding-bottom:8px; margin-bottom:12px; font-size:1.2rem; }}

.spain {{ border-left:5px solid #c60b1e; background:#fff9f9; }}
.statgrid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }}
.stat {{ background:#f8f9fa; padding:14px; border-radius:8px; text-align:center; border:1px solid #eaeaea; }}
.stat .n {{ font-size:1.7rem; font-weight:800; color:#2b6ca3; }}
.stat .l {{ font-size:.9rem; color:#666; }}
.spain .n {{ color:#c60b1e; }}

.keybox {{ background:#e8f4ff; border-radius:8px; padding:12px; margin:10px 0; }}
.keybox h3 {{ color:#2b6ca3; margin-bottom:6px; }}
.keybox ul {{ margin-left:20px; }}
.rtag {{ display:inline-block; padding:5px 10px; border-radius:20px; font-size:.85rem; font-weight:700; }}
.low {{ background:#d4edda; color:#155724; }} .mid {{ background:#fff3cd; color:#856404; }} .hi {{ background:#f8d7da; color:#721c24; }}

.cta {{ text-align:center; margin:14px 0 4px; }}
.btn {{ display:inline-block; background:#0b5cab; color:#fff !important; text-decoration:none; padding:10px 16px; border-radius:8px; font-weight:700; }}

.footer {{ text-align:center; margin-top:10px; color:#667; font-size:.85rem; }}
@media (max-width:900px) {{ .statgrid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
  <div class="header">
    <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
    <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
    <div class="week">{week_label}</div>
    <div class="cta" style="margin-top:12px">
      <a class="btn" href="{pdf_url}">Abrir / Descargar PDF del informe</a>
    </div>
  </div>

  <div class="section">
    <h2>Resumen Ejecutivo</h2>
    <p>La actividad de virus respiratorios en la UE/EEE se mantiene en niveles bajos o basales tras el verano,
       con incrementos graduales de SARS-CoV-2 pero con hospitalizaciones y muertes por debajo del mismo período de 2024.
       Se reportan casos humanos esporádicos de gripe aviar A(H9N2) en China y un brote de Ébola en la República Democrática del Congo.
       Continúa la vigilancia estacional de enfermedades transmitidas por vectores (WNV, dengue, chikungunya, CCHF).</p>
  </div>

  <div class="section spain">
    <h2>🇪🇸 Datos Destacados para España</h2>
    <div class="statgrid">
      <div class="stat"><div class="n">5</div><div class="l">Casos de Virus del Nilo Occidental</div></div>
      <div class="stat"><div class="n">3</div><div class="l">Casos de Fiebre Crimea-Congo (2025)</div></div>
      <div class="stat"><div class="n">1</div><div class="l">Brote aviar de WNV (Almería)</div></div>
      <div class="stat"><div class="n">0</div><div class="l">Nuevos casos de CCHF esta semana</div></div>
    </div>
  </div>

  <div class="section">
    <h2>Virus Respiratorios en la UE/EEA</h2>
    <div class="keybox">
      <h3>Puntos clave</h3>
      <ul>
        <li>Positividad SARS-CoV-2 en AP: <b>22.3%</b>.</li>
        <li>Positividad SARS-CoV-2 en hospital: <b>10%</b>.</li>
        <li>Influenza y VRS: <b>niveles bajos</b>.</li>
      </ul>
    </div>
    <p><b>Evaluación de riesgo:</b> aumento gradual de SARS-CoV-2 pero con impacto sanitario limitado. <span class="rtag low">RIESGO BAJO</span></p>
  </div>

  <div class="section">
    <h2>Gripe Aviar A(H9N2) – China</h2>
    <p>Se notifican 4 nuevos casos en niños (9-sep). Total 2025: <b>26</b> (todos en China); letalidad global ~<b>1.1%</b>.</p>
    <p><b>Riesgo UE/EEE:</b> <span class="rtag low">MUY BAJO</span></p>
  </div>

  <div class="section">
    <h2>Virus del Nilo Occidental (WNV) – Europa 2025</h2>
    <div class="keybox">
      <ul>
        <li><b>652</b> casos humanos autóctonos; <b>38</b> muertes (hasta 3-sep).</li>
        <li>Países más afectados: Italia (500), Grecia (69), Serbia (33), Francia (20).</li>
      </ul>
    </div>
  </div>

  <div class="section">
    <h2>Otras enfermedades transmitidas por vectores</h2>
    <ul>
      <li>Dengue autóctono: Francia <b>21</b>, Italia <b>4</b>, Portugal <b>2</b>.</li>
      <li>Chikungunya importado: Francia <b>383</b>, Italia <b>167</b>.</li>
    </ul>
  </div>

  <div class="section">
    <h2>Ébola – R. D. del Congo</h2>
    <p><b>68</b> casos sospechosos y <b>16</b> muertes (letalidad 23.5%). Cepa Zaire confirmada. <b>Riesgo UE/EEE:</b> <span class="rtag low">MUY BAJO</span></p>
  </div>

  <div class="section">
    <h2>Sarampión – vigilancia mensual (UE/EEE)</h2>
    <ul>
      <li>Julio 2025: <b>188</b> casos en <b>13</b> países; <b>83.3%</b> no vacunados; <b>8</b> muertes en 12 meses.</li>
    </ul>
  </div>

  <div class="cta">
    <a class="btn" href="{pdf_url}">Abrir / Descargar PDF del informe</a>
  </div>

  <div class="footer">
    Generado automáticamente · Fuente: ECDC (CDTR) · Fecha: {gen_date_es}
  </div>
</body>
</html>
"""

    # ---- envío: SOLO HTML como cuerpo (más un fallback de texto mínimo) ----
    def send_email(self, subject: str, html_body: str):
        if not self.cfg.sender_email or not self.cfg.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.cfg.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        # parse destinatarios
        raw = self.cfg.receiver_email
        for sep in [";", "\n"]:
            raw = raw.replace(sep, ",")
        to_addrs = [x.strip() for x in raw.split(",") if x.strip()]
        if not to_addrs:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.cfg.sender_email
        msg['To'] = ", ".join(to_addrs)

        # Fallback mínimo (no “texto crudo” del resumen)
        msg.attach(MIMEText("Este mensaje requiere un cliente con soporte HTML.", 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        logging.info("SMTP: from=%s → to=%s", self.cfg.sender_email, to_addrs)
        ctx = ssl.create_default_context()
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

    # ---- run ----
    def run(self):
        try:
            pdf_url, article_url, week, year = self.fetch_latest_pdf()
        except Exception as e:
            logging.exception("No se pudo localizar el PDF más reciente: %s", e)
            return

        # anti-duplicados
        state = self._load_last_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado previamente; no se reenvía.")
            return

        week_label = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        gen_date_es = fecha_es(dt.datetime.utcnow())
        html = self.build_full_html(week_label, pdf_url, gen_date_es)

        subject = f"ECDC CDTR – {week_label}"

        if self.cfg.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto: %s | HTML len: %d", subject, len(html))
            return

        try:
            self.send_email(subject, html)
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)

# --------------------------- main -----------------------------------

if __name__ == "__main__":
    WeeklyReportAgent(Config()).run()

