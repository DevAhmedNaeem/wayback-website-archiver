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


