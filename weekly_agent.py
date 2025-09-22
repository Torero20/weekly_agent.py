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
from urllib.parse import urljoin, unquote, quote_plus
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# =========================
# Configuración
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
    attach_html = os.getenv("ATTACH_HTML", "0") == "1"

    translate = os.getenv("TRANSLATE", "1") != "0"  # traducir EN->ES
    max_points = int(os.getenv("MAX_POINTS", "8") or "8")


# =========================
# Utilidades
# =========================

MESES_ES = {
    1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",
    7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"
}

def fecha_es(dt_utc: dt.datetime) -> str:
    return f"{dt_utc.day} de {MESES_ES.get(dt_utc.month,'mes')} de {dt_utc.year} (UTC)"

def normalize_recipients(raw: str) -> list[str]:
    if not raw: return []
    raw = raw.replace(";", ",").replace("\n", ",").replace("\r", ",")
    return [x.strip() for x in raw.split(",") if x.strip()]

# =========================
# Agente
# =========================

class WeeklyReportAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        logging.basicConfig(
            level=getattr(logging, self.cfg.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # ---------- Localizar artículo y PDF ----------

    @staticmethod
    def _parse_week_year(text: str):
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None, int(y.group(1)) if y else None)

    def fetch_article_pdf(self):
        r = self.session.get(self.cfg.list_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            l = href.lower()
            if "communicable-disease-threats-report" in l and ("/publications-data/" in l or "/publications-and-data/" in l):
                url = href if href.startswith("http") else urljoin("https://www.ecdc.europa.eu", href)
                if url not in candidates:
                    candidates.append(url)

        if not candidates:
            raise RuntimeError("No se encontraron artículos CDTR en el listado.")

        for art in candidates:
            ar = self.session.get(art, timeout=30)
            if ar.status_code != 200:
                continue
            soup_art = BeautifulSoup(ar.text, "html.parser")
            pdf_a = soup_art.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a:
                continue
            pdf = pdf_a["href"]
            if not pdf.startswith("http"):
                pdf = urljoin(art, pdf)

            title = (soup_art.title.get_text(strip=True) if soup_art.title else "").strip()
            week, year = self._parse_week_year(title + " " + pdf)
            logging.info("Artículo CDTR: %s", art)
            logging.info("PDF CDTR: %s (semana=%s, año=%s)", pdf, week, year)
            return art, pdf, week, year, title

        raise RuntimeError("No se encontró PDF en los artículos candidatos.")

    # ---------- Extracción de puntos clave (afinada) ----------

    def extract_key_points(self, article_url: str, max_points: int) -> list[str]:
        """
        Extrae bullets desde el CUERPO del artículo (no del menú global).
        1) Busca contenedores del artículo (field--name-body, schema:articleBody…)
        2) Prioriza <ul><li>. Si no hay, toma párrafos sustantivos.
        3) Limpia items tipo “A B C D …” y breadcrumbs.
        """
        r = self.session.get(article_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Contenedores posibles del cuerpo
        candidates = []
        selectors = [
            'div.field--name-body',
            '[property="schema:articleBody"]',
            'article .field--name-body',
            'article .node__content',
            'div.region-content article',
            'article'
        ]
        for sel in selectors:
            candidates += soup.select(sel)
        # dedup
        seen = set()
        containers = []
        for c in candidates:
            h = hash(str(c)[:500])
            if h not in seen:
                containers.append(c)
                seen.add(h)

        def clean_text(t: str) -> str:
            t = re.sub(r"\s+", " ", t).strip()
            # quitar alfabetos / navegación
            if re.fullmatch(r"[A-Z](?:\s[A-Z]){10,}", t):  # A B C D … largo
                return ""
            if len(t) < 40:
                return ""
            return t

        items = []
        for cont in containers:
            for li in cont.find_all("li"):
                txt = clean_text(li.get_text(separator=" ", strip=True))
                if txt:
                    items.append(txt)

        # Si no hay <li>, usar párrafos "sustantivos"
        if not items:
            KEYWORDS = ["case", "cases", "death", "deaths", "report", "reported",
                        "surveillance", "influenza", "covid", "sars", "west nile",
                        "dengue", "chikungunya", "measles", "ebola", "nile", "cchf",
                        "malaria", "outbreak", "trend", "risk", "europe", "ue/eea", "eu/eea"]
            for cont in containers:
                for p in cont.find_all("p"):
                    txt = clean_text(p.get_text(separator=" ", strip=True))
                    if not txt:
                        continue
                    score = sum(k in txt.lower() for k in KEYWORDS)
                    if score >= 1:
                        items.append(txt)

        # Unificar, recortar y deduplicar
        uniq = []
        seen = set()
        for it in items:
            it = it.rstrip(".; ")
            if it and it not in seen:
                uniq.append(it)
                seen.add(it)

        logging.info("Bullets extraídos (antes de recorte): %d", len(uniq))
        return uniq[:max_points] if uniq else []

    # ---------- Traducción EN -> ES (Google Web) ----------

    def translate_bullets_to_es(self, bullets: list[str]) -> list[str]:
        if not bullets:
            return bullets
        try:
            # Construimos un bloque de texto separado por " ||| " para traducir de una vez
            block = " ||| ".join(bullets)

            # Endpoint “ligero” de Google Translate Web
            url = ("https://translate.google.com/_/TranslateWebserverUi/data/batchexecute"
                   "?rpcids=MkEWBc&bl=boq_translate-webserver_20201207.13_p0&soc-app=1"
                   "&soc-platform=1&soc-device=1&rt=c")

            # payload (mismo formato que usa el sitio)
            data = {
                "f.req": json.dumps([[["MkEWBc", json.dumps([[block, "en", "es", True],[None]]), None, "generic"]]]),
            }

            headers = {
                "Content-Type":"application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": self.session.headers["User-Agent"],
                "Referer":"https://translate.google.com",
            }

            resp = self.session.post(url, data=data, headers=headers, timeout=30)
            resp.raise_for_status()
            text = resp.text

            # Parseo bruto del “JSON” anidado.
            # Buscamos el primer bloque grande con la traducción.
            m = re.search(r'"\[\\"(.*?)\\"\]"', text)
            if not m:
                # Alternativa: buscar un array con la traducción
                m2 = re.search(r'\[\[\["MkEWBc",\s*"(.*?)"', text)
                if not m2:
                    logging.warning("No se pudo parsear la respuesta de translate.")
                    return bullets
                inner = m2.group(1).encode('utf-8').decode('unicode_escape')
            else:
                inner = m.group(1).encode('utf-8').decode('unicode_escape')

            # A veces viene como JSON escapado; intentamos otra vía:
            parts = inner.split(" ||| ")
            if len(parts) == 1:
                # plan B: extraer todas las cadenas traducidas separadas por “|||”
                body = re.search(r'\\n(.*?)\\n', inner) or re.search(r'(.*)', inner)
                parts = (body.group(1) if body else inner).split(" ||| ")

            out = [p.strip() for p in parts if p.strip()]
            # si el número no cuadra, devolvemos los originales para evitar “barullo”
            return out if len(out) == len(bullets) else bullets
        except Exception as e:
            logging.warning("Fallo al traducir (se envían en inglés): %s", e)
            return bullets

    # ---------- HTML rico (cuerpo) ----------

    @staticmethod
    def _chip(color_bg: str, text: str) -> str:
        return (f"<span style='display:inline-block;padding:6px 10px;border-radius:999px;"
                f"font-size:12px;font-weight:700;color:#fff;background:{color_bg}'>{text}</span>")

    def build_html(self, week, year, pdf_url, article_url, bullets_es: list[str]) -> str:
        period = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        gen_date = fecha_es(dt.datetime.utcnow())

        colors = ["#2e7d32", "#ef6c00", "#1565c0", "#c62828"]
        def bullet(i: int, text: str) -> str:
            dot = f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;background:{colors[i%len(colors)]};margin-right:8px;vertical-align:middle'></span>"
            return f"<div style='padding:10px 12px;border-left:5px solid {colors[i%len(colors)]};background:#f8fafc;border-radius:6px;margin:6px 0'>{dot}<span style='font-size:14px;color:#111'>{text}</span></div>"

        bullets_html = "".join(bullet(i, t) for i, t in enumerate(bullets_es)) if bullets_es else \
            bullet(0, "No fue posible auto-extraer puntos clave del artículo esta semana; consulte el PDF para el detalle.")

        return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECDC CDTR – {period}</title></head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#1f2937">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px 12px;background:#f5f7fb">
    <tr><td align="center">
      <table role="presentation" width="820" cellspacing="0" cellpadding="0" style="max-width:820px;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 6px 18px rgba(0,0,0,.08)">
        <tr><td style="background:#0b5cab;color:#fff;padding:24px 26px">
          <div style="font-size:24px;font-weight:800;letter-spacing:.2px">Resumen Semanal de Amenazas de Enfermedades Transmisibles</div>
          <div style="opacity:.95;font-size:13px;margin-top:4px">Centro Europeo para la Prevención y el Control de Enfermedades (ECDC)</div>
          <div style="margin-top:10px">{self._chip("#2563eb", period)}</div>
        </td></tr>

        <tr><td align="center" style="padding:18px 18px 0">
          <a href="{pdf_url}" style="display:inline-block;background:#0b5cab;color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-weight:700">Abrir / Descargar PDF del informe</a>
          <div style="font-size:11px;color:#6b7280;margin-top:8px">Si el botón no funciona, copia y pega este enlace: <br><span style="word-break:break-all">{pdf_url}</span></div>
        </td></tr>

        <tr><td style="padding:18px 22px">
          <div style="font-weight:800;color:#0b5cab;margin-bottom:8px">Puntos clave de la semana (auto-extraídos del ECDC)</div>
          {bullets_html}
        </td></tr>

        <tr><td align="center" style="padding:8px 18px 20px">
          <a href="{article_url}" style="display:inline-block;background:#111827;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:700">Página del informe (ECDC)</a>
        </td></tr>

        <tr><td style="background:#f3f4f6;color:#6b7280;padding:12px 20px;font-size:12px;text-align:center">
          Generado automáticamente · Fuente: ECDC (CDTR{' semana '+str(week) if week else ''}) · Fecha: {gen_date}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    # ---------- Estado ----------

    def _load_state(self):
        if not os.path.exists(self.cfg.state_file):
            return {}
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, pdf_url: str):
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump({"last_pdf_url": pdf_url, "ts": dt.datetime.utcnow().isoformat()}, f)

    # ---------- Envío email ----------

    def send_email(self, subject: str, plain_text: str, html_body: str,
                   attachment_html: str | None = None, attachment_name: str = "resumen_ecdc.html"):
        if not self.cfg.sender_email or not self.cfg.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.cfg.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        to_addresses = normalize_recipients(self.cfg.receiver_email)
        if not to_addresses:
            raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        root_kind = 'mixed' if (attachment_html is not None) else 'alternative'
        msg_root = MIMEMultipart(root_kind)
        msg_root['Subject'] = subject
        msg_root['From'] = self.cfg.sender_email
        msg_root['To'] = ", ".join(to_addresses)

        if root_kind == 'mixed':
            alt = MIMEMultipart('alternative')
            msg_root.attach(alt)
            alt.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            alt.attach(MIMEText(html_body, 'html', 'utf-8'))

            attach = MIMEText(attachment_html or "", 'html', 'utf-8')
            attach.add_header('Content-Disposition', 'attachment', filename=attachment_name)
            msg_root.attach(attach)
        else:
            msg_root.attach(MIMEText(plain_text or "(vacío)", 'plain', 'utf-8'))
            msg_root.attach(MIMEText(html_body, 'html', 'utf-8'))

        logging.info("SMTP from=%s → to=%s", self.cfg.sender_email, to_addresses)

        ctx = ssl.create_default_context()
        if int(self.cfg.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.cfg.smtp_server, self.cfg.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.cfg.email_password:
                    s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addresses, msg_root.as_string())
        else:
            with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                if self.cfg.email_password:
                    s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addresses, msg_root.as_string())

        logging.info("Correo enviado correctamente.")

    # ---------- Run ----------

    def run(self):
        try:
            article_url, pdf_url, week, year, title = self.fetch_article_pdf()
        except Exception as e:
            logging.exception("No se pudo localizar artículo/PDF: %s", e)
            return

        state = self._load_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado antes; se omite.")
            return

        # Extraer bullets (cuerpo del artículo, no menú)
        bullets = self.extract_key_points(article_url, self.cfg.max_points)
        logging.info("Bullets tras extracción: %d", len(bullets))

        # Traducir EN→ES si procede
        bullets_es = self.translate_bullets_to_es(bullets) if self.cfg.translate else bullets

        html = self.build_html(week, year, pdf_url, article_url, bullets_es)
        subject = f"ECDC CDTR – {'Semana ' + str(week) if week else 'Último'} ({year or dt.date.today().year})"
        plain = "Resumen semanal del ECDC. Este correo es HTML; si no lo ves, usa el botón para abrir el PDF."

        if self.cfg.dry_run:
            logging.info("DRY_RUN=1: no envío. Asunto=%s | bullets=%d", subject, len(bullets_es))
            return

        try:
            self.send_email(
                subject=subject,
                plain_text=plain,
                html_body=html,
                attachment_html=(html if self.cfg.attach_html else None),
                attachment_name="resumen_ecdc.html"
            )
            self._save_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)


# =========================
# main
# =========================

if __name__ == "__main__":
    WeeklyReportAgent(Config()).run()

