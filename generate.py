#!/usr/bin/env python3
"""
Daily Digest Generator
Reads feeds.json, fetches recent entries from each RSS/Atom feed,
and renders a styled index.html digest grouped by category.
"""

import json
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error

LOOKBACK_DAYS = 1
FEEDS_FILE = "feeds.json"
OUTPUT_FILE = "index.html"
MAX_SUMMARY_LEN = 220
USER_AGENT = "Mozilla/5.0 (compatible; NewsletterDigestBot/1.0)"


def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate(text, length):
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


def parse_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    # Try RFC 822 (RSS standard)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        pass
    # Try ISO 8601 (Atom standard)
    try:
        cleaned = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    return None


def parse_feed(xml_bytes, source_name):
    """Parse RSS 2.0 or Atom feed bytes into a list of entry dicts."""
    entries = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return entries

    tag = root.tag.lower()

    if "rss" in tag or root.find("channel") is not None:
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else []
        for item in items:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = item.findtext("description") or item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded"
            ) or ""
            pub_date = item.findtext("pubDate") or item.findtext("date")
            entries.append({
                "title": html.unescape(title) or "(untitled)",
                "link": link,
                "summary": truncate(strip_html(desc), MAX_SUMMARY_LEN),
                "date": parse_date(pub_date),
                "source": source_name,
            })
    else:
        # Atom
        ns = {"a": "http://www.w3.org/2005/Atom"}
        items = root.findall("a:entry", ns)
        for item in items:
            title = (item.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_el = item.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            summary = item.findtext("a:summary", default="", namespaces=ns) or \
                item.findtext("a:content", default="", namespaces=ns) or ""
            pub_date = item.findtext("a:updated", default="", namespaces=ns) or \
                item.findtext("a:published", default="", namespaces=ns)
            entries.append({
                "title": html.unescape(title) or "(untitled)",
                "link": link,
                "summary": truncate(strip_html(summary), MAX_SUMMARY_LEN),
                "date": parse_date(pub_date),
                "source": source_name,
            })

    return entries


def collect_entries(feeds_config):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    categories = []

    for cat in feeds_config["categories"]:
        cat_entries = []
        for feed in cat["feeds"]:
            name = feed["name"]
            url = feed["url"]
            try:
                raw = fetch_url(url)
                parsed = parse_feed(raw, name)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                print(f"  [warn] failed to fetch {name}: {e}")
                continue

            recent = [e for e in parsed if e["date"] is None or e["date"] >= cutoff]
            cat_entries.extend(recent)
            print(f"  {name}: {len(recent)} recent / {len(parsed)} total")

        cat_entries.sort(key=lambda e: e["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        categories.append({"name": cat["name"], "entries": cat_entries})

    return categories


def render_html(categories):
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    cards_html = []
    for cat in categories:
        if not cat["entries"]:
            continue
        entry_cards = []
        for e in cat["entries"]:
            date_str = e["date"].strftime("%b %d") if e["date"] else ""
            entry_cards.append(f"""
            <a class="card" href="{html.escape(e['link'])}" target="_blank" rel="noopener">
              <div class="card-meta">
                <span class="card-source">{html.escape(e['source'])}</span>
                <span class="card-date">{date_str}</span>
              </div>
              <h3 class="card-title">{html.escape(e['title'])}</h3>
              <p class="card-summary">{html.escape(e['summary'])}</p>
            </a>""")

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
    margin: 0;
    padding: 0;
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
  header p {{
    color: var(--ink-soft);
    font-size: 0.9rem;
    margin: 0;
  }}
  main {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 40px 24px 80px;
  }}
  .category {{
    margin-bottom: 48px;
  }}
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
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 18px;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    text-decoration: none;
    color: inherit;
    box-shadow: var(--shadow);
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    display: flex;
    flex-direction: column;
  }}
  .card:hover {{
    transform: translateY(-2px);
    box-shadow: 0 4px 14px rgba(0,0,0,0.08);
  }}
  .card-meta {{
    display: flex;
    justify-content: space-between;
    font-size: 0.75rem;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 10px;
    font-weight: 600;
  }}
  .card-date {{
    color: var(--ink-soft);
    font-weight: 400;
    text-transform: none;
  }}
  .card-title {{
    font-size: 1.05rem;
    margin: 0 0 8px;
    line-height: 1.3;
  }}
  .card-summary {{
    font-size: 0.88rem;
    color: var(--ink-soft);
    margin: 0;
    flex-grow: 1;
  }}
  .empty {{
    text-align: center;
    color: var(--ink-soft);
    padding: 80px 0;
  }}
  footer {{
    text-align: center;
    padding: 24px;
    color: var(--ink-soft);
    font-size: 0.8rem;
  }}
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
</body>
</html>
"""


def main():
    with open(FEEDS_FILE, "r") as f:
        feeds_config = json.load(f)

    print("Fetching feeds...")
    categories = collect_entries(feeds_config)

    print("Rendering HTML...")
    html_out = render_html(categories)

    with open(OUTPUT_FILE, "w") as f:
        f.write(html_out)

    total = sum(len(c["entries"]) for c in categories)
    print(f"Done. {total} entries written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
