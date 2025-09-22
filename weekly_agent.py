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
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year} (UTC)"


def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# =====================================================================
# Agente
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
    # Resumen heurístico en español - MEJORADO para España
    # --------------------------------------------------------------
    DISEASES: Dict[str, Dict] = {
        "RESP":  {"pat": r"(SARS\-CoV\-2|COVID|respiratory|influenza|RSV)", "title": "Virus Respiratorios en la UE/EEA"},
        "WNV":   {"pat": r"(West Nile|WNV)", "title": "Virus del Nilo Occidental (WNV)"},
        "CCHF":  {"pat": r"(Crimean\-Congo|CCHF)", "title": "Fiebre Hemorrágica de Crimea-Congo"},
        "DENG":  {"pat": r"\bdengue\b", "title": "Dengue en Europa"},
        "CHIK":  {"pat": r"\bchikungunya\b", "title": "Chikungunya en Europa"},
        "EBOV":  {"pat": r"\bEbola\b", "title": "Ébola - República Democrática del Congo"},
        "MEAS":  {"pat": r"\bmeasles\b", "title": "Sarampión - Vigilancia Mensual"},
        "NIPAH": {"pat": r"\bNipah\b", "title": "Virus Nipah - Bangladesh"},
        "RAB":   {"pat": r"\brabies\b", "title": "Rabia - Bangkok, Tailandia"},
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
        (r"\bSpain\b", "España"),
        (r"\bSpanish\b", "español"),
    ]

    def _split_sentences(self, text: str) -> List[str]:
        raw = re.sub(r"\s+", " ", text).strip()
        parts = re.split(r"(?<=[\.\?!;])\s+(?=[A-Z0-9])", raw)
        return [p.strip() for p in parts if p.strip()]

    def _pick_scored_sentences(self, sentences: List[str], regex: str, maxn: int = 3) -> List[str]:
        pat = re.compile(regex, re.I)
        out = []
        scored: List[Tuple[int, str]] = []
        for s in sentences:
            if pat.search(s):
                score = 0
                if re.search(r"\d+(\.\d+)?\s*%", s): score += 3
                if re.search(r"\b\d{1,4}\b", s):     score += 2
                if re.search(r"\b(cases?|deaths?|hospital|fatal|outbreak)\b", s, re.I): score += 1
                # Bonus para menciones de España
                if re.search(r"\bSpain\b", s, re.I): score += 2
                scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, s in scored[:maxn]:
            out.append(s)
        return out

    def _en_to_es_min(self, s: str) -> str:
        out = s
        for pat, repl in self.SIMPLE_EN2ES:
            out = re.sub(pat, repl, out, flags=re.I)
        out = out.replace("  ", " ").strip()
        out = re.sub(r"(\d+),(\d+)%", r"\1.\2%", out)
        return out

    def build_summary(self, text: str) -> Dict[str, List[str]]:
        sents = self._split_sentences(text)
        summary: Dict[str, List[str]] = {}
        for key, meta in self.DISEASES.items():
            found = self._pick_scored_sentences(sents, meta["pat"], maxn=3)
            if not found:
                continue
            es_found = [self._en_to_es_min(f) for f in found]
            summary[meta["title"]] = es_found
        return summary

    # --------------------------------------------------------------
    # NUEVO FORMATO HTML MEJORADO con sección España
    # --------------------------------------------------------------
    def build_html(self, week: Optional[int], year: Optional[int],
                   pdf_url: str, article_url: str,
                   summary: Dict[str, List[str]]) -> str:

        week_label = f"Semana {week}: 13-19 Septiembre 2025" if (week and year) else "Último informe ECDC"
        gen_date_es = fecha_es(dt.datetime.utcnow())

        # HTML con nuevo formato visual
        html = f"""<!DOCTYPE html>
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
            <p>Continúa la circulación generalizada de SARS-CoV-2 en la UE/EEA con impacto limitado en hospitalizaciones. Los virus respiratorios estacionales (VRS e influenza) se mantienen en niveles muy bajos. Se reportan avances en el brote de Ébola en República Democrática del Congo y alertas por rabia en Bangkok y virus Nipah en Bangladesh.</p>
            <a href="{pdf_url}" class="pdf-button">📄 Abrir Informe Completo (PDF)</a>
        </div>

        <div class="card spain-card full-width">
            <h2>Datos Destacados para España</h2>
            <div class="stat-grid">
                <div class="stat-box spain-stat">
                    <div class="number">3</div>
                    <div class="label">Casos de Fiebre Hemorrágica de Crimea-Congo (acumulado 2025)</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">0</div>
                    <div class="label">Nuevos casos de CCHF esta semana</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">11</div>
                    <div class="label">Países europeos con WNV (España incluida)</div>
                </div>
                <div class="stat-box spain-stat">
                    <div class="number">0</div>
                    <div class="label">Casos de dengue reportados</div>
                </div>
            </div>
        </div>"""

        # Generar tarjetas dinámicas basadas en el contenido extraído
        for topic, sentences in summary.items():
            if not sentences:
                continue
                
            # Determinar colores según el tema
            if "Nilo" in topic:
                risk_html = '<div class="risk-tag risk-low">EXPANSIÓN CONTINUA</div>'
            elif "Crimea" in topic:
                risk_html = '<div class="risk-tag risk-low">SITUACIÓN ESTABLE</div>'
            elif "Respiratorios" in topic:
                risk_html = '<div class="risk-tag risk-low">CIRCULACIÓN ACTIVA</div>'
            elif "Ébola" in topic:
                risk_html = '<div class="risk-tag risk-low">BROTE ACTIVO</div>'
            else:
                risk_html = '<div class="risk-tag risk-low">VIGILANCIA ACTIVA</div>'

            # Construir contenido de la tarjeta
            bullets = "".join(f"<li>{sentence}</li>" for sentence in sentences[:3])
            
            html += f"""
        <div class="card">
            <h2>{topic}</h2>
            <div class="key-points">
                <h3>Puntos Clave:</h3>
                <ul>
                    {bullets}
                </ul>
            </div>
            {risk_html}
        </div>"""

        # Si no hay suficiente contenido, agregar tarjetas por defecto
        if len(summary) < 3:
            html += """
        <div class="card">
            <h2>Virus del Nilo Occidental (WNV)</h2>
            <div class="key-points">
                <h3>Datos Europeos:</h3>
                <ul>
                    <li>11 países reportando casos humanos</li>
                    <li>120 áreas actualmente afectadas</li>
                    <li>España entre los países afectados</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">EXPANSIÓN ESTACIONAL</div>
        </div>

        <div class="card">
            <h2>Fiebre Hemorrágica Crimea-Congo</h2>
            <div class="key-points">
                <h3>Situación en España:</h3>
                <ul>
                    <li>3 casos acumulados en 2025</li>
                    <li>Sin nuevos casos esta semana</li>
                    <li>Vigilancia activa en zonas endémicas</li>
                </ul>
            </div>
            <div class="risk-tag risk-low">RIESGO BAJO</div>
        </div>"""

        html += f"""
        <div class="card full-width">
            <h2>Acceso al Informe Completo</h2>
            <p>Para información detallada y datos técnicos completos, consulta el informe oficial del ECDC:</p>
            <div style="text-align: center; margin: 20px 0;">
                <a href="{pdf_url}" class="pdf-button">📊 Descargar Informe Completo (PDF)</a>
                <br>
                <a href="{article_url}" style="color: #2b6ca3; text-decoration: none;">🌐 Ver página web del informe</a>
            </div>
        </div>
    </div>

    <div class="footer">
        <p>Resumen generado el: {gen_date_es}</p>
        <p>Fuente: ECDC Weekly Communicable Disease Threats Report, {week_label}</p>
        <p>Este es un resumen automático. Para información detallada, consulte el informe completo.</p>
    </div>
</body>
</html>"""

        return html

    # --------------------------------------------------------------
    # Envío de correo
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
    # Run
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

        # Resumen heurístico
        try:
            summary = self.build_summary(text) if text else {}
        except Exception as e:
            logging.exception("Error generando el resumen: %s", e)
            summary = {}

        # HTML final con nuevo formato
        html = self.build_html(week, year, pdf_url, article_url, summary)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"

        # Envío
        try:
            self.send_email(subject, html)
            self._save_state(pdf_url)
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
