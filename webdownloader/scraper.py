"""Core site-mirroring engine.

Crawls a website starting from a URL, downloads every reachable resource
(pages, stylesheets, scripts, images, fonts, media) and rewrites all links
so the result can be browsed fully offline with correct relative paths.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlsplit, urldefrag
from urllib import robotparser

import requests
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
                "zip_ready": self.zip_path is not None and os.path.exists(self.zip_path),
                "error_message": self.error_message,
                "created_at": self.created_at,
            }

    def request_stop(self) -> None:
        self._stop_requested = True


class SiteDownloader:
    """Crawls and mirrors a site into ``job.output_dir`` for offline browsing."""

    def __init__(self, job: DownloadJob, delay: float = 0.25, timeout: int = 15):
        self.job = job
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

        self.start_host = urlsplit(job.start_url).netloc.lower()
        self.url_to_local: dict[str, str] = {}
        self.visited_pages: set[str] = set()
        self.queued_pages: set[str] = set()
        self.downloaded_assets: set[str] = set()
        self._robots_cache: dict[str, robotparser.RobotFileParser] = {}

    # ---------------------------------------------------------------- run

    def run(self) -> None:
        job = self.job
        job.status = "running"
        job.log_line(f"Starting crawl of {job.start_url}")
        queue = deque([(job.start_url, 0)])
        self.queued_pages.add(normalize_url(job.start_url))

        try:
            while queue and (job.max_pages <= 0 or job.pages_done < job.max_pages):
                if job._stop_requested:
                    job.status = "cancelled"
                    job.log_line("Cancelled by user.")
                    break
                url, depth = queue.popleft()
                norm = normalize_url(url)
                if norm in self.visited_pages:
                    continue
                self.visited_pages.add(norm)

                if job.respect_robots and not self._allowed_by_robots(url):
                    job.log_line(f"Skipped (robots.txt disallows): {url}")
                    continue

                try:
                    new_links = self._process_page(url, depth)
                except Exception as exc:  # noqa: BLE001 - keep crawl alive
                    job.errors_count += 1
                    job.log_line(f"Error fetching {url}: {exc}")
                    continue

                job.pages_done += 1
                depth_ok_unlimited = job.max_depth < 0
                pages_ok_unlimited = job.max_pages <= 0
                for link, link_depth in new_links:
                    lnorm = normalize_url(link)
                    if (
                        lnorm not in self.visited_pages
                        and lnorm not in self.queued_pages
                        and (depth_ok_unlimited or link_depth <= job.max_depth)
                        and (pages_ok_unlimited or len(self.queued_pages) < job.max_pages * 4)
                    ):
                        self.queued_pages.add(lnorm)
                        queue.append((link, link_depth))

                time.sleep(self.delay)

            if job.status == "running":
                job.status = "completed"
                job.log_line(
                    f"Done. {job.pages_done} page(s), {job.assets_done} asset(s), "
                    f"{job.errors_count} error(s)."
                )
                self._zip_output()
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error_message = str(exc)
            job.log_line(f"Fatal error: {exc}")

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

        # Downloadable single-URL attributes.
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
                    try:
                        target_local = self._download_asset(abs_url)
                        tag[attr] = relative_link(local_path, target_local)
                    except Exception as exc:  # noqa: BLE001
                        job.errors_count += 1
                        job.log_line(f"Asset failed ({abs_url}): {exc}")

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
            try:
                target_local = self._download_asset(abs_url)
                tag["href"] = relative_link(local_path, target_local)
            except Exception as exc:  # noqa: BLE001
                job.errors_count += 1
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

        if job.entry_path is None:
            job.entry_path = local_path

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

    def _download_asset(self, abs_url: str) -> str:
        """Download a non-page resource (or return its already-known local path)."""
        norm = normalize_url(abs_url)
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

        self.job.assets_done += 1
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
