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
MAX_FULL_LEN = 6000         # chars stored for full expanded view
FETCH_WORKERS = 6           # parallel article fetches
USER_AGENT = "Mozilla/5.0 (compatible; NewsletterDigestBot/1.0)"


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
# Content extraction  —  TWO implementations, swap by commenting one block
# ---------------------------------------------------------------------------

# ── ACTIVE: newspaper3k ────────────────────────────────────────────────────
# Requires: pip install newspaper3k  (added to digest.yml workflow)
def extract_full_content(url):
    """Fetch and extract article body using newspaper3k."""
    try:
        from newspaper import Article
        article = Article(url)
        article.download()
        article.parse()
        text = article.text.strip()
        char_count = len(text)
        if char_count < 100:
            print(f"    [debug] {url[:60]} — extracted {char_count} chars (below 100 threshold, skipped)")
            return ""
        # Trim to max length at a sentence boundary
        if char_count > MAX_FULL_LEN:
            text = text[:MAX_FULL_LEN].rsplit(". ", 1)[0] + "."
        print(f"    [debug] {url[:60]} — extracted {char_count} chars OK")
        return text
    except Exception as ex:
        print(f"    [debug] {url[:60]} — exception: {ex}")
        return ""


# ── COMMENTED OUT: lightweight custom extractor (no pip dependency) ────────
# To switch: comment the newspaper3k block above and uncomment this block.
# Also remove the `pip install newspaper3k` step from digest.yml.
#
# def extract_full_content(url):
#     """Fetch and extract article body using standard library only."""
#     try:
#         raw = fetch_url(url, timeout=12).decode("utf-8", errors="ignore")
#         # Strip <style>, <script>, <nav>, <footer>, <header> blocks
#         for tag in ("style", "script", "nav", "footer", "header", "aside"):
#             raw = re.sub(
#                 rf"<{tag}[^>]*>.*?</{tag}>", " ", raw,
#                 flags=re.DOTALL | re.IGNORECASE
#             )
#         # Collect all <p> tag content
#         paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL | re.IGNORECASE)
#         # Strip inner tags and unescape
#         cleaned = []
#         for p in paragraphs:
#             t = re.sub(r"<[^>]+>", " ", p)
#             t = html.unescape(t)
#             t = re.sub(r"\s+", " ", t).strip()
#             if len(t) > 40:   # skip nav links, captions, etc.
#                 cleaned.append(t)
#         text = "\n\n".join(cleaned)
#         if len(text) < 100:
#             return ""
#         if len(text) > MAX_FULL_LEN:
#             text = text[:MAX_FULL_LEN].rsplit(". ", 1)[0] + "."
#         return text
#     except Exception:
#         return ""


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def strip_html(raw):
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


def truncate(text, length):
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


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
                "date": parse_date(item.findtext("pubDate") or item.findtext("date")),
                "source": source_name,
                "full_content": "",
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
                "date": parse_date(pub),
                "source": source_name,
                "full_content": "",
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
                pool.submit(extract_full_content, e["link"]): e
                for e in all_recent
                if e["link"]
            }
            done = 0
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                try:
                    entry["full_content"] = future.result()
                except Exception as ex:
                    print(f"  [warn] future exception for {entry.get('link','?')[:60]}: {ex}")
                    entry["full_content"] = ""
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

            # Escape content for embedding in data attribute (JSON-safe)
            full_escaped = html.escape(full, quote=True) if full else ""

            if full:
                # Card has full content: show preview + expand toggle
                preview = summary or truncate(full, MAX_SUMMARY_LEN)
                content_block = f"""
              <p class="card-preview" id="{cid}-preview">{html.escape(preview)}</p>
              <div class="card-full" id="{cid}-full" hidden>
                <div class="card-full-text">{html.escape(full)}</div>
              </div>
              <div class="card-actions">
                <button class="btn-expand" onclick="toggleExpand(event, '{cid}')">Read more</button>
                <a class="btn-link" href="{html.escape(e['link'])}" target="_blank" rel="noopener">Open ↗</a>
              </div>"""
            elif summary:
                # Card has RSS summary only, no full content
                content_block = f"""
              <p class="card-preview">{html.escape(summary)}</p>
              <div class="card-actions">
                <a class="btn-link" href="{html.escape(e['link'])}" target="_blank" rel="noopener">Open ↗</a>
              </div>"""
            else:
                # Nothing extracted at all
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
  .card-full-text {{
    font-size: 0.9rem;
    line-height: 1.7;
    color: var(--ink);
    white-space: pre-wrap;
    max-height: 420px;
    overflow-y: auto;
    padding-right: 4px;
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
