# Wayback Archiver

An automated bulk website archiver for the Internet Archive Wayback Machine (Save Page Now / SPN2). Includes an interactive web dashboard and a standalone CLI tool.

## Features

- **Automated URL Discovery**: Crawls websites via sitemaps (`sitemap.xml`, `robots.txt`), navigation menus, and internal page links.
- **Official SPN2 API**: Authenticates with Internet Archive S3 credentials and polls background archive jobs.
- **Smart Rate Limiting**: Exponential backoff, configurable request delays, and automatic HTTP 429 handling.
- **Snapshot Verification**: Checks existing archives via Wayback Availability API to avoid duplicate captures.
- **Job Resumption**: Resume interrupted runs from where they left off.
- **Export Formats**: Outputs results to CSV, JSON, and visual HTML reports.
- **Dual Interface**: Modern web dashboard (FastAPI) and lightweight standalone CLI.

## Quick Start

### 1. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/DevAhmedNaeem/wayback-website-archiver.git
cd wayback-website-archiver
pip install -r requirements.txt
```

### 2. Launch Web Dashboard

- **Windows**: Double-click `START_ARCHIVER.bat` or `run_archiver.bat`.
- **Terminal**:
  ```bash
  cd wayback-website-archiver
  python app.py
  ```
Open `http://127.0.0.1:8000` in your browser.

### 3. CLI Usage

Run the standalone CLI archiver directly:

```bash
# Auto-crawl website and submit pages:
python wayback_bulk_archiver.py https://example.com --crawl

# Submit URLs from a text file:
python wayback_bulk_archiver.py urls.txt

# With Internet Archive credentials:
python wayback_bulk_archiver.py urls.txt --access-key YOUR_KEY --secret-key YOUR_SECRET
```

## Configuration

Obtain free Internet Archive S3 API keys at [archive.org/account/s3.php](https://archive.org/account/s3.php).

Configure credentials via:
1. **Web Dashboard**: Navigate to **Settings** and input your Access & Secret keys.
2. **Environment Variables**:
   ```bash
   export SAVEPAGENOW_ACCESS_KEY="your_access_key"
   export SAVEPAGENOW_SECRET_KEY="your_secret_key"
   ```
3. **CLI Arguments**: Pass `--access-key` and `--secret-key` flags.

## Project Structure

```
├── START_ARCHIVER.bat          # 1-Click Windows launcher
├── run_archiver.bat            # Alternative launcher shortcut
├── wayback_bulk_archiver.py    # Standalone CLI archiver
├── requirements.txt            # Python dependencies
├── urls.txt                    # Sample URL list
└── wayback-website-archiver/   # Web application
    ├── app.py                  # FastAPI backend server
    ├── core/                   # Crawler, sitemap, wayback API engines
    ├── templates/              # HTML views (dashboard, settings, reports)
    └── static/                 # Stylesheets and frontend scripts
```

## License

MIT
