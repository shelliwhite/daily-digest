# Daily Digest

A self-hosted, auto-updating daily digest of your newsletters and RSS feeds, grouped by category. Runs on GitHub Actions and is served via GitHub Pages.

## How it works

1. `feeds.json` lists your RSS sources grouped into categories you define.
2. Gmail newsletters are fetched directly via the Gmail API using a label you apply.
3. `generate.py` fetches each source, pulls entries from the last 24 hours, and renders a styled `index.html`.
4. A GitHub Action runs `generate.py` daily at 17:00 UTC and commits the updated page.
5. GitHub Pages serves `index.html` at your public URL.

Each card shows a short preview by default. "Read more" expands the full content inline. For Gmail newsletters, the email renders in a sandboxed iframe exactly as designed — all links open in new tabs, unsubscribe links are neutralized, and your email address is redacted. "Open ↗" links to the original source. "Save" saves an item for later; saved items expire after 7 days.

## One-time setup

### 1. Create the repo
Go to github.com → New repository → name it `daily-digest` → Public → Create.

### 2. Upload the files
Unzip `daily-digest.zip`. The `.github` folder is hidden by default — reveal it first:
- **Mac**: open the unzipped folder in Finder, press **Cmd+Shift+.**
- **Windows**: File Explorer → View tab → check "Hidden items"

On your repo's GitHub page, click **"Add file" → "Upload files"** and drag in the **contents** of the folder (not the folder itself). Commit the upload.

> **Tip:** If the `.github` folder doesn't survive upload, create the workflow file manually: "Add file" → "Create new file" → type `.github/workflows/digest.yml` as the filename → paste the workflow content → commit.

### 3. Enable GitHub Pages
Settings → Pages → Source: "Deploy from a branch" → Branch `main`, folder `/ (root)` → Save.

Your digest will be live at `https://YOUR_USERNAME.github.io/daily-digest/` after the first successful run.

### 4. Enable Actions permissions
Settings → Actions → General → Workflow permissions → select **"Read and write permissions"** → Save.

### 5. Set up Gmail API

#### Create a Google Cloud project
1. Go to console.cloud.google.com → New Project → name it `daily-digest` → Create
2. APIs & Services → Library → search "Gmail API" → Enable
3. APIs & Services → OAuth consent screen → External → fill in app name and email → Save
4. APIs & Services → Credentials → "+ Create Credentials" → OAuth client ID → **Web application** → add `https://developers.google.com/oauthplayground` as an authorized redirect URI → Create

#### Generate a refresh token
1. Go to developers.google.com/oauthplayground
2. Click the gear icon → check "Use your own OAuth credentials" → enter your web client Client ID and Secret → Close
3. In Step 1, expand "Gmail API v1" → check `https://www.googleapis.com/auth/gmail.readonly` → Authorize APIs
4. Sign in with the Gmail account that receives your newsletters
5. Step 2 → "Exchange authorization code for tokens" → copy the `refresh_token` value

#### Add GitHub Secrets
Settings → Secrets and variables → Actions → add these secrets:
- `GMAIL_REFRESH_TOKEN` — the refresh token from OAuth Playground
- `GMAIL_CLIENT_ID` — from your web application credentials JSON
- `GMAIL_CLIENT_SECRET` — from your web application credentials JSON
- `GH_PAT` — a GitHub Fine-grained token with Contents read/write on this repo only (named `GH_PAT`, not `GITHUB_PAT`)
- `DIGEST_EMAIL` — your newsletter email address(es), comma-separated (e.g. `name@gmail.com,name+newsletter@gmail.com`)

### 6. Set up Gmail label and filters
1. In Gmail, create a label called exactly `Daily Digest`
2. For each newsletter, create a filter: Settings → Filters → Create new filter → enter the sender address → apply the `Daily Digest` label
3. Filters only apply to new incoming mail — existing emails won't be labeled retroactively

### 7. Edit feeds.json
Replace the placeholder RSS feeds with your real sources. The `gmail` block at the top level controls Gmail fetching — set the label name if you used something other than `Daily Digest`:

```json
{
  "gmail": {
    "label": "Daily Digest"
  },
  "categories": [
    {
      "name": "News",
      "feeds": [
        { "name": "Example Blog", "url": "https://example.com/feed.xml" }
      ]
    }
  ]
}
```

Substack feeds follow the pattern `https://NAME.substack.com/feed`. Most blogs have a native RSS feed — add them directly here.

### 8. Trigger the first run
Actions tab → "Generate Daily Digest" → "Run workflow." After ~1-2 minutes, check your Pages URL.

## Customizing

- **Lookback window**: change `LOOKBACK_DAYS = 1` in `generate.py`
- **Schedule time**: change the cron line in `digest.yml` (currently `0 17 * * *` = 17:00 UTC daily)
- **Saved item expiry**: change `SAVED_EXPIRE_DAYS = 7` in `generate.py`
- **Preview length**: `MAX_SUMMARY_LEN = 300` controls collapsed card preview length
- **Full content length**: `MAX_FULL_LEN = 6000` caps plain-text article extraction
- **Parallel fetches**: `FETCH_WORKERS = 6` controls simultaneous RSS article fetches
- **Styling**: all CSS is in the `render_html()` function in `generate.py`

## Swapping the content extractor

Two article extraction implementations are included for RSS feeds. `newspaper3k` is active by default. To switch to the lightweight custom extractor (no pip dependency):

1. Comment out the active `extract_full_content` function in `generate.py`
2. Uncomment the custom extractor block below it
3. Remove `newspaper3k` and `lxml_html_clean` from `requirements.txt`

## Dependencies

- **Python 3.11** — provided by GitHub Actions
- **newspaper3k** + **lxml_html_clean** — RSS article extraction
- **google-auth**, **google-auth-oauthlib**, **google-api-python-client** — Gmail API

All installed automatically by the Action via `pip install -r requirements.txt`.

## Troubleshooting

- **0 entries / feeds failing**: check the Action log for per-feed warnings. Common causes: feed URL changed, server temporarily down.
- **Gmail unauthorized_client error**: your `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` must match the credentials used to generate the refresh token (web application client, not desktop app client).
- **Gmail label not found**: the label in `feeds.json` must match your Gmail label exactly, including capitalization.
- **"Read more" shows no content**: the article URL may be paywalled or JS-rendered. "Open ↗" still links to the original.
- **Generation slow**: fetching full content for 20+ RSS articles can take 1-2 minutes. Normal and within GitHub Actions limits.
- **Pages deployment failing**: check githubstatus.com for incidents. If Pages source resets, go to Settings → Pages and set it back to "Deploy from a branch" → `main` → `/ (root)`.
- **Save button failing**: ensure the `GH_PAT` secret is a fine-grained token with Contents read/write on the `daily-digest` repo. Tokens expire — see "Regenerating the PAT" below.

## Regenerating the PAT

Your GitHub Personal Access Token expires 6 months after creation. When it expires the "Save" button on cards will silently fail. GitHub will also send you an email warning ~7 days before expiry.

### Steps to regenerate

1. Go to **github.com → Settings** (your account settings, top right) → **Developer settings** → **Personal access tokens** → **Fine-grained tokens**
2. Find the existing token (named whatever you called it when you created it) → click it → click **"Regenerate token"**
3. Set a new expiration (another 6 months, or choose a longer period) → confirm regeneration
4. Copy the new token value — you only see it once

### Update the GitHub Secret

1. Go to your `daily-digest` **repo** → **Settings** → **Secrets and variables** → **Actions**
2. Find `GH_PAT` in the list → click the pencil/edit icon
3. Paste the new token value → click **"Update secret"**

That's it — no code changes needed. The next digest run will automatically use the new token.

### Setting a calendar reminder

Since GitHub sends an email warning ~7 days before expiry, you'll get a heads up. But it's worth setting a calendar reminder for ~5 months from now so you're not caught off guard if the email gets buried.
