#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, ssl, json, smtplib, logging, datetime as dt, requests
from bs4 import BeautifulSoup, NavigableString, Tag
from urllib.parse import urljoin, unquote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --------------------------- Config ---------------------------------

class Config:
    list_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"

    smtp_server   = os.getenv("SMTP_SERVER", "")
    smtp_port     = int(os.getenv("SMTP_PORT", "465") or "465")  # 465 SSL / 587 STARTTLS
    sender_email  = os.getenv("SENDER_EMAIL", "")
    email_password= os.getenv("EMAIL_PASSWORD", "")
    receiver_email= os.getenv("RECEIVER_EMAIL","miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es")

    dry_run   = os.getenv("DRY_RUN","0") == "1"
    log_level = os.getenv("LOG_LEVEL","INFO")
    state_file= ".weekly_agent_state.json"

# --------------------------- Util -----------------------------------

MESES_ES = {1:"enero",2:"febrero",3:"marzo",4:"abril",5:"mayo",6:"junio",7:"julio",8:"agosto",9:"septiembre",10:"octubre",11:"noviembre",12:"diciembre"}
def fecha_es(now_utc: dt.datetime) -> str:
    return f"{now_utc.day} de {MESES_ES[now_utc.month]} de {now_utc.year} (UTC)"

def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())

# --------------------------- Agent ----------------------------------

class WeeklyReportAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(message)s")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept":"text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
        })

    # ---------- localizar último artículo y PDF ----------
    def _parse_week_year(self, text: str):
        s = unquote(text or "").lower()
        w = re.search(r"\bweek[\s\-]?(\d{1,2})\b", s)
        y = re.search(r"\b(20\d{2})\b", s)
        return (int(w.group(1)) if w else None, int(y.group(1)) if y else None)

    def fetch_latest(self):
        r = self.session.get(self.cfg.list_url, timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        articles = []
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            if "communicable-disease-threats-report" in href and ("/publications-data/" in href or "/publications-and-data/" in href):
                url = a["href"] if a["href"].startswith("http") else urljoin("https://www.ecdc.europa.eu", a["href"])
                if url not in articles: articles.append(url)

        if not articles: raise RuntimeError("No se encontraron artículos CDTR.")

        for article_url in articles:
            ar = self.session.get(article_url, timeout=30)
            if ar.status_code != 200: continue
            asoup = BeautifulSoup(ar.text, "html.parser")
            pdf_a = asoup.find("a", href=re.compile(r"\.pdf$", re.I))
            if not pdf_a: continue
            pdf_url = pdf_a["href"]; 
            if not pdf_url.startswith("http"):
                pdf_url = urljoin(article_url, pdf_url)

            title_text = (asoup.title.get_text(strip=True) if asoup.title else "") + " " + pdf_url
            week, year = self._parse_week_year(title_text)

            logging.info("Artículo CDTR: %s", article_url)
            logging.info("PDF CDTR: %s (semana=%s, año=%s)", pdf_url, week, year)
            return article_url, pdf_url, week, year, asoup

        raise RuntimeError("No se encontró PDF en los artículos candidatos.")

    # ---------- extraer “puntos clave” robustamente ----------
    def extract_key_points(self, asoup: BeautifulSoup):
        # 1) Delimitamos el contenedor principal del artículo
        root = asoup.find(attrs={"role":"main"}) or asoup.find("article") \
               or asoup.find("div", class_=re.compile(r"(field--name-body|article|content)", re.I)) \
               or asoup

        # 2) Buscamos headings típicos y tomamos la UL/OL inmediatamente después
        headings_patterns = re.compile(r"(this week|key points|highlights|summary|at a glance)", re.I)

        def next_list_after(node: Tag):
            cur = node
            for _ in range(12):  # avanzamos pocos hermanos
                cur = cur.next_sibling
                if not cur: break
                if isinstance(cur, NavigableString): 
                    if not cur.strip(): 
                        continue
                    # texto suelto: seguimos
                if isinstance(cur, Tag):
                    if cur.name in ("ul","ol"):
                        return cur
                    # si hay un div contenedor con una lista dentro
                    lst = cur.find(["ul","ol"])
                    if lst: return lst
            return None

        # Intento A: heading + lista posterior
        for h in root.find_all(re.compile(r"^h[1-6]$"), string=headings_patterns):
            lst = next_list_after(h)
            if lst:
                items = [norm_text(li.get_text(" ", strip=True)) for li in lst.find_all("li")]
                items = [i for i in items if len(i.split()) >= 4]
                if items:
                    logging.debug("Bullets (heading match): %s", items[:3])
                    return items[:8]

        # Intento B: primera lista “sustanciosa” dentro del cuerpo
        candidate = None
        for lst in root.find_all(["ul","ol"]):
            items = [norm_text(li.get_text(" ", strip=True)) for li in lst.find_all("li")]
            items = [i for i in items if len(i.split()) >= 5]
            # descartamos menús / navegación
            joined = " ".join(items).lower()
            if items and not re.search(r"(share|cookie|subscribe|menu|related)", joined):
                candidate = items
                break
        if candidate:
            logging.debug("Bullets (first substantive list): %s", candidate[:3])
            return candidate[:8]

        # Intento C: párrafos cortos como viñetas
        paras = []
        for p in root.find_all("p"):
            t = norm_text(p.get_text(" ", strip=True))
            if 40 <= len(t) <= 240: paras.append(t)
            if len(paras) >= 6: break
        if paras:
            logging.debug("Bullets (paragraph fallback): %s", paras[:3])
            return paras

        logging.debug("No se pudieron extraer bullets.")
        return []

    # ---------- HTML (dinámico con bullets) ----------
    def build_html(self, week_label: str, pdf_url: str, key_points):
        bullets_html = "".join(f"<li>{b}</li>" for b in key_points) if key_points else "<li>(Sin puntos clave extraídos esta semana)</li>"
        gen_date = fecha_es(dt.datetime.utcnow())

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ECDC CDTR – {week_label}</title>
<style>
* {{ box-sizing:border-box; font-family:'Segoe UI',Tahoma,Verdana,sans-serif; }}
body {{ background:#f5f7fa; color:#333; line-height:1.6; padding:20px; max-width:1200px; margin:0 auto; }}
.header {{ text-align:center; padding:22px; background:linear-gradient(135deg,#2b6ca3 0%,#1a4e7a 100%); color:#fff; border-radius:10px; margin-bottom:22px; }}
.header h1 {{ font-size:2.0rem; margin:0 0 8px; }}
.header .week {{ display:inline-block; background:rgba(255,255,255,.2); padding:6px 14px; border-radius:22px; font-weight:600; }}
.section {{ background:#fff; border-radius:10px; padding:16px 18px; box-shadow:0 2px 8px rgba(0,0,0,.06); margin-bottom:16px; }}
.section h2 {{ color:#2b6ca3; border-bottom:2px solid #eaeaea; padding-bottom:8px; margin-bottom:12px; }}
.keybox {{ background:#e8f4ff; border-radius:8px; padding:12px; margin:10px 0; }}
.btn {{ display:inline-block; background:#0b5cab; color:#fff !important; text-decoration:none; padding:10px 16px; border-radius:8px; font-weight:700; }}
.footer {{ text-align:center; margin-top:10px; color:#667; font-size:.9rem; }}
</style>
</head>
<body>
  <div class="header">
    <h1>Resumen Semanal de Amenazas de Enfermedades Transmisibles</h1>
    <div class="week">{week_label}</div>
    <div style="margin-top:12px"><a class="btn" href="{pdf_url}">Abrir / Descargar PDF del informe</a></div>
  </div>

  <div class="section">
    <h2>Puntos clave de la semana (auto-extraídos del ECDC)</h2>
    <div class="keybox">
      <ul>
        {bullets_html}
      </ul>
    </div>
  </div>

  <div class="footer">
    Generado automáticamente · Fuente: ECDC (CDTR) · Fecha: {gen_date}
  </div>
</body>
</html>"""

    # ---------- envío ----------
    def send_email(self, subject: str, html_body: str):
        if not self.cfg.sender_email or not self.cfg.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.cfg.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        raw = self.cfg.receiver_email
        for sep in [";", "\n"]: raw = raw.replace(sep, ",")
        to_addrs = [x.strip() for x in raw.split(",") if x.strip()]
        if not to_addrs: raise ValueError("RECEIVER_EMAIL vacío tras el parseo.")

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.cfg.sender_email
        msg['To'] = ", ".join(to_addrs)

        msg.attach(MIMEText("Este mensaje requiere un cliente con soporte HTML.", 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        ctx = ssl.create_default_context()
        logging.info("SMTP: from=%s → to=%s", self.cfg.sender_email, to_addrs)
        if int(self.cfg.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.cfg.smtp_server, self.cfg.smtp_port, context=ctx, timeout=30) as s:
                s.ehlo()
                if self.cfg.email_password: s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port, timeout=30) as s:
                s.ehlo(); s.starttls(context=ctx); s.ehlo()
                if self.cfg.email_password: s.login(self.cfg.sender_email, self.cfg.email_password)
                s.sendmail(self.cfg.sender_email, to_addrs, msg.as_string())
        logging.info("Correo enviado correctamente.")

    # ---------- estado ----------
    def _load_last_state(self):
        if not os.path.exists(self.cfg.state_file): return {}
        try:
            with open(self.cfg.state_file,"r") as f: return json.load(f)
        except Exception: return {}

    def _save_last_state(self, pdf_url):
        with open(self.cfg.state_file,"w") as f:
            json.dump({"last_pdf_url": pdf_url, "timestamp": dt.datetime.utcnow().isoformat()}, f)

    # ---------- run ----------
    def run(self):
        try:
            article_url, pdf_url, week, year, asoup = self.fetch_latest()
        except Exception as e:
            logging.exception("No se pudo localizar el CDTR: %s", e); return

        st = self._load_last_state()
        if st.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado previamente; no se reenvía.")
            return

        key_points = self.extract_key_points(asoup)
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug("Key points extraídos (%d): %s", len(key_points), [kp[:200] for kp in key_points])

        week_label = f"Semana {week} · {year}" if week and year else "Último informe ECDC"
        html = self.build_html(week_label, pdf_url, key_points)
        subject = f"ECDC CDTR – {week_label}"

        if self.cfg.dry_run:
            logging.info("DRY_RUN=1 (no envío). Asunto=%s | bullets=%d", subject, len(key_points))
            return

        try:
            self.send_email(subject, html)
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)

# --------------------------- main -----------------------------------

if __name__ == "__main__":
    WeeklyReportAgent(Config()).run()

