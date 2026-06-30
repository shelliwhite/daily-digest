# Daily Digest

A self-hosted, auto-updating daily digest of your newsletters and RSS feeds, grouped by category.

## How it works

1. `feeds.json` lists your sources, grouped into categories.
2. `generate.py` fetches each feed, pulls entries from the last 2 days, and renders `index.html`.
3. A GitHub Action runs `generate.py` daily at 17:00 UTC and commits the updated page.
4. GitHub Pages serves `index.html` at your public URL.

## One-time setup

### 1. Create the repo
- Go to github.com → New repository → name it e.g. `daily-digest` → Public → Create.
- Upload these files (`feeds.json`, `generate.py`, `.github/workflows/digest.yml`, this `README.md`) to the repo root, preserving the `.github/workflows/` folder path.
  - Easiest way: on the repo page, click "Add file" → "Upload files," drag everything in (GitHub preserves folder structure from drag-and-drop of a zip's extracted contents, or use `git` locally — see below).

**Using git locally instead (recommended if comfortable with command line):**
```bash
git clone https://github.com/YOUR_USERNAME/daily-digest.git
cd daily-digest
# copy in feeds.json, generate.py, .github/workflows/digest.yml, README.md
git add .
git commit -m "Initial setup"
git push
```

### 2. Enable GitHub Pages
- In your repo: Settings → Pages.
- Under "Build and deployment," set Source to **"Deploy from a branch."**
- Branch: `main`, folder: `/ (root)`. Save.
- Your digest will be live at `https://YOUR_USERNAME.github.io/daily-digest/` after the first successful run.

### 3. Enable Actions permissions
- Settings → Actions → General → scroll to "Workflow permissions."
- Select **"Read and write permissions."** Save.
- This lets the daily job commit the updated `index.html` back to the repo.

### 4. Set up email-to-RSS for newsletters
For any newsletter that only sends email (no native RSS feed):
1. Go to **kill-the-newsletter.com**.
2. Enter a name (e.g. "Morning Brew") → it gives you a unique email address and a matching RSS feed URL.
3. Go to the newsletter's site (or your email's "change subscription" settings) and update your subscription email to that generated address — or set up an email forwarding rule from your real inbox to it.
4. Copy the RSS feed URL into `feeds.json` under the right category.

For newsletters/blogs that already have RSS (most blogs, Substacks, etc.), just add their feed URL directly — no Kill the Newsletter step needed. Substack feeds are usually `https://NAME.substack.com/feed`.

### 5. Edit feeds.json
Replace the placeholder entries with your real sources, grouped under whatever categories you want (Finance, Tech, Travel, etc.). Add as many categories/feeds as you like — just keep the JSON structure intact.

### 6. Trigger the first run
- Go to the Actions tab → "Generate Daily Digest" → "Run workflow" (this uses the manual `workflow_dispatch` trigger so you don't have to wait for the daily schedule).
- After it finishes (~30 seconds), check your Pages URL.

## Customizing

- **Lookback window**: change `LOOKBACK_DAYS = 1` in `generate.py`.
- **Schedule time**: change the cron line in `.github/workflows/digest.yml` (currently `0 12 * * *` = 12:00 UTC daily).
- **Styling**: all CSS is inline in the `render_html()` function in `generate.py` — colors, fonts, card layout, etc. are all editable there.
- **Summary length**: `MAX_SUMMARY_LEN = 220` controls how much of each entry's description shows on the card.

## Troubleshooting

- If a feed shows 0 entries, check the Action's log output (Actions tab → latest run) — it prints a warning for any feed that fails to fetch, plus a per-feed entry count.
- Some sites block generic bots; if a feed consistently 403s, that source may need a different feed URL or isn't fetchable this way.
