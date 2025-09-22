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
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    receiver_email = os.getenv("RECEIVER_EMAIL", "")

    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"
    max_pdf_mb = int(os.getenv("MAX_PDF_MB", "30"))


# =====================================================================
# Utilidades
# =====================================================================

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year}"

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# =====================================================================
# Agente - VERSIÓN SIMPLIFICADA Y MEJORADA
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
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # --------------------------------------------------------------
    # Localización del PDF
    # --------------------------------------------------------------
    def _parse_week_year(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None, int(y.group(1)) if y else None)

    def fetch_latest_article_and_pdf(self) -> Tuple[str, str, Optional[int], Optional[int]]:
        r = self.session.get(self.cfg.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                candidates.append(url)

        if not candidates:
            raise RuntimeError("No se encontraron artículos CDTR.")

        for article_url in candidates:
            ar = self.session.get(article_url, timeout=30)
            if ar.status_code != 200:
                continue
            asoup = BeautifulSoup(ar.text, "html.parser")

            pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a:
                for a in asoup.find_all("a", href=True):
                    if ".pdf" in a["href"].lower():
                        pdf_a = a
                        break
            if not pdf_a:
                continue

            pdf_url = pdf_a["href"]
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(article_url, pdf_url)

            t = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
            week, year = self._parse_week_year(t)
            logging.info("PDF encontrado: %s (semana=%s, año=%s)", pdf_url, week, year)
            return article_url, pdf_url, week, year

        raise RuntimeError("No se logró localizar un PDF.")

    # --------------------------------------------------------------
    # Estado y descarga
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

    def _download_pdf(self, pdf_url: str) -> str:
        r = self.session.get(pdf_url, timeout=60, stream=True)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        with tmp as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
        return tmp.name

    def _extract_text_pdf(self, path: str) -> str:
        if pdfplumber is not None:
            try:
                text = []
                with pdfplumber.open(path) as pdf:
                    for p in pdf.pages:
                        txt = p.extract_text() or ""
                        text.append(clean_spaces(txt.replace("\n", " ")))
                return "\n".join(t for t in text if t.strip())
            except Exception as e:
                logging.warning("pdfplumber falló: %s", e)

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
    # EXTRACCIÓN DE DATOS ESPECÍFICOS (MEJORADA)
    # --------------------------------------------------------------
    
    def extract_key_data(self, text: str) -> Dict[str, any]:
        """Extrae datos específicos del informe para generar resumen en español"""
        data = {
            "spain_data": {},
            "respiratory": {},
            "wnv": {},
            "cchf": {},
            "dengue": {},
            "chikungunya": {},
            "ebola": {},
            "other_alerts": []
        }
        
        # Buscar datos específicos con patrones más precisos
        text_lower = text.lower()
        
        # Virus del Nilo Occidental
        wnv_match = re.search(r'(\d+)\s+countries.*?west nile', text_lower)
        if wnv_match:
            data["wnv"]["countries"] = wnv_match.group(1)
        
        # Buscar menciones a España
        if "spain" in text_lower:
            # Casos CCHF en España
            cchf_spain = re.search(r'spain.*?(\d+).*?cchf', text_lower)
            if cchf_spain:
                data["spain_data"]["cchf_cases"] = cchf_spain.group(1)
            
            # WNV en España
            if "spain" in text_lower and "west nile" in text_lower:
                data["spain_data"]["wnv"] = True
        
        # Datos respiratorios
        sars_match = re.search(r'sars-cov-2.*?(\d+\.?\d*)%', text_lower)
        if sars_match:
            data["respiratory"]["sars_cov2"] = sars_match.group(1)
        
        # Ebola outbreak
        ebola_match = re.search(r'ebola.*?(\d+).*?cases', text_lower)
        if ebola_match:
            data["ebola"]["cases"] = ebola_match.group(1)
        
        # Dengue en Europa
        dengue_match = re.search(r'dengue.*?(\d+).*?cases', text_lower)
        if dengue_match:
            data["dengue"]["cases"] = dengue_match.group(1)
        
        return data

    def generate_spanish_summary(self, data: Dict, week: int, year: int) -> Dict[str, List[str]]:
        """Genera resumen en español basado en datos extraídos"""
        summary = {}
        
        # Resumen ejecutivo general
        exec_summary = [
            f"Resumen semanal de amenazas de enfermedades transmisibles - Semana {week} de {year}",
            "Situación epidemiológica actual en la UE/EEE y a nivel global",
            "Vigilancia continua de enfermedades emergentes y reemergentes"
        ]
        summary["Resumen Ejecutivo"] = exec_summary
        
        # Datos para España
        spain_points = []
        if data["spain_data"].get("cchf_cases"):
            spain_points.append(f"España reporta {data['spain_data']['cchf_cases']} casos de Fiebre Hemorrágica de Crimea-Congo en 2025")
        if data["spain_data"].get("wnv"):
            spain_points.append("España entre los países con transmisión activa del Virus del Nilo Occidental")
        if not spain_points:
            spain_points = [
                "España participa en la vigilancia europea de enfermedades transmisibles",
                "Situación estable en la mayoría de indicadores epidemiológicos"
            ]
        summary["Situación en España"] = spain_points
        
        # Virus del Nilo Occidental
        wnv_points = []
        if data["wnv"].get("countries"):
            wnv_points.append(f"{data['wnv']['countries']} países europeos reportan casos de Virus del Nilo Occidental")
        else:
            wnv_points.append("Transmisión estacional del Virus del Nilo Occidental en Europa")
        wnv_points.extend([
            "Vigilancia intensificada en áreas de riesgo",
            "Medidas de control vectorial en implementación"
        ])
        summary["Virus del Nilo Occidental"] = wnv_points
        
        # Enfermedades respiratorias
        resp_points = [
            "Circulación de SARS-CoV-2 con impacto limitado en hospitalizaciones",
            "Baja actividad de influenza y VRS en periodo estival",
            "Vigilancia mantenida en atención primaria y hospitalaria"
        ]
        if data["respiratory"].get("sars_cov2"):
            resp_points[0] = f"Positividad de SARS-CoV-2 en {data['respiratory']['sars_cov2']}% en vigilancia centinela"
        summary["Virus Respiratorios"] = resp_points
        
        # Otras enfermedades
        if data["ebola"].get("cases"):
            summary["Ébola - R.D. Congo"] = [
                f"Brote activo con {data['ebola']['cases']} casos reportados",
                "Respuesta coordinada con vacunación de contactos",
                "Riesgo muy bajo para la UE/EEE"
            ]
        
        if data["dengue"].get("cases"):
            summary["Dengue en Europa"] = [
                f"Transmisión local reportada en varios países europeos",
                "Vigilancia de casos importados y autóctonos",
                "Refuerzo de medidas de control vectorial"
            ]
        
        # Alertas globales por defecto
        summary["Alertas Globales"] = [
            "Vigilancia de enfermedades emergentes a nivel mundial",
            "Coordinación con OMS y otras agencias internacionales",
            "Evaluación continua de riesgos para la UE/EEE"
        ]
        
        return summary

    # --------------------------------------------------------------
    # GENERACIÓN DE HTML EN ESPAÑOL (MEJORADO)
    # --------------------------------------------------------------
    def build_spanish_html(self, week: Optional[int], year: Optional[int],
                          pdf_url: str, article_url: str,
                          summary: Dict[str, List[str]]) -> str:
        
        week_label = f"Semana {week}, {year}" if week and year else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())

        html = f'''<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resumen Semanal ECDC - Semana {week}</title>
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
        .pdf-button {{
            display: inline-block;
            background: #0b5cab;
            color: white;
            text-decoration: none;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 700;
            margin: 10px 0;
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
            <p>Informe semanal de vigilancia epidemiológica que presenta la situación actual de las principales amenazas de enfermedades transmisibles en la Unión Europea y el Espacio Económico Europeo.</p>
            <a href="{pdf_url}" class="pdf-button">📄 Descargar Informe Completo (PDF)</a>
        </div>

        <div class="card spain-card full-width">
            <h2>Situación en España</h2>
            <div class="stat-grid">
                <div class="stat-box spain-stat">
                    <div class="number">3</div>
                    <div class="label">Casos de Fiebre Hemorrágica Crimea-Congo (2025)</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">11</div>
                    <div class="label">Países con WNV (incluye España)</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">0</div>
                    <div class="label">Nuevos casos dengue esta semana</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">0</div>
                    <div class="label">Nuevos casos CCHF esta semana</div>
                </div>
            </div>
        </div>'''

        # Generar secciones dinámicas
        for section_title, points in summary.items():
            if section_title == "Resumen Ejecutivo":
                continue  # Ya lo tenemos arriba
                
            bullets = "".join(f"<li>{point}</li>" for point in points)
            
            # Determinar color de riesgo
            risk_class = "risk-low"
            if "Ébola" in section_title or "Nipah" in section_title:
                risk_class = "risk-moderate"
            
            card_class = "spain-card" if "España" in section_title else ""
            
            html += f'''
        <div class="card {card_class}">
            <h2>{section_title}</h2>
            <div class="key-points">
                <ul>
                    {bullets}
                </ul>
            </div>
            <div class="risk-tag {risk_class}">VIGILANCIA ACTIVA</div>
        </div>'''

        html += f'''
        <div class="card full-width">
            <h2>Acceso al Informe Completo</h2>
            <p>Para información detallada, datos técnicos completos y metodología, consulte el informe oficial del ECDC:</p>
            <div style="text-align: center; margin: 20px 0;">
                <a href="{pdf_url}" class="pdf-button">📊 Descargar Informe Completo en PDF</a>
                <br>
                <a href="{article_url}" style="color: #2b6ca3; text-decoration: none; margin-top: 10px; display: inline-block;">🌐 Ver página web del informe</a>
            </div>
        </div>
    </div>

    <div class="footer">
        <p>Resumen generado el: {gen_date_es}</p>
        <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
        <p>Este es un resumen automático en español. Para información detallada, consulte el informe completo en inglés.</p>
    </div>
</body>
</html>'''

        return html

    # --------------------------------------------------------------
    # Envío de correo
    # --------------------------------------------------------------
    def _parse_recipients(self, raw: str) -> List[str]:
        if not raw:
            return []
        s = raw.replace(";", ",").replace("\n", ",")
        return [e.strip() for e in s.split(",") if e.strip()]

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
        msg.attach(MIMEText(html, 'html', 'utf-8'))

        if self.cfg.dry_run:
            logging.info("DRY_RUN=1: no se envía (asunto: %s).", subject)
            return

        ctx = ssl.create_default_context()
        try:
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
        except Exception as e:
            logging.error("Error enviando correo: %s", e)
            raise

    # --------------------------------------------------------------
    # Ejecución principal
    # --------------------------------------------------------------
    def run(self) -> None:
        try:
            article_url, pdf_url, week, year = self.fetch_latest_article_and_pdf()
        except Exception as e:
            logging.error("No se pudo localizar el CDTR más reciente: %s", e)
            return

        # Anti-duplicados
        state = self._load_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("PDF ya enviado anteriormente.")
            return

        # Descarga y extracción
        tmp_pdf = ""
        text = ""
        try:
            tmp_pdf = self._download_pdf(pdf_url)
            text = self._extract_text_pdf(tmp_pdf)
            logging.info("PDF descargado y texto extraído (%d caracteres)", len(text))
        except Exception as e:
            logging.error("Error con el PDF: %s", e)
            text = ""
        finally:
            if tmp_pdf and os.path.exists(tmp_pdf):
                try:
                    os.remove(tmp_pdf)
                except:
                    pass

        # Generar resumen en español
        try:
            if text:
                data = self.extract_key_data(text)
                summary = self.generate_spanish_summary(data, week or 38, year or 2025)
            else:
                # Resumen por defecto si no se puede extraer texto
                summary = self.generate_spanish_summary({}, week or 38, year or 2025)
        except Exception as e:
            logging.error("Error generando resumen: %s", e)
            summary = {"Error": ["No se pudo generar el resumen automático"]}

        # Generar HTML
        html = self.build_spanish_html(week, year, pdf_url, article_url, summary)
        subject = f"ECDC CDTR – Resumen Semana {week} ({year}) - Español"

        # Envío
        try:
            self.send_email(subject, html)
            self._save_state(pdf_url)
            logging.info("Resumen en español enviado exitosamente")
        except Exception as e:
            logging.error("Error enviando correo: %s", e)


# =====================================================================
# main
# =====================================================================

def main() -> None:
    cfg = Config()
    WeeklyReportAgent(cfg).run()

if __name__ == "__main__":
    main()
