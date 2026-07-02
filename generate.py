#!/usr/bin/env python3
"""
Daily Digest Generator
Reads feeds.json, fetches recent entries from each RSS/Atom feed,
fetches full article content, and renders a styled index.html digest
grouped by category with inline expand/collapse.
"""

import json
import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

LOOKBACK_DAYS = 1
FEEDS_FILE = "feeds.json"
OUTPUT_FILE = "index.html"
MAX_SUMMARY_LEN = 300       # chars shown in collapsed preview
MAX_FULL_LEN = 6000         # chars stored for full expanded view (plain text only)
FETCH_WORKERS = 6           # parallel article fetches
USER_AGENT = "Mozilla/5.0 (compatible; NewsletterDigestBot/1.0)"

# Tags allowed to pass through sanitize_html (everything else is stripped)
ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "strike",
    "h1", "h2", "h3", "h4",
    "ul", "ol", "li",
    "a", "img",
    "blockquote", "hr", "div", "span", "table", "tr", "td", "th", "tbody", "thead",
}

# Attributes allowed per tag (others stripped to prevent style/script injection)
ALLOWED_ATTRS = {
    "a":   {"href", "title"},
    "img": {"src", "alt", "width", "height"},
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=30, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries:
                print(f"  [retry {attempt}] {url[:60]}: {e}")
    raise last_err


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def strip_html(raw):
    """Remove all HTML tags, returning plain text."""
    if not raw:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if re.match(r"^[\d\s]*\*?\s*\{", text) or "box-sizing" in text[:80]:
        return ""
    return text


def sanitize_html(raw):
    """
    Sanitize email HTML for safe inline display, preserving images and formatting.

    - Removes <style>, <script>, <head>, <html>, <body> wrapper tags
    - Strips tracking pixels (images 1-3px wide/tall or explicitly 1x1)
    - Keeps allowed tags (see ALLOWED_TAGS), strips the rest but keeps their text
    - On allowed tags, keeps only safe attributes (see ALLOWED_ATTRS)
    - Strips all inline style= attributes to prevent email CSS bleeding into the digest
    - Makes relative URLs absolute where possible (skips them otherwise)
    """
    if not raw:
        return ""

    # Remove full block elements we never want
    for tag in ("style", "script", "head"):
        raw = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", raw, flags=re.DOTALL | re.IGNORECASE)

    # Unwrap <html> and <body> wrapper tags (keep their contents)
    for tag in ("html", "body"):
        raw = re.sub(rf"</?{tag}[^>]*>", "", raw, flags=re.IGNORECASE)

    def process_tag(m):
        full_tag = m.group(0)
        # Closing tag — allow if tag is allowed
        if full_tag.startswith("</"):
            tag_name = re.match(r"</\s*(\w+)", full_tag)
            if tag_name and tag_name.group(1).lower() in ALLOWED_TAGS:
                return f"</{tag_name.group(1).lower()}>"
            return ""

        # Self-closing or opening tag
        tag_match = re.match(r"<\s*(\w+)", full_tag)
        if not tag_match:
            return ""
        tag_name = tag_match.group(1).lower()

        if tag_name not in ALLOWED_TAGS:
            return ""  # strip tag entirely (text content still flows through)

        # Parse attributes
        attrs_raw = full_tag[len(tag_match.group(0)):]
        allowed = ALLOWED_ATTRS.get(tag_name, set())
        kept_attrs = []

        for attr_m in re.finditer(r'(\w[\w-]*)(?:\s*=\s*(?:"([^"]*)"\'([^\']*)\'|(\S+)))?', attrs_raw):
            attr_name = attr_m.group(1).lower()
            attr_val = attr_m.group(2) or attr_m.group(3) or attr_m.group(4) or ""

            if attr_name not in allowed:
                continue

            # Block javascript: hrefs
            if attr_name == "href" and attr_val.strip().lower().startswith("javascript:"):
                continue

            # Filter tracking pixels on img tags
            if tag_name == "img" and attr_name in ("width", "height"):
                try:
                    if int(attr_val) <= 3:
                        return ""  # drop the whole img tag
                except ValueError:
                    pass

            kept_attrs.append(f'{attr_name}="{html.escape(attr_val)}"')

        # For imgs, also check for common 1x1 tracking patterns in src
        if tag_name == "img":
            src_m = re.search(r'src\s*=\s*["\']?([^"\'>\s]+)', full_tag, re.IGNORECASE)
            if src_m:
                src = src_m.group(1).lower()
                if any(x in src for x in ("pixel", "track", "beacon", "open.php", "1x1")):
                    return ""

        self_closing = "/" in full_tag[-3:] or tag_name in ("img", "br", "hr")
        attrs_str = (" " + " ".join(kept_attrs)) if kept_attrs else ""
        return f"<{tag_name}{attrs_str}{'/' if self_closing else ''}>"

    sanitized = re.sub(r"<[^>]+>", process_tag, raw)

    # Collapse excessive whitespace but preserve paragraph breaks
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    sanitized = re.sub(r"[ \t]+", " ", sanitized)
    return sanitized.strip()


def truncate(text, length):
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------------------
# Content extraction  —  TWO implementations, swap by commenting one block
# ---------------------------------------------------------------------------

# ── ACTIVE: newspaper3k ────────────────────────────────────────────────────
# Requires: pip install newspaper3k lxml_html_clean  (in requirements.txt)
def extract_full_content(url, feed_content=""):
    """Fetch and extract article body.

    For Kill the Newsletter feeds, the full email HTML is already in the RSS
    feed — inject it raw into a sandboxed iframe so it renders exactly as the
    sender designed it. The iframe sandbox handles isolation; no sanitization needed.
    For all other sources, newspaper3k fetches and extracts plain text.

    Returns a tuple: (content: str, is_html: bool)
    """
    # Kill the Newsletter: use raw feed HTML directly — iframe sandbox handles isolation
    if "kill-the-newsletter.com" in url and feed_content:
        plain_len = len(strip_html(feed_content))
        if plain_len >= 100:
            print(f"    [debug] {url[:60]} — raw feed HTML ({plain_len} plain chars)")
            return feed_content, True
        print(f"    [debug] {url[:60]} — feed content too short ({plain_len} chars)")
        return "", False

    # All other sources: fetch via newspaper3k (returns plain text)
    try:
        from newspaper import Article
        article = Article(url)
        article.download()
        article.parse()
        text = article.text.strip()
        char_count = len(text)
        if char_count < 100:
            print(f"    [debug] {url[:60]} — extracted {char_count} chars (below threshold, skipped)")
            return "", False
        if char_count > MAX_FULL_LEN:
            text = text[:MAX_FULL_LEN].rsplit(". ", 1)[0] + "."
        print(f"    [debug] {url[:60]} — extracted {char_count} chars OK")
        return text, False
    except Exception as ex:
        print(f"    [debug] {url[:60]} — exception: {ex}")
        return "", False


# ── COMMENTED OUT: lightweight custom extractor (no pip dependency) ────────
# To switch: comment the newspaper3k block above and uncomment this block.
# Also remove newspaper3k and lxml_html_clean from requirements.txt.
#
# def extract_full_content(url, feed_content=""):
#     """Fetch and extract article body using standard library only."""
#     # Kill the Newsletter: sanitize feed HTML directly
#     if "kill-the-newsletter.com" in url and feed_content:
#         sanitized = sanitize_html(feed_content)
#         plain_len = len(strip_html(feed_content))
#         if plain_len >= 100:
#             return sanitized, True
#         return "", False
#     # All other sources: extract <p> blocks from fetched page
#     try:
#         raw = fetch_url(url, timeout=12).decode("utf-8", errors="ignore")
#         for tag in ("style", "script", "nav", "footer", "header", "aside"):
#             raw = re.sub(
#                 rf"<{tag}[^>]*>.*?</{tag}>", " ", raw,
#                 flags=re.DOTALL | re.IGNORECASE
#             )
#         paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL | re.IGNORECASE)
#         cleaned = []
#         for p in paragraphs:
#             t = re.sub(r"<[^>]+>", " ", p)
#             t = html.unescape(t)
#             t = re.sub(r"\s+", " ", t).strip()
#             if len(t) > 40:
#                 cleaned.append(t)
#         text = "\n\n".join(cleaned)
#         if len(text) < 100:
#             return "", False
#         if len(text) > MAX_FULL_LEN:
#             text = text[:MAX_FULL_LEN].rsplit(". ", 1)[0] + "."
#         return text, False
#     except Exception:
#         return "", False


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def parse_feed(xml_bytes, source_name):
    entries = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return entries

    if "rss" in root.tag.lower() or root.find("channel") is not None:
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = item.findtext("description") or item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded"
            ) or ""
            entries.append({
                "title": html.unescape(title) or "(untitled)",
                "link": link,
                "summary": truncate(strip_html(desc), MAX_SUMMARY_LEN),
                "feed_content": desc,
                "date": parse_date(item.findtext("pubDate") or item.findtext("date")),
                "source": source_name,
                "full_content": "",
                "full_is_html": False,
            })
    else:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for item in root.findall("a:entry", ns):
            title = (item.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = item.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            summary = (
                item.findtext("a:summary", default="", namespaces=ns)
                or item.findtext("a:content", default="", namespaces=ns)
                or ""
            )
            pub = (
                item.findtext("a:updated", default="", namespaces=ns)
                or item.findtext("a:published", default="", namespaces=ns)
            )
            entries.append({
                "title": html.unescape(title) or "(untitled)",
                "link": link,
                "summary": truncate(strip_html(summary), MAX_SUMMARY_LEN),
                "feed_content": summary,
                "date": parse_date(pub),
                "source": source_name,
                "full_content": "",
                "full_is_html": False,
            })

    return entries


# ---------------------------------------------------------------------------
# Feed + article collection
# ---------------------------------------------------------------------------

def collect_entries(feeds_config):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    categories = []
    all_recent = []

    # Pass 1: fetch all feeds
    for cat in feeds_config["categories"]:
        cat_entries = []
        for feed in cat["feeds"]:
            name, url = feed["name"], feed["url"]
            try:
                raw = fetch_url(url)
                parsed = parse_feed(raw, name)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                print(f"  [warn] failed to fetch feed {name}: {e}")
                continue
            recent = [e for e in parsed if e["date"] is None or e["date"] >= cutoff]
            cat_entries.extend(recent)
            print(f"  {name}: {len(recent)} recent / {len(parsed)} total")

        cat_entries.sort(
            key=lambda e: e["date"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        categories.append({"name": cat["name"], "entries": cat_entries})
        all_recent.extend(cat_entries)

    # Pass 2: fetch full article content in parallel
    if all_recent:
        print(f"\nFetching full content for {len(all_recent)} articles...")
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            future_to_entry = {
                pool.submit(extract_full_content, e["link"], e.get("feed_content", "")): e
                for e in all_recent
                if e["link"]
            }
            done = 0
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                try:
                    content, is_html = future.result()
                    entry["full_content"] = content
                    entry["full_is_html"] = is_html
                except Exception as ex:
                    print(f"  [warn] future exception for {entry.get('link','?')[:60]}: {ex}")
                    entry["full_content"] = ""
                    entry["full_is_html"] = False
                done += 1
                if done % 5 == 0 or done == len(future_to_entry):
                    print(f"  {done}/{len(future_to_entry)} articles fetched")

        with_content = sum(1 for e in all_recent if e["full_content"])
        print(f"  {with_content}/{len(all_recent)} articles had extractable content")

    return categories


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_html(categories):
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    cards_html = []
    card_id = 0

    for cat in categories:
        if not cat["entries"]:
            continue
        entry_cards = []
        for e in cat["entries"]:
            card_id += 1
            cid = f"card-{card_id}"
            date_str = e["date"].strftime("%b %d") if e["date"] else ""
            summary = e["summary"]
            full = e["full_content"]
            is_html = e.get("full_is_html", False)

            if full:
                preview = summary or truncate(strip_html(full) if is_html else full, MAX_SUMMARY_LEN)
                if is_html:
                    # Wrap in sandboxed srcdoc iframe so newsletter renders with
                    # its own layout but cannot affect the digest page.
                    # Auto-resize JS runs inside the iframe and posts height to parent.
                    iframe_doc = (
                        "<!DOCTYPE html><html><head>"
                        "<meta charset=\"UTF-8\">"
                        "<style>"
                        "html,body{margin:0;padding:8px;font-family:sans-serif;"
                        "font-size:14px;line-height:1.5;}"
                        "img{max-width:100%;height:auto;}"
                        "table{max-width:100%;width:100%!important;table-layout:fixed;}"
                        "td,th{word-break:break-word;}"
                        "a{color:#c1492d;}"
                        "</style>"
                        f"<script>"
                        f"window.addEventListener('load',function(){{"
                        f"  var h=document.documentElement.scrollHeight;"
                        f"  parent.postMessage({{type:'resize',id:'{cid}',h:h}},'*');"
                        f"}});"
                        f"</script>"
                        "</head><body>"
                        + full +
                        "</body></html>"
                    )
                    srcdoc = html.escape(iframe_doc, quote=True)
                    full_block = (
                        f'<iframe id="{cid}-iframe" srcdoc="{srcdoc}"'
                        f' sandbox="allow-popups allow-popups-to-escape-sandbox"'
                        f' style="width:100%;border:none;min-height:200px;max-height:600px;"'
                        f' loading="lazy"></iframe>'
                    )
                else:
                    full_block = f'<div class="card-full-text">{html.escape(full)}</div>'

                content_block = f"""
              <p class="card-preview" id="{cid}-preview">{html.escape(preview)}</p>
              <div class="card-full" id="{cid}-full" hidden>
                {full_block}
              </div>
              <div class="card-actions">
                <button class="btn-expand" onclick="toggleExpand(event, '{cid}')">Read more</button>
                <a class="btn-link" href="{html.escape(e['link'])}" target="_blank" rel="noopener">Open ↗</a>
              </div>"""
            elif summary:
                content_block = f"""
              <p class="card-preview">{html.escape(summary)}</p>
              <div class="card-actions">
                <a class="btn-link" href="{html.escape(e['link'])}" target="_blank" rel="noopener">Open ↗</a>
              </div>"""
            else:
                content_block = f"""
              <div class="card-actions">
                <a class="btn-link" href="{html.escape(e['link'])}" target="_blank" rel="noopener">Open ↗</a>
              </div>"""

            entry_cards.append(f"""
            <div class="card" id="{cid}">
              <div class="card-meta">
                <span class="card-source">{html.escape(e['source'])}</span>
                <span class="card-date">{date_str}</span>
              </div>
              <h3 class="card-title">{html.escape(e['title'])}</h3>
              {content_block}
            </div>""")

        cards_html.append(f"""
        <section class="category">
          <h2 class="category-title">{html.escape(cat['name'])}</h2>
          <div class="card-grid">
            {''.join(entry_cards)}
          </div>
        </section>""")

    body = "".join(cards_html) if cards_html else '<p class="empty">No recent entries found. Check back soon.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Digest</title>
<style>
  :root {{
    --bg: #faf8f5;
    --card-bg: #ffffff;
    --ink: #1a1a1a;
    --ink-soft: #6b6b6b;
    --accent: #c1492d;
    --border: #e8e3dc;
    --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }}
  header {{
    padding: 48px 24px 32px;
    text-align: center;
    border-bottom: 1px solid var(--border);
    background: var(--card-bg);
  }}
  header h1 {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 2.4rem;
    margin: 0 0 8px;
    letter-spacing: -0.02em;
  }}
  header p {{ color: var(--ink-soft); font-size: 0.9rem; margin: 0; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 40px 24px 80px; }}
  .category {{ margin-bottom: 48px; }}
  .category-title {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1.4rem;
    border-bottom: 2px solid var(--accent);
    display: inline-block;
    padding-bottom: 6px;
    margin-bottom: 20px;
  }}
  .card-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 18px;
    align-items: start;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    color: inherit;
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .card-meta {{
    display: flex;
    justify-content: space-between;
    font-size: 0.75rem;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
  }}
  .card-date {{ color: var(--ink-soft); font-weight: 400; text-transform: none; }}
  .card-title {{ font-size: 1.05rem; margin: 0; line-height: 1.3; font-weight: 600; }}
  .card-preview {{ font-size: 0.88rem; color: var(--ink-soft); margin: 0; }}
  .card-full {{ margin-top: 4px; border-top: 1px solid var(--border); padding-top: 12px; }}

  /* Plain text content (newspaper3k sources) */
  .card-full-text {{
    font-size: 0.9rem;
    line-height: 1.7;
    color: var(--ink);
    white-space: pre-wrap;
    max-height: 520px;
    overflow-y: auto;
    padding-right: 4px;
  }}

  /* Sanitized HTML content (Kill the Newsletter sources) */
  .card-full-html {{
    font-size: 0.9rem;
    line-height: 1.7;
    color: var(--ink);
    max-height: 520px;
    overflow-y: auto;
    padding-right: 4px;
    overflow-x: hidden;
  }}
  /* Tame email images so they don't overflow the card */
  .card-full-html img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 8px 0;
    border-radius: 4px;
  }}
  /* Rein in email table layouts */
  .card-full-html table {{
    width: 100% !important;
    max-width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    font-size: 0.88rem;
  }}
  .card-full-html td,
  .card-full-html th {{
    padding: 4px 8px;
    word-break: break-word;
    vertical-align: top;
  }}
  /* Tone down email headings so they don't dominate the card */
  .card-full-html h1 {{ font-size: 1.15rem; margin: 12px 0 6px; }}
  .card-full-html h2 {{ font-size: 1.05rem; margin: 10px 0 4px; }}
  .card-full-html h3 {{ font-size: 0.95rem; margin: 8px 0 4px; }}
  .card-full-html p  {{ margin: 0 0 8px; }}
  .card-full-html a  {{ color: var(--accent); }}
  .card-full-html blockquote {{
    border-left: 3px solid var(--border);
    margin: 8px 0;
    padding: 4px 12px;
    color: var(--ink-soft);
    font-style: italic;
  }}

  .card-actions {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 4px;
  }}
  .btn-expand {{
    background: none;
    border: none;
    padding: 0;
    font-size: 0.82rem;
    font-weight: 600;
    color: var(--accent);
    cursor: pointer;
    letter-spacing: 0.01em;
  }}
  .btn-expand:hover {{ text-decoration: underline; }}
  .btn-link {{
    font-size: 0.82rem;
    color: var(--ink-soft);
    text-decoration: none;
  }}
  .btn-link:hover {{ color: var(--ink); text-decoration: underline; }}
  .empty {{ text-align: center; color: var(--ink-soft); padding: 80px 0; }}
  footer {{ text-align: center; padding: 24px; color: var(--ink-soft); font-size: 0.8rem; }}
</style>
</head>
<body>
<header>
  <h1>Daily Digest</h1>
  <p>Generated {generated_at} · last {LOOKBACK_DAYS} days</p>
</header>
<main>
{body}
</main>
<footer>Auto-generated by GitHub Actions</footer>
<script>
function toggleExpand(evt, cid) {{
  evt.preventDefault();
  var btn = evt.target;
  var preview = document.getElementById(cid + '-preview');
  var full = document.getElementById(cid + '-full');
  if (!full) return;
  var expanded = !full.hidden;
  full.hidden = expanded;
  if (preview) preview.style.display = expanded ? '' : 'none';
  btn.textContent = expanded ? 'Read more' : 'Collapse';
}}

// Auto-resize srcdoc iframes after their content loads
window.addEventListener('message', function(evt) {{
  if (!evt.data || evt.data.type !== 'resize') return;
  var iframe = document.getElementById(evt.data.id + '-iframe');
  if (!iframe) return;
  var h = Math.min(evt.data.h + 16, 600); // cap at 600px
  iframe.style.minHeight = h + 'px';
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    with open(FEEDS_FILE, "r") as f:
        feeds_config = json.load(f)

    print("Fetching feeds and articles...")
    categories = collect_entries(feeds_config)

    print("\nRendering HTML...")
    html_out = render_html(categories)

    with open(OUTPUT_FILE, "w") as f:
        f.write(html_out)

    total = sum(len(c["entries"]) for c in categories)
    print(f"Done. {total} entries written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
