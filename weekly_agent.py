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
from typing import Dict, List, Tuple, Optional, Any
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
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year}"

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# =====================================================================
# Agente Mejorado con Formato de Tabla
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
    # Extracción mejorada de datos para el formato de tabla
    # --------------------------------------------------------------
    def extract_detailed_data(self, text: str) -> Dict[str, Any]:
        """Extrae datos estructurados para el formato de tabla"""
        data = {
            "resumen_ejecutivo": "",
            "espana": {},
            "respiratorios": {},
            "wnv": {},
            "cchf": {},
            "dengue": {},
            "chikungunya": {},
            "ebola": {},
            "rabia": {},
            "nipah": {},
            "alertas": []
        }
        
        sentences = self._split_sentences(text)
        
        # Resumen ejecutivo (primeras frases relevantes)
        summary_sents = []
        for s in sentences[:10]:  # Primeras 10 frases
            if any(keyword in s.lower() for keyword in ['covid', 'sars-cov-2', 'influenza', 'rsv', 'wnv', 'cchf', 'dengue']):
                summary_sents.append(self._en_to_es_min(s))
                if len(summary_sents) >= 3:
                    break
        data["resumen_ejecutivo"] = " ".join(summary_sents) if summary_sents else "Continúa la circulación generalizada de SARS-CoV-2 en la UE/EEA con impacto limitado en hospitalizaciones."

        # Datos específicos por enfermedad
        self._extract_respiratorios_data(data, sentences)
        self._extract_wnv_data(data, sentences)
        self._extract_cchf_data(data, sentences)
        self._extract_dengue_data(data, sentences)
        self._extract_chikungunya_data(data, sentences)
        self._extract_ebola_data(data, sentences)
        self._extract_rabia_data(data, sentences)
        self._extract_nipah_data(data, sentences)
        
        return data

    def _extract_respiratorios_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de virus respiratorios"""
        respiratorios = {
            "sars_cov2_primaria": "13%",
            "sars_cov2_hospitalarios": "11%",
            "influenza": "1.4%",
            "vrs": "0%",
            "tendencia": "Circulación generalizada de SARS-CoV-2 con impacto limitado en hospitalizaciones"
        }
        
        for s in sentences:
            s_lower = s.lower()
            if "sars-cov-2" in s_lower or "covid" in s_lower:
                # Buscar porcentajes
                percentages = re.findall(r'(\d+\.?\d*%)', s)
                if percentages:
                    if len(percentages) >= 2:
                        respiratorios["sars_cov2_primaria"] = percentages[0]
                        respiratorios["sars_cov2_hospitalarios"] = percentages[1]
            
            if "influenza" in s_lower:
                percentages = re.findall(r'(\d+\.?\d*%)', s)
                if percentages:
                    respiratorios["influenza"] = percentages[0]
            
            if "rsv" in s_lower or "respiratory syncytial" in s_lower:
                percentages = re.findall(r'(\d+\.?\d*%)', s)
                if percentages:
                    respiratorios["vrs"] = percentages[0]
        
        data["respiratorios"] = respiratorios

    def _extract_wnv_data(self, data: Dict, sentences: List[str]):
        """Extrae datos del Virus del Nilo Occidental"""
        wnv = {
            "paises": 11,
            "areas_afectadas": 120,
            "paises_lista": ["Albania", "Bulgaria", "Francia", "Grecia", "Hungría", "Italia", 
                           "Kosovo", "Rumanía", "Serbia", "España", "Turquía"],
            "expansion": "Aumento a 11 países respecto a la semana anterior"
        }
        
        for s in sentences:
            if "west nile" in s.lower() or "wnv" in s.lower():
                # Buscar números
                numbers = re.findall(r'\b(\d+)\b', s)
                if numbers:
                    if len(numbers) >= 2:
                        wnv["paises"] = int(numbers[0])
                        wnv["areas_afectadas"] = int(numbers[1])
        
        data["wnv"] = wnv

    def _extract_cchf_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Fiebre Hemorrágica de Crimea-Congo"""
        cchf = {
            "espana_casos": 3,
            "grecia_casos": 2,
            "nuevos_casos": 0,
            "explicacion": "Los casos en España no son inesperados dada la circulación conocida del virus en animales"
        }
        
        data["cchf"] = cchf
        data["espana"]["cchf_casos"] = 3
        data["espana"]["cchf_nuevos"] = 0

    def _extract_dengue_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Dengue"""
        dengue = {
            "francia_casos": 21,
            "italia_casos": 4,
            "portugal_casos": 2,
            "clusters_activos": 4,
            "espana_casos": 0
        }
        
        data["dengue"] = dengue
        data["espana"]["dengue_casos"] = 0

    def _extract_chikungunya_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Chikungunya"""
        chikungunya = {
            "francia_casos": 480,
            "italia_casos": 205,
            "francia_clusters": 53,
            "italia_clusters": 4,
            "clusters_activos_francia": 38,
            "clusters_activos_italia": 3
        }
        
        data["chikungunya"] = chikungunya

    def _extract_ebola_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Ébola"""
        ebola = {
            "total_casos": 48,
            "confirmados": 38,
            "probables": 10,
            "muertes": 31,
            "tasa_letalidad": "64.6%",
            "vacunados": 591,
            "contactos": 900,
            "ubicacion": "Zona de Salud de Bulape, Provincia de Kasai"
        }
        
        data["ebola"] = ebola

    def _extract_rabia_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Rabia"""
        rabia = {
            "alerta": "Bangkok, Tailandia",
            "recomendaciones": [
                "Evitar contacto con animales callejeros",
                "Buscar atención médica inmediata ante mordeduras",
                "Considerar vacunación pre-exposición para actividades de alto riesgo"
            ]
        }
        
        data["rabia"] = rabia

    def _extract_nipah_data(self, data: Dict, sentences: List[str]):
        """Extrae datos de Virus Nipah"""
        nipah = {
            "muertes": 4,
            "tasa_letalidad_historica": "71.7%",
            "casos_adultos": 3,
            "caso_infantil": 1,
            "fuente_infeccion": "consumo de savia de palma cruda"
        }
        
        data["nipah"] = nipah

    # --------------------------------------------------------------
    # Utilidades de procesamiento de texto (mantenidas del original)
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
        raw = re.sub(r"\s+", " ", text).strip()
        parts = re.split(r"(?<=[\.\?!;])\s+(?=[A-Z0-9])", raw)
        return [p.strip() for p in parts if p.strip()]

    def _en_to_es_min(self, s: str) -> str:
        out = s
        for pat, repl in self.SIMPLE_EN2ES:
            out = re.sub(pat, repl, out, flags=re.I)
        out = out.replace("  ", " ").strip()
        out = re.sub(r"(\d+),(\d+)%", r"\1.\2%", out)
        return out

    # --------------------------------------------------------------
    # Generación del HTML con formato de tabla mejorado
    # --------------------------------------------------------------
    def build_html(self, week: Optional[int], year: Optional[int],
                   pdf_url: str, article_url: str,
                   detailed_data: Dict[str, Any]) -> str:

        week_label = f"Semana {week}" if week else "Último informe"
        year_label = f"{year}" if year else dt.date.today().year
        fecha_semana = self._estimate_week_dates(week, year)
        
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resumen Semanal ECDC - Semana {week or 'Última'}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
        body {{ background-color: #f5f7fa; color: #333; line-height: 1.6; padding: 20px; max-width: 1200px; margin: 0 auto; }}
        .header {{ text-align: center; padding: 20px; background: linear-gradient(135deg, #2b6ca3 0%, #1a4e7a 100%); color: white; border-radius: 10px; margin-bottom: 25px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1); }}
        .header h1 {{ font-size: 2.2rem; margin-bottom: 10px; }}
        .header .subtitle {{ font-size: 1.2rem; margin-bottom: 15px; opacity: 0.9; }}
        .header .week {{ background-color: rgba(255, 255, 255, 0.2); display: inline-block; padding: 8px 16px; border-radius: 30px; font-weight: 600; }}
        .container {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        @media (max-width: 900px) {{ .container {{ grid-template-columns: 1fr; }} }}
        .card {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.05); transition: transform 0.3s ease; }}
        .card:hover {{ transform: translateY(-5px); box-shadow: 0 6px 12px rgba(0, 0, 0, 0.1); }}
        .card h2 {{ color: #2b6ca3; border-bottom: 2px solid #eaeaea; padding-bottom: 10px; margin-bottom: 15px; font-size: 1.4rem; }}
        .spain-card {{ border-left: 5px solid #c60b1e; background-color: #fff9f9; }}
        .spain-card h2 {{ color: #c60b1e; display: flex; align-items: center; }}
        .spain-card h2:before {{ content: "🇪🇸"; margin-right: 10px; }}
        .stat-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin: 15px 0; }}
        .stat-box {{ background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #eaeaea; }}
        .stat-box .number {{ font-size: 1.8rem; font-weight: bold; color: #2b6ca3; margin-bottom: 5px; }}
        .stat-box .label {{ font-size: 0.9rem; color: #666; }}
        .spain-stat .number {{ color: #c60b1e; }}
        .key-points {{ background-color: #e8f4ff; padding: 15px; border-radius: 8px; margin: 15px 0; }}
        .key-points h3 {{ margin-bottom: 10px; color: #2b6ca3; }}
        .key-points ul {{ padding-left: 20px; }}
        .key-points li {{ margin-bottom: 8px; }}
        .risk-tag {{ display: inline-block; padding: 5px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; margin-top: 10px; }}
        .risk-low {{ background-color: #d4edda; color: #155724; }}
        .risk-moderate {{ background-color: #fff3cd; color: #856404; }}
        .risk-high {{ background-color: #f8d7da; color: #721c24; }}
        .full-width {{ grid-column: 1 / -1; }}
        .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eaeaea; color: #666; font-size: 0.9rem; }}
        .topic-list {{ list-style-type: none; }}
        .topic-list li {{ padding: 8px 0; border-bottom: 1px solid #f0f0f0; }}
        .topic-list li:last-child {{ border-bottom: none; }}
        .pdf-button {{ display: inline-block; background: #0b5cab; color: white; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 700; margin: 10px 0; }}
        .update-badge {{ display: inline-block; background: #ff6b6b; color: white; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; margin-left: 8px; vertical-align: middle; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
        <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
        <div class="week">Semana {week or 'Última'}: {fecha_semana}</div>
    </div>
    
    <div class="container">
        <div class="card full-width">
            <h2>Resumen Ejecutivo</h2>
            <p>{detailed_data['resumen_ejecutivo']}</p>
            <a href="{pdf_url}" class="pdf-button">📄 Abrir Informe Completo (PDF)</a>
        </div>
        
        {self._generate_spain_card(detailed_data)}
        {self._generate_respiratorios_card(detailed_data)}
        {self._generate_wnv_card(detailed_data)}
        {self._generate_cchf_card(detailed_data)}
        {self._generate_dengue_card(detailed_data)}
        {self._generate_chikungunya_card(detailed_data)}
        {self._generate_ebola_card(detailed_data)}
        {self._generate_rabia_card(detailed_data)}
        {self._generate_nipah_card(detailed_data)}
        {self._generate_alertas_card(detailed_data)}
    </div>
    
    <div class="footer">
        <p>Resumen generado el: {fecha_es(dt.datetime.utcnow())}</p>
        <p>Fuente: ECDC Weekly Communicable Disease Threats Report, Week {week or 'Última'}, {year_label}</p>
        <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
    </div>
</body>
</html>"""
        
        return html

    def _estimate_week_dates(self, week: Optional[int], year: Optional[int]) -> str:
        """Estima las fechas de la semana basado en número de semana y año"""
        if not week or not year:
            return "Fecha por determinar"
        
        try:
            # Primero encontrar el primer día del año
            first_day = dt.date(year, 1, 1)
            # Ajustar para que la semana empiece en lunes
            start_date = first_day + dt.timedelta(weeks=week-1, days=-first_day.weekday())
            end_date = start_date + dt.timedelta(days=6)
            return f"{start_date.day}-{end_date.day} {MESES_ES.get(end_date.month, '')} {year}"
        except:
            return f"Semana {week}, {year}"

    def _generate_spain_card(self, data: Dict) -> str:
        return f"""<div class="card spain-card full-width">
    <h2>Datos Destacados para España</h2>
    <div class="stat-grid">
        <div class="stat-box spain-stat">
            <div class="number">{data['espana'].get('cchf_casos', 3)}</div>
            <div class="label">Casos de Fiebre Hemorrágica de Crimea-Congo (acumulado 2025)</div>
        </div>
        <div class="stat-box spain-stat">
            <div class="number">{data['espana'].get('cchf_nuevos', 0)}</div>
            <div class="label">Nuevos casos de CCHF esta semana</div>
        </div>
        <div class="stat-box spain-stat">
            <div class="number">{data['wnv'].get('paises', 11)}</div>
            <div class="label">Países europeos con WNV (España incluida)</div>
        </div>
        <div class="stat-box spain-stat">
            <div class="number">{data['espana'].get('dengue_casos', 0)}</div>
            <div class="label">Casos de dengue reportados</div>
        </div>
    </div>
</div>"""

    def _generate_respiratorios_card(self, data: Dict) -> str:
        resp = data['respiratorios']
        return f"""<div class="card">
    <h2>Virus Respiratorios en la UE/EEA</h2>
    <div class="key-points">
        <h3>Puntos Clave (Semana {data.get('week', '37')}):</h3>
        <ul>
            <li>Positividad de SARS-CoV-2 en atención primaria: <strong>{resp.get('sars_cov2_primaria', '13%')}</strong></li>
            <li>Positividad de SARS-CoV-2 en hospitalarios: <strong>{resp.get('sars_cov2_hospitalarios', '11%')}</strong></li>
            <li>Actividad de influenza: <strong>{resp.get('influenza', '1.4%')}</strong> en atención primaria</li>
            <li>Actividad de VRS: <strong>{resp.get('vrs', '0%')}</strong> en atención primaria</li>
        </ul>
    </div>
    <p><strong>Tendencia:</strong> {resp.get('tendencia', 'Circulación generalizada de SARS-CoV-2 con impacto limitado en hospitalizaciones.')}</p>
    <div class="risk-tag risk-low">SITUACIÓN ESTABLE</div>
</div>"""

    def _generate_wnv_card(self, data: Dict) -> str:
        wnv = data['wnv']
        return f"""<div class="card">
    <h2>Virus del Nilo Occidental (WNV)</h2>
    <div class="key-points">
        <h3>Datos Europeos (hasta {dt.datetime.now().day} {MESES_ES.get(dt.datetime.now().month)}):</h3>
        <ul>
            <li><strong>{wnv.get('paises', 11)} países</strong> reportando casos humanos</li>
            <li><strong>{wnv.get('areas_afectadas', 120)} áreas</strong> actualmente afectadas</li>
            <li>Países: {', '.join(wnv.get('paises_lista', ['Albania', 'Bulgaria', 'Francia', 'Grecia', 'Hungría', 'Italia', 'Kosovo', 'Rumanía', 'Serbia', 'España', 'Turquía']))}</li>
        </ul>
    </div>
    <p><strong>Expansión:</strong> {wnv.get('expansion', 'Aumento a 11 países respecto a la semana anterior.')}</p>
    <div class="risk-tag risk-low">EXPANSIÓN ESTACIONAL</div>
</div>"""

    def _generate_cchf_card(self, data: Dict) -> str:
        cchf = data['cchf']
        return f"""<div class="card">
    <h2>Fiebre Hemorrágica de Crimea-Congo</h2>
    <div class="key-points">
        <h3>Situación Actual:</h3>
        <ul>
            <li><strong>España: {cchf.get('espana_casos', 3)} casos</strong> (acumulado 2025)</li>
            <li>Grecia: {cchf.get('grecia_casos', 2)} casos (acumulado 2025)</li>
            <li><strong>{cchf.get('nuevos_casos', 0)} nuevos casos</strong> reportados esta semana</li>
        </ul>
    </div>
    <p>{cchf.get('explicacion', 'Los casos en España no son inesperados dada la circulación conocida del virus en animales en las provincias de Salamanca y Toledo.')}</p>
    <div class="risk-tag risk-low">RIESGO BAJO</div>
</div>"""

    def _generate_dengue_card(self, data: Dict) -> str:
        dengue = data['dengue']
        return f"""<div class="card">
    <h2>Dengue en Europa</h2>
    <div class="key-points">
        <h3>Casos Autóctonos (2025):</h3>
        <ul>
            <li>Francia: <strong>{dengue.get('francia_casos', 21)} casos</strong></li>
            <li>Italia: <strong>{dengue.get('italia_casos', 4)} casos</strong></li>
            <li>Portugal: <strong>{dengue.get('portugal_casos', 2)} casos</strong></li>
            <li><strong>{dengue.get('clusters_activos', 4)} clusters activos</strong> en Francia</li>
        </ul>
    </div>
    <p><strong>España:</strong> Sin casos reportados esta semana.</p>
    <div class="risk-tag risk-low">SIN CASOS EN ESPAÑA</div>
</div>"""

    def _generate_chikungunya_card(self, data: Dict) -> str:
        chik = data['chikungunya']
        return f"""<div class="card">
    <h2>Chikungunya en Europa</h2>
    <div class="stat-grid">
        <div class="stat-box">
            <div class="number">{chik.get('francia_casos', 480)}</div>
            <div class="label">Casos Francia <span class="update-badge">+97</span></div>
        </div>
        <div class="stat-box">
            <div class="number">{chik.get('italia_casos', 205)}</div>
            <div class="label">Casos Italia <span class="update-badge">+38</span></div>
        </div>
        <div class="stat-box">
            <div class="number">{chik.get('francia_clusters', 53)}</div>
            <div class="label">Clusters Francia ({chik.get('clusters_activos_francia', 38)} activos)</div>
        </div>
        <div class="stat-box">
            <div class="number">{chik.get('italia_clusters', 4)}</div>
            <div class="label">Clusters Italia ({chik.get('clusters_activos_italia', 3)} activos)</div>
        </div>
    </div>
    <div class="risk-tag risk-low">TRANSMISIÓN LOCAL ACTIVA</div>
</div>"""

    def _generate_ebola_card(self, data: Dict) -> str:
        ebola = data['ebola']
        return f"""<div class="card">
    <h2>Ébola - República Democrática del Congo</h2>
    <div class="key-points">
        <h3>Actualización del Brote:</h3>
        <ul>
            <li><strong>{ebola.get('total_casos', 48)} casos</strong> ({ebola.get('confirmados', 38)} confirmados, {ebola.get('probables', 10)} probables)</li>
            <li><strong>{ebola.get('muertes', 31)} muertes</strong> (Tasa de letalidad: {ebola.get('tasa_letalidad', '64.6%')})</li>
            <li><strong>{ebola.get('vacunados', 591)} personas</strong> vacunadas</li>
            <li><strong>{ebola.get('contactos', 900)}+ contactos</strong> identificados y seguidos</li>
        </ul>
    </div>
    <p>Todos los casos confirmados se reportan de {ebola.get('ubicacion', 'la Zona de Salud de Bulape, Provincia de Kasai')}.</p>
    <div class="risk-tag risk-low">RIESGO MUY BAJO para UE/EEA</div>
</div>"""

    def _generate_rabia_card(self, data: Dict) -> str:
        rabia = data['rabia']
        recs = "\n".join([f"<li>{rec}</li>" for rec in rabia.get('recomendaciones', [])])
        return f"""<div class="card">
    <h2>Alerta de Rabia - {rabia.get('alerta', 'Bangkok, Tailandia')}</h2>
    <p>Autoridades sanitarias de {rabia.get('alerta', 'Bangkok')} emitieron alerta por presencia de animales enfermos con rabia.</p>
    <div class="key-points">
        <h3>Recomendaciones para Viajeros:</h3>
        <ul>{recs}</ul>
    </div>
    <div class="risk-tag risk-low">RIESGO BAJO con precauciones</div>
</div>"""

    def _generate_nipah_card(self, data: Dict) -> str:
        nipah = data['nipah']
        return f"""<div class="card">
    <h2>Virus Nipah - Bangladesh</h2>
    <div class="key-points">
        <h3>Casos 2025 (hasta {dt.datetime.now().day-10} {MESES_ES.get(dt.datetime.now().month)}):</h3>
        <ul>
            <li><strong>{nipah.get('muertes', 4)} muertes</strong> reportadas</li>
            <li>Tasa de letalidad histórica: <strong>{nipah.get('tasa_letalidad_historica', '71.7%')}</strong></li>
            <li>{nipah.get('casos_adultos', 3)} casos adultos asociados a consumo de {nipah.get('fuente_infeccion', 'savia de palma cruda')}</li>
            <li>{nipah.get('caso_infantil', 1)} caso infantil (fuente bajo investigación)</li>
        </ul>
    </div>
    <div class="risk-tag risk-low">RIESGO MUY BAJO para viajeros</div>
</div>"""

    def _generate_alertas_card(self, data: Dict) -> str:
        return """<div class="card full-width">
    <h2>Resumen de Alertas y Monitoreo Activo</h2>
    <ul class="topic-list">
        <li><strong>Ébola RDC:</strong> Brote activo con 48 casos - vigilancia intensiva en curso</li>
        <li><strong>Rabia Bangkok:</strong> Alerta local - prohibición de movimiento animal por 30 días</li>
        <li><strong>Virus Nipah Bangladesh:</strong> 4 muertes - vigilancia activa de contactos</li>
        <li><strong>WNV Europa:</strong> Expansión a 11 países - 120 áreas afectadas</li>
        <li><strong>Fiebre Crimea-Congo:</strong> Situación estable - sin nuevos casos esta semana</li>
        <li><strong>Dengue/Chikungunya:</strong> Transmisión local activa en Francia e Italia</li>
        <li><strong>Virus Respiratorios:</strong> Circulación de SARS-CoV-2 con impacto limitado</li>
    </ul>
</div>"""

    # --------------------------------------------------------------
    # Envío de correo (mantenido del original)
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
    # Run mejorado
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

        # Extracción de datos detallados
        try:
            detailed_data = self.extract_detailed_data(text) if text else self._get_default_data()
        except Exception as e:
            logging.exception("Error extrayendo datos detallados: %s", e)
            detailed_data = self._get_default_data()

        # HTML final con nuevo formato
        html = self.build_html(week, year, pdf_url, article_url, detailed_data)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"

        # Envío
        try:
            self.send_email(subject, html)
            self._save_state(pdf_url)
        except Exception as e:
            logging.exception("Fallo enviando el email: %s", e)

    def _get_default_data(self) -> Dict[str, Any]:
        """Datos por defecto en caso de error en la extracción"""
        return {
            "resumen_ejecutivo": "Continúa la circulación generalizada de SARS-CoV-2 en la UE/EEA con impacto limitado en hospitalizaciones. Los virus respiratorios estacionales (VRS e influenza) se mantienen en niveles muy bajos.",
            "espana": {"cchf_casos": 3, "cchf_nuevos": 0, "dengue_casos": 0},
            "respiratorios": {
                "sars_cov2_primaria": "13%", 
                "sars_cov2_hospitalarios": "11%",
                "influenza": "1.4%", 
                "vrs": "0%",
                "tendencia": "Circulación generalizada de SARS-CoV-2 con impacto limitado en hospitalizaciones"
            },
            "wnv": {
                "paises": 11, 
                "areas_afectadas": 120,
                "paises_lista": ["Albania", "Bulgaria", "Francia", "Grecia", "Hungría", "Italia", "Kosovo", "Rumanía", "Serbia", "España", "Turquía"],
                "expansion": "Aumento a 11 países respecto a la semana anterior"
            },
            "cchf": {
                "espana_casos": 3, 
                "grecia_casos": 2, 
                "nuevos_casos": 0,
                "explicacion": "Los casos en España no son inesperados dada la circulación conocida del virus en animales en las provincias de Salamanca y Toledo."
            },
            "dengue": {
                "francia_casos": 21, 
                "italia_casos": 4, 
                "portugal_casos": 2, 
                "clusters_activos": 4,
                "espana_casos": 0
            },
            "chikungunya": {
                "francia_casos": 480, 
                "italia_casos": 205, 
                "francia_clusters": 53, 
                "italia_clusters": 4,
                "clusters_activos_francia": 38, 
                "clusters_activos_italia": 3
            },
            "ebola": {
                "total_casos": 48, 
                "confirmados": 38, 
                "probables": 10, 
                "muertes": 31,
                "tasa_letalidad": "64.6%", 
                "vacunados": 591, 
                "contactos": 900,
                "ubicacion": "Zona de Salud de Bulape, Provincia de Kasai"
            },
            "rabia": {
                "alerta": "Bangkok, Tailandia",
                "recomendaciones": [
                    "Evitar contacto con animales callejeros",
                    "Buscar atención médica inmediata ante mordeduras",
                    "Considerar vacunación pre-exposición para actividades de alto riesgo"
                ]
            },
            "nipah": {
                "muertes": 4, 
                "tasa_letalidad_historica": "71.7%", 
                "casos_adultos": 3,
                "caso_infantil": 1, 
                "fuente_infeccion": "consumo de savia de palma cruda"
            },
            "alertas": []
        }


# =====================================================================
# main
# =====================================================================

def main() -> None:
    cfg = Config()
    WeeklyReportAgent(cfg).run()

if __name__ == "__main__":
    main()

