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


# =========================
# Configuración
# =========================

class Config:
    # Listado de informes semanales (ECDC)
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    # SMTP / Email
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")  # 465 SSL; 587 STARTTLS
    sender_email = os.getenv("SENDER_EMAIL", "")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    receiver_email = os.getenv("RECEIVER_EMAIL", "")  # permite múltiples separados por coma o ;

    # Varios
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"
    attach_html = os.getenv("ATTACH_HTML", "0") == "1"  # adjuntar el mismo HTML como .html


# =========================
# Utilidades
# =========================

MESES_ES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
}


def fecha_es(dt_utc: dt.datetime) -> str:
    """Devuelve '18 de septiembre de 2025 (UTC)'."""
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month, 'mes')} de {dt_utc.year} (UTC)"


def normalize_recipients(raw: str) -> list[str]:
    """Convierte 'a@b.com; c@d.com, e@f.com' en lista."""
    if not raw:
        return []
    tmp = raw.replace(";", ",").replace("\n", ",").replace("\r", ",")
    return [x.strip() for x in tmp.split(",") if x.strip()]


# =========================
# Agente
# =========================

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

    # ---------- Localización PDF/artículo ----------

    @staticmethod
    def _parse_week_year(text: str) -> tuple[int | None, int | None]:
        """Intenta detectar 'week 38' y '2025' en un texto."""
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None,
                int(y.group(1)) if y else None)

    def fetch_latest_article_and_pdf(self) -> tuple[str, str, int | None, int | None, str]:
        """Devuelve (article_url, pdf_url, week, year, article_title)."""
        r = self.session.get(self.config.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Candidatos: enlaces a páginas de publicación del CDTR
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                if url not in candidates:
                    candidates.append(url)

        if not candidates:
            raise RuntimeError("No se encontraron artículos CDTR en la página de listados.")

        # Recorremos candidatos hasta encontrar un <a href="...pdf">
        for article_url in candidates:
            ar = self.session.get(article_url, timeout=30)
            if ar.status_code != 200:
                continue
            asoup = BeautifulSoup(ar.text, "html.parser")
            # título del artículo
            article_title = (asoup.title.get_text(strip=True) if asoup.title else "").strip()

            # primer enlace .pdf que aparezca
            pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a:
                continue
            pdf_url = pdf_a["href"]
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(article_url, pdf_url)

            week, year = self._parse_week_year(article_title + " " + pdf_url)
            logging.info("Artículo CDTR: %s", article_url)
            logging.info("PDF CDTR: %s (semana=%s, año=%s)", pdf_url, week, year)
            return article_url, pdf_url, week, year, article_title

        raise RuntimeError("No se encontró PDF en los artículos candidatos.")

    # ---------- Extractor de puntos clave ----------

    def extract_key_points(self, article_url: str, max_points: int = 8) -> list[str]:
        """Extrae <li> significativos del cuerpo del artículo. Si no hay, devuelve lista vacía."""
        try:
            r = self.session.get(article_url, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # heurística: tomar lis con texto suficiente
            lis = [li.get_text(separator=" ", strip=True) for li in soup.find_all("li")]
            lis = [x for x in lis if len(x) >= 40]  # filtrar ítems triviales
            # eliminar duplicados conservando orden
            seen = set()
            uniq = []
            for x in lis:
                if x not in seen:
                    uniq.append(x)
                    seen.add(x)
            return uniq[:max_points]
        except Exception as e:
            logging.warning("No se pudieron extraer puntos clave: %s", e)
            return []

    # ---------- HTML rico (cuerpo del correo) ----------

    @staticmethod
    def _chip(color_bg: str, text: str) -> str:
        return (f"<span style='display:inline-block;padding:6px 10px;border-radius:999px;"
                f"font-size:12px;font-weight:700;color:#fff;background:{color_bg}'>{text}</span>")

    def build_rich_html(self, week: int | None, year: int | None,
                        pdf_url: str, article_url: str,
                        key_points: list[str]) -> str:
        """HTML con cabecera, botón PDF y puntos clave."""
        period = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        gen_date = fecha_es(dt.datetime.utcnow())

        # bullets con “semáforo” rotatorio
        colors = ["#2e7d32", "#ef6c00", "#1565c0", "#c62828"]
        def bullet(i: int, text: str) -> str:
            dot = f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:{colors[i%len(colors)]};margin-right:8px;vertical-align:middle'></span>"
            return f"<div style='padding:10px 12px;border-left:5px solid {colors[i%len(colors)]};background:#f8fafc;border-radius:6px;margin:6px 0'>{dot}<span style='font-size:14px;color:#111'>{text}</span></div>"

        bullets_html = "".join(bullet(i, t) for i, t in enumerate(key_points)) if key_points else \
            bullet(0, "No fue posible auto-extraer puntos clave del artículo esta semana; consulte el PDF para el detalle.")

        html = f"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ECDC CDTR – {period}</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#1f2937">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px 12px;background:#f5f7fb">
    <tr><td align="center">
      <table role="presentation" width="820" cellspacing="0" cellpadding="0" style="max-width:820px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 6px 18px rgba(0,0,0,.08)">
        <tr>
          <td style="background:#0b5cab;color:#fff;padding:24px 26px">
            <div style="font-size:24px;font-weight:800;letter-spacing:.2px">Resumen Semanal de Amenazas de Enfermedades Transmisibles</div>
            <div style="opacity:.95;font-size:13px;margin-top:4px">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
            <div style="margin-top:10px">{self._chip("#2563eb", period)}</div>
          </td>
        </tr>

        <tr>
          <td align="center" style="padding:18px 18px 0">
            <a href="{pdf_url}" style="display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700">Abrir / Descargar PDF del informe</a>
            <div style="font-size:11px;color:#6b7280;margin-top:8px">Si el botón no funciona, copia y pega este enlace: <br><span style="word-break:break-all">{pdf_url}</span></div>
          </td>
        </tr>

        <tr>
          <td style="padding:18px 22px">
            <div style="font-weight:800;color:#0b5cab;margin-bottom:8px">Puntos clave de la semana (auto-extraídos del ECDC)</div>
            {bullets_html}
          </td>
        </tr>

        <tr>
          <td align="center" style="padding:8px 18px 20px">
            <a href="{article_url}" style="display:inline-block;background:#111827;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:700">Página del informe (ECDC)</a>
          </td>
        </tr>

        <tr>
          <td style="background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center">
            Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · Fecha: {gen_date}
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""".strip()
        return html

    # ---------- Estado (anti-duplicados) ----------

    def _load_last_state(self) -> dict:
        if not os.path.exists(self.config.state_file):
            return {}
        try:
            with open(self.config.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_last_state(self, pdf_url: str):
        state = {"last_pdf_url": pdf_url, "timestamp": dt.datetime.utcnow().isoformat()}
        with open(self.config.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    # ---------- Envío email ----------

    def send_email(self, subject: str, plain_text: str, html_body: str,
                   attachment_html: str | None = None, attachment_name: str = "resumen_ecdc.html"):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        to_addresses = normalize_recipients(self.config.receiver_email)
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        root_kind = 'mixed' if (attachment_html is not None) else 'alternative'
        msg_root = MIMEMultipart(root_kind)
        msg_root['Subject'] = subject
        msg_root['From'] = self.config.sender_email
        msg_root['To'] = ", ".join(to_addresses)

        if root_kind == 'mixed':
            msg_alt = MIMEMultipart('alternative')
            msg_root.attach(msg_alt)
            msg_alt.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            msg_alt.attach(MIMEText(html_body, 'html', 'utf-8'))

            attach_part = MIMEText(attachment_html or "", 'html', 'utf-8')
            attach_part.add_header('Content-Disposition', 'attachment', filename=attachment_name)
            msg_root.attach(attach_part)
        else:
            msg_root.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            msg_root.attach(MIMEText(html_body, 'html', 'utf-8'))

        logging.info("SMTP: from=%s → to=%s", self.config.sender_email, to_addresses)

        ctx = ssl.create_default_context()
        if int(self.config.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.sendmail(self.config.sender_email, to_addresses, msg_root.as_string())
        else:
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.config.email_password:
                    s.login(self.config.sender_email, self.config.email_password)
                s.sendmail(self.config.sender_email, to_addresses, msg_root.as_string())

        logging.info("Correo enviado correctamente.")

    # ---------- Run ----------

    def run(self):
        try:
            article_url, pdf_url, week, year, article_title = self.fetch_latest_article_and_pdf()
        except Exception as e:
            logging.exception("No se pudo localizar el PDF/artículo más reciente: %s", e)
            return

        # Anti-duplicados
        state = self._load_last_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado previamente, no se reenvía.")
            return

        # Extraer puntos clave
        key_points = self.extract_key_points(article_url, max_points=8)
        logging.info("Puntos clave extraídos: %d", len(key_points))

        # HTML rico: SIEMPRE como cuerpo
        rich_html = self.build_rich_html(week, year, pdf_url, article_url, key_points)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"
        plain = "Resumen semanal del ECDC. Este correo contiene contenido HTML enriquecido; si no lo ves, abre el PDF del informe desde el botón."

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto: %s", subject)
            logging.debug("HTML length: %d", len(rich_html))
            return

        try:
            self.send_email(
                subject=subject,
                plain_text=plain,
                html_body=rich_html,
                attachment_html=(rich_html if self.config.attach_html else None),
                attachment_name="resumen_ecdc.html"
            )
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# =========================
# main
# =========================

if __name__ == "__main__":
    cfg = Config()
    WeeklyReportAgent(cfg).run()
