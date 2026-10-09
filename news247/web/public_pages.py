"""Public pages Meta asks for before a WhatsApp app can go live: a website for the business
profile, a privacy policy (with data-deletion instructions) and terms of service. They describe
this installation honestly: a personal, non-commercial alert tool with exactly one user."""

from __future__ import annotations

import html
import re

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg: #f6f7f9; --panel: #fff; --text: #18202c; --muted: #5d6a7e; --line: #dfe4ec; --accent: #1f6fd1; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #0b0e13; --panel: #121722; --text: #dce3ee; --muted: #8592a6; --line: #232b3b; --accent: #4ea1ff; }} }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 720px; margin: 0 auto; padding: 32px 16px 64px; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; }} h2 {{ font-size: 18px; margin: 28px 0 6px; }}
  p, li {{ color: var(--text); }} .muted {{ color: var(--muted); }}
  nav a {{ color: var(--accent); margin-right: 16px; text-decoration: none; }}
  section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; margin-top: 18px; }}
</style>
</head>
<body><main>
<nav><a href="/about">About</a><a href="/privacy">Privacy policy</a><a href="/terms">Terms of service</a></nav>
{body}
<p class="muted" style="margin-top:32px;font-size:14px">Contact: {contact}</p>
</main></body></html>"""


def contact_email(user_agent: str, env_email: str = "") -> str:
    if env_email and "@" in env_email:
        return env_email
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", user_agent or "")
    return m.group(0) if m else ""


def _page(title: str, body: str, contact: str) -> str:
    c = html.escape(contact)
    link = f'<a href="mailto:{c}">{c}</a>' if contact else "the operator of this website"
    return _SHELL.format(title=html.escape(title), body=body, contact=link)


def about_page(contact: str, name: str = "News247") -> str:
    body = f"""
<h1>{html.escape(name)}</h1>
<p class="muted">A personal market-news alert service</p>
<section>
<p>{html.escape(name)} is a private, non-commercial tool run by one person for their own use. It watches public
news sources (company newsrooms, regulators, exchanges and news feeds) around the clock, scores how likely each
headline is to move stock prices, and sends its owner a WhatsApp message when something important happens.</p>
<p>It has no customers, does not sell anything, does not advertise, and only ever messages its owner's own
phone number, who set it up and asked to receive these alerts.</p>
<p>Messages are sent through the official WhatsApp Business Platform (Cloud API) by Meta.</p>
</section>"""
    return _page(f"{name}: personal market-news alerts", body, contact)


def privacy_page(contact: str, name: str = "News247") -> str:
    c = html.escape(contact) or "the operator"
    body = f"""
<h1>Privacy policy</h1>
<p class="muted">{html.escape(name)} · last updated 2026-10-09</p>
<section>
<h2>Who we are</h2>
<p>{html.escape(name)} is a personal, non-commercial alert service operated by one individual for their own use
(contact: {c}). It sends market-news alerts to the operator's own WhatsApp number.</p>
<h2>What data is processed</h2>
<ul>
<li><b>The recipient's phone number</b> (the operator's own), configured by the operator, to deliver alerts.</li>
<li><b>Messages exchanged with the WhatsApp number of this service</b>: the alerts it sends, and replies such as
"pause 2h" or "status", which are used only to carry out those commands.</li>
<li><b>Delivery status</b> reported by WhatsApp (sent, delivered, read), shown on the operator's private dashboard.</li>
<li><b>Public news headlines</b> from public websites and feeds. These contain no personal data about users.</li>
</ul>
<p>No other personal data is collected. There are no user accounts, no tracking, no cookies for analytics or
advertising, and nothing is sold, shared or used for marketing.</p>
<h2>How data is used and shared</h2>
<p>Data is used only to deliver alerts to the operator and to respond to the operator's commands. Messages are
transmitted through Meta's WhatsApp Business Platform (Cloud API), which processes them under
<a href="https://www.whatsapp.com/legal/business-policy/">WhatsApp's Business Policy</a> and
<a href="https://www.facebook.com/privacy/policy/">Meta's Privacy Policy</a>. The service is hosted on a cloud server
(Render); no other third parties receive personal data.</p>
<h2>Retention</h2>
<p>Alert history and delivery status are kept on the server for at most 30 days and are erased automatically
(on free hosting, sooner: whenever the server restarts). The recipient phone number is stored only in the
server's configuration.</p>
<h2 id="deletion">Data deletion</h2>
<p>To have all data about you deleted, send an e-mail to {c} with the subject "Delete my data", or reply
<b>STOP</b> to stop all messages. The operator removes the phone number from the configuration and erases the
alert history within 7 days and confirms by e-mail. Because the service only ever messages its own operator,
no other person's data is stored.</p>
<h2>Security</h2>
<p>The dashboard is protected by a secret access token; WhatsApp webhook calls are verified with Meta's
signature; all traffic uses HTTPS.</p>
<h2>Changes and contact</h2>
<p>Changes to this policy are published on this page. Questions: {c}.</p>
</section>"""
    return _page(f"{name}: privacy policy", body, contact)


def terms_page(contact: str, name: str = "News247") -> str:
    c = html.escape(contact) or "the operator"
    body = f"""
<h1>Terms of service</h1>
<p class="muted">{html.escape(name)} · last updated 2026-10-09</p>
<section>
<h2>The service</h2>
<p>{html.escape(name)} is a personal, non-commercial tool that sends market-news alerts to its operator's own
WhatsApp number. It is not offered to the public and there is nothing to buy.</p>
<h2>No financial advice</h2>
<p>Alerts are automated summaries of public news for information only. They are not investment advice and may be
late, incomplete or wrong. Never trade on an alert without checking the original source.</p>
<h2>Messages</h2>
<p>Only the operator receives messages, and only because they configured the service and asked for them. Reply
<b>STOP</b> to pause all messages, or <b>RESUME</b> to restart them. Standard WhatsApp terms apply.</p>
<h2>Availability</h2>
<p>The service is provided as is, without any warranty or guarantee of availability.</p>
<h2>Contact</h2>
<p>{c}</p>
</section>"""
    return _page(f"{name}: terms of service", body, contact)
