# Wayback Website Archiver

An automated Windows desktop and web application to discover website URLs and submit them to the Internet Archive Wayback Machine Save Page Now (SPN2) with live progress tracking, deduplication, rate-limiting safety, and visual reports.

---

## 🌟 Key Features

- **Zero Manual URL Collection**: Enter a single website homepage URL (e.g. `https://americantowingut.com/`) and the app automatically discovers all public pages.
- **Three Discovery Modes**:
  - **🚀 Smart Discovery (Default)**: Combines Header/Navigation Menus + Sitemaps (`sitemap.xml`, `wp-sitemap.xml`) + BFS Internal Link Crawling.
  - **🗺️ Sitemap Only**: Discovers links from XML sitemaps, sitemap indexes, and `robots.txt`.
  - **🧭 Menu Only**: Targets pages exposed in `<nav>`, `<header>`, Elementor menus, and WordPress navigation blocks.
- **WordPress & Page Builder Friendly**: Seamlessly discovers menus built with Elementor, Gutenberg, standard WP themes, or custom HTML.
- **Official Internet Archive SPN2 API**: Authenticates using official S3 Access & Secret keys, tracks background capture jobs, and extracts verified Wayback snapshot URLs.
- **Intelligent Rate-Limiting**: Respects Internet Archive limits with configurable delays (default: 10s), exponential backoffs, and HTTP 429 Retry-After handling.
- **Existing Snapshot Check**: Checks Wayback Availability API and skips pages captured recently (e.g. within 30 days) to save time and API quota.
- **Live Progress & Interactive UI**: Real-time progress bar, live stage indicators, and responsive results table.
- **One-Click Resumption**: If the application is closed midway, you can resume the previous job from the exact page it left off.
- **Instant Export**: Export clean reports to `CSV`, `JSON`, and a visual `HTML` dashboard with direct snapshot links.

---

## 🚀 Quick Start (One-Click Windows Launcher)

1. Simply double-click **`run.bat`** (inside `wayback-website-archiver/`) or **`run_archiver.bat`** in the main folder.
2. Your default web browser will automatically open:
   ```
   http://127.0.0.1:8000
   ```
3. Type or paste your target website URL:
   ```
   https://americantowingut.com/
   ```
4. Click **`🚀 Smart Discovery`**.
5. Once pages are discovered, review the checkmarks and click **`💾 ARCHIVE SELECTED PAGES`**.
6. Watch the live progress and open your generated snapshots!

---

## 🔑 Internet Archive Keys Configuration

Your application comes **pre-configured** with the keys you provided in `data/settings.json`.

If you ever need to generate new keys or update them:

1. Create a free account at [archive.org](https://archive.org) (if you don't already have one).
2. Go to: **[https://archive.org/account/s3.php](https://archive.org/account/s3.php)**
3. Click **"Generate New Keys"** (or view your current keys).
4. In the app, click **Settings** in the top navigation bar.
5. Paste your:
   - **Access Key**
   - **Secret Key**
6. Click **Test Connection** to confirm connectivity.
7. Click **Save Settings**.

*(Alternatively, you can set environment variables `SAVEPAGENOW_ACCESS_KEY` and `SAVEPAGENOW_SECRET_KEY`.)*

---

## 💻 Manual Terminal Start (Command Line)

If you prefer using PowerShell or Command Prompt:

```powershell
# Navigate into the project folder
cd "wayback-website-archiver"

# Install dependencies (FastAPI, Uvicorn, BeautifulSoup4, LXML, Requests)
pip install -r requirements.txt

# Start the application
python app.py
```

Then visit [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

---

## 📁 Project Structure

```
wayback-website-archiver/
│
├── app.py                  # FastAPI desktop server & API endpoints
├── requirements.txt        # Python package dependencies
├── run.bat                 # 1-Click Windows execution launcher
├── README.md               # User guide & documentation
├── .env.example            # Environment variables template
│
├── core/
│   ├── crawler.py          # Multi-strategy discovery crawler
│   ├── navigation.py       # Menu & Elementor/WP navigation parser
│   ├── sitemap.py          # XML sitemap & index recursive extractor
│   ├── url_filter.py       # Domain restriction & URL deduplicator
│   ├── wayback.py          # Save Page Now SPN2 client & poll engine
│   ├── results.py          # CSV/JSON export & HTML report generator
│   └── settings.py         # Persistent JSON settings manager
│
├── templates/
│   ├── index.html          # Main archiver interface
│   ├── settings.html       # API keys & crawler configuration
│   └── results.html        # Historical jobs & reports viewer
│
├── static/
│   ├── style.css           # Modern dark-mode glassmorphic styling
│   └── app.js              # Real-time UI logic & live status polling
│
├── data/
│   ├── settings.json       # Configured keys & scan settings
│   └── jobs/               # Saved job state files (for resumption)
│
└── reports/                # Generated CSV, JSON, and HTML reports
```

---

## 📊 Viewing Snapshots & Exported Reports

- **In the App**: While archiving is running, each row in the results table displays a clickable **"🌐 View Snapshot →"** link as soon as Wayback confirms the save.
- **Visual HTML Report**: At completion, click **"Open Visual Report"** to open a standalone dashboard table of all pages.
- **CSV / JSON Exports**: Download `results.csv` or `results.json` directly from the UI or find them in `reports/<job_id>/`.
- **Jobs & Reports Tab**: Click **"Jobs & Reports"** in the top navigation to view the complete history of all past runs.

---

## 🛠️ Troubleshooting

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| **"Authentication rejected (HTTP 401/403)"** | Invalid or expired Access/Secret keys. | Go to Settings, re-check your S3 keys from `https://archive.org/account/s3.php`, and click **Test Connection**. |
| **"Rate limited (HTTP 429)"** | Too many requests sent in a short window. | Increase the **Archive Delay** in Settings to 15s or 30s. The tool automatically sleeps and retries. |
| **"Site blocked discovery"** | Target website has Cloudflare or anti-bot challenge. | Check if the site is reachable in your browser. You can also try **"🗺️ Sitemap Only"** mode as sitemaps are often accessible even when HTML is protected. |
| **Crawl stops prematurely** | Max pages limit reached. | Increase **Max Pages** in Advanced Options or Settings. |
| **App closed by accident** | Windows restarted or console closed. | Relaunch `run.bat`. A banner will automatically prompt: **"Resume Previous Job"**. Click it to continue from the last unfinished page. |
