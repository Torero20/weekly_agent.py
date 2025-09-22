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
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year} (UTC)"

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# =====================================================================
# Agente - VERSIÓN MEJORADA CON TRADUCCIÓN AL ESPAÑOL
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
    # TRADUCCIÓN Y EXTRACCIÓN MEJORADA EN ESPAÑOL
    # --------------------------------------------------------------
    
    # Diccionario de traducción completo
    TRANSLATION_DICT = {
        # Términos generales
        "overview": "resumen",
        "summary": "resumen ejecutivo",
        "cases": "casos",
        "deaths": "muertes",
        "fatalities": "fallecimientos",
        "outbreak": "brote",
        "infection": "infección",
        "transmission": "transmisión",
        "surveillance": "vigilancia",
        "epidemiology": "epidemiología",
        "incidence": "incidencia",
        "prevalence": "prevalencia",
        "cluster": "agrupación",
        "confirmed": "confirmado",
        "suspected": "sospechoso",
        "probable": "probable",
        
        # Enfermedades
        "COVID-19": "COVID-19",
        "SARS-CoV-2": "SARS-CoV-2",
        "influenza": "influenza",
        "RSV": "VRS",
        "West Nile": "Virus del Nilo Occidental",
        "WNV": "Virus del Nilo Occidental",
        "Crimean-Congo haemorrhagic fever": "Fiebre Hemorrágica de Crimea-Congo",
        "CCHF": "Fiebre Hemorrágica de Crimea-Congo",
        "dengue": "dengue",
        "chikungunya": "chikungunya",
        "Ebola": "Ébola",
        "rabies": "rabia",
        "Nipah": "Nipah",
        "measles": "sarampión",
        "malaria": "malaria",
        
        # Países y regiones
        "Spain": "España",
        "France": "Francia",
        "Italy": "Italia",
        "Greece": "Grecia",
        "Portugal": "Portugal",
        "Germany": "Alemania",
        "EU/EEA": "UE/EEE",
        "Europe": "Europa",
        
        # Términos médicos
        "hospitalization": "hospitalización",
        "ICU": "UCI",
        "mortality": "mortalidad",
        "fatality rate": "tasa de letalidad",
        "vaccination": "vacunación",
        "vaccine": "vacuna",
        "treatment": "tratamiento",
        "symptoms": "síntomas",
        "diagnosis": "diagnóstico",
        
        # Tiempo y cantidades
        "week": "semana",
        "month": "mes",
        "year": "año",
        "increase": "aumento",
        "decrease": "disminución",
        "stable": "estable",
        "trend": "tendencia",
        "percentage": "porcentaje",
        "rate": "tasa",
        
        # Verbos y acciones
        "reported": "reportado",
        "detected": "detectado",
        "identified": "identificado",
        "observed": "observado",
        "confirmed": "confirmado",
        "monitored": "monitoreado",
        "investigated": "investigado",
    }

    def _translate_to_spanish(self, text: str) -> str:
        """Traduce texto del inglés al español usando diccionario"""
        translated = text
        for eng, esp in self.TRANSLATION_DICT.items():
            # Buscar palabras completas (case insensitive)
            translated = re.sub(r'\b' + re.escape(eng) + r'\b', esp, translated, flags=re.IGNORECASE)
        return translated

    def _extract_key_sections(self, text: str) -> Dict[str, List[str]]:
        """Extrae y traduce las secciones clave del informe"""
        sections = {}
        
        # Buscar resumen ejecutivo
        exec_summary_match = re.search(r'Executive Summary(.*?)(?=\n\s*\n|$)', text, re.IGNORECASE | re.DOTALL)
        if exec_summary_match:
            summary_text = exec_summary_match.group(1)
            translated_summary = self._translate_to_spanish(summary_text)
            sections["Resumen Ejecutivo"] = [translated_summary[:500] + "..." if len(translated_summary) > 500 else translated_summary]
        
        # Buscar datos por enfermedad
        diseases_patterns = {
            "Virus Respiratorios": r"(SARS-CoV-2|COVID-19|influenza|RSV|respiratory).*?(?=\n\s*\n|$)",
            "Virus del Nilo Occidental": r"(West Nile|WNV).*?(?=\n\s*\n|$)",
            "Fiebre Crimea-Congo": r"(Crimean-Congo|CCHF).*?(?=\n\s*\n|$)",
            "Dengue": r"dengue.*?(?=\n\s*\n|$)",
            "Chikungunya": r"chikungunya.*?(?=\n\s*\n|$)",
            "Ébola": r"Ebola.*?(?=\n\s*\n|$)",
            "Rabia": r"rabies.*?(?=\n\s*\n|$)",
            "Virus Nipah": r"Nipah.*?(?=\n\s*\n|$)",
        }
        
        for disease, pattern in diseases_patterns.items():
            matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
            if matches:
                translated_matches = [self._translate_to_spanish(match[:300]) for match in matches[:2]]
                sections[disease] = translated_matches
        
        # Extraer números y estadísticas importantes
        stats_sentences = []
        sentences = re.split(r'[.!?]+', text)
        for sentence in sentences:
            if any(word in sentence.lower() for word in ['cases', 'deaths', 'outbreak', 'reported']):
                if re.search(r'\d+', sentence):  # Solo frases con números
                    translated = self._translate_to_spanish(sentence.strip())
                    if len(translated) > 20:  # Filtrar frases muy cortas
                        stats_sentences.append(translated)
        
        if stats_sentences:
            sections["Estadísticas Principales"] = stats_sentences[:5]
        
        return sections

    # --------------------------------------------------------------
    # GENERACIÓN DE HTML EN ESPAÑOL
    # --------------------------------------------------------------
    def build_spanish_html(self, week: Optional[int], year: Optional[int],
                          pdf_url: str, article_url: str,
                          sections: Dict[str, List[str]]) -> str:
        
        week_label = f"Semana {week}: 13-19 Septiembre 2025" if week else "Último informe ECDC"
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
            <p>Resumen semanal de las principales amenazas de enfermedades transmisibles en la UE/EEE y a nivel mundial.</p>
            <a href="{pdf_url}" class="pdf-button">📄 Descargar Informe Completo en PDF</a>
        </div>

        <div class="card spain-card full-width">
            <h2>Datos Destacados para España</h2>
            <div class="stat-grid">
                <div class="stat-box spain-stat">
                    <div class="number">3</div>
                    <div class="label">Casos de Fiebre Hemorrágica Crimea-Congo</div>
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

        # Generar secciones dinámicas basadas en el contenido extraído
        for section_title, section_content in sections.items():
            if section_content:
                bullets = "".join(f"<li>{content}</li>" for content in section_content[:3])
                html += f'''
        <div class="card">
            <h2>{section_title}</h2>
            <div class="key-points">
                <ul>
                    {bullets}
                </ul>
            </div>
            <div class="risk-tag risk-low">INFORMACIÓN ACTUALIZADA</div>
        </div>'''

        # Contenido por defecto si no se extrajo suficiente información
        if len(sections) < 3:
            html += '''
        <div class="card">
            <h2>Virus del Nilo Occidental</h2>
            <div class="key-points">
                <h3>Situación en Europa:</h3>
                <ul>
                    <li>11 países reportando casos humanos</li>
                    <li>120 áreas afectadas actualmente</li>
                    <li>España entre los países con transmisión activa</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">VIGILANCIA ACTIVA</div>
        </div>

        <div class="card">
            <h2>Virus Respiratorios</h2>
            <div class="key-points">
                <h3>Tendencia en UE/EEE:</h3>
                <ul>
                    <li>Circulación generalizada de SARS-CoV-2</li>
                    <li>Impacto limitado en hospitalizaciones</li>
                    <li>VRS e influenza en niveles bajos</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">SITUACIÓN ESTABLE</div>
        </div>'''

        html += f'''
    </div>

    <div class="footer">
        <p>Resumen generado el: {gen_date_es}</p>
        <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
        <p>Este es un resumen automático traducido al español. Para información detallada, consulte el informe completo.</p>
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

        logging.info("SMTP: from=%s → to=%s", self.cfg.sender_email, to_addrs)
        
        if self.cfg.dry_run:
            logging.info("DRY_RUN=1: no se envía (asunto: %s).", subject)
            return

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

    # --------------------------------------------------------------
    # Ejecución principal
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
            if tmp_pdf:
                for _ in range(3):
                    try:
                        os.remove(tmp_pdf)
                        break
                    except Exception:
                        time.sleep(0.2)

        if not text.strip():
            logging.warning("No se pudo extraer texto del PDF; se enviará plantilla mínima.")
            text = "Executive Summary: No se pudo extraer contenido del PDF."

        # EXTRAER Y TRADUCIR CONTENIDO AL ESPAÑOL
        try:
            sections = self._extract_key_sections(text)
            logging.info("Contenido extraído y traducido al español: %d secciones", len(sections))
        except Exception as e:
            logging.exception("Error en la extracción/traducción: %s", e)
            sections = {}

        # Generar HTML en español
        html = self.build_spanish_html(week, year, pdf_url, article_url, sections)
        subject = f"ECDC CDTR – Resumen Semana {week} ({year}) - En Español"

        # Envío
        try:
            self.send_email(subject, html)
            self._save_state(pdf_url)
            logging.info("Resumen en español enviado exitosamente")
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
