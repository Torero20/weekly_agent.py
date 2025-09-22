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


# =========================
# Config
# =========================

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
    html_template_path = os.getenv("HTML_TEMPLATE_PATH", "").strip()  # opcional


# =========================
# Utils
# =========================

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}

def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year}"

def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def ul(items: List[str]) -> str:
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


# =========================
# Agent
# =========================

class WeeklyReportAgent:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        logging.basicConfig(
            level=getattr(logging, cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = self._build_session()

    # ---------- Networking / ECDC ----------
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
        for sel in ['time[datetime]', 'meta[property="article:published_time"]',
                    'meta[name="date"]', 'meta[name="pubdate"]']:
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

    # ---------- State & download ----------
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

    # ---------- Extraction (with evidence) ----------
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
        data: Dict[str, Any] = {"spain": {}, "eu": {}, "evidence": {}, "topics": {}}
        tl = (text or "").lower()

        # WNV EU
        m = re.search(r'west nile[^.]*?\b(\d{1,3})\b[^.]*?\bcountr', tl)
        if m:
            data["eu"]["wnv_countries"] = int(m.group(1))
            ev = self._capture_sentence(tl, "west nile")
            if ev: data["evidence"]["wnv_countries"] = ev

        # WNV Spain
        m = re.search(r'spain[^.]*west nile[^.]*?(\d{1,4})[^.]*case', tl)
        if m:
            data["spain"]["wnv_cases"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_wnv"] = ev
        m = re.search(r'spain[^.]*west nile[^.]*?(\d{1,3})[^.]*?(?:municipal|area|zone)', tl)
        if m:
            data["spain"]["wnv_municipalities"] = int(m.group(1))

        # Dengue EU (autóctono total / países)
        m = re.search(r'(?:eu|europe|\bue\b)[^.]*?dengue[^.]*?(\d{1,6})[^.]*?cases', tl)
        if m:
            data["eu"]["dengue_auto_total"] = int(m.group(1))
            ev = self._capture_sentence(tl, "dengue")
            if ev: data["evidence"]["dengue_eu"] = ev
        m = re.search(r'(?:autochthonous|local)\s+dengue[^.]*?in\s+(\d{1,3})\s+(?:countries|member states)', tl)
        if m:
            data["eu"]["dengue_auto_countries"] = int(m.group(1))

        # Dengue Spain (autóctono / importado)
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

        # CCHF Spain
        m = re.search(r'spain[^.]*?(?:crimean|crimea).*?(?:congo|cchf)[^.]*?(\d{1,4})[^.]*?case', tl)
        if m:
            data["spain"]["cchf_cases"] = int(m.group(1))
            ev = self._capture_sentence(tl, "spain")
            if ev: data["evidence"]["spain_cchf"] = ev
        # CCHF EU countries
        m = re.search(r'(?:eu|europe|\bue\b)[^.]*?(?:crimean|crimea).*?(?:congo|cchf)[^.]*?(\d{1,3})[^.]*?countr', tl)
        if m:
            data["eu"]["cchf_countries"] = int(m.group(1))

        # SARS-CoV-2 positivity (generic)
        m = re.search(r'sars[- ]?cov[- ]?2[^%]*?(\d{1,2}(?:\.\d+)?)\s*%', tl)
        if m:
            data["spain"]["sars_pos"] = float(m.group(1))
            data["evidence"]["sars_pos"] = self._capture_sentence(tl, "sars-cov-2") or ""

        # Ebola (DRC) – cases/deaths (best-effort)
        eb_cases = re.search(r'ebola[^.]*?(\d{1,4})[^.]*?cases', tl)
        eb_deaths = re.search(r'ebola[^.]*?(\d{1,4})[^.]*?deaths', tl)
        if eb_cases or eb_deaths:
            data["topics"]["ebola"] = {
                "cases": int(eb_cases.group(1)) if eb_cases else None,
                "deaths": int(eb_deaths.group(1)) if eb_deaths else None,
            }
            ev = self._capture_sentence(tl, "ebola")
            if ev: data["evidence"]["ebola"] = ev

        # Nipah (Bangladesh)
        ni_deaths = re.search(r'nipah[^.]*?(\d{1,4})[^.]*?deaths', tl)
        if ni_deaths:
            data["topics"]["nipah"] = {"deaths": int(ni_deaths.group(1))}
            ev = self._capture_sentence(tl, "nipah")
            if ev: data["evidence"]["nipah"] = ev

        # Rabies (Bangkok)
        if "rabies" in tl and "bangkok" in tl:
            data["topics"]["rabies_bkk"] = True
            ev = self._capture_sentence(tl, "rabies")
            if ev: data["evidence"]["rabies_bkk"] = ev

        # Chikungunya (EU – very best-effort)
        if "chikungunya" in tl:
            data["topics"]["chik"] = True
            ev = self._capture_sentence(tl, "chikungunya")
            if ev: data["evidence"]["chik"] = ev

        return data

    # ---------- Summary (ES focus + EU context) ----------
    def generate_spanish_summary_rico(self, data: Dict[str, Any],
                                      week: Optional[int], year: Optional[int]) -> Dict[str, List[str]]:
        w = week or "N/D"; y = year or "N/D"
        summary: Dict[str, List[str]] = {}

        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}
        ev = data.get("evidence", {}) if isinstance(data.get("evidence"), dict) else {}
        topics = data.get("topics", {}) if isinstance(data.get("topics"), dict) else {}

        # Resumen ejecutivo
        exec_points = [f"Informe semanal ECDC – Semana {w} de {y}.",
                       "Foco en España con contexto comparado UE/EEE y evidencias textuales."]
        if sp.get("wnv_cases") is not None:
            fr = f"WNV (España): {sp['wnv_cases']} caso(s)"
            if sp.get("wnv_municipalities"): fr += f" en {sp['wnv_municipalities']} zona(s)"
            exec_points.append(fr + ".")
        if (sp.get("dengue_local") is not None) or (sp.get("dengue_imported") is not None):
            frs = []
            if sp.get("dengue_local") is not None: frs.append(f"autóctonos {sp['dengue_local']}")
            if sp.get("dengue_imported") is not None: frs.append(f"importados {sp['dengue_imported']}")
            exec_points.append("Dengue (España): " + ", ".join(frs) + ".")
        if sp.get("cchf_cases") is not None:
            exec_points.append(f"CCHF (España): {sp['cchf_cases']} caso(s).")
        if eu.get("wnv_countries") is not None:
            exec_points.append(f"WNV (UE/EEE): {eu['wnv_countries']} países con transmisión.")
        summary["Resumen Ejecutivo"] = exec_points

        # España – visión detallada
        es_points: List[str] = []
        if sp.get("wnv_cases") is not None:
            msg = f"WNV: {sp['wnv_cases']} caso(s)"
            if sp.get("wnv_municipalities"): msg += f" en {sp['wnv_municipalities']} municipio(s)/zona(s)"
            msg += ". Implicaciones: control vectorial local y cribado en hemoderivados según protocolos."
            es_points.append(msg)
        if (sp.get("dengue_local") is not None) or (sp.get("dengue_imported") is not None):
            dl = sp.get("dengue_local"); di = sp.get("dengue_imported")
            frag = []
            if dl is not None: frag.append(f"autóctonos={dl}")
            if di is not None: frag.append(f"importados={di}")
            es_points.append("Dengue: " + ", ".join(frag) + ". Relevancia: vigilancia de Aedes, triaje de fiebre post-viaje y notificación ágil.")
        if sp.get("cchf_cases") is not None:
            es_points.append(f"CCHF: {sp['cchf_cases']} caso(s). Riesgo ocupacional en entornos rurales/ganaderos; EPI y educación sanitaria.")
        if sp.get("sars_pos") is not None:
            es_points.append(f"Respiratorios: SARS-CoV-2 positividad ≈ {sp['sars_pos']}%. Mantener vigilancia y circuitos asistenciales.")
        if not es_points:
            es_points.append("Sin indicadores específicos detectados para España esta semana; mantener vigilancia de rutina.")
        summary["España – visión detallada"] = es_points

        # UE/EEE
        eu_points: List[str] = []
        if eu.get("wnv_countries") is not None:
            eu_points.append(f"WNV (UE/EEE): {eu['wnv_countries']} países con transmisión. Patrón estacional con picos verano-otoño.")
        if eu.get("dengue_auto_countries") is not None or eu.get("dengue_auto_total") is not None:
            fr = []
            if eu.get("dengue_auto_countries") is not None:
                fr.append(f"autóctono en {eu['dengue_auto_countries']} país(es)")
            if eu.get("dengue_auto_total") is not None:
                fr.append(f"≈ {eu['dengue_auto_total']} casos")
            eu_points.append("Dengue (UE/EEE): " + "; ".join(fr) + ". Expansión vectorial ligada al clima; vigilancia transfronteriza.")
        if eu.get("cchf_countries") is not None:
            eu_points.append(f"CCHF (UE/EEE): circulación en {eu['cchf_countries']} país(es). Riesgo bajo-moderado general.")
        if eu.get("sars_pos") is not None:
            eu_points.append(f"Respiratorios: positividad SARS-CoV-2 (UE/EEE) ≈ {eu['sars_pos']}%.")
        if not eu_points:
            eu_points.append("Sin señales pan-europeas destacables adicionales esta semana.")
        summary["Panorama UE/EEE"] = eu_points

        # Recomendaciones
        summary["Recomendaciones operativas"] = [
            "Comunicación rápida con salud pública ante fiebre sin foco con viaje/picadura.",
            "Control vectorial local (aguas estancadas) y mensajes a población en zonas de riesgo.",
            "Cribado transfusional y de trasplantes según transmisión WNV en el área de captación.",
            "Laboratorio: circuito de notificación ágil y paneles sindrómicos en picos estacionales."
        ]

        # Alertas (otros tópicos si se detectan)
        alertas: List[str] = []
        if "ebola" in topics:
            c = topics["ebola"].get("cases"); d = topics["ebola"].get("deaths")
            frag = "Ébola (RDC): "
            if c is not None: frag += f"{c} casos"
            if d is not None: frag += f" – {d} muertes"
            alertas.append(frag)
        if "rabies_bkk" in topics:
            alertas.append("Rabia – Bangkok: alerta local; evitar contacto con animales callejeros.")
        if "nipah" in topics:
            d = topics["nipah"].get("deaths")
            alertas.append(f"Virus Nipah – Bangladesh: {d} muertes reportadas." if d else "Virus Nipah – Bangladesh: actualización de brote.")
        if "chik" in topics:
            alertas.append("Chikungunya – Europa: transmisión local activa (datos país-dependientes).")
        if alertas:
            summary["Alertas y monitoreo activo"] = alertas

        # Evidencias
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

    # ---------- HTML rendering ----------
    def _default_html(self, week: Optional[int], year: Optional[int],
                      pdf_url: str, article_url: str,
                      summary: Dict[str, List[str]]) -> str:
        """
        Plantilla integrada inspirada en tu HTML (mismo look&feel y secciones),
        pero 100% dinámica. Se muestran solo datos disponibles (sin inventar).
        """
        week_label = f"Semana {week}, {year}" if week and year else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())

        data = summary.get("data", {}) if isinstance(summary.get("data"), dict) else {}
        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}

        def stat_box(number: Optional[str], label: str, accent: bool=False) -> str:
            if number is None: number = "–"
            cls = "spain-stat" if accent else ""
            return f'''<div class="stat-box {cls}">
                <div class="number">{number}</div>
                <div class="label">{label}</div>
            </div>'''

        # Grids dinámicos
        es_grid = "".join([
            stat_box(str(sp.get('cchf_cases')) if sp.get('cchf_cases') is not None else None,
                     "CCHF – casos (España)", True),
            stat_box(str(sp.get('wnv_cases')) if sp.get('wnv_cases') is not None else None,
                     "WNV – casos (España)", True),
            stat_box(str(sp.get('wnv_municipalities')) if sp.get('wnv_municipalities') is not None else None,
                     "WNV – zonas/municipios", True),
            stat_box(str(sp.get('dengue_local')) if sp.get('dengue_local') is not None else None,
                     "Dengue autóctono (España)", True),
            stat_box(str(sp.get('dengue_imported')) if sp.get('dengue_imported') is not None else None,
                     "Dengue importado (España)", True),
            stat_box((f"{sp.get('sars_pos')}%" if sp.get('sars_pos') is not None else None),
                     "SARS-CoV-2 – positividad", True),
        ])

        eu_grid = "".join([
            stat_box(str(eu.get('wnv_countries')) if eu.get('wnv_countries') is not None else None,
                     "Países con WNV (UE/EEE)"),
            stat_box(str(eu.get('dengue_auto_countries')) if eu.get('dengue_auto_countries') is not None else None,
                     "Países con dengue autóctono"),
            stat_box(str(eu.get('dengue_auto_total')) if eu.get('dengue_auto_total') is not None else None,
                     "Dengue autóctono – total UE/EEE"),
            stat_box(str(eu.get('cchf_countries')) if eu.get('cchf_countries') is not None else None,
                     "Países con CCHF"),
        ])

        resumen_exec = ul(summary.get("Resumen Ejecutivo", []))
        es_bullets = ul(summary.get("España – visión detallada", []))
        eu_bullets = ul(summary.get("Panorama UE/EEE", []))
        alertas_ul = ul(summary.get("Alertas y monitoreo activo", [])) if summary.get("Alertas y monitoreo activo") else ""

        # HTML (mismo estilo visual que tu ejemplo)
        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Resumen Semanal ECDC - {week_label}</title>
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
    <div class="week">{week_label}</div>
  </div>

  <div class="container">
    <div class="card full-width">
      <h2>Resumen Ejecutivo</h2>
      <p>Informe de vigilancia epidemiológica con foco en España y comparativa UE/EEE.</p>
      <a href="{pdf_url}" class="pdf-button">📄 Abrir Informe Completo (PDF)</a>
      <a href="{article_url}" class="pdf-button" style="background:#125e2a">🌐 Ver página del informe</a>
      <div class="key-points">{resumen_exec}</div>
    </div>

    <div class="card spain-card full-width">
      <h2>Datos Destacados para España</h2>
      <div class="stat-grid">{es_grid}</div>
      <div class="key-points"><h3>España – lectura clínica/operativa</h3>{es_bullets}</div>
      <div class="risk-tag risk-low">VIGILANCIA ACTIVA</div>
    </div>

    <div class="card full-width">
      <h2>Panorama UE/EEE</h2>
      <div class="stat-grid">{eu_grid}</div>
      <div class="key-points"><h3>Puntos Clave</h3>{eu_bullets}</div>
      <div class="risk-tag risk-low">ESTACIONAL / HETEROGÉNEO</div>
    </div>

    {"<div class='card full-width'><h2>Resumen de Alertas y Monitoreo Activo</h2><ul class='topic-list'>" + "".join(f"<li>{x}</li>" for x in summary.get("Alertas y monitoreo activo", [])) + "</ul></div>" if summary.get("Alertas y monitoreo activo") else ""}

    <div class="card full-width">
      <h2>Recomendaciones operativas</h2>
      <div class="key-points">{recomendaciones}</div>
    </div>

    <div class="card full-width">
      <h2>Notas de trazabilidad</h2>
      <div class="key-points">{evidencias}</div>
    </div>
  </div>

  <div class="footer">
    <p>Resumen generado el: {gen_date_es}</p>
    <p>Fuente: ECDC Weekly Communicable Disease Threats Report</p>
    <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
  </div>
</body>
</html>""".format(
            week_label=week_label,
            pdf_url=pdf_url,
            article_url=article_url,
            resumen_exec=ul(summary.get("Resumen Ejecutivo", [])),
            es_grid=es_grid or "<em>Sin datos específicos de España capturados.</em>",
            es_bullets=ul(summary.get("España – visión detallada", [])),
            eu_grid=eu_grid or "<em>Sin datos comparativos UE/EEE capturados.</em>",
            eu_bullets=ul(summary.get("Panorama UE/EEE", [])),
            recomendaciones=ul(summary.get("Recomendaciones operativas", [])),
            evidencias=(summary.get("evidence_html", "") or "<em>Sin evidencias capturadas.</em>"),
            gen_date_es=gen_date_es
        )
        return html

    def _render_with_external_template(self, template_text: str,
                                       week: Optional[int], year: Optional[int],
                                       pdf_url: str, article_url: str,
                                       summary: Dict[str, List[str]]) -> str:
        """
        Sustituye placeholders si existen. Si faltan, simplemente deja el trozo original.
        """
        week_label = f"Semana {week}, {year}" if week and year else "Último informe"
        gen_date_es = fecha_es(dt.datetime.utcnow())

        data = summary.get("data", {}) if isinstance(summary.get("data"), dict) else {}
        sp = data.get("spain", {}) if isinstance(data.get("spain"), dict) else {}
        eu = data.get("eu", {}) if isinstance(data.get("eu"), dict) else {}

        def stat_box(number: Optional[str], label: str, accent: bool=False) -> str:
            if number is None: number = "–"
            cls = "spain-stat" if accent else ""
            return f'''<div class="stat-box {cls}">
                <div class="number">{number}</div>
                <div class="label">{label}</div>
            </div>'''

        es_grid = "".join([
            stat_box(str(sp.get('cchf_cases')) if sp.get('cchf_cases') is not None else None, "CCHF – casos (España)", True),
            stat_box(str(sp.get('wnv_cases')) if sp.get('wnv_cases') is not None else None, "WNV – casos (España)", True),
            stat_box(str(sp.get('wnv_municipalities')) if sp.get('wnv_municipalities') is not None else None, "WNV – zonas/municipios", True),
            stat_box(str(sp.get('dengue_local')) if sp.get('dengue_local') is not None else None, "Dengue autóctono (España)", True),
            stat_box(str(sp.get('dengue_imported')) if sp.get('dengue_imported') is not None else None, "Dengue importado (España)", True),
            stat_box((f"{sp.get('sars_pos')}%" if sp.get('sars_pos') is not None else None), "SARS-CoV-2 – positividad", True),
        ])

        eu_grid = "".join([
            stat_box(str(eu.get('wnv_countries')) if eu.get('wnv_countries') is not None else None, "Países con WNV (UE/EEE)"),
            stat_box(str(eu.get('dengue_auto_countries')) if eu.get('dengue_auto_countries') is not None else None, "Países con dengue autóctono"),
            stat_box(str(eu.get('dengue_auto_total')) if eu.get('dengue_auto_total') is not None else None, "Dengue autóctono – total UE/EEE"),
            stat_box(str(eu.get('cchf_countries')) if eu.get('cchf_countries') is not None else None, "Países con CCHF"),
        ])

        alertas_ul = ul(summary.get("Alertas y monitoreo activo", [])) if summary.get("Alertas y monitoreo activo") else ""

        replacements = {
            "{{WEEK_LABEL}}": week_label,
            "{{PDF_URL}}": pdf_url,
            "{{ARTICLE_URL}}": article_url,
            "{{GEN_DATE}}": gen_date_es,
            "{{RESUMEN_EJECUTIVO_UL}}": ul(summary.get("Resumen Ejecutivo", [])),
            "{{ES_STAT_GRID}}": es_grid or "<em>Sin datos específicos de España.</em>",
            "{{ES_BULLETS_UL}}": ul(summary.get("España – visión detallada", [])),
            "{{EU_STAT_GRID}}": eu_grid or "<em>Sin datos UE/EEE.</em>",
            "{{EU_BULLETS_UL}}": ul(summary.get("Panorama UE/EEE", [])),
            "{{ALERTAS_UL}}": alertas_ul,
            "{{EVIDENCIAS_HTML}}": (summary.get("evidence_html", "") or "<em>Sin evidencias capturadas.</em>"),
        }
        out = template_text
        for k, v in replacements.items():
            out = out.replace(k, v)
        return out

    def build_html(self, week: Optional[int], year: Optional[int],
                   pdf_url: str, article_url: str,
                   summary: Dict[str, List[str]]) -> str:
        """
        Si hay plantilla externa (con placeholders), úsala.
        En otro caso, usa la plantilla integrada (look&feel igual al que pasaste).
        """
        path = self.cfg.html_template_path
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    template_text = f.read()
                return self._render_with_external_template(template_text, week, year, pdf_url, article_url, summary)
            except Exception as e:
                logging.warning("No se pudo usar la plantilla externa (%s). Se usa la integrada.", e)
        return self._default_html(week, year, pdf_url, article_url, summary)

    # ---------- Email ----------
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

    # ---------- Run ----------
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

        try:
            if text:
                data = self.extract_key_data(text)
                summary = self.generate_spanish_summary_rico(data, week, year)
            else:
                summary = self.generate_spanish_summary_rico({}, week, year)
        except Exception as e:
            logging.error("Error generando resumen: %s", e)
            summary = {"Resumen Ejecutivo": ["No se pudo generar el resumen automático."], "data": {}, "evidence_html": ""}

        html = self.build_html(week, year, pdf_url, article_url, summary)
        subject = f"ECDC CDTR – Resumen Semana {week or 'N/D'} ({year or 'N/D'}) – Español"

        try:
            self.send_email(subject, html)
            self._save_state(pdf_url, pdf_hash or "")
            logging.info("Resumen en español enviado exitosamente")
        except Exception as e:
            logging.error("Error enviando correo: %s", e)


# =========================
# main
# =========================

def main() -> None:
    cfg = Config()
    WeeklyReportAgent(cfg).run()

if __name__ == "__main__":
    main()

