#!/usr/bin/env python3
"""
Daily Digest Generator
Reads feeds.json, fetches recent entries from RSS feeds and Gmail,
fetches full article content, and renders a styled index.html digest
grouped by category with inline expand/collapse and a Save for Later section.
"""

import base64
import json
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
import urllib.parse
import urllib.request
import urllib.error

LOOKBACK_DAYS = 1
SAVED_EXPIRE_DAYS = 7
FEEDS_FILE = "feeds.json"
SAVED_FILE = "saved.json"
OUTPUT_FILE = "index.html"
MAX_SUMMARY_LEN = 300
MAX_FULL_LEN = 6000
FETCH_WORKERS = 6
USER_AGENT = "Mozilla/5.0 (compatible; NewsletterDigestBot/1.0)"

# GitHub repo details for Save for Later API calls — injected into page JS
GH_OWNER = os.environ.get("GH_OWNER", "")
GH_REPO  = os.environ.get("GH_REPO", "")
GH_PAT   = os.environ.get("GH_PAT", "")

# Email addresses to redact from newsletter content (comma-separated in env var)
DIGEST_EMAILS = [
    e.strip()
    for e in os.environ.get("DIGEST_EMAIL", "").split(",")
    if e.strip()
]


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
    return text[:length].rsplit(" ", 1)[0] + "..."


# ---------------------------------------------------------------------------
# Save for Later — load, expire, save
# ---------------------------------------------------------------------------

def load_saved():
    """Load saved.json, expiring entries older than SAVED_EXPIRE_DAYS."""
    if not os.path.exists(SAVED_FILE):
        return []
    try:
        with open(SAVED_FILE) as f:
            items = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=SAVED_EXPIRE_DAYS)
    kept = []
    for item in items:
        saved_at = item.get("saved_at", "")
        try:
            dt = datetime.fromisoformat(saved_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(item)
            else:
                print(f"  [saved] expired: {item.get('title','')[:50]}")
        except (ValueError, TypeError):
            kept.append(item)  # keep if date unparseable
    return kept


def write_saved(items):
    """Write the current saved list back to saved.json."""
    with open(SAVED_FILE, "w") as f:
        json.dump(items, f, indent=2, default=str)
    print(f"  [saved] wrote {len(items)} items to {SAVED_FILE}")


# ---------------------------------------------------------------------------
# Gmail API
# ---------------------------------------------------------------------------

def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("gmail", "v1", credentials=creds)


def get_email_html(service, msg_id):
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    def find_html(parts):
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if "parts" in part:
                result = find_html(part["parts"])
                if result:
                    return result
        return ""

    payload = msg.get("payload", {})
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return find_html(payload.get("parts", []))


def fetch_gmail_entries(gmail_config, cutoff):
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"  [warn] Gmail auth failed: {e}")
        return []

    label = gmail_config.get("label", "Daily Digest")
    after_ts = int(cutoff.timestamp())

    try:
        labels_resp = service.users().labels().list(userId="me").execute()
        label_map = {l["name"]: l["id"] for l in labels_resp.get("labels", [])}
        label_id = label_map.get(label)
        if not label_id:
            print(f"  [warn] Gmail label '{label}' not found. Available: {list(label_map.keys())}")
            return []
    except Exception as e:
        print(f"  [warn] Gmail labels fetch failed: {e}")
        return []

    try:
        results = service.users().messages().list(
            userId="me",
            labelIds=[label_id],
            q=f"after:{after_ts}",
            maxResults=50,
        ).execute()
    except Exception as e:
        print(f"  [warn] Gmail label search failed: {e}")
        return []

    messages = results.get("messages", [])
    print(f"  Gmail [{label}]: {len(messages)} messages found")

    entries = []
    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "(no subject)")
            sender = headers.get("From", "")
            date_str = headers.get("Date", "")
            sender_name = re.match(r'^"?([^"<]+)"?\s*<', sender)
            source = sender_name.group(1).strip() if sender_name else sender.split("@")[0]
            date = parse_date(date_str)
            email_html = get_email_html(service, msg_ref["id"])
            plain_len = len(strip_html(email_html))
            entries.append({
                "title": subject,
                "link": f"https://mail.google.com/mail/u/0/#inbox/{msg_ref['id']}",
                "summary": truncate(strip_html(email_html), MAX_SUMMARY_LEN),
                "feed_content": email_html,
                "date": date,
                "source": source,
                "full_content": email_html if plain_len >= 100 else "",
                "full_is_html": plain_len >= 100,
            })
            print(f"    {subject[:50]} — {plain_len} plain chars")
        except Exception as e:
            print(f"  [warn] failed to fetch Gmail message {msg_ref['id']}: {e}")

    return entries


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

# ── ACTIVE: newspaper3k ────────────────────────────────────────────────────
def extract_full_content(url, feed_content=""):
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
#     try:
#         raw = fetch_url(url, timeout=12).decode("utf-8", errors="ignore")
#         for tag in ("style", "script", "nav", "footer", "header", "aside"):
#             raw = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
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
# RSS Feed parsing
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
# Entry collection
# ---------------------------------------------------------------------------

def collect_entries(feeds_config):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    categories = []
    all_rss_entries = []

    for cat in feeds_config["categories"]:
        cat_entries = []
        for feed in cat.get("feeds", []):
            name, url = feed["name"], feed["url"]
            try:
                raw = fetch_url(url)
                parsed = parse_feed(raw, name)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                print(f"  [warn] failed to fetch feed {name}: {e}")
                continue
            recent = [e for e in parsed if e["date"] is None or e["date"] >= cutoff]
            cat_entries.extend(recent)
            all_rss_entries.extend(recent)
            print(f"  {name}: {len(recent)} recent / {len(parsed)} total")

        cat_entries.sort(
            key=lambda e: e["date"] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        if cat_entries:
            categories.append({"name": cat["name"], "entries": cat_entries})

    if "gmail" in feeds_config:
        print("\nFetching Gmail newsletters...")
        gmail_entries = fetch_gmail_entries(feeds_config["gmail"], cutoff)
        if gmail_entries:
            gmail_entries.sort(
                key=lambda e: e["date"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            categories.append({"name": "Newsletters", "entries": gmail_entries})

    if all_rss_entries:
        print(f"\nFetching full content for {len(all_rss_entries)} RSS articles...")
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            future_to_entry = {
                pool.submit(extract_full_content, e["link"], e.get("feed_content", "")): e
                for e in all_rss_entries
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

        with_content = sum(1 for e in all_rss_entries if e["full_content"])
        print(f"  {with_content}/{len(all_rss_entries)} RSS articles had extractable content")

    return categories


# ---------------------------------------------------------------------------
# Email HTML sanitization
# ---------------------------------------------------------------------------

# Patterns that indicate unsubscribe / opt-out links
UNSUB_PATTERNS = re.compile(
    r'(unsubscribe|opt.?out|remove.?me|email.?preferences|manage.?subscription'
    r'|notification.?settings|email.?settings|update.?preferences)',
    re.IGNORECASE,
)

def sanitize_email_html(raw_html):
    """Prepare raw email HTML for safe iframe injection:
    1. Force all links to open in a new tab (target=_blank, rel=noopener)
    2. Neutralize unsubscribe/optout links (disabled visually, href=#)
    3. Redact configured email addresses from link URLs and plain text
    """
    if not raw_html:
        return raw_html

    ATTR_RE   = re.compile(r'href=(["\'])([^"\']*)\1', re.IGNORECASE)
    ANCHOR_RE = re.compile(r'<a(\b[^>]*)>', re.IGNORECASE)
    TAG_RE    = re.compile(r'(<[^>]+>)|([^<]+)')

    def process_anchor(m):
        # Work on the full matched tag without the closing >
        attrs = m.group(1) or ""
        tag_inner = "<a" + attrs  # e.g. <a href="..."

        href_m = ATTR_RE.search(attrs)
        if not href_m:
            # No href — just ensure new tab
            return tag_inner + ' target="_blank" rel="noopener noreferrer">'

        quote = href_m.group(1)
        href  = href_m.group(2)

        # Neutralize unsubscribe / optout links
        if UNSUB_PATTERNS.search(href):
            new_attrs = ATTR_RE.sub('href="#"', attrs, count=1)
            return '<a' + new_attrs + ' title="Unsubscribe link disabled" style="opacity:0.4;cursor:not-allowed;">'

        # Redact email addresses from href (plain and URL-encoded)
        new_href = href
        for email in DIGEST_EMAILS:
            new_href = re.sub(re.escape(email), '[redacted]', new_href, flags=re.IGNORECASE)
            new_href = re.sub(re.escape(urllib.parse.quote(email)), '[redacted]', new_href, flags=re.IGNORECASE)
            new_href = re.sub(re.escape(urllib.parse.quote(email, safe="")), '[redacted]', new_href, flags=re.IGNORECASE)
        new_attrs = ATTR_RE.sub('href=' + quote + new_href + quote, attrs, count=1)

        # Force new tab
        if re.search(r'target=', new_attrs, re.IGNORECASE):
            new_attrs = re.sub(r'target=(["\'])[^"\']*\1', 'target="_blank"', new_attrs, flags=re.IGNORECASE)
        else:
            new_attrs += ' target="_blank"'

        # rel=noopener
        if not re.search(r'rel=', new_attrs, re.IGNORECASE):
            new_attrs += ' rel="noopener noreferrer"'

        return "<a" + new_attrs + ">"

    result = ANCHOR_RE.sub(process_anchor, raw_html)

    # Redact email addresses from plain text (outside HTML tags)
    def redact_text(m):
        part = m.group(0)
        if part.startswith('<'):
            return part
        for email in DIGEST_EMAILS:
            part = re.sub(re.escape(email), '[redacted]', part, flags=re.IGNORECASE)
        return part

    result = TAG_RE.sub(redact_text, result)
    return result


# ---------------------------------------------------------------------------
# Card HTML helper — shared between main digest and saved section
# ---------------------------------------------------------------------------

def render_card(e, cid, show_save_btn=True, show_remove_btn=False):
    date_str = e["date"].strftime("%b %d") if e.get("date") else ""
    summary = e.get("summary", "")
    full = e.get("full_content", "")
    is_html = e.get("full_is_html", False)

    # Serialize entry data for the save button (exclude full_content to keep size down)
    save_data = json.dumps({
        "title":        e.get("title", ""),
        "link":         e.get("link", ""),
        "source":       e.get("source", ""),
        "summary":      summary,
        "date":         e["date"].isoformat() if e.get("date") else "",
        "full_content": full,
        "full_is_html": is_html,
    }, ensure_ascii=False)
    save_data_escaped = html.escape(save_data, quote=True)

    if full:
        preview = summary or truncate(strip_html(full) if is_html else full, MAX_SUMMARY_LEN)
        if is_html:
            iframe_doc = (
                "<!DOCTYPE html><html><head>"
                "<meta charset=\"UTF-8\">"
                "<style>"
                "html,body{margin:0;padding:8px;font-family:sans-serif;font-size:14px;line-height:1.5;}"
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
                + sanitize_email_html(full) +
                "</body></html>"
            )
            srcdoc = html.escape(iframe_doc, quote=True)
            full_block = (
                f'<iframe id="{cid}-iframe" srcdoc="{srcdoc}"'
                f' sandbox="allow-popups allow-popups-to-escape-sandbox"'
                f' style="width:100%;border:none;min-height:400px;max-height:852px;"'
                f' loading="lazy"></iframe>'
            )
        else:
            full_block = f'<div class="card-full-text">{html.escape(full)}</div>'

        action_btns = f'<button class="btn-expand" onclick="toggleExpand(event, \'{cid}\')">Read more</button>'
        action_btns += f' <a class="btn-link" href="{html.escape(e.get("link",""))}" target="_blank" rel="noopener">Open ↗</a>'
        if show_save_btn:
            action_btns += f' <button class="btn-save" onclick="saveForLater(event, this)" data-entry="{save_data_escaped}">Save</button>'
        if show_remove_btn:
            action_btns += f' <button class="btn-remove" onclick="removeSaved(event, this)" data-link="{html.escape(e.get("link",""))}">Remove</button>'

        content_block = f"""
              <p class="card-preview" id="{cid}-preview">{html.escape(preview)}</p>
              <div class="card-full" id="{cid}-full" hidden>
                {full_block}
              </div>
              <div class="card-actions">{action_btns}</div>"""

    elif summary:
        action_btns = f'<a class="btn-link" href="{html.escape(e.get("link",""))}" target="_blank" rel="noopener">Open ↗</a>'
        if show_save_btn:
            action_btns += f' <button class="btn-save" onclick="saveForLater(event, this)" data-entry="{save_data_escaped}">Save</button>'
        if show_remove_btn:
            action_btns += f' <button class="btn-remove" onclick="removeSaved(event, this)" data-link="{html.escape(e.get("link",""))}">Remove</button>'
        content_block = f"""
              <p class="card-preview">{html.escape(summary)}</p>
              <div class="card-actions">{action_btns}</div>"""
    else:
        action_btns = f'<a class="btn-link" href="{html.escape(e.get("link",""))}" target="_blank" rel="noopener">Open ↗</a>'
        if show_remove_btn:
            action_btns += f' <button class="btn-remove" onclick="removeSaved(event, this)" data-link="{html.escape(e.get("link",""))}">Remove</button>'
        content_block = f'<div class="card-actions">{action_btns}</div>'

    return f"""
            <div class="card" id="{cid}">
              <div class="card-meta">
                <span class="card-source">{html.escape(e.get("source",""))}</span>
                <span class="card-date">{date_str}</span>
              </div>
              <h3 class="card-title">{html.escape(e.get("title",""))}</h3>
              {content_block}
            </div>"""


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_html(categories, saved_items):
    generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    cards_html = []
    card_id = 0

    for cat in categories:
        if not cat["entries"]:
            continue
        entry_cards = []
        for e in cat["entries"]:
            card_id += 1
            entry_cards.append(render_card(e, f"card-{card_id}", show_save_btn=True))

        cards_html.append(f"""
        <section class="category">
          <h2 class="category-title">{html.escape(cat['name'])}</h2>
          <div class="card-grid">
            {''.join(entry_cards)}
          </div>
        </section>""")

    # Saved for Later section at the bottom
    if saved_items:
        saved_cards = []
        for item in saved_items:
            card_id += 1
            # Re-parse date if stored as string
            if item.get("date") and isinstance(item["date"], str):
                item["date"] = parse_date(item["date"])
            saved_cards.append(render_card(item, f"card-{card_id}", show_save_btn=False, show_remove_btn=True))

        cards_html.append(f"""
        <section class="category saved-section">
          <h2 class="category-title">Saved for Later</h2>
          <p class="saved-note">Items expire after {SAVED_EXPIRE_DAYS} days. Click Remove to delete immediately.</p>
          <div class="card-grid">
            {''.join(saved_cards)}
          </div>
        </section>""")

    body = "".join(cards_html) if cards_html else '<p class="empty">No recent entries found. Check back soon.</p>'

    # Safely embed PAT into JS — only grants narrow write access to this repo
    gh_owner_js = json.dumps(GH_OWNER)
    gh_repo_js  = json.dumps(GH_REPO)
    gh_pat_js   = json.dumps(GH_PAT)

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
  .saved-section {{ border-top: 2px solid var(--border); padding-top: 32px; }}
  .saved-note {{ font-size: 0.82rem; color: var(--ink-soft); margin: -12px 0 16px; }}
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
    max-height: 520px;
    overflow-y: auto;
    padding-right: 4px;
  }}
  .card-actions {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 4px;
    flex-wrap: wrap;
  }}
  .btn-expand, .btn-save, .btn-remove {{
    background: none;
    border: none;
    padding: 0;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    letter-spacing: 0.01em;
  }}
  .btn-expand {{ color: var(--accent); }}
  .btn-expand:hover {{ text-decoration: underline; }}
  .btn-save {{ color: #2d7ac1; }}
  .btn-save:hover {{ text-decoration: underline; }}
  .btn-save:disabled {{ color: var(--ink-soft); cursor: default; }}
  .btn-remove {{ color: #888; }}
  .btn-remove:hover {{ color: var(--accent); text-decoration: underline; }}
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
var GH_OWNER = {gh_owner_js};
var GH_REPO  = {gh_repo_js};
var GH_PAT   = {gh_pat_js};
var SAVED_FILE = 'saved.json';

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

window.addEventListener('message', function(evt) {{
  if (!evt.data || evt.data.type !== 'resize') return;
  var iframe = document.getElementById(evt.data.id + '-iframe');
  if (!iframe) return;
  iframe.style.minHeight = Math.min(evt.data.h + 16, 852) + 'px';
}});

// --- GitHub API helpers ---

async function ghGetFile(path) {{
  var resp = await fetch(
    'https://api.github.com/repos/' + GH_OWNER + '/' + GH_REPO + '/contents/' + path,
    {{ headers: {{ 'Authorization': 'token ' + GH_PAT, 'Accept': 'application/vnd.github.v3+json' }} }}
  );
  if (resp.status === 404) return {{ content: [], sha: null }};
  if (!resp.ok) throw new Error('GitHub GET failed: ' + resp.status);
  var data = await resp.json();
  var content = JSON.parse(atob(data.content.replace(/\n/g, '')));
  return {{ content: content, sha: data.sha }};
}}

async function ghPutFile(path, content, sha, message) {{
  var body = {{
    message: message,
    content: btoa(unescape(encodeURIComponent(JSON.stringify(content, null, 2)))),
  }};
  if (sha) body.sha = sha;
  var resp = await fetch(
    'https://api.github.com/repos/' + GH_OWNER + '/' + GH_REPO + '/contents/' + path,
    {{
      method: 'PUT',
      headers: {{ 'Authorization': 'token ' + GH_PAT, 'Accept': 'application/vnd.github.v3+json', 'Content-Type': 'application/json' }},
      body: JSON.stringify(body),
    }}
  );
  if (!resp.ok) throw new Error('GitHub PUT failed: ' + resp.status);
}}

// --- Save for Later ---

async function saveForLater(evt, btn) {{
  evt.preventDefault();
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {{
    var entry = JSON.parse(btn.getAttribute('data-entry'));
    entry.saved_at = new Date().toISOString();
    var file = await ghGetFile(SAVED_FILE);
    var items = file.content;
    // Avoid duplicates by link
    if (items.some(function(i) {{ return i.link === entry.link; }})) {{
      btn.textContent = 'Saved';
      return;
    }}
    items.push(entry);
    await ghPutFile(SAVED_FILE, items, file.sha, 'Save: ' + entry.title.substring(0, 60));
    btn.textContent = 'Saved ✓';
  }} catch(e) {{
    console.error(e);
    btn.disabled = false;
    btn.textContent = 'Save failed';
  }}
}}

// --- Remove Saved ---

async function removeSaved(evt, btn) {{
  evt.preventDefault();
  btn.disabled = true;
  btn.textContent = 'Removing...';
  try {{
    var link = btn.getAttribute('data-link');
    var file = await ghGetFile(SAVED_FILE);
    var items = file.content.filter(function(i) {{ return i.link !== link; }});
    await ghPutFile(SAVED_FILE, items, file.sha, 'Remove saved item');
    var card = btn.closest('.card');
    if (card) card.style.opacity = '0.4';
    btn.textContent = 'Removed';
  }} catch(e) {{
    console.error(e);
    btn.disabled = false;
    btn.textContent = 'Remove failed';
  }}
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

    # Load and expire saved items
    print("Loading saved items...")
    saved_items = load_saved()
    write_saved(saved_items)
    print(f"  {len(saved_items)} saved items after expiry check")

    print("\nFetching feeds and articles...")
    categories = collect_entries(feeds_config)

    print("\nRendering HTML...")
    html_out = render_html(categories, saved_items)

    with open(OUTPUT_FILE, "w") as f:
        f.write(html_out)

    total = sum(len(c["entries"]) for c in categories)
    print(f"Done. {total} entries + {len(saved_items)} saved written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
