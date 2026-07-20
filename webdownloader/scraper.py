"""Core site-mirroring engine.

Crawls a website starting from a URL, downloads every reachable resource
(pages, stylesheets, scripts, images, fonts, media) and rewrites all links
so the result can be browsed fully offline with correct relative paths.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib import robotparser

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

from .utils import normalize_url, url_to_local_path, relative_link

DEFAULT_USER_AGENT = "WebDownloaderBot/1.0 (+offline archiver; personal use)"

# Attributes that hold a single URL, keyed by tag name.
_SINGLE_URL_ATTRS = {
    "img": ["src"],
    "source": ["src"],
    "script": ["src"],
    "video": ["src", "poster"],
    "audio": ["src"],
    "embed": ["src"],
    "iframe": ["src"],
    "track": ["src"],
    "object": ["data"],
}
_SRCSET_TAGS = {"img", "source"}
_PAGE_EXCLUDE_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "whatsapp:")

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*['\"]?([^'\")]+)['\"]?\s*\)|['\"]([^'\"]+)['\"])",
    re.IGNORECASE,
)

_HEADER_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?\s*([\w-]+)', re.IGNORECASE)


def _decode_html(raw: bytes, content_type: str) -> str:
    """Best-effort decode: HTTP header charset, then <meta charset>, then guess.

    Many servers (including plain dev servers) omit the charset on the
    Content-Type header even for UTF-8 (e.g. Arabic) pages, so trusting
    ``requests``' default Latin-1 fallback silently mangles the text.
    """
    match = _HEADER_CHARSET_RE.search(content_type)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass

    match = _META_CHARSET_RE.search(raw[:2048])
    if match:
        try:
            return raw.decode(match.group(1).decode("ascii"), errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("windows-1256", errors="replace")


@dataclass
class DownloadJob:
    """Mutable, thread-safe-ish progress record for one crawl job."""

    id: str
    start_url: str
    output_dir: str
    max_pages: int  # <= 0 means unlimited
    max_depth: int  # < 0 means unlimited
    same_domain_only: bool
    respect_robots: bool
    status: str = "queued"  # queued -> running -> completed | error | cancelled
    pages_done: int = 0
    assets_done: int = 0
    errors_count: int = 0
    current_action: str = ""
    log: list = field(default_factory=list)
    entry_path: Optional[str] = None
    zip_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    _stop_requested: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log_line(self, message: str) -> None:
        with self._lock:
            stamp = datetime.utcnow().strftime("%H:%M:%S")
            self.log.append(f"[{stamp}] {message}")
            if len(self.log) > 300:
                self.log = self.log[-300:]
            self.current_action = message

    def increment_pages(self) -> None:
        with self._lock:
            self.pages_done += 1

    def increment_assets(self) -> None:
        with self._lock:
            self.assets_done += 1

    def increment_errors(self) -> None:
        with self._lock:
            self.errors_count += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "start_url": self.start_url,
                "status": self.status,
                "pages_done": self.pages_done,
                "assets_done": self.assets_done,
                "errors_count": self.errors_count,
                "current_action": self.current_action,
                "log": list(self.log[-60:]),
                "entry_path": self.entry_path,
                "output_dir": self.output_dir,
                "zip_ready": self.zip_path is not None and os.path.exists(self.zip_path),
                "error_message": self.error_message,
                "created_at": self.created_at,
            }

    def request_stop(self) -> None:
        self._stop_requested = True


class SiteDownloader:
    """Crawls and mirrors a site into ``job.output_dir`` for offline browsing."""

    def __init__(
        self,
        job: DownloadJob,
        timeout: int = 15,
        page_workers: int = 6,
        asset_workers: int = 10,
    ):
        self.job = job
        self.timeout = timeout
        self.page_workers = max(1, page_workers)
        self.asset_workers = max(1, asset_workers)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        # A bigger connection pool than the default (10) so concurrent
        # page/asset workers aren't stuck queuing for a free connection.
        pool_size = self.page_workers + self.asset_workers
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.start_host = urlsplit(job.start_url).netloc.lower()
        self.url_to_local: dict[str, str] = {}
        # "Seen" = already downloaded or scheduled to be downloaded - the single
        # dedup set that prevents the same page being queued twice.
        self.seen_pages: set[str] = set()
        self._pages_lock = threading.Lock()
        self._asset_locks: dict[str, threading.Lock] = {}
        self._asset_locks_guard = threading.Lock()
        self._robots_cache: dict[str, robotparser.RobotFileParser] = {}
        self._robots_lock = threading.Lock()
        # Assets (images/CSS/JS/fonts) for a page are downloaded in parallel
        # on this shared pool - this is usually where most of the wall-clock
        # time goes, so it's the biggest lever for making a crawl faster.
        self.asset_executor = ThreadPoolExecutor(max_workers=self.asset_workers)

    # ---------------------------------------------------------------- run

    def _reserve_page(self, url: str) -> bool:
        """Atomically claim a URL for crawling; False if already seen/queued."""
        job = self.job
        with self._pages_lock:
            norm = normalize_url(url)
            if norm in self.seen_pages:
                return False
            if job.max_pages > 0 and len(self.seen_pages) >= job.max_pages:
                return False
            self.seen_pages.add(norm)
            return True

    def _safe_process_page(self, url: str, depth: int) -> list[tuple[str, int]]:
        job = self.job
        if job.respect_robots and not self._allowed_by_robots(url):
            job.log_line(f"Skipped (robots.txt disallows): {url}")
            return []
        return self._process_page(url, depth)

    def run(self) -> None:
        job = self.job
        job.status = "running"
        job.log_line(f"Starting crawl of {job.start_url}")
        # Fixed up front (rather than "whichever page finishes first") so it
        # stays correct even though pages complete out of order.
        job.entry_path = url_to_local_path(job.start_url, is_page=True)

        executor = ThreadPoolExecutor(max_workers=self.page_workers)
        futures: dict[Future, tuple[str, int]] = {}

        def submit(url: str, depth: int) -> None:
            if not self._reserve_page(url):
                return
            fut = executor.submit(self._safe_process_page, url, depth)
            futures[fut] = (url, depth)

        submit(job.start_url, 0)

        try:
            while futures:
                if job._stop_requested:
                    job.status = "cancelled"
                    job.log_line("Cancelled by user.")
                    break

                done, _pending = wait(list(futures.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                if not done:
                    continue

                for fut in done:
                    url, _depth = futures.pop(fut)
                    try:
                        new_links = fut.result()
                    except Exception as exc:  # noqa: BLE001 - keep crawl alive
                        job.increment_errors()
                        job.log_line(f"Error fetching {url}: {exc}")
                        continue

                    job.increment_pages()
                    if job.max_pages > 0 and job.pages_done >= job.max_pages:
                        continue

                    depth_ok_unlimited = job.max_depth < 0
                    for link, link_depth in new_links:
                        if not depth_ok_unlimited and link_depth > job.max_depth:
                            continue
                        submit(link, link_depth)

            if job.status == "running":
                executor.shutdown(wait=True)
                job.status = "completed"
                job.log_line(
                    f"Done. {job.pages_done} page(s), {job.assets_done} asset(s), "
                    f"{job.errors_count} error(s)."
                )
                self._zip_output()
            else:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error_message = str(exc)
            job.log_line(f"Fatal error: {exc}")
        finally:
            self.asset_executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------ robots

    def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots_cache.get(origin)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                resp = self.session.get(urljoin(origin, "/robots.txt"), timeout=self.timeout)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])
            except requests.RequestException:
                rp.parse([])
            self._robots_cache[origin] = rp
        try:
            return rp.can_fetch(DEFAULT_USER_AGENT, url)
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------- pages

    def _in_scope(self, url: str) -> bool:
        if not url or url.startswith(_PAGE_EXCLUDE_SCHEMES):
            return False
        scheme = urlsplit(url).scheme
        if scheme not in ("http", "https"):
            return False
        if self.job.same_domain_only and urlsplit(url).netloc.lower() != self.start_host:
            return False
        return True

    def _process_page(self, url: str, depth: int) -> list[tuple[str, int]]:
        job = self.job
        job.log_line(f"Fetching page: {url}")
        resp = self.session.get(url, timeout=self.timeout)
        content_type = resp.headers.get("Content-Type", "")

        if "text/html" not in content_type and depth > 0:
            # Linked resource turned out not to be HTML (e.g. a PDF) - store as asset instead.
            self._save_asset_bytes(url, resp.content, content_type)
            return []

        html_text = _decode_html(resp.content, content_type)
        soup = BeautifulSoup(html_text, "html.parser")

        base_tag = soup.find("base", href=True)
        base_url = urljoin(url, base_tag["href"]) if base_tag else url

        local_path = url_to_local_path(url, is_page=True)
        self.url_to_local[normalize_url(url)] = local_path

        new_page_links: list[tuple[str, int]] = []

        # Collect every downloadable (tag, attribute, url) up front so the
        # actual HTTP fetches can run in parallel on the shared asset pool
        # instead of one-at-a-time.
        download_tasks: list[tuple] = []  # (tag, attr, abs_url)

        for tag_name, attrs in _SINGLE_URL_ATTRS.items():
            for tag in soup.find_all(tag_name):
                for attr in attrs:
                    if not tag.get(attr):
                        continue
                    raw = tag[attr]
                    if raw.strip().lower().startswith("data:"):
                        continue
                    abs_url = urljoin(base_url, raw)
                    if tag_name == "iframe" and self._in_scope(abs_url):
                        # Treat same-site iframes as pages so they render offline too.
                        new_page_links.append((abs_url, depth + 1))
                        continue
                    download_tasks.append((tag, attr, abs_url))

        # <link> tags: stylesheets, icons, preloaded fonts/images, manifests.
        # rel="alternate"/"canonical"/"dns-prefetch" etc. are left untouched.
        _downloadable_rels = {
            "stylesheet", "icon", "shortcut icon", "apple-touch-icon",
            "apple-touch-icon-precomposed", "mask-icon", "manifest", "preload", "prefetch",
        }
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower() if tag.get("rel") else ""
            if rel not in _downloadable_rels:
                continue
            raw = tag["href"]
            if raw.strip().lower().startswith("data:"):
                continue
            abs_url = urljoin(base_url, raw)
            download_tasks.append((tag, "href", abs_url))

        future_to_task = {
            self.asset_executor.submit(self._download_asset, abs_url): (tag, attr, abs_url)
            for tag, attr, abs_url in download_tasks
        }
        for fut in as_completed(future_to_task):
            tag, attr, abs_url = future_to_task[fut]
            try:
                target_local = fut.result()
                tag[attr] = relative_link(local_path, target_local)
            except Exception as exc:  # noqa: BLE001
                job.increment_errors()
                job.log_line(f"Asset failed ({abs_url}): {exc}")

        # srcset (multiple URLs with descriptors).
        for tag_name in _SRCSET_TAGS:
            for tag in soup.find_all(tag_name):
                if not tag.get("srcset"):
                    continue
                tag["srcset"] = self._rewrite_srcset(tag["srcset"], base_url, local_path)

        # Inline style="" attributes with url(...).
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_text(tag["style"], base_url, local_path)

        # <style> blocks.
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                style_tag.string.replace_with(
                    self._rewrite_css_text(style_tag.string, base_url, local_path)
                )

        # Anchors - crawl within scope, leave out-of-scope links pointing at the live site.
        for a in soup.find_all("a", href=True):
            raw = a["href"]
            if raw.strip().lower().startswith(_PAGE_EXCLUDE_SCHEMES) or raw.startswith("#"):
                continue
            abs_url, frag = urldefrag(urljoin(base_url, raw))
            if not abs_url.startswith(("http://", "https://")):
                continue
            if self._in_scope(abs_url):
                new_page_links.append((abs_url, depth + 1))
                target_local = url_to_local_path(abs_url, is_page=True)
                a["href"] = relative_link(local_path, target_local) + (f"#{frag}" if frag else "")
            # else: leave external link absolute so it still works online.

        if base_tag is not None:
            base_tag.decompose()
        self._ensure_charset(soup)

        full_path = os.path.join(job.output_dir, local_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(str(soup))

        return new_page_links

    def _ensure_charset(self, soup: BeautifulSoup) -> None:
        head = soup.find("head")
        if head is None:
            head = soup.new_tag("head")
            if soup.html:
                soup.html.insert(0, head)
        if not head.find("meta", charset=True) and not head.find(
            "meta", attrs={"http-equiv": re.compile("content-type", re.I)}
        ):
            meta = soup.new_tag("meta")
            meta["charset"] = "utf-8"
            head.insert(0, meta)

    # ------------------------------------------------------------ assets

    def _get_asset_lock(self, norm: str) -> threading.Lock:
        with self._asset_locks_guard:
            lock = self._asset_locks.get(norm)
            if lock is None:
                lock = threading.Lock()
                self._asset_locks[norm] = lock
            return lock

    def _download_asset(self, abs_url: str) -> str:
        """Download a non-page resource (or return its already-known local path).

        Guarded by a per-URL lock so the same asset referenced from several
        pages/threads at once is only ever fetched once.
        """
        norm = normalize_url(abs_url)
        if norm in self.url_to_local:
            return self.url_to_local[norm]

        with self._get_asset_lock(norm):
            if norm in self.url_to_local:
                return self.url_to_local[norm]
            resp = self.session.get(abs_url, timeout=self.timeout)
            content_type = resp.headers.get("Content-Type", "")
            return self._save_asset_bytes(abs_url, resp.content, content_type)

    def _save_asset_bytes(self, abs_url: str, content: bytes, content_type: str) -> str:
        norm = normalize_url(abs_url)
        if norm in self.url_to_local:
            return self.url_to_local[norm]

        local_path = url_to_local_path(abs_url, is_page=False, content_type=content_type)
        # Registered before any recursive rewrite below so a CSS file that
        # (indirectly) references itself resolves instead of deadlocking.
        self.url_to_local[norm] = local_path
        full_path = os.path.join(self.job.output_dir, local_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        is_css = "text/css" in content_type or abs_url.split("?")[0].lower().endswith(".css")
        if is_css:
            text = _decode_html(content, content_type)
            rewritten = self._rewrite_css_text(text, abs_url, local_path)
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(rewritten)
        else:
            with open(full_path, "wb") as fh:
                fh.write(content)

        self.job.increment_assets()
        self.job.log_line(f"Downloaded asset: {abs_url}")
        return local_path

    # --------------------------------------------------------------- css

    def _rewrite_css_text(self, css_text: str, base_url: str, referrer_local_path: str) -> str:
        def replace_url(match: re.Match) -> str:
            raw = match.group(2).strip()
            if not raw or raw.lower().startswith("data:"):
                return match.group(0)
            abs_url = urljoin(base_url, raw)
            if not abs_url.startswith(("http://", "https://")):
                return match.group(0)
            try:
                target_local = self._download_asset(abs_url)
            except Exception:  # noqa: BLE001
                return match.group(0)
            rel = relative_link(referrer_local_path, target_local)
            return f"url('{rel}')"

        def replace_import(match: re.Match) -> str:
            raw = (match.group(1) or match.group(2) or "").strip()
            if not raw:
                return match.group(0)
            abs_url = urljoin(base_url, raw)
            if not abs_url.startswith(("http://", "https://")):
                return match.group(0)
            try:
                target_local = self._download_asset(abs_url)
            except Exception:  # noqa: BLE001
                return match.group(0)
            rel = relative_link(referrer_local_path, target_local)
            return f"@import url('{rel}')"

        css_text = _CSS_IMPORT_RE.sub(replace_import, css_text)
        css_text = _CSS_URL_RE.sub(replace_url, css_text)
        return css_text

    def _rewrite_srcset(self, srcset: str, base_url: str, referrer_local_path: str) -> str:
        parts = []
        for candidate in srcset.split(","):
            candidate = candidate.strip()
            if not candidate:
                continue
            bits = candidate.split()
            url_part = bits[0]
            descriptor = " ".join(bits[1:])
            abs_url = urljoin(base_url, url_part)
            try:
                target_local = self._download_asset(abs_url)
                rel = relative_link(referrer_local_path, target_local)
            except Exception:  # noqa: BLE001
                rel = url_part
            parts.append(f"{rel} {descriptor}".strip())
        return ", ".join(parts)

    # --------------------------------------------------------------- zip

    def _zip_output(self) -> None:
        job = self.job
        zip_path = job.output_dir.rstrip(os.sep) + ".zip"
        job.log_line("Packaging ZIP archive...")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(job.output_dir):
                for name in files:
                    full = os.path.join(root, name)
                    arcname = os.path.relpath(full, job.output_dir)
                    zf.write(full, arcname)
        job.zip_path = zip_path
        job.log_line("ZIP archive ready.")


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]
