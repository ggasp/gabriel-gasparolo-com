#!/usr/bin/env python3
"""Generate the static gasparolo.com site.

The generator is intentionally local-first:
- LinkedIn posts are read from an already-open Chrome tab exposed through CDP.
- X Articles are read the same way when an X Articles tab is open.
- If either source is unavailable, the generator keeps a small curated fallback.
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"


@dataclass(frozen=True)
class LinkItem:
    title: str
    date: str
    url: str


FALLBACK_LINKEDIN_POSTS = [
    LinkItem(
        "API Manager: integrations, telco, and OpenAPIs",
        "2025-08-12",
        "https://www.linkedin.com/feed/update/urn:li:activity:7361163410534793216/",
    ),
    LinkItem(
        "Programming is thinking, not typing",
        "2025-03-11",
        "https://www.linkedin.com/feed/update/urn:li:activity:7305291379562213377/",
    ),
    LinkItem(
        "Technology is frozen experience",
        "2025-03-05",
        "https://www.linkedin.com/feed/update/urn:li:activity:7303195611472809985/",
    ),
    LinkItem(
        "A one-person AI production stack",
        "2025-02-22",
        "https://www.linkedin.com/feed/update/urn:li:activity:7299065700051046400/",
    ),
    LinkItem(
        "A loss becomes a gain over time",
        "2025-02-21",
        "https://www.linkedin.com/feed/update/urn:li:activity:7298672898636984320/",
    ),
    LinkItem(
        "Biometrics in telcos: security and customer experience",
        "2025-02-20",
        "https://www.linkedin.com/feed/update/urn:li:activity:7298380167263969280/",
    ),
]


KNOWN_LINKEDIN_TITLES = {
    "7361163410534793216": "API Manager: integrations, telco, and OpenAPIs",
    "7305291379562213377": "Programming is thinking, not typing",
    "7303195611472809985": "Technology is frozen experience",
    "7299065700051046400": "A one-person AI production stack",
    "7298672898636984320": "A loss becomes a gain over time",
    "7298380167263969280": "Biometrics in telcos: security and customer experience",
}


def fetch_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=4) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_pages(port: int) -> list[dict]:
    try:
        pages = fetch_json(f"http://127.0.0.1:{port}/json/list")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [page for page in pages if page.get("type") == "page"]


def cdp_eval(page: dict, expression: str) -> object:
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Python package websocket-client is required for CDP reads") from exc

    ws = websocket.create_connection(
        page["webSocketDebuggerUrl"], timeout=8, suppress_origin=True
    )
    counter = itertools.count(1)

    def call(method: str, params: dict | None = None) -> dict:
        message_id = next(counter)
        ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == message_id:
                return msg

    result = call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
    ws.close()
    if "exceptionDetails" in result.get("result", {}):
        raise RuntimeError(result["result"]["exceptionDetails"].get("text", "CDP eval failed"))
    return result["result"]["result"].get("value")


def linkedin_activity_date(activity_id: str) -> str:
    # LinkedIn activity IDs encode Unix milliseconds in the high bits.
    timestamp_ms = int(activity_id) >> 22
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def clean_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    line = line.replace("hashtag #", "#")
    return line


def infer_linkedin_title(activity_id: str, card_text: str) -> str:
    if activity_id in KNOWN_LINKEDIN_TITLES:
        return KNOWN_LINKEDIN_TITLES[activity_id]

    skip_patterns = [
        r"^Feed post number",
        r"^Gabriel Gasparolo$",
        r"^CIO \|",
        r"^\d+(mo|yr|d|h)\b",
        r"^Show translation$",
        r"^Like$",
        r"^Comment$",
        r"^Repost$",
        r"^Send$",
        r"^View analytics$",
        r"^\d+[,\d]* impressions$",
    ]
    for raw_line in card_text.splitlines():
        line = clean_line(raw_line)
        if not line:
            continue
        if any(re.search(pattern, line, re.I) for pattern in skip_patterns):
            continue
        if line in {"• You", "…more"}:
            continue
        return line[:92].rstrip(" .")
    return f"LinkedIn post {activity_id}"


def is_site_safe_post(item: LinkItem) -> bool:
    title = item.title.lower()
    personal_markers = [
        "newborn",
        "daughter",
        "hija",
        "familia",
        "boda",
        "wedding",
        "cumpleanos",
        "cumpleaños",
    ]
    if any(marker in title for marker in personal_markers):
        return False
    if len(item.title.strip(" .!?¿¡")) < 20:
        return False
    return True


def read_linkedin_posts(port: int, limit: int) -> list[LinkItem]:
    pages = cdp_pages(port)
    page = next(
        (
            p
            for p in pages
            if "linkedin.com/in/gasparolo/recent-activity" in p.get("url", "")
        ),
        None,
    )
    if not page:
        return []

    expression = r"""
    (async () => {
      for (const y of [0, 1200, 2600, 4200, 6200, 8400, 10800]) {
        window.scrollTo(0, y);
        await new Promise(resolve => setTimeout(resolve, 650));
      }
      return [...document.querySelectorAll('[data-urn], .feed-shared-update-v2')]
        .map(el => ({
          urn: el.getAttribute('data-urn') || '',
          text: el.innerText || ''
        }))
        .filter(item => item.urn.startsWith('urn:li:activity:'));
    })()
    """
    cards = cdp_eval(page, expression)
    posts: list[LinkItem] = []
    seen: set[str] = set()
    for card in cards or []:
        urn = card.get("urn", "")
        activity_id = urn.rsplit(":", 1)[-1]
        text = card.get("text", "")
        if activity_id in seen:
            continue
        seen.add(activity_id)
        if "Gabriel Gasparolo" not in text or "• You" not in text:
            continue
        if "reposted this" in text:
            continue
        item = LinkItem(
            infer_linkedin_title(activity_id, text),
            linkedin_activity_date(activity_id),
            f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/",
        )
        if not is_site_safe_post(item):
            continue
        posts.append(item)
        if len(posts) >= limit:
            break
    return posts


def read_x_articles(port: int, limit: int) -> list[LinkItem]:
    pages = cdp_pages(port)
    page = next(
        (p for p in pages if "x.com/ggasp/articles" in p.get("url", "")),
        None,
    )
    if not page:
        return []

    expression = r"""
    (() => [...document.querySelectorAll('a')]
      .map(a => ({
        title: (a.innerText || a.textContent || '').trim(),
        url: a.href
      }))
      .filter(item => item.url.includes('/i/article/') || item.url.includes('/ggasp/status/'))
      .slice(0, 20))()
    """
    rows = cdp_eval(page, expression)
    articles: list[LinkItem] = []
    seen: set[str] = set()
    for row in rows or []:
        url = row.get("url", "")
        title = clean_line(row.get("title", "")) or "X Article"
        if not url or url in seen:
            continue
        seen.add(url)
        articles.append(LinkItem(title[:92], "", url))
        if len(articles) >= limit:
            break
    return articles


def render_items(items: list[LinkItem]) -> str:
    lines = ['        <ol class="post-list" aria-label="Selected posts">']
    for item in items:
        date_html = (
            f'            <time datetime="{html.escape(item.date)}">{html.escape(item.date)}</time>'
            if item.date
            else '            <span class="post-date">Article</span>'
        )
        lines.extend(
            [
                "          <li>",
                date_html,
                f'            <a href="{html.escape(item.url)}">',
                f"              {html.escape(item.title)}",
                "            </a>",
                "          </li>",
            ]
        )
    lines.append("        </ol>")
    return "\n".join(lines)


def render_language_script() -> str:
    return """    <script>
      (() => {
        const supported = new Set(["en", "es"]);
        const browserLanguages = navigator.languages || [navigator.language || ""];
        const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
        let stored = "";
        try {
          stored = localStorage.getItem("siteLanguage") || "";
        } catch {
          stored = "";
        }
        const inferred =
          browserLanguages.some(lang => lang.toLowerCase().startsWith("es")) ||
          /America\\/(Santiago|Buenos_Aires|Argentina|Cordoba)/.test(timeZone)
            ? "es"
            : "en";

        function setLanguage(language) {
          const lang = supported.has(language) ? language : inferred;
          document.documentElement.lang = lang;
          document.body.dataset.language = lang;
          try {
            localStorage.setItem("siteLanguage", lang);
          } catch {
            /* Private browsing can disable storage; the selector still works. */
          }
          document.querySelectorAll("[data-language-option]").forEach(button => {
            const active = button.dataset.languageOption === lang;
            button.setAttribute("aria-pressed", active ? "true" : "false");
          });
        }

        document.addEventListener("DOMContentLoaded", () => {
          document.querySelectorAll("[data-language-option]").forEach(button => {
            button.addEventListener("click", () => setLanguage(button.dataset.languageOption));
          });
          setLanguage(stored || inferred);
        });
      })();
    </script>
"""


def render_site(linkedin_posts: list[LinkItem], x_articles: list[LinkItem]) -> str:
    selected = linkedin_posts + x_articles
    posts_html = render_items(selected)
    language_script = render_language_script()
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark">
    <meta
      name="description"
      content="Gabriel Gasparolo - technology executive focused on digital transformation, architecture, AI leverage, and resilient systems."
    >
    <meta property="og:title" content="Gabriel Gasparolo">
    <meta
      property="og:description"
      content="Technology executive focused on digital transformation, architecture, AI leverage, and resilient systems."
    >
    <meta property="og:type" content="website">
    <meta property="og:url" content="https://gasparolo.com">
    <meta name="twitter:card" content="summary">
    <title>Gabriel Gasparolo</title>
    <link rel="icon" href="favicon.svg" type="image/svg+xml">
    <link rel="stylesheet" href="styles.css">
{language_script}
  </head>
  <body data-language="en">
    <main class="page" aria-label="Personal site">
      <nav class="language-switcher" aria-label="Language">
        <button type="button" data-language-option="es" aria-pressed="false">ES</button>
        <button type="button" data-language-option="en" aria-pressed="false">EN</button>
      </nav>
      <section class="intro" aria-labelledby="name">
        <p class="eyebrow">gasparolo.com</p>
        <h1 id="name" class="sr-only">Gabriel Gasparolo</h1>
        <pre class="ascii-name" aria-hidden="true"><span class="ascii-orange"> ██████╗  █████╗ ██████╗ ██████╗ ██╗███████╗██╗</span>
<span class="ascii-pink">██╔════╝ ██╔══██╗██╔══██╗██╔══██╗██║██╔════╝██║</span>
<span class="ascii-purple">██║  ███╗███████║██████╔╝██████╔╝██║█████╗  ██║</span>
<span class="ascii-cyan">██║   ██║██╔══██║██╔══██╗██╔══██╗██║██╔══╝  ██║</span>
<span class="ascii-green">╚██████╔╝██║  ██║██████╔╝██║  ██║██║███████╗███████╗</span>
<span class="ascii-comment"> ╚═════╝ ╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝╚═╝╚══════╝╚══════╝</span>
<span class="ascii-gap"></span>
<span class="ascii-orange"> ██████╗  █████╗ ███████╗██████╗  █████╗ ██████╗  ██████╗ ██╗      ██████╗ </span>
<span class="ascii-pink">██╔════╝ ██╔══██╗██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗██║     ██╔═══██╗</span>
<span class="ascii-purple">██║  ███╗███████║███████╗██████╔╝███████║██████╔╝██║   ██║██║     ██║   ██║</span>
<span class="ascii-cyan">██║   ██║██╔══██║╚════██║██╔═══╝ ██╔══██║██╔══██╗██║   ██║██║     ██║   ██║</span>
<span class="ascii-green">╚██████╔╝██║  ██║███████║██║     ██║  ██║██║  ██║╚██████╔╝███████╗╚██████╔╝</span>
<span class="ascii-comment"> ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝ ╚═════╝ </span></pre>
        <p class="lead" data-lang="en">
          CIO at ON*NET FIBRA, focused on technology and innovation, digital
          transformation, technology strategy, and enterprise architecture.
        </p>
        <p class="lead" data-lang="es">
          CIO de ON*NET FIBRA, enfocado en tecnología e innovación, transformación
          digital, estrategia tecnológica y arquitectura empresarial.
        </p>
      </section>

      <section class="section" aria-labelledby="about">
        <h2 id="about"><span data-lang="en">[About]</span><span data-lang="es">[Acerca]</span></h2>
        <p data-lang="en">
          I help organizations turn complex technology estates into clearer platforms,
          stronger teams, and better execution. My work sits where strategy gets real:
          systems architecture, software delivery, automation, data, security posture,
          and the operating discipline needed to make change stick.
        </p>
        <p data-lang="es">
          Ayudo a las organizaciones a convertir entornos tecnológicos complejos en
          plataformas más claras, equipos más fuertes y mejor ejecución. Mi trabajo
          vive donde la estrategia se vuelve real: arquitectura de sistemas, entrega
          de software, automatización, datos, seguridad y la disciplina operativa
          necesaria para sostener el cambio.
        </p>
        <p data-lang="en">
          I am especially interested in how AI changes management work, how telecom and
          infrastructure operations become more autonomous, how APIs and integrations
          reshape operating models, and how leaders can use digital tools without
          losing the human judgement that makes them useful.
        </p>
        <p data-lang="es">
          Me interesa especialmente cómo la IA cambia el trabajo de gestión, cómo las
          operaciones de telecomunicaciones e infraestructura se vuelven más autónomas,
          cómo las APIs e integraciones redefinen los modelos operativos, y cómo los
          líderes pueden usar herramientas digitales sin perder el criterio humano que
          las vuelve útiles.
        </p>
      </section>

      <section class="section" aria-labelledby="work">
        <h2 id="work"><span data-lang="en">[Work]</span><span data-lang="es">[Trabajo]</span></h2>
        <ul class="plain-list" data-lang="en">
          <li>Currently CIO at ON*NET FIBRA.</li>
          <li>Technology leadership in telecom and fiber infrastructure.</li>
          <li>Enterprise architecture from SOA and APIs to microservices and cloud-native patterns.</li>
          <li>Digital transformation programs that connect customer experience, cost, quality, and execution.</li>
          <li>AI, automation, data platforms, and practical productivity systems for teams.</li>
        </ul>
        <ul class="plain-list" data-lang="es">
          <li>Actualmente CIO en ON*NET FIBRA.</li>
          <li>Liderazgo tecnológico en telecomunicaciones e infraestructura de fibra.</li>
          <li>Arquitectura empresarial desde SOA y APIs hasta microservicios y patrones cloud-native.</li>
          <li>Programas de transformación digital que conectan experiencia de cliente, costo, calidad y ejecución.</li>
          <li>IA, automatización, plataformas de datos y sistemas prácticos de productividad para equipos.</li>
        </ul>
      </section>

      <section class="section" aria-labelledby="thinking">
        <h2 id="thinking"><span data-lang="en">[Thinking]</span><span data-lang="es">[Ideas]</span></h2>
        <p data-lang="en">
          My LinkedIn activity circles around technology leadership, API and integration
          platforms, AI, digital transformation, architecture, productivity, and the
          future of infrastructure work.
        </p>
        <p data-lang="es">
          Mi actividad en LinkedIn gira alrededor de liderazgo tecnológico, plataformas
          de APIs e integración, IA, transformación digital, arquitectura,
          productividad y el futuro del trabajo en infraestructura.
        </p>
{posts_html}
        <div class="links" aria-label="Writing and posts">
          <a href="https://www.linkedin.com/in/gasparolo/recent-activity/all/" rel="me"><span data-lang="en">LinkedIn posts</span><span data-lang="es">Posts en LinkedIn</span></a>
          <a href="https://www.linkedin.com/in/gasparolo/" rel="me"><span data-lang="en">LinkedIn profile</span><span data-lang="es">Perfil de LinkedIn</span></a>
          <a href="https://x.com/ggasp" rel="me">X / @ggasp</a>
          <a href="https://x.com/ggasp/articles" rel="me">X Articles</a>
        </div>
      </section>

      <section class="section" aria-labelledby="signal">
        <h2 id="signal">[Signal]</h2>
        <div class="terminal" role="img" aria-label="A Dracula themed system note describing Gabriel's work focus">
          <div class="terminal-row">
            <span class="prompt">focus</span>
            <span class="operator">=</span>
            <span class="value">"technology and innovation"</span>
          </div>
          <div class="terminal-row">
            <span class="prompt">method</span>
            <span class="operator">=</span>
            <span class="value">["strategy", "architecture", "execution", "learning"]</span>
          </div>
          <div class="terminal-row">
            <span class="prompt">bias</span>
            <span class="operator">=</span>
            <span class="value">"execution with taste"</span>
          </div>
        </div>
      </section>

      <section class="section contact" aria-labelledby="contact">
        <h2 id="contact"><span data-lang="en">[Contact]</span><span data-lang="es">[Contacto]</span></h2>
        <p data-lang="en">
          The cleanest public paths are LinkedIn and X.
        </p>
        <p data-lang="es">
          Los caminos públicos más directos son LinkedIn y X.
        </p>
        <div class="links">
          <a href="https://www.linkedin.com/in/gasparolo/" rel="me">LinkedIn</a>
          <a href="https://x.com/ggasp" rel="me">X</a>
        </div>
      </section>
    </main>
  </body>
</html>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, default=18803)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--no-cdp", action="store_true")
    args = parser.parse_args(argv)

    linkedin_posts: list[LinkItem] = []
    x_articles: list[LinkItem] = []
    if not args.no_cdp:
        try:
            linkedin_posts = read_linkedin_posts(args.cdp_port, args.limit)
        except Exception as exc:  # noqa: BLE001 - generator should degrade gracefully.
            print(f"LinkedIn CDP read failed: {exc}", file=sys.stderr)
        try:
            x_articles = read_x_articles(args.cdp_port, max(0, args.limit - len(linkedin_posts)))
        except Exception as exc:  # noqa: BLE001
            print(f"X Articles CDP read failed: {exc}", file=sys.stderr)

    if not linkedin_posts:
        linkedin_posts = FALLBACK_LINKEDIN_POSTS[: args.limit]

    items = (linkedin_posts + x_articles)[: args.limit]
    linkedin_count = min(len(linkedin_posts), len(items))
    x_count = max(0, len(items) - linkedin_count)
    index_html = render_site(linkedin_posts[: args.limit], x_articles)
    (ROOT / "index.html").write_text(index_html, encoding="utf-8")
    PUBLIC_DIR.mkdir(exist_ok=True)
    (PUBLIC_DIR / "index.html").write_text(index_html, encoding="utf-8")
    shutil.copy2(ROOT / "styles.css", PUBLIC_DIR / "styles.css")
    shutil.copy2(ROOT / "favicon.svg", PUBLIC_DIR / "favicon.svg")
    print(
        f"Generated index.html with {linkedin_count} LinkedIn posts and {x_count} X Articles."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
