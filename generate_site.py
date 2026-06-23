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
ASSET_VERSION = "202606230712"


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


def render_structured_data() -> str:
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": "https://gabriel.gasparolo.com/#website",
                "url": "https://gabriel.gasparolo.com/",
                "name": "Gabriel Gasparolo",
                "inLanguage": ["en", "es"],
            },
            {
                "@type": "ProfilePage",
                "@id": "https://gabriel.gasparolo.com/#profile",
                "url": "https://gabriel.gasparolo.com/",
                "name": "Gabriel Gasparolo",
                "isPartOf": {"@id": "https://gabriel.gasparolo.com/#website"},
                "about": {"@id": "https://gabriel.gasparolo.com/#person"},
                "inLanguage": ["en", "es"],
            },
            {
                "@type": "Person",
                "@id": "https://gabriel.gasparolo.com/#person",
                "name": "Gabriel Gasparolo",
                "givenName": "Gabriel",
                "familyName": "Gasparolo",
                "jobTitle": "CIO",
                "worksFor": {"@id": "https://www.onnetfibra.cl/#organization"},
                "description": (
                    "Technology executive focused on telecom infrastructure, "
                    "enterprise architecture, software delivery, data, automation, and AI."
                ),
                "knowsAbout": [
                    "telecommunications",
                    "fiber infrastructure",
                    "enterprise architecture",
                    "API platforms",
                    "software delivery",
                    "digital transformation",
                    "automation",
                    "artificial intelligence",
                    "data platforms",
                ],
                "sameAs": [
                    "https://www.linkedin.com/in/gasparolo/",
                    "https://x.com/ggasp",
                ],
            },
            {
                "@type": "Organization",
                "@id": "https://www.onnetfibra.cl/#organization",
                "name": "ON*NET FIBRA",
                "url": "https://www.onnetfibra.cl/",
            },
        ],
    }
    json_ld = json.dumps(data, ensure_ascii=False, indent=6)
    return f'    <script type="application/ld+json">\n{json_ld}\n    </script>\n'


def render_signal_json(language: str) -> str:
    descriptions = {
        "en": (
            "Technology executive working on telecom infrastructure, enterprise "
            "architecture, software delivery, automation, data, and AI."
        ),
        "es": (
            "Ejecutivo de tecnología trabajando en infraestructura telco, arquitectura "
            "empresarial, desarrollo de software, automatización, datos e IA."
        ),
    }
    knows_about = {
        "en": [
            "telecommunications",
            "fiber infrastructure",
            "enterprise architecture",
            "API platforms",
            "automation",
            "artificial intelligence",
        ],
        "es": [
            "telecomunicaciones",
            "infraestructura de fibra",
            "arquitectura empresarial",
            "plataformas API",
            "automatización",
            "inteligencia artificial",
        ],
    }
    data = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": "Gabriel Gasparolo",
        "jobTitle": "CIO",
        "worksFor": {
            "@type": "Organization",
            "name": "ON*NET FIBRA",
        },
        "description": descriptions[language],
        "knowsAbout": knows_about[language],
        "sameAs": [
            "https://www.linkedin.com/in/gasparolo/",
            "https://x.com/ggasp",
        ],
    }
    return highlight_json(json.dumps(data, ensure_ascii=False, indent=2))


def highlight_json(json_text: str) -> str:
    def highlight_punctuation(text: str) -> str:
        escaped = html.escape(text)
        return re.sub(
            r"([{}\[\]:,])",
            r'<span class="json-punctuation">\1</span>',
            escaped,
        )

    parts: list[str] = []
    position = 0
    for match in re.finditer(r'"(?:\\.|[^"\\])*"', json_text):
        parts.append(highlight_punctuation(json_text[position : match.start()]))
        lookahead = json_text[match.end() :]
        token_class = "json-key" if re.match(r"\s*:", lookahead) else "json-string"
        parts.append(
            f'<span class="{token_class}">{html.escape(match.group(0))}</span>'
        )
        position = match.end()
    parts.append(highlight_punctuation(json_text[position:]))
    return "".join(parts)


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
          document.querySelectorAll("[data-lang]").forEach(element => {
            element.style.display = element.dataset.lang === lang ? "" : "none";
          });
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
    structured_data = render_structured_data()
    signal_json_en = render_signal_json("en")
    signal_json_es = render_signal_json("es")
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
    <meta property="og:url" content="https://gabriel.gasparolo.com/">
    <meta name="twitter:card" content="summary">
    <title>Gabriel Gasparolo</title>
    <link rel="canonical" href="https://gabriel.gasparolo.com/">
    <link rel="icon" href="favicon.svg" type="image/svg+xml">
    <link rel="stylesheet" href="styles.css?v={ASSET_VERSION}">
{structured_data}
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
          I work where technology strategy has to survive real operations: fiber,
          telecom platforms, architecture, software delivery, data, automation, and AI.
        </p>
        <p class="lead" data-lang="es">
          Trabajo donde la estrategia tecnológica tiene que sobrevivir a la operación
          real: fibra, plataformas telco, arquitectura, desarrollo, datos,
          automatización e IA.
        </p>
      </section>

      <section class="section" aria-labelledby="about">
        <h2 id="about"><span data-lang="en">[About]</span><span data-lang="es">[Acerca]</span></h2>
        <p data-lang="en">
          I grew up building systems, then leading the teams that have to run them.
          That changes how I look at transformation: the slide is the easy part. The
          hard part is making the architecture, vendors, metrics, and people line up
          well enough that the change actually ships.
        </p>
        <p data-lang="es">
          Crecí construyendo sistemas y después liderando los equipos que tienen que
          operarlos. Eso cambia la forma de mirar la transformación: la presentación es
          la parte fácil. Lo difícil es hacer que arquitectura, proveedores, métricas y
          personas se ordenen lo suficiente como para que el cambio llegue a producción.
        </p>
        <p data-lang="en">
          These days I spend a lot of time on AI as management leverage, on telecom
          operations becoming more autonomous, and on the old problem of integrations:
          if the APIs are weak, the operating model usually is too.
        </p>
        <p data-lang="es">
          Últimamente estoy muy metido en la IA como palanca de gestión, en operaciones
          telco cada vez más autónomas y en el viejo problema de las integraciones: si
          las APIs son débiles, normalmente el modelo operativo también lo es.
        </p>
      </section>

      <section class="section" aria-labelledby="work">
        <h2 id="work"><span data-lang="en">[Work]</span><span data-lang="es">[Trabajo]</span></h2>
        <ul class="plain-list" data-lang="en">
          <li>CIO at ON*NET FIBRA in Chile.</li>
          <li>Technology leadership for telecom and fiber infrastructure.</li>
          <li>Enterprise architecture, integration platforms, APIs, microservices, and legacy modernization.</li>
          <li>Transformation programs tied to customer experience, operating cost, quality, and delivery discipline.</li>
          <li>Practical AI and automation: tools that remove friction from teams, not demos that die in a slide deck.</li>
        </ul>
        <ul class="plain-list" data-lang="es">
          <li>Actualmente CIO en ON*NET FIBRA.</li>
          <li>Liderazgo tecnológico en telecomunicaciones e infraestructura de fibra en Chile.</li>
          <li>Arquitectura empresarial, plataformas de integración, APIs, microservicios y modernización de legados.</li>
          <li>Programas de transformación conectados con experiencia de cliente, costo operativo, calidad y disciplina de entrega.</li>
          <li>IA y automatización práctica: herramientas que le sacan fricción al trabajo, no demos que mueren en una presentación.</li>
        </ul>
      </section>

      <section class="section" aria-labelledby="thinking">
        <h2 id="thinking"><span data-lang="en">[Thinking]</span><span data-lang="es">[Ideas]</span></h2>
        <p data-lang="en">
          I write mostly about the things I keep testing in practice: technology
          leadership, APIs, AI, architecture, productivity, and what happens when
          infrastructure work gets more automated.
        </p>
        <p data-lang="es">
          Escribo sobre las cosas que voy probando en la práctica: liderazgo
          tecnológico, APIs, IA, arquitectura, productividad y qué pasa cuando el
          trabajo de infraestructura se automatiza cada vez más.
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
        <h2 id="signal"><span data-lang="en">[Signal]</span><span data-lang="es">[Señal]</span></h2>
        <p data-lang="en">
          A small structured signal for people, crawlers, APIs, and LLMs.
        </p>
        <p data-lang="es">
          Una pequeña señal estructurada para personas, crawlers, APIs y LLMs.
        </p>
        <pre class="terminal json-signal" data-lang="en" aria-label="JSON-LD signal for Gabriel Gasparolo"><code>{signal_json_en}</code></pre>
        <pre class="terminal json-signal" data-lang="es" aria-label="Señal JSON-LD para Gabriel Gasparolo"><code>{signal_json_es}</code></pre>
      </section>

      <section class="section contact" aria-labelledby="contact">
        <h2 id="contact"><span data-lang="en">[Contact]</span><span data-lang="es">[Contacto]</span></h2>
        <p data-lang="en">
          The easiest public paths are LinkedIn and X.
        </p>
        <p data-lang="es">
          Los caminos públicos más simples son LinkedIn y X.
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
