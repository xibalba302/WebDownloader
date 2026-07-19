# WebDownloader

A simple web app that downloads (mirrors) a website for offline use: it
crawls pages within the site, downloads every page, stylesheet, script,
image and font it finds, and rewrites all the links so the result can be
opened and browsed with no internet connection.

The UI works in **English and Arabic** (with right-to-left layout and an
Arabic-friendly font), and the crawler downloads any custom web fonts
(`@font-face`, `.woff`/`.woff2`/`.ttf`) referenced by a site's CSS, so
Arabic (and other) typography is preserved in the offline copy too.

## Features

- Crawls same-domain pages up to a configurable depth/page limit
- Downloads HTML, CSS, JS, images, fonts, media and rewrites:
  - `<a href>`, `<img src/srcset>`, `<link href>`, `<script src>`,
    `<source>`, `<video>`, `<audio>`, `<iframe>`, `<object>`
  - `url(...)` and `@import` references inside CSS (including nested/
    imported stylesheets)
  - inline `style="..."` attributes and `<style>` blocks
- Leaves out-of-scope external links pointing at the live site so
  navigation "off the archive" still works
- Optional `robots.txt` compliance (on by default) and a polite delay
  between requests
- Live progress log, cancel button, one-click ZIP download, and an
  in-browser "browse offline copy" preview
- Simple, bilingual (EN/AR) responsive UI, light/dark aware

## Install & run

### Windows

Double-click **`build.bat`** (or run it from a terminal). It will:

1. Find your Python installation (installs nothing itself — if Python
   isn't found it points you to https://www.python.org/downloads/)
2. Create a local virtual environment in `.\venv` (first run only)
3. Download/install all dependencies into that virtual environment
4. Start the app and open http://127.0.0.1:5000 in your browser automatically

Subsequent runs reuse the existing `venv` and skip straight to installing
(a no-op if nothing changed) and launching — just double-click `build.bat`
again any time you want to use the app. Close the console window (or press
Ctrl+C in it) to stop the server.

### macOS / Linux

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000, enter a URL, and click **Start download**.
When it finishes you can browse the offline copy right in the browser or
download it as a ZIP.

## Responsible use

This tool fetches pages like a normal browser would. Please only archive
sites you own or have permission to copy, keep `Respect robots.txt`
enabled unless you have a specific reason not to, and avoid pointing it
at very large sites with a huge `Max pages` value — be considerate of the
target server's bandwidth.

## How it works

- `webdownloader/scraper.py` — the crawler/downloader engine
  (`SiteDownloader`) and the `DownloadJob` progress record it updates as
  it runs in a background thread.
- `webdownloader/utils.py` — URL → local file path mapping and relative
  link computation.
- `app.py` — Flask app: starts jobs, exposes a small JSON API for
  progress polling, serves the finished mirror for in-browser preview,
  and offers the ZIP download.
- `templates/index.html`, `static/` — the single-page UI.

## Known limitations

- JavaScript that fetches resources dynamically at runtime (via `fetch`,
  `import()`, etc.) is not rewritten — only static HTML/CSS references
  are. Sites that render content client-side may not work fully offline.
- Pages behind login or requiring JavaScript execution to reveal links
  are not handled (no headless browser is used).
