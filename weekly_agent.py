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
import hashlib
import datetime as dt
from typing import Dict, List, Tuple, Optional, Any
from urllib.parse import urljoin, unquote

import requests
from requests.adapters import HTTPAdapter, Retry
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

def safe_num(v, default="—"):
    return default if (v is None or v == "") else str(v)

def first_day_last_day_of_iso_week(year: int, week: int) -> Tuple[dt.datetime, dt.datetime]:
    # lunes a domingo
    d1 = dt.datetime.fromisocalendar(year, week, 1)
    d7 = dt.datetime.fromisocalendar(year, week, 7)
    return d1, d7


# =====================================================================
# Agente – versión con plantilla del usuario integrada
# =====================================================================

class WeeklyReportAgent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = self._build_session()

    # --------------------------------------------------------------
    # Red y parsing ECDC
    # --------------------------------------------------------------
    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retries))
        s.mount("http://", HTTPAdapter(max_retries=retries))
        return s

    def _parse_week_year(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None, int(y.group(1)) if y else None)

    def _extract_article_date(self, soup: BeautifulSoup) -> Optional[dt.datetime]:
        selectors = [
            'time[datetime]', 'meta[property="article:published_time"]',
            'meta[name="date"]', 'meta[name="pubdate"]'
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            val = (el.get("datetime") or el.get("content")) if el else None
            if val:
                try:
                    return dt.datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass
        t = (soup.title.get_text(strip=True) if soup.title else "").lower()
        w, y = self._parse_week_year(t)
        if w and y:
            try:
                return dt.datetime.fromisocalendar(y, w, 1)
            except Exception:
                return None
        return None

    def fetch_latest_article_and_pdf(self) -> Tuple[str, str, Optional[int], Optional[int]]:
        r = self.session.get(self.cfg.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        articles: List[Tuple[dt.datetime, str, str, BeautifulSoup]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                article_url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                try:
                    ar = self.session.get(article_url, timeout=30)
                    ar.raise_for_status()
                    asoup = BeautifulSoup(ar.text, "html.parser")
                    date = self._extract_article_date(asoup) or dt.datetime.min
                    pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
                    if not pdf_a:
                        pdf_a = next((x for x in asoup.find_all("a", href=True) if ".pdf" in x["href"].lower()), None)
                    if not pdf_a:
                        continue
                    pdf_url = pdf_a["href"]
                    if not pdf_url.startswith("http"):
                        pdf_url = urljoin(article_url, pdf_url)
                    articles.append((date, article_url, pdf_url, asoup))
                except Exception as e:
                    logging.warning("Artículo omitido (%s): %s", article_url, e)
                    continue

        if not articles:
            raise RuntimeError("No se encontraron artículos CDTR con PDF.")

        articles.sort(key=lambda x: x[0], reverse=True)
        date, article_url, pdf_url, asoup = articles[0]
        title = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
        week, year = self._parse_week_year(title)
        logging.info("PDF seleccionado: %s (semana=%s, año=%s)", pdf_url, week, year)
        return article_url, pdf_url, week, year

    # --------------------------------------------------------------
    # Estado y descarga
    # --------------------------------------------------------------
    def _load_state(self) -> Dict[str, Any]:
        if not os.path.exists(self.cfg.state_file):
            return {}
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, pdf_url: str, pdf_hash: str) -> None:
        state = {"last_pdf_url": pdf_url, "last_pdf_hash": pdf_hash, "ts": dt.datetime.utcnow().isoformat()}
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _download_pdf(self, pdf_url: str) -> Tuple[str, str]:
        r = self.session.get(pdf_url, timeout=60, stream=True)
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "").lower()
        if "pdf" not in ct:
            logging.warning("Content-Type no parece PDF: %s", ct)
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > self.cfg.max_pdf_mb * 1024 * 1024:
            raise RuntimeError(f"PDF supera {self.cfg.max_pdf_mb} MB")

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        h = hashlib.sha256()
        with tmp as f:
            for chunk in r.iter_content(16384):
                if chunk:
                    h.update(chunk)
                    f.write(chunk)
        return tmp.name, h.hexdigest()

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
    # EXTRACCIÓN DE DATOS (con evidencias)
    # --------------------------------------------------------------
    def _capture_sentence(self, text_lower: str, anchor: str, window: int = 180) -> Optional[str]:
        i = text_lower.find(anchor)
        if i == -1:
            return None
        start = max(0, i - window)
        end = min(len(text_lower), i + window)
        snippet = text_lower[start:end]
        m = re.search(r'([^.]*\b' + re.escape(anchor) + r'\b[^.]*)\.', snippet)
        return (m.group(1).strip() + ".") if m else snippet.strip()

    def extract_key_data(self, text: str) -> Dict[str, Any]:
        """Extrae datos clave con cierta robustez y guarda frases evidenciales."""
        data: Dict[str, Any] = {"spain": {}, "eu": {}, "evidence": {}}
        tl = (text or "").lower()

        # WNV – países en UE/EEE
        m = re.search(r'west nile[^.]*?\b(\d{1,3})\b[^.]*?\bcountr', tl)
        if m:
            data["eu"]["wnv_countries"] = int(m.group(1))
            ev = self._capture_sentence(tl, "west nile")
            if ev: data["evidence"]["wnv_countries"] = ev

        # WNV España – casos
        m = re.search(r'spain[^.]*west nile[^.]*?(\d{1,4})[^.]*case', tl)
        if m:
            data["spain"]["wnv_cases"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_wnv"] = ev

        # Zonas/municipios WNV en España (opcional)
        m = re.search(r'spain[^.]*west nile[^.]*?(\d{1,3})[^.]*?(?:municipal|area|zone)', tl)
        if m:
            data["spain"]["wnv_municipalities"] = int(m.group(1))

        # Dengue UE/EEE autóctono
        m = re.search(r'(?:eu|europe|\bue\b)[^.]*?dengue[^.]*?(\d{1,6})[^.]*?cases', tl)
        if m:
            data["eu"]["dengue_auto_total"] = int(m.group(1))
            ev = self._capture_sentence(tl, "dengue")
            if ev: data["evidence"]["dengue_eu"] = ev

        m = re.search(r'(?:autochthonous|local)\s+dengue[^.]*?in\s+(\d{1,3})\s+(?:countries|member states)', tl)
        if m:
            data["eu"]["dengue_auto_countries"] = int(m.group(1))

        # Dengue España (autóctono/importado)
        m = re.search(r'spain[^.]*dengue[^.]*?(\d{1,4})[^.]*?(?:autochthonous|local)', tl)
        if m:
            data["spain"]["dengue_local"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_dengue_local"] = ev

        m = re.search(r'spain[^.]*dengue[^.]*?(\d{1,4})[^.]*?import', tl)
        if m:
            data["spain"]["dengue_imported"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_dengue_imported"] = ev

        # CCHF España
        m = re.search(r'spain[^.]*?(?:crimean|crimea).*?(?:congo|cchf)[^.]*?(\d{1,4})[^.]*?case', tl)
        if m:
            data["spain"]["cchf_cases"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_cchf"] = ev

        # SARS-CoV-2 positividad (si aparece algún %)
        m = re.search(r'sars[- ]?cov[- ]?2[^%]*?(\d{1,2}(?:\.\d+)?)\s*%', tl)
        if m:
            data["spain"]["sars_pos"] = float(m.group(1))
            data["evidence"]["sars_pos"] = self._capture_sentence(tl, "sars-cov-2") or ""

        return data

    # --------------------------------------------------------------
    # Render a la PLANTILLA del usuario (parametrizada)
    # --------------------------------------------------------------
    USER_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Resumen Semanal ECDC - [[TITLE_WEEK]]</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
body { background-color: #f5f7fa; color: #333; line-height: 1.6; padding: 20px; max-width: 1200px; margin: 0 auto; }
.header { text-align: center; padding: 20px; background: linear-gradient(135deg, #2b6ca3 0%, #1a4e7a 100%); color: white; border-radius: 10px; margin-bottom: 25px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1); }
.header h1 { font-size: 2.2rem; margin-bottom: 10px; }
.header .subtitle { font-size: 1.2rem; margin-bottom: 15px; opacity: 0.9; }
.header .week { background-color: rgba(255, 255, 255, 0.2); display: inline-block; padding: 8px 16px; border-radius: 30px; font-weight: 600; }
.container { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 900px) { .container { grid-template-columns: 1fr; } }
.card { background: white; border-radius: 10px; padding: 20px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.05); transition: transform 0.3s ease; }
.card:hover { transform: translateY(-5px); box-shadow: 0 6px 12px rgba(0, 0, 0, 0.1); }
.card h2 { color: #2b6ca3; border-bottom: 2px solid #eaeaea; padding-bottom: 10px; margin-bottom: 15px; font-size: 1.4rem; }
.spain-card { border-left: 5px solid #c60b1e; background-color: #fff9f9; }
.spain-card h2 { color: #c60b1e; display: flex; align-items: center; }
.spain-card h2:before { content: "🇪🇸"; margin-right: 10px; }
.stat-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; margin: 15px 0; }
.stat-box { background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #eaeaea; }
.stat-box .number { font-size: 1.8rem; font-weight: bold; color: #2b6ca3; margin-bottom: 5px; }
.stat-box .label { font-size: 0.9rem; color: #666; }
.spain-stat .number { color: #c60b1e; }
.key-points { background-color: #e8f4ff; padding: 15px; border-radius: 8px; margin: 15px 0; }
.key-points h3 { margin-bottom: 10px; color: #2b6ca3; }
.key-points ul { padding-left: 20px; }
.key-points li { margin-bottom: 8px; }
.risk-tag { display: inline-block; padding: 5px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; margin-top: 10px; }
.risk-low { background-color: #d4edda; color: #155724; }
.risk-moderate { background-color: #fff3cd; color: #856404; }
.risk-high { background-color: #f8d7da; color: #721c24; }
.full-width { grid-column: 1 / -1; }
.footer { text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eaeaea; color: #666; font-size: 0.9rem; }
.topic-list { list-style-type: none; }
.topic-list li { padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
.topic-list li:last-child { border-bottom: none; }
.pdf-button { display: inline-block; background: #0b5cab; color: white; text-decoration: none; padding: 12px 24px; border-radius: 8px; font-weight: 700; margin: 10px 0; }
.update-badge { display: inline-block; background: #ff6b6b; color: white; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; margin-left: 8px; vertical-align: middle; }
</style>
</head>
<body>
<div class="header">
  <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
  <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
  <div class="week">[[WEEK_RANGE_LABEL]]</div>
</div>

<div class="container">
  <div class="card full-width">
    <h2>Resumen Ejecutivo</h2>
    <p>[[EXEC_PARAGRAPH]]</p>
    <a href="[[PDF_URL]]" class="pdf-button">📄 Abrir Informe Completo (PDF)</a>
    <a href="[[ARTICLE_URL]]" class="pdf-button" style="background:#125e2a">🌐 Ver página del informe</a>
  </div>

  <div class="card spain-card full-width">
    <h2>Datos Destacados para España</h2>
    <div class="stat-grid">
      <div class="stat-box spain-stat">
        <div class="number">[[ES_CCHF_TOTAL]]</div>
        <div class="label">Casos de Fiebre Hemorrágica de Crimea-Congo (acumulado)</div>
      </div>
      <div class="stat-box spain-stat">
        <div class="number">[[ES_CCHF_NEW]]</div>
        <div class="label">Nuevos casos de CCHF esta semana</div>
      </div>
      <div class="stat-box spain-stat">
        <div class="number">[[EU_WNV_COUNTRIES]]</div>
        <div class="label">Países europeos con WNV</div>
      </div>
      <div class="stat-box spain-stat">
        <div class="number">[[ES_DENGUE_THIS_WEEK]]</div>
        <div class="label">Casos de dengue reportados (semana)</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Virus Respiratorios en la UE/EEA</h2>
    <div class="key-points">
      <h3>Puntos Clave (estimaciones del informe):</h3>
      <ul>
        <li>Positividad de SARS-CoV-2 (España aprox.): <strong>[[ES_SARS_POS]]</strong></li>
        <li>Otras virosis respiratorias: actividad estacional; niveles variables por país.</li>
      </ul>
    </div>
    <p><strong>Tendencia:</strong> [[RESP_TREND]].</p>
    <div class="risk-tag risk-low">SITUACIÓN ESTABLE</div>
  </div>

  <div class="card">
    <h2>Virus del Nilo Occidental (WNV)</h2>
    <div class="key-points">
      <h3>Datos Europeos:</h3>
      <ul>
        <li><strong>[[EU_WNV_COUNTRIES]] países</strong> reportando casos humanos</li>
        <li>España: [[ES_WNV_CASES]] caso(s) detectados</li>
      </ul>
    </div>
    <p><strong>Expansión:</strong> [[WNV_EXPANSION]].</p>
    <div class="risk-tag risk-low">EXPANSIÓN ESTACIONAL</div>
  </div>

  <div class="card">
    <h2>Fiebre Hemorrágica de Crimea-Congo</h2>
    <div class="key-points">
      <h3>Situación Actual:</h3>
      <ul>
        <li><strong>España: [[ES_CCHF_TOTAL]] casos</strong> (acumulado año)</li>
        <li>[[CCHF_OTHER_INFO]]</li>
        <li><strong>[[ES_CCHF_NEW]] nuevos casos</strong> esta semana</li>
      </ul>
    </div>
    <p>[[CCHF_COMMENT]].</p>
    <div class="risk-tag risk-low">RIESGO BAJO</div>
  </div>

  <div class="card">
    <h2>Dengue en Europa</h2>
    <div class="key-points">
      <h3>Casos Autóctonos (UE/EEE):</h3>
      <ul>
        <li>Países con transmisión autóctona: <strong>[[EU_DENGUE_AUTO_COUNTRIES]]</strong></li>
        <li>Total casos autóctonos estimados: <strong>[[EU_DENGUE_AUTO_TOTAL]]</strong></li>
      </ul>
    </div>
    <p><strong>España:</strong> sin señal autóctona si no se indica lo contrario (esta semana: [[ES_DENGUE_THIS_WEEK]]).</p>
    <div class="risk-tag risk-low">SEGUIMIENTO ACTIVO</div>
  </div>

  <div class="card full-width">
    <h2>Notas de trazabilidad</h2>
    <div class="key-points">
      [[EVIDENCE_HTML]]
    </div>
  </div>

</div>

<div class="footer">
  <p>Resumen generado el: [[GEN_DATE_ES]]</p>
  <p>Fuente: ECDC Weekly Communicable Disease Threats Report, [[TITLE_WEEK]]</p>
  <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
</div>
</body>
</html>"""

    def _render_user_template(self,
                              pdf_url: str,
                              article_url: str,
                              week: Optional[int],
                              year: Optional[int],
                              data: Dict[str, Any],
                              evidence_html: str) -> str:
        # Derivar etiquetas de semana y rango de fechas
        if week and year:
            try:
                d1, d7 = first_day_last_day_of_iso_week(year, week)
                week_range = f"Semana {week}: {d1.day}-{d7.day} {MESES_ES[d7.month]} {year}"
            except Exception:
                week_range = f"Semana {week}, {year}"
        else:
            week_range = "Último informe"

        title_week = week_range

        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}

        # Valores
        es_cchf_total = safe_num(sp.get("cchf_cases"))
        es_cchf_new = "—"  # sin extracción específica de “nuevos”
        eu_wnv_countries = safe_num(eu.get("wnv_countries"))
        es_dengue_week = safe_num(sp.get("dengue_local"))  # si hay autóctonos; si no, “—”
        es_sars_pos = (f"{sp.get('sars_pos')}%" if sp.get("sars_pos") is not None else "—")
        resp_trend = "Circulación generalizada de SARS-CoV-2 con impacto limitado en hospitalizaciones"
        es_wnv_cases = safe_num(sp.get("wnv_cases"))
        wnv_expansion = "Patrón estacional con ampliación geográfica en verano-otoño"
        cchf_other_info = "Casos también reportados en otros países de la región (según informe)."
        cchf_comment = "Casos esperables en áreas endémicas; mantener vigilancia y protocolos de bioseguridad."

        eu_dengue_countries = safe_num(eu.get("dengue_auto_countries"))
        eu_dengue_total = safe_num(eu.get("dengue_auto_total"))

        exec_points = []
        if es_wnv_cases != "—":
            exec_points.append(f"WNV (España): {es_wnv_cases} caso(s).")
        if es_cchf_total != "—":
            exec_points.append(f"CCHF (España): {es_cchf_total} caso(s) acumulados.")
        if eu_wnv_countries != "—":
            exec_points.append(f"WNV (UE/EEE): {eu_wnv_countries} países reportando transmisión.")
        if eu_dengue_total != "—" or eu_dengue_countries != "—":
            frag = []
            if eu_dengue_countries != "—":
                frag.append(f"{eu_dengue_countries} país(es)")
            if eu_dengue_total != "—":
                frag.append(f"{eu_dengue_total} casos autóctonos")
            exec_points.append("Dengue (UE/EEE): " + " | ".join(frag) + ".")

        if not exec_points:
            exec_points = ["Sin indicadores cuantitativos destacados esta semana en la extracción automática."]

        exec_paragraph = " ".join(exec_points)

        html = self.USER_TEMPLATE
        replacements = {
            "[[TITLE_WEEK]]": title_week,
            "[[WEEK_RANGE_LABEL]]": week_range,
            "[[EXEC_PARAGRAPH]]": exec_paragraph,
            "[[PDF_URL]]": pdf_url or "#",
            "[[ARTICLE_URL]]": article_url or "#",
            "[[ES_CCHF_TOTAL]]": es_cchf_total,
            "[[ES_CCHF_NEW]]": es_cchf_new,
            "[[EU_WNV_COUNTRIES]]": eu_wnv_countries,
            "[[ES_DENGUE_THIS_WEEK]]": es_dengue_week,
            "[[ES_SARS_POS]]": es_sars_pos,
            "[[RESP_TREND]]": resp_trend,
            "[[ES_WNV_CASES]]": es_wnv_cases,
            "[[WNV_EXPANSION]]": wnv_expansion,
            "[[CCHF_OTHER_INFO]]": cchf_other_info,
            "[[CCHF_COMMENT]]": cchf_comment,
            "[[EU_DENGUE_AUTO_COUNTRIES]]": eu_dengue_countries,
            "[[EU_DENGUE_AUTO_TOTAL]]": eu_dengue_total,
            "[[EVIDENCE_HTML]]": evidence_html or "<em>Sin evidencias capturadas.</em>",
            "[[GEN_DATE_ES]]": fecha_es(dt.datetime.utcnow()),
        }
        for k, v in replacements.items():
            html = html.replace(k, v)

        return html

    # --------------------------------------------------------------
    # Email
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

        text_plain = re.sub("<[^>]+>", "", html)
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.cfg.sender_email
        msg['To'] = ", ".join(to_addrs)
        msg.attach(MIMEText(text_plain, 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))

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
            logging.error("No se pudo localizar el CDTR más reciente: %s", e)
            return

        state = self._load_state()
        tmp_pdf = ""
        pdf_hash = ""
        text = ""

        try:
            tmp_pdf, pdf_hash = self._download_pdf(pdf_url)
            if state.get("last_pdf_hash") == pdf_hash:
                logging.info("PDF (hash) ya enviado anteriormente.")
                return

            t0 = time.time()
            text = self._extract_text_pdf(tmp_pdf)
            logging.info("PDF descargado y texto extraído (%d caracteres en %.2fs)", len(text), time.time()-t0)
        except Exception as e:
            logging.error("Error con el PDF: %s", e)
            text = ""
        finally:
            if tmp_pdf and os.path.exists(tmp_pdf):
                try:
                    os.remove(tmp_pdf)
                except Exception:
                    pass

        # Datos y evidencias
        try:
            if text:
                data = self.extract_key_data(text)
                # construir evidencias HTML
                ev = data.get("evidence", {}) if isinstance(data.get("evidence"), dict) else {}
                if ev:
                    items = []
                    for k, v in ev.items():
                        if v:
                            items.append(f"<li><strong>{k}:</strong> {v}</li>")
                    evidence_html = "<ul>" + "".join(items) + "</ul>"
                else:
                    evidence_html = ""
            else:
                data = {}
                evidence_html = ""
        except Exception as e:
            logging.error("Error extrayendo datos: %s", e)
            data = {}
            evidence_html = ""

        # Render con TU PLANTILLA
        html = self._render_user_template(
            pdf_url=pdf_url,
            article_url=article_url,
            week=week,
            year=year,
            data=data,
            evidence_html=evidence_html
        )

        subject = f"ECDC CDTR – Resumen Semana {week or 'N/D'} ({year or 'N/D'}) – Español"

        try:
            self.send_email(subject, html)
            self._save_state(pdf_url, pdf_hash or "")
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


