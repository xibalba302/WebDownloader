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

- Crawls same-domain pages up to a configurable depth/page limit — set
  `Max pages` to `0` or `Max link depth` to `-1` for no limit (crawl the
  entire reachable site)
- Downloads HTML, CSS, JS, images, fonts, media and rewrites:
  - `<a href>`, `<img src/srcset>`, `<link href>`, `<script src>`,
    `<source>`, `<video>`, `<audio>`, `<iframe>`, `<object>`
  - `url(...)` and `@import` references inside CSS (including nested/
    imported stylesheets)
  - inline `style="..."` attributes and `<style>` blocks
- Lets you pick the destination folder to save into (native folder
  picker, or type/paste a path) so the mirror lands exactly where you
  want it
- Rewrites every in-scope link to a relative path and every out-of-scope
  link to its absolute URL, so opening a saved page straight from disk
  never produces broken `C:\...` (drive-root) links
- Handles non-ASCII (e.g. Arabic) URLs correctly: pages such as
  `/category/إضاءات/` are stored under their real names and linked with
  matching percent-encoding, so Arabic menu/category links open offline
  instead of 404-ing
- Shows the elapsed download time (live while running, final when done)
  alongside the page/asset/error counts
- Optional `robots.txt` compliance (on by default)
- Pages and assets download concurrently (6 pages / 10 assets at a time
  by default) instead of one request at a time, so crawls finish
  significantly faster
- Live progress log, cancel button, an in-browser "browse offline copy"
  preview, and an "Open folder" button that reveals the files in your OS
  file manager
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

Then open http://127.0.0.1:5000, enter a URL, pick where to save it, and
click **Start download**. When it finishes you can browse the offline
copy right in the browser or open the folder on disk.

## Where the downloaded files go

Use the **Save to folder** field to choose the destination — click
**Browse…** for a native folder picker, or type/paste a path. If you
leave it blank, downloads go to a `downloads/` folder next to the app.

The site is mirrored under `<your folder>/<site-host>/…`
(e.g. `<your folder>/example.com/index.html`), with all links between
pages rewritten as relative paths, so you can open `index.html` directly
from disk or move the whole folder anywhere and it still works offline.

When a job finishes the progress panel shows the exact **Saved to:** path
(with a **Copy** button) and an **Open folder** button that reveals the
files in Explorer/Finder. No ZIP is produced — the files are written
straight to your chosen folder.

## Responsible use

This tool fetches pages like a normal browser would. Please only archive
sites you own or have permission to copy, and keep `Respect robots.txt`
enabled unless you have a specific reason not to.

`Max pages` and `Max link depth` have no upper bound — setting either to
unlimited (`0` / `-1`) means the crawl only stops when it runs out of
same-domain pages to find, or when you click **Cancel**. On a large site
that can mean a lot of requests and disk space, so use unlimited crawls
considerately and keep an eye on the progress log.

## How it works

- `webdownloader/scraper.py` — the crawler/downloader engine
  (`SiteDownloader`) and the `DownloadJob` progress record it updates as
  it runs in a background thread.
- `webdownloader/utils.py` — URL → local file path mapping and relative
  link computation.
- `app.py` — Flask app: starts jobs, exposes a small JSON API for
  progress polling, serves the finished mirror for in-browser preview,
  and provides the native folder picker / open-folder helpers.
- `templates/index.html`, `static/` — the single-page UI.

## Known limitations

- JavaScript that fetches resources dynamically at runtime (via `fetch`,
  `import()`, etc.) is not rewritten — only static HTML/CSS references
  are. Sites that render content client-side may not work fully offline.
- Pages behind login or requiring JavaScript execution to reveal links
  are not handled (no headless browser is used).
