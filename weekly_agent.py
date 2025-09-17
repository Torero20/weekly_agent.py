#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
weekly_agent.py
- Localiza el último "Communicable disease threats report" (ECDC).
- Construye un email HTML visual (tarjetas, chips, tabla WNV, foco España).
- Envía el correo por SMTP (SSL 465 o STARTTLS).

Variables de entorno esperadas:
  SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, EMAIL_PASSWORD, RECEIVER_EMAIL
Opcionales:
  LOG_LEVEL=INFO|DEBUG
  DRY_RUN=1  (no envía correo, solo logs)
"""

import os
import re
import ssl
import smtplib
import logging
import datetime as dt
from typing import Optional, Tuple, List
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
from urllib.parse import unquote


# ----------------------------- LOGGING -----------------------------

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ----------------------------- FETCH -------------------------------

class ECDCClient:
    """
    Localiza el enlace al PDF más reciente del CDTR.
    - Primero explora la página de listados del CDTR.
    - Abre el primer artículo y toma su PDF.
    - Intenta deducir semana y año desde URL/título.
    """

    LIST_URL = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    @staticmethod
    def _parse_week_year_from_text(s: str) -> Tuple[Optional[int], Optional[int]]:
        s = unquote(s or "").lower()
        mw = re.search(r"week(?:[\s_\-]?)(\d{1,2})", s)
        wy = int(mw.group(1)) if mw else None
        my = re.search(r"(20\d{2})", s)
        yy = int(my.group(1)) if my else None
        return wy, yy

    def fetch_latest_pdf(self) -> Tuple[str, Optional[int], Optional[int], str]:
        """
        Returns:
          pdf_url, week, year, article_url
        Raises:
          RuntimeError si no encuentra el PDF.
        """
        r = self.session.get(self.LIST_URL, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        article_url = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                article_url = href if href.startswith("http") else ("https://www.ecdc.europa.eu" + href)
                break
        if not article_url:
            raise RuntimeError("No encuentro el artículo CDTR más reciente.")

        ar = self.session.get(article_url, timeout=25)
        ar.raise_for_status()
        asoup = BeautifulSoup(ar.text, "html.parser")

        pdf_url = None
        for a in asoup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                pdf_url = a["href"] if a["href"].startswith("http") else ("https://www.ecdc.europa.eu" + a["href"])
                break
        if not pdf_url:
            raise RuntimeError("Artículo CDTR sin PDF.")

        # Intentar deducir semana y año
        t = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
        week, year = self._parse_week_year_from_text(t)

        logging.info("PDF más reciente: %s (semana=%s, año=%s)", pdf_url, week, year)
        return pdf_url, week, year, article_url


# ----------------------------- HTML ---------------------------------

def build_html_email(pdf_url: str,
                     article_url: str,
                     week: Optional[int],
                     year: Optional[int]) -> str:
    """
    Construye el HTML del correo (estilos inline, tarjetas, chips, tabla WNV).
    Las cifras del contenido están precargadas para el CDTR semana 37 (ejemplo visual).
    En el siguiente paso podemos automatizar su extracción desde el PDF.
    """
    title_week = "Semana {} · {}".format(week, year) if week and year else "Último informe ECDC"

    # Fechas de referencia (ejemplo semana 37) – opcional/visual
    period_label = "ECDC · Semana 37 · 6–12 septiembre 2025" if week == 37 and year == 2025 else title_week

    # HTML con placeholders formateados (sin f-strings anidadas)
    html = (
        "<html>"
        "<body style='margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#222;'>"
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='padding:20px 12px;background:#f5f7fb;'>"
        "<tr><td align='center'>"
        "<table role='presentation' width='760' cellspacing='0' cellpadding='0' style='max-width:760px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 14px rgba(0,0,0,.06)'>"

        # Header
        "<tr><td style='background:#0b5cab;color:#fff;padding:18px 22px'>"
        "<div style='font-size:22px;font-weight:800'>Boletín semanal de amenazas infecciosas</div>"
        "<div style='opacity:.95;font-size:13px;margin-top:2px'>{period}</div>"
        "</td></tr>"

        # Titulares/cards (WNV, CCHF, respiratorios)
        "<tr><td style='padding:0 18px'>"

        # WNV
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #2e7d32;background:#f0f7f2;border-radius:10px'>"
        "<tr><td style='padding:12px 14px'>"
        "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#2e7d32;text-transform:uppercase;margin-bottom:4px'>Virus del Nilo Occidental</div>"
        "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>652 casos humanos y 38 muertes en Europa (acumulado a 3-sep)</div>"
        "<div style='font-size:14px;color:#333;opacity:.95'>Italia concentra la mayoría de casos; "
        "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 5 casos humanos y 3 brotes en équidos/aves</span>."
        "</div></td></tr></table>"

        # CCHF
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #d32f2f;background:#fbf1f1;border-radius:10px'>"
        "<tr><td style='padding:12px 14px'>"
        "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#d32f2f;text-transform:uppercase;margin-bottom:4px'>Fiebre Crimea-Congo (CCHF)</div>"
        "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>Sin nuevos casos esta semana</div>"
        "<div style='font-size:14px;color:#333;opacity:.95'>"
        "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 3 casos en 2025</span>; "
        "Grecia 2 casos. Riesgo bajo en general, mayor en áreas con garrapatas."
        "</div></td></tr></table>"

        # Respiratorios
        "<table role='presentation' width='100%' cellspacing='0' cellpadding='0' style='margin:12px 0;border-left:6px solid #1565c0;background:#eef4fb;border-radius:10px'>"
        "<tr><td style='padding:12px 14px'>"
        "<div style='font-size:12px;font-weight:700;letter-spacing:.3px;color:#1565c0;text-transform:uppercase;margin-bottom:4px'>Respiratorios</div>"
        "<div style='font-size:16px;font-weight:800;color:#0b5cab;margin-bottom:4px'>COVID-19 al alza en detección, con bajo impacto hospitalario; Influenza y VRS en niveles bajos</div>"
        "<div style='font-size:14px;color:#333;opacity:.95'>"
        "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>: descenso de positividad SARI por SARS-CoV-2."
        "</div></td></tr></table>"

        "</td></tr>"

        # Puntos clave (chips a color)
        "<tr><td style='padding:6px 18px 4px'>"
        "<div style='font-weight:800;color:#333;margin:10px 0 8px'>Puntos clave</div>"
        "<ul style='padding-left:0;margin:0'>"

        "<li style='list-style:none;margin:8px 0'>"
        "<div style='border-left:6px solid #2e7d32;padding-left:10px'>"
        "<span style='background:#2e7d32;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>WNV</span>"
        "Expansión estacional en 9 países; mortalidad global ~6%."
        "</div></li>"

        "<li style='list-style:none;margin:8px 0'>"
        "<div style='border-left:6px solid #ef6c00;padding-left:10px'>"
        "<span style='background:#ef6c00;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Dengue</span>"
        "Casos autóctonos en Francia (21), Italia (4), Portugal (2); sin casos en "
        "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España</span>."
        "</div></li>"

        "<li style='list-style:none;margin:8px 0'>"
        "<div style='border-left:6px solid #8d6e63;padding-left:10px'>"
        "<span style='background:#8d6e63;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Chikungunya</span>"
        "Francia 383 (82 nuevos), Italia 167 (60 nuevos); sin casos en España."
        "</div></li>"

        "<li style='list-style:none;margin:8px 0'>"
        "<div style='border-left:6px solid #1565c0;padding-left:10px'>"
        "<span style='background:#1565c0;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>A(H9N2)</span>"
        "4 casos leves en China (niños); riesgo para UE/EEE: muy bajo."
        "</div></li>"

        "<li style='list-style:none;margin:8px 0'>"
        "<div style='border-left:6px solid #6a1b9a;padding-left:10px'>"
        "<span style='background:#6a1b9a;color:#fff;padding:2px 6px;border-radius:999px;font-size:11px;margin-right:6px'>Sarampión</span>"
        "Aumento en Europa central/oriental asociado a coberturas subóptimas; sin cambios en España."
        "</div></li>"

        "</ul>"
        "</td></tr>"

        # Tabla WNV por país
        "<tr><td style='padding:8px 18px 2px'>"
        "<div style='font-weight:800;color:#2e7d32;margin:12px 0 6px'>🦟 Virus del Nilo Occidental — situación por país</div>"
        "<table role='presentation' cellspacing='0' cellpadding='0' width='100%' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        "<thead><tr style='background:#f0f7f2'>"
        "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>País</th>"
        "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Casos humanos</th>"
        "<th align='right' style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Muertes</th>"
        "<th align='left'  style='padding:10px 8px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#2e7d32'>Notas</th>"
        "</tr></thead>"
        "<tbody>"

        "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Italia</td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>500</td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>32</td>"
        "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Mayor carga 2025</td></tr>"

        "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong style='color:#0b6e0b'>España 🇪🇸</strong></td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'><strong>5</strong></td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>0</td>"
        "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>3 brotes en équidos/aves</td></tr>"

        "<tr><td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Grecia</td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
        "<td align='right' style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>—</td>"
        "<td style='padding:10px 8px;border-bottom:1px solid #f3f4f6'>Transmisión activa</td></tr>"

        "<tr><td style='padding:10px 8px'>Otros (Rumanía, Hungría, Francia, Alemania, Croacia, Bulgaria)</td>"
        "<td align='right' style='padding:10px 8px'>—</td>"
        "<td align='right' style='padding:10px 8px'>—</td>"
        "<td style='padding:10px 8px'>Transmisión estacional</td></tr>"

        "</tbody></table>"
        "<div style='font-size:11px;color:#6b7280;margin-top:6px'>Totales Europa: 652 casos humanos, 38 muertes (hasta 03-sep-2025).</div>"
        "</td></tr>"

        # Otras secciones cortas
        "<tr><td style='padding:10px 18px 12px'>"

        "<div style='font-weight:800;color:#333;margin:12px 0 6px'>🧬 CCHF (Fiebre hemorrágica Crimea-Congo)</div>"
        "<div style='border-left:6px solid #d32f2f;background:#fbf1f1;padding:8px 10px;border-radius:8px'>"
        "Sin nuevos casos esta semana. Acumulado 2025: "
        "<span style='background:#fff7d6;padding:2px 4px;border-radius:4px;border-left:4px solid #ff9800'>🇪🇸 España: 3 casos</span>; "
        "Grecia: 2 casos. Riesgo bajo en general; ocupacionalmente mayor en zonas endémicas."
        "</div>"

        "<div style='font-weight:800;color:#333;margin:14px 0 6px'>🐦 Influenza aviar A(H9N2)</div>"
        "<div style='border-left:6px solid #1565c0;background:#eef4fb;padding:8px 10px;border-radius:8px'>"
        "4 casos leves en niños (China). En UE/EEE: muy bajo riesgo; sin casos en España."
        "</div>"

        "<div style='font-weight:800;color:#333;margin:14px 0 6px'>🦠 COVID-19, Gripe y VRS</div>"
        "<div style='border-left:6px solid #1565c0;background:#eef4fb;padding:8px 10px;border-radius:8px'>"
        "COVID-19: aumento en detección con impacto hospitalario bajo frente a 2024. Influenza y VRS: niveles bajos en Europa. España: descenso de positividad SARI por SARS-CoV-2."
        "</div>"

        "<div style='font-weight:800;color:#333;margin:14px 0 6px'>💉 Sarampión</div>"
        "<div style='border-left:6px solid #6a1b9a;background:#f5eefb;padding:8px 10px;border-radius:8px'>"
        "Incremento en Europa central/oriental por coberturas vacunales subóptimas. España: sin cambios relevantes esta semana."
        "</div>"

        "<div style='font-weight:800;color:#333;margin:14px 0 6px'>🦟 Dengue y Chikungunya</div>"
        "<div style='border-left:6px solid #ef6c00;background:#fff6e9;padding:8px 10px;border-radius:8px'>"
        "Dengue autóctono en Francia (21), Italia (4), Portugal (2). Chikungunya: Francia 383 (82 nuevos), Italia 167 (60 nuevos). España: sin casos."
        "</div>"

        "</td></tr>"

        # Botón PDF
        "<tr><td align='center' style='padding:8px 18px 20px'>"
        "<a href='{article}' style='display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:700'>Abrir informe completo (PDF)</a>"
        "</td></tr>"

        # Footer
        "<tr><td style='background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center'>"
        "Generado automáticamente · Fuente: ECDC (CDTR{wk}) · Fecha (UTC): {utc}"
        "</td></tr>"

        "</table>"
        "</td></tr>"
        "</table>"
        "</body>"
        "</html>"
    ).format(
        period=period_label,
        article=article_url or pdf_url,
        wk=f' semana {week}' if week else '',
        utc=dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    )

    return html


# ----------------------------- EMAIL -------------------------------

def send_email(
    smtp_server: str,
    smtp_port: int,
    sender_email: str,
    email_password: str,
    receiver_email: str,
    subject: str,
    html_body: str,
    plain_fallback: Optional[str] = None,
) -> None:
    if not smtp_server or not sender_email or not receiver_email:
        raise ValueError("Faltan parámetros SMTP o emails (SENDER/RECEIVER/SMTP_SERVER).")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content(plain_fallback or "Ver versión HTML del mensaje.")
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_server, smtp_port, context=ctx) as s:
            s.ehlo()
            if email_password:
                s.login(sender_email, email_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            if email_password:
                s.login(sender_email, email_password)
            s.send_message(msg)

    logging.info("Correo enviado correctamente a %s", receiver_email)


# ----------------------------- MAIN --------------------------------

def main() -> None:
    # 1) Localizar último PDF/CDTR
    client = ECDCClient()
    pdf_url, week, year, article_url = client.fetch_latest_pdf()

    # 2) Construir HTML
    html = build_html_email(pdf_url=pdf_url, article_url=article_url, week=week, year=year)

    # 3) Enviar
    subject = "ECDC CDTR – {} ({})".format(f"Semana {week}" if week else "Último", year or dt.date.today().year)

    if os.getenv("DRY_RUN", "0") == "1":
        logging.info("DRY_RUN=1: no se envía email. Asunto: %s", subject)
        logging.info("HTML length: %d chars", len(html))
        return

    send_email(
        smtp_server=os.getenv("SMTP_SERVER", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        sender_email=os.getenv("SENDER_EMAIL", ""),
        email_password=os.getenv("EMAIL_PASSWORD", ""),
        receiver_email=os.getenv("RECEIVER_EMAIL", ""),
        subject=subject,
        html_body=html,
        plain_fallback="Boletín semanal del ECDC. Abre este correo con un cliente que soporte HTML.",
    )


if __name__ == "__main__":
    main()

