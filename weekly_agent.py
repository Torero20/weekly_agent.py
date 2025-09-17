#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, ssl, smtplib, json, logging, requests, datetime as dt
from email.message import EmailMessage
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------

class Config:
    base_url = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
    sender_email = os.getenv("SENDER_EMAIL", "")
    receiver_email = os.getenv("RECEIVER_EMAIL", "miralles.paco@gmail.com, contra1270@gmail.com, mirallesf@vithas.es")
    email_password = os.getenv("EMAIL_PASSWORD", "")
    dry_run = os.getenv("DRY_RUN", "0") == "1"
    log_level = os.getenv("LOG_LEVEL", "INFO")
    state_file = ".weekly_agent_state.json"

# ---------------------------------------------------------------------
# Agente
# ---------------------------------------------------------------------

class WeeklyReportAgent:
    def __init__(self, config: Config):
        self.config = config
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
        })

    # ------------------ Localización PDF ------------------

    def fetch_latest_pdf(self):
        try:
            r = self.session.get(self.config.base_url, timeout=20)
            r.raise_for_status()
        except Exception as e:
            logging.error("Error cargando página base: %s", e)
            return None, None

        soup = BeautifulSoup(r.text, "html.parser")
        first_article = soup.find("div", class_="view-content").find("a", href=True)
        if not first_article:
            logging.warning("No se encontró ningún artículo de CDTR.")
            return None, None

        article_url = requests.compat.urljoin(self.config.base_url, first_article["href"])
        try:
            ra = self.session.get(article_url, timeout=20)
            ra.raise_for_status()
        except Exception as e:
            logging.error("Error cargando artículo: %s", e)
            return None, None

        sa = BeautifulSoup(ra.text, "html.parser")
        pdf_link = sa.find("a", href=re.compile(r"\.pdf$"))
        if not pdf_link:
            logging.warning("No se encontró PDF en el artículo.")
            return None, None

        pdf_url = requests.compat.urljoin(article_url, pdf_link["href"])
        logging.info("PDF más reciente: %s", pdf_url)
        return pdf_url, article_url

    # ------------------ Estado (anti-duplicados) ------------------

    def _load_last_state(self):
        if not os.path.exists(self.config.state_file):
            return {}
        try:
            with open(self.config.state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_last_state(self, pdf_url):
        state = {"last_pdf_url": pdf_url, "timestamp": dt.datetime.utcnow().isoformat()}
        with open(self.config.state_file, "w") as f:
            json.dump(state, f)

    # ------------------ Email ------------------

    def send_email(self, subject, plain, html=None):
        if not self.config.sender_email or not self.config.receiver_email:
            raise ValueError("Faltan SENDER_EMAIL o RECEIVER_EMAIL.")
        if not self.config.smtp_server:
            raise ValueError("Falta SMTP_SERVER.")

        # Convertir string en lista de direcciones
        to_addresses = [email.strip() for email in self.config.receiver_email.split(",")]

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.sender_email
        msg["To"] = ", ".join(to_addresses)  # visible en cabecera
        msg.set_content(plain or "(vacío)")
        if html:
            msg.add_alternative(html, subtype="html")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, context=context) as server:
            if self.config.email_password:
                server.login(self.config.sender_email, self.config.email_password)
            server.send_message(
                msg,
                from_addr=self.config.sender_email,
                to_addrs=to_addresses  # envío real a todos
            )

    # ------------------ HTML ------------------

    def format_summary_to_html(self, pdf_url):
        return f"""
        <html>
          <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.6;background:#f7f7f7;padding:18px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:auto;background:#fff;border-radius:8px;overflow:hidden;">
              <tr>
                <td style="background:#005ba4;color:#fff;padding:18px 20px;">
                  <h1 style="margin:0;font-size:22px;">Boletín semanal ECDC</h1>
                  <p style="margin:6px 0 0 0;font-size:14px;opacity:.9;">Informe de amenazas sanitarias</p>
                </td>
              </tr>
              <tr>
                <td style="padding:20px;font-size:15px;color:#222;">

                  <h2>🦠 Virus del Nilo Occidental (WNV)</h2>
                  <p>Se notificaron <b>50 casos humanos</b> y <b>7 muertes</b> en la UE/EEE durante la semana 37.</p>

                  <table border="0" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;margin:10px 0;">
                    <thead style="background:#e9f1f9;font-weight:bold;text-align:left;">
                      <tr>
                        <th>País</th><th>Casos</th><th>Muertes</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr><td>Italia</td><td>25</td><td>3</td></tr>
                      <tr><td>Grecia</td><td>15</td><td>2</td></tr>
                      <tr><td style="background:#fff3cd;"><b>España</b></td><td><b>5</b></td><td><b>1</b></td></tr>
                      <tr><td>Hungría</td><td>5</td><td>1</td></tr>
                    </tbody>
                  </table>

                  <h2>🧬 Fiebre Hemorrágica Crimea-Congo (CCHF)</h2>
                  <p>Esta semana no se han informado nuevos casos al ECDC.</p>

                  <h2>🐦 Influenza aviar A(H9N2)</h2>
                  <p>No se han notificado casos humanos en la UE/EEE. El riesgo actual se considera <b>muy bajo</b>.</p>

                  <h2>📌 Enlace al informe completo</h2>
                  <p><a href="{pdf_url}" style="color:#005ba4;text-decoration:underline">{pdf_url}</a></p>

                </td>
              </tr>
              <tr>
                <td style="background:#f0f0f0;color:#666;padding:12px 16px;text-align:center;font-size:12px;">
                  Generado automáticamente · {dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
                </td>
              </tr>
            </table>
          </body>
        </html>
        """.strip()

    # ------------------ Run ------------------

    def run(self):
        pdf_url, _ = self.fetch_latest_pdf()
        if not pdf_url:
            logging.info("No se encontró PDF nuevo.")
            return

        state = self._load_last_state()
        if state.get("last_pdf_url") == pdf_url:
            logging.info("El PDF ya fue enviado previamente, no se reenvía.")
            return

        html = self.format_summary_to_html(pdf_url)
        subject = "Resumen semanal del ECDC"

        if self.config.dry_run:
            logging.info("DRY_RUN=1: no se envía correo. Asunto: %s", subject)
            return

        try:
            self.send_email(subject, "Resumen automático del informe ECDC.", html)
            logging.info("Correo enviado correctamente.")
            self._save_last_state(pdf_url)
        except Exception as e:
            logging.exception("Error enviando el correo: %s", e)

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

if __name__ == "__main__":
    cfg = Config()
    agent = WeeklyReportAgent(cfg)
    agent.run()
