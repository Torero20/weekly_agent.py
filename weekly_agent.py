import os
import re
import ssl
import smtplib
import logging
import requests
from bs4 import BeautifulSoup
from email.message import EmailMessage
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lsa import LsaSummarizer
from sumy.nlp.stemmers import Stemmer
from sumy.utils import get_stop_words
from googletrans import Translator

# ========================
# CONFIGURACIÓN Y LOGGING
# ========================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

LANGUAGE = "english"
SENTENCES_COUNT = 10

# ========================
# CLASE AGENTE
# ========================

class WeeklyAgent:
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER")
        self.smtp_port = int(os.getenv("SMTP_PORT", "465"))
        self.sender_email = os.getenv("SENDER_EMAIL")
        self.email_password = os.getenv("EMAIL_PASSWORD")
        self.receiver_email = os.getenv("RECEIVER_EMAIL")  # coma separada
        self.translator = Translator()

    # ---- Localizar PDF más reciente ----
    def fetch_latest_pdf(self):
        url = "https://www.ecdc.europa.eu/en/publications-data/communicable-disease-threats-report"
        logging.info("Buscando último informe en %s", url)
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        pdf_links = []
        for a in soup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                link = a["href"]
                if not link.startswith("http"):
                    link = "https://www.ecdc.europa.eu" + link
                pdf_links.append(link)

        if not pdf_links:
            raise RuntimeError("No se encontraron enlaces PDF en la página")

        latest_pdf = pdf_links[0]
        logging.info("PDF más reciente: %s", latest_pdf)
        return latest_pdf

    # ---- Extraer texto del PDF ----
    def extract_text_from_pdf(self, pdf_url):
        r = requests.get(pdf_url, timeout=60)
        r.raise_for_status()
        with open("latest.pdf", "wb") as f:
            f.write(r.content)
        reader = PdfReader("latest.pdf")
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text

    # ---- Resumir texto ----
    def summarize(self, text, sentences_count=SENTENCES_COUNT):
        parser = PlaintextParser.from_string(text, Tokenizer(LANGUAGE))
        stemmer = Stemmer(LANGUAGE)
        summarizer = LsaSummarizer(stemmer)
        summarizer.stop_words = get_stop_words(LANGUAGE)
        summary = summarizer(parser.document, sentences_count)
        return " ".join(str(sentence) for sentence in summary)

    # ---- Traducir a español ----
    def translate(self, text, dest="es"):
        if not text.strip():
            return ""
        try:
            return self.translator.translate(text, dest=dest).text
        except Exception as e:
            logging.warning("Fallo en traducción (%s), devolviendo texto original", e)
            return text

    # ---- Formatear HTML ----
    def format_summary_to_html(self, summary, pdf_url):
        html = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; }}
                .title {{ background: #004080; color: white; padding: 12px; font-size: 20px; }}
                .section {{ margin: 15px 0; }}
                .highlight {{ color: #d32f2f; font-weight: bold; }}
                .table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
                .table th, .table td {{ border: 1px solid #ccc; padding: 8px; }}
                .table th {{ background: #f0f0f0; }}
            </style>
        </head>
        <body>
            <div class="title">🦠 Resumen semanal ECDC</div>
            <p><b>Fuente:</b> <a href="{pdf_url}">{pdf_url}</a></p>
            <div class="section">
                <p>{summary}</p>
            </div>
        </body>
        </html>
        """
        return html

    # ---- Enviar email ----
    def send_email(self, subject, plain, html=None):
        to_addresses = [e.strip() for e in self.receiver_email.split(",") if e.strip()]
        if not to_addresses:
            raise ValueError("No hay destinatarios válidos")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender_email
        msg["To"] = ", ".join(to_addresses)
        msg.set_content(plain or "(sin texto)")
        if html:
            msg.add_alternative(html, subtype="html")

        logging.info("SMTP: from=%s to=%s", self.sender_email, to_addresses)

        ctx = ssl.create_default_context()

        try:
            # Primero SSL
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=ctx, timeout=30) as s:
                s.login(self.sender_email, self.email_password)
                s.send_message(msg, from_addr=self.sender_email, to_addrs=to_addresses)
            logging.info("Correo enviado correctamente")
        except Exception as e_ssl:
            logging.warning("SSL falló (%s). Probando STARTTLS...", e_ssl)
            # Fallback STARTTLS
            with smtplib.SMTP(self.smtp_server, 587, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(self.sender_email, self.email_password)
                s.send_message(msg, from_addr=self.sender_email, to_addrs=to_addresses)
            logging.info("Correo enviado correctamente con STARTTLS")

    # ---- Ejecución principal ----
    def run(self):
        pdf_url = self.fetch_latest_pdf()
        text = self.extract_text_from_pdf(pdf_url)
        summary_en = self.summarize(text)
        summary_es = self.translate(summary_en)
        html = self.format_summary_to_html(summary_es, pdf_url)
        self.send_email("Resumen semanal ECDC", summary_es, html)


if __name__ == "__main__":
    agent = WeeklyAgent()
    agent.run()
