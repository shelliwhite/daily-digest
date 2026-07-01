# Daily Digest

A self-hosted, auto-updating daily digest of your newsletters and RSS feeds, grouped by category. Runs on GitHub Actions and is served via GitHub Pages.

## How it works

1. `feeds.json` lists your sources grouped into categories you define.
2. `generate.py` fetches each feed, pulls entries from the last 24 hours, fetches the full article content for each entry using `newspaper3k`, and renders a styled `index.html`.
3. A GitHub Action runs `generate.py` daily at 17:00 UTC and commits the updated page.
4. GitHub Pages serves `index.html` at your public URL.

Each card on the digest shows a short preview by default. If full content was extracted, a "Read more" button expands the article inline. "Collapse" folds it back. An "Open ↗" link is always available to go to the original source.

## One-time setup

### 1. Create the repo
Go to github.com → New repository → name it `daily-digest` → Public → Create.

### 2. Unzip and upload the files
Unzip `daily-digest.zip`. The `.github` folder is hidden by default — reveal it first:
- **Mac**: open the unzipped folder in Finder, press **Cmd+Shift+.**
- **Windows**: File Explorer → View tab → check "Hidden items"

You should see 5 items: `feeds.json`, `generate.py`, `index.html`, `README.md`, and `.github`.

On your repo's GitHub page, click **"Add file" → "Upload files"** and drag in the **contents** of the folder (not the folder itself — dragging the parent folder nests everything one level too deep). Commit the upload.

> **Note:** If the `.github` folder doesn't survive the upload, create the workflow file manually: in your repo click "Add file" → "Create new file", type `.github/workflows/digest.yml` as the filename (GitHub creates the folders automatically as you type the slashes), paste in the workflow content, and commit.

### 3. Enable GitHub Pages
Settings → Pages → Source: "Deploy from a branch" → Branch `main`, folder `/ (root)` → Save.

Your digest will be live at `https://YOUR_USERNAME.github.io/daily-digest/` after the first successful run.

### 4. Enable Actions permissions
Settings → Actions → General → Workflow permissions → select **"Read and write permissions"** → Save.

This lets the daily job commit the updated `index.html` back to the repo.

### 5. Set up email-to-RSS for newsletters
For newsletters that only arrive via email (no native RSS feed):
1. Go to **kill-the-newsletter.com**.
2. Enter a name (e.g. "Morning Brew") → get a unique forwarding email address and a matching RSS feed URL.
3. Update your newsletter subscription to that forwarding address, or set up a forwarding rule from your real inbox to it.
4. Add the RSS feed URL to `feeds.json` under the right category.

Blogs and Substacks usually have native RSS already — add their feed URL directly, no Kill the Newsletter step needed. Substack feeds follow the pattern `https://NAME.substack.com/feed`.

### 6. Edit feeds.json
Replace the placeholder entries with your real sources. Add as many categories and feeds as you like — just keep the JSON structure intact:

```json
{
  "categories": [
    {
      "name": "Finance",
      "feeds": [
        { "name": "My Newsletter", "url": "https://kill-the-newsletter.com/feeds/YOUR_ID.xml" },
        { "name": "A Blog", "url": "https://example.com/feed.xml" }
      ]
    },
    {
      "name": "Tech",
      "feeds": [
        { "name": "My Substack", "url": "https://name.substack.com/feed" }
      ]
    }
  ]
}
```

### 7. Trigger the first run
Actions tab → "Generate Daily Digest" → "Run workflow." After ~1-2 minutes (newspaper3k adds some fetch time), check your Pages URL.

## Customizing

- **Lookback window**: change `LOOKBACK_DAYS = 1` in `generate.py` (currently 1 day).
- **Schedule time**: change the cron line in `.github/workflows/digest.yml` (currently `0 17 * * *` = 17:00 UTC daily).
- **Preview length**: `MAX_SUMMARY_LEN = 300` controls how many characters show in the collapsed card preview.
- **Full content length**: `MAX_FULL_LEN = 6000` caps how much article text is stored per entry.
- **Parallel fetches**: `FETCH_WORKERS = 6` controls how many articles are fetched simultaneously during generation.
- **Styling**: all CSS lives in the `render_html()` function in `generate.py` — colors, fonts, card layout are all editable there.

## Swapping the content extractor

`generate.py` ships with two article extraction implementations. `newspaper3k` is active by default. To switch to the lightweight custom extractor (no pip dependency):

1. In `generate.py`, comment out the `newspaper3k` block (the `extract_full_content` function that imports `from newspaper import Article`).
2. Uncomment the custom extractor block directly below it.
3. In `.github/workflows/digest.yml`, remove the `pip install newspaper3k` step.

## Dependencies

- **Python 3.11** (provided by GitHub Actions, no local install needed)
- **newspaper3k** — installed automatically by the GitHub Action via `pip install newspaper3k`

## Troubleshooting

- **Action runs but shows 0 entries**: check the Action log (Actions tab → latest run). It prints a per-feed entry count and a warning for any feed that fails to fetch.
- **CSS junk in card summaries**: the strip_html function detects and discards this automatically. If it still appears, that feed's content is unusually structured — the full-content extractor should handle it better on the next run.
- **"Read more" shows no content**: the article URL may have blocked the bot (403), be paywalled, or be JavaScript-rendered. The "Open ↗" link will still take you to the original.
- **Some sites consistently 403**: try adding them via Kill the Newsletter instead of direct RSS, or accept that those sources won't have inline full content.
- **Generation takes a long time**: with 20+ sources, fetching full content can take 1-2 minutes. This is normal and well within GitHub Actions' limits. Reduce `FETCH_WORKERS` if you hit rate limits on specific sites.
