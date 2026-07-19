"""Small helpers for turning URLs into safe, predictable local file paths."""
import mimetypes
import posixpath
import re
from urllib.parse import urlsplit, urlunsplit, quote

# Extensions that mean "server-rendered page" rather than a static asset.
# We save the fetched (final, rendered) HTML but give it a plain .html name
# so an offline browser doesn't try to execute it.
_DYNAMIC_PAGE_EXTS = {".php", ".asp", ".aspx", ".jsp", ".jspx", ".cgi", ".pl", ".do"}

_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def normalize_url(url: str) -> str:
    """Strip fragments and normalize so the same resource isn't fetched twice."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def sanitize_segment(segment: str) -> str:
    segment = _UNSAFE_CHARS.sub("_", segment)
    return segment or "_"


def query_suffix(query: str) -> str:
    if not query:
        return ""
    h = 0
    for ch in query:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return f"_q{h:08x}"


def url_to_local_path(url: str, is_page: bool, content_type: str = "") -> str:
    """Map an absolute URL to a POSIX-style relative path under the job's output root.

    Pages are namespaced under <host>/... and always resolve to a concrete
    *.html file (index.html for directories). Assets keep their path/extension
    where possible so relative CSS/JS references still make sense.
    """
    parts = urlsplit(url)
    host = sanitize_segment(parts.netloc.lower())
    raw_path = parts.path or "/"
    ends_with_slash = raw_path.endswith("/")
    # Drop the empty strings produced by leading/trailing/duplicate slashes;
    # only real path segments remain.
    segments = [sanitize_segment(quote(seg, safe="")) for seg in raw_path.split("/") if seg]

    if is_page:
        if ends_with_slash or not segments:
            segments.append("index.html")
        else:
            last = segments[-1]
            base, ext = posixpath.splitext(last)
            if not ext:
                segments[-1] = last
                segments.append("index.html")
            elif ext.lower() in _DYNAMIC_PAGE_EXTS:
                segments[-1] = base + ".html"
        suffix = query_suffix(parts.query)
        if suffix:
            base, ext = posixpath.splitext(segments[-1])
            segments[-1] = base + suffix + (ext or ".html")
        path = "/".join(segments)
        return f"{host}/{path}"

    # Asset: mirror the URL path under the host folder, like wget --mirror.
    path = "/".join(segments)
    if not path:
        path = "index"
    _base, ext = posixpath.splitext(path)
    if not ext and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            path = path + guessed
    suffix = query_suffix(parts.query)
    if suffix:
        base, ext = posixpath.splitext(path)
        path = base + suffix + ext
    return f"{host}/{path}"


def relative_link(from_path: str, to_path: str) -> str:
    """POSIX-relative link from one output-root-relative file path to another."""
    from_dir = posixpath.dirname(from_path)
    rel = posixpath.relpath(to_path, from_dir or ".")
    return rel
