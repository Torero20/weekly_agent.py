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


# =====================================================================
# Agente – versión robusta
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
        # meta/semántico
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
        # Fallback: título con Week NN, YYYY
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

        # Ordenar por fecha (desc)
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
        data: Dict[str, Any] = {
            "spain": {},
            "eu": {},
            "evidence": {}
        }
        tl = (text or "").lower()

        # WNV – países en UE/EEE
        m = re.search(r'west nile[^.]*?\b(\d{1,3})\b[^.]*?\bcountr', tl)
        if m:
            data["eu"]["wnv_countries"] = int(m.group(1))
            ev = self._capture_sentence(tl, "west nile")
            if ev: data["evidence"]["wnv_countries"] = ev

        # WNV España – casos (heurística: oración con "spain" y "west nile")
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

        # SARS-CoV-2 positividad (España y/o UE)
        m = re.search(r'sars[- ]?cov[- ]?2[^%]*?(\d{1,2}(?:\.\d+)?)\s*%', tl)
        if m:
            # Si no podemos distinguir, lo usamos para España por defecto
            data["spain"]["sars_pos"] = float(m.group(1))
            data["evidence"]["sars_pos"] = self._capture_sentence(tl, "sars-cov-2") or ""

        return data

    # --------------------------------------------------------------
    # RESUMEN ENRIQUECIDO (España + UE/EEE + evidencias)
    # --------------------------------------------------------------
    def generate_spanish_summary_rico(self, data: Dict[str, Any],
                                      week: Optional[int], year: Optional[int]) -> Dict[str, List[str]]:
        w = week or "N/D"; y = year or "N/D"
        summary: Dict[str, List[str]] = {}

        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}
        ev = data.get("evidence", {}) if isinstance(data.get("evidence"), dict) else {}

        # Resumen ejecutivo
        exec_points = [f"Informe semanal ECDC – Semana {w} de {y}.",
                       "Se destacan eventos relevantes priorizando España y su comparativa con UE/EEE."]
        if sp.get("wnv_cases") is not None:
            frag = f"Virus del Nilo Occidental (España): {sp['wnv_cases']} caso(s)"
            if sp.get("wnv_municipalities") is not None:
                frag += f" en {sp['wnv_municipalities']} zona(s)"
            exec_points.append(frag + ".")
        if (sp.get("dengue_local") is not None) or (sp.get("dengue_imported") is not None):
            frs = []
            if sp.get("dengue_local") is not None: frs.append(f"autóctonos {sp['dengue_local']}")
            if sp.get("dengue_imported") is not None: frs.append(f"importados {sp['dengue_imported']}")
            if frs: exec_points.append("Dengue (España): " + ", ".join(frs) + ".")
        if sp.get("cchf_cases") is not None:
            exec_points.append(f"CCHF (España): {sp['cchf_cases']} caso(s).")
        if eu.get("wnv_countries") is not None:
            exec_points.append(f"WNV (UE/EEE): {eu['wnv_countries']} países con transmisión.")
        summary["Resumen Ejecutivo"] = exec_points

        # España – visión detallada
        esp_points: List[str] = []
        if sp.get("wnv_cases") is not None:
            msg = f"WNV: {sp['wnv_cases']} caso(s)"
            if sp.get("wnv_municipalities") is not None:
                msg += f" en {sp['wnv_municipalities']} municipio(s)/zona(s)"
            msg += ". Implicaciones: reforzar control vectorial local y cribado en hemoderivados según protocolos."
            esp_points.append(msg)
        if (sp.get("dengue_local") is not None) or (sp.get("dengue_imported") is not None):
            dl = sp.get("dengue_local"); di = sp.get("dengue_imported")
            frag = []
            if dl is not None: frag.append(f"autóctonos={dl}")
            if di is not None: frag.append(f"importados={di}")
            esp_points.append("Dengue: " + ", ".join(frag) + ". Relevancia: vigilancia de Aedes, triaje de fiebre post-viaje y notificación ágil.")
        if sp.get("cchf_cases") is not None:
            esp_points.append("CCHF: {} caso(s). Riesgo ocupacional en entornos rurales/ganaderos; valorar EPI y educación sanitaria.".format(sp["cchf_cases"]))
        if sp.get("sars_pos") is not None:
            esp_points.append("Virus respiratorios: positividad SARS-CoV-2 ≈ {}%. Mantener vigilancia en AP/Hospital y circuitos según presión asistencial.".format(sp["sars_pos"]))
        if not esp_points:
            esp_points.append("Sin indicadores específicos detectados para España esta semana; mantener vigilancia de rutina.")
        summary["España – visión detallada"] = esp_points

        # Panorama UE/EEE
        eu_points: List[str] = []
        if eu.get("wnv_countries") is not None:
            eu_points.append("WNV (UE/EEE): {} países con transmisión. Patrón estacional con picos verano-otoño; seguridad transfusional prioritaria.".format(eu["wnv_countries"]))
        if eu.get("dengue_auto_countries") is not None or eu.get("dengue_auto_total") is not None:
            fr = []
            if eu.get("dengue_auto_countries") is not None:
                fr.append(f"autóctono en {eu['dengue_auto_countries']} país(es)")
            if eu.get("dengue_auto_total") is not None:
                fr.append(f"≈ {eu['dengue_auto_total']} casos")
            eu_points.append("Dengue (UE/EEE): " + "; ".join(fr) + ". Expansión vectorial ligada a clima; relevancia para vigilancia transfronteriza.")
        if eu.get("cchf_countries") is not None:
            eu_points.append(f"CCHF (UE/EEE): circulación en {eu['cchf_countries']} país(es). Riesgo bajo-moderado general; ocupacional en sectores específicos.")
        if eu.get("sars_pos") is not None:
            eu_points.append(f"Respiratorios: positividad SARS-CoV-2 (UE/EEE) ≈ {eu['sars_pos']}%. Señales heterogéneas por región.")
        if not eu_points:
            eu_points.append("Sin señales pan-europeas destacables adicionales esta semana.")
        summary["Panorama UE/EEE"] = eu_points

        # Recomendaciones operativas
        summary["Recomendaciones operativas"] = [
            "Refuerzo de comunicación con salud pública ante fiebre sin foco (viaje/picadura).",
            "Control vectorial local (aguas estancadas) y mensajes a población en zonas de riesgo.",
            "Cribado transfusional y de trasplantes según transmisión WNV en el área de captación.",
            "Laboratorio: circuito de notificación ágil y paneles sindrómicos en picos estacionales."
        ]

        # Evidencias (HTML)
        if ev:
            items = []
            for k, v in ev.items():
                if v:
                    items.append(f"<li><strong>{k}:</strong> {v}</li>")
            evid_html = "<ul>" + "".join(items) + "</ul>"
        else:
            evid_html = ""

        summary["data"] = data
        summary["evidence_html"] = evid_html
        return summary

    # --------------------------------------------------------------
    # HTML
    # --------------------------------------------------------------
    def build_spanish_html(self, week: Optional[int], year: Optional[int],
                           pdf_url: str, article_url: str,
                           summary: Dict[str, List[str]]) -> str:

        week_label = f"Semana {week}, {year}" if week and year else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())

        data = summary.get("data", {}) if isinstance(summary.get("data"), dict) else {}
        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}

        def li(points: List[str]) -> str:
            return "".join(f"<li>{p}</li>" for p in points)

        def stat_box(number: Optional[str], label: str, accent: bool=False) -> str:
            if number is None:
                number = "–"
            cls = "spain-stat" if accent else ""
            return f'''<div class="stat-box {cls}">
                <div class="number">{number}</div>
                <div class="label">{label}</div>
            </div>'''

        html = f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ECDC – {week_label}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
body {{ background:#f5f7fa; color:#333; line-height:1.6; padding:20px; max-width:1200px; margin:0 auto; }}
.header {{ text-align:center; padding:20px; background:linear-gradient(135deg,#2b6ca3 0%,#1a4e7a 100%); color:#fff; border-radius:10px; margin-bottom:25px; box-shadow:0 4px 12px rgba(0,0,0,.1); }}
.header h1 {{ font-size:2.2rem; margin-bottom:10px; }}
.header .subtitle {{ font-size:1.1rem; opacity:.9 }}
.header .week {{ background:rgba(255,255,255,.2); display:inline-block; padding:8px 16px; border-radius:30px; font-weight:600; margin-top:10px; }}
.container {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
@media (max-width:900px) {{ .container {{ grid-template-columns:1fr; }} }}
.card {{ background:#fff; border-radius:10px; padding:20px; box-shadow:0 4px 8px rgba(0,0,0,.05); }}
.card h2 {{ color:#2b6ca3; border-bottom:2px solid #eaeaea; padding-bottom:10px; margin-bottom:15px; font-size:1.35rem; }}
.full-width {{ grid-column:1 / -1; }}
.spain-card {{ border-left:5px solid #c60b1e; background:#fff9f9; }}
.spain-card h2 {{ color:#c60b1e; display:flex; align-items:center; }}
.spain-card h2:before {{ content:"🇪🇸"; margin-right:10px; }}
.key-points {{ background:#e8f4ff; padding:15px; border-radius:8px; margin:15px 0; }}
.key-points ul {{ padding-left:20px; }}
.risk-tag {{ display:inline-block; padding:5px 12px; border-radius:20px; font-size:.85rem; font-weight:600; margin-top:10px; background:#d4edda; color:#155724; }}
.pdf-button {{ display:inline-block; background:#0b5cab; color:#fff; text-decoration:none; padding:12px 24px; border-radius:8px; font-weight:700; margin:10px 0; }}
.stat-grid {{ display:grid; grid-template-columns:repeat(2, 1fr); gap:15px; margin:15px 0; }}
.stat-box {{ background:#f8f9fa; padding:15px; border-radius:8px; text-align:center; border:1px solid #eaeaea; }}
.stat-box .number {{ font-size:1.6rem; font-weight:700; color:#2b6ca3; margin-bottom:5px; }}
.spain-stat .number {{ color:#c60b1e; }}
.note {{ font-size:.92rem; color:#666; margin-top:8px; }}
.small {{ font-size:.9rem; color:#555; }}
</style>
</head>
<body>
<div class="header">
  <h1>Resumen semanal – Amenazas de enfermedades transmisibles</h1>
  <div class="subtitle">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
  <div class="week">{week_label}</div>
</div>

<div class="container">
  <div class="card full-width">
    <h2>Resumen Ejecutivo</h2>
    <p>Informe de vigilancia epidemiológica con énfasis en España y comparativa UE/EEE.</p>
    <a href="{pdf_url}" class="pdf-button">📄 Descargar Informe Completo (PDF)</a>
    <a href="{article_url}" class="pdf-button" style="background:#125e2a">🌐 Ver página del informe</a>
    <div class="key-points">
      <ul>{li(summary.get("Resumen Ejecutivo", []))}</ul>
    </div>
  </div>

  <!-- España -->
  <div class="card spain-card full-width">
    <h2>España – visión detallada</h2>
    <div class="stat-grid">
      {stat_box(str(sp.get('wnv_cases')) if sp.get('wnv_cases') is not None else None, "WNV – casos", True)}
      {stat_box(str(sp.get('wnv_municipalities')) if sp.get('wnv_municipalities') is not None else None, "Zonas/municipios WNV", True)}
      {stat_box((str(sp.get('dengue_local')) if sp.get('dengue_local') is not None else None), "Dengue autóctono", True)}
      {stat_box((str(sp.get('dengue_imported')) if sp.get('dengue_imported') is not None else None), "Dengue importado", True)}
      {stat_box((str(sp.get('cchf_cases')) if sp.get('cchf_cases') is not None else None), "CCHF – casos", True)}
      {stat_box((f"{sp.get('sars_pos')}%" if sp.get('sars_pos') is not None else None), "SARS-CoV-2 – positividad", True)}
    </div>
    <div class="key-points">
      <ul>{li(summary.get("España – visión detallada", []))}</ul>
    </div>
    <div class="risk-tag">VIGILANCIA ACTIVA</div>
  </div>

  <!-- UE/EEE -->
  <div class="card full-width">
    <h2>Panorama UE/EEE</h2>
    <div class="stat-grid">
      {stat_box((str(eu.get('wnv_countries')) if eu.get('wnv_countries') is not None else None), "Países con WNV")}
      {stat_box((str(eu.get('wnv_total')) if eu.get('wnv_total') is not None else None), "WNV – total casos UE/EEE")}
      {stat_box((str(eu.get('dengue_auto_countries')) if eu.get('dengue_auto_countries') is not None else None), "Países con dengue autóctono")}
      {stat_box((str(eu.get('dengue_auto_total')) if eu.get('dengue_auto_total') is not None else None), "Dengue autóctono – total casos")}
      {stat_box((str(eu.get('cchf_countries')) if eu.get('cchf_countries') is not None else None), "Países con CCHF")}
      {stat_box((f"{eu.get('sars_pos')}%" if eu.get('sars_pos') is not None else None), "SARS-CoV-2 – positividad UE/EEE")}
    </div>
    <div class="key-points">
      <ul>{li(summary.get("Panorama UE/EEE", []))}</ul>
    </div>
  </div>

  <!-- Recomendaciones -->
  <div class="card full-width">
    <h2>Recomendaciones operativas</h2>
    <div class="key-points">
      <ul>{li(summary.get("Recomendaciones operativas", []))}</ul>
    </div>
    <p class="note">Adaptar a protocolos autonómicos y al contexto asistencial local.</p>
  </div>

  <!-- Trazabilidad -->
  <div class="card full-width">
    <h2>Notas de trazabilidad</h2>
    <p class="small">Frases fuente extraídas del PDF (para validar cifras):</p>
    <div class="key-points">{summary.get("evidence_html", "") or "<em>Sin evidencias capturadas.</em>"}</div>
  </div>
</div>

<div class="footer" style="text-align:center; margin-top:30px; padding-top:20px; border-top:1px solid #eaeaea; color:#666; font-size:.9rem;">
  <p>Resumen generado el: {gen_date_es}</p>
  <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
  <p>Este es un resumen automático en español. Para interpretación clínica, consulte el informe completo.</p>
</div>
</body>
</html>'''
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

        # Versión texto plano (muy básica, quitando tags)
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

        # Anti-duplicados por hash
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

        # Resumen extendido
        try:
            if text:
                data = self.extract_key_data(text)
                summary = self.generate_spanish_summary_rico(data, week, year)
            else:
                summary = self.generate_spanish_summary_rico({}, week, year)
        except Exception as e:
            logging.error("Error generando resumen: %s", e)
            summary = {"Resumen Ejecutivo": ["No se pudo generar el resumen automático."], "data": {}, "evidence_html": ""}

        # HTML y asunto robustos
        html = self.build_spanish_html(week, year, pdf_url, article_url, summary)
        subject = f"ECDC CDTR – Resumen Semana {week or 'N/D'} ({year or 'N/D'}) – Español"

        # Envío y guardado de estado
        try:
            self.send_email(subject, html)
            # Guardamos estado solo si el envío no falló
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

