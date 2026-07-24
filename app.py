"""WebDownloader - a simple offline website downloader/scraper with a web UI.

Run with:  python app.py
Then open http://127.0.0.1:5000 in your browser.

Also runs as a standalone PyInstaller executable (see build_exe.py). When
frozen, templates/static are read from the bundle and downloads are saved
next to the .exe.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from urllib.parse import urlsplit, quote

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, abort

from webdownloader.scraper import DownloadJob, SiteDownloader, new_job_id

FROZEN = getattr(sys, "frozen", False)


def _resource_dir() -> str:
    """Where bundled templates/static live (PyInstaller temp dir when frozen)."""
    if FROZEN:
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _app_base_dir() -> str:
    """A writable, persistent folder next to the app (not the temp bundle)."""
    if FROZEN:
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# --- Native folder picker helpers ---------------------------------------
# When frozen, `sys.executable` is our own .exe (not python), so the picker
# is invoked by re-launching ourselves with a marker argument that runs the
# Tk dialog and prints the chosen path.
_PICK_FOLDER_FLAG = "--pick-folder-dialog"


def _run_folder_dialog_and_exit() -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose download folder")
        sys.stdout.write(path or "")
    except Exception:  # noqa: BLE001
        sys.exit(2)
    sys.exit(0)


if FROZEN and _PICK_FOLDER_FLAG in sys.argv:
    _run_folder_dialog_and_exit()


_RES_DIR = _resource_dir()
# Default output location, used only when the user doesn't choose their own folder.
DEFAULT_OUTPUT_ROOT = os.path.join(_app_base_dir(), "downloads")
os.makedirs(DEFAULT_OUTPUT_ROOT, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(_RES_DIR, "templates"),
    static_folder=os.path.join(_RES_DIR, "static"),
)
JOBS: dict[str, DownloadJob] = {}
JOBS_LOCK = threading.Lock()


def _parse_pages(value, default=50):
    """0 (or blank/negative) means unlimited pages."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _parse_depth(value, default=3):
    """A negative value means unlimited depth."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else -1


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Open a native folder picker on the machine running the app.

    Because the app runs locally, the picker (Tk) shows on the user's own
    desktop. Runs in a short-lived subprocess so Tk stays out of Flask's
    worker threads (Tk must own the main thread). When frozen we re-launch
    our own executable with a marker flag; otherwise we run python -c.
    """
    if FROZEN:
        argv = [sys.executable, _PICK_FOLDER_FLAG]
    else:
        code = (
            "import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            "path = filedialog.askdirectory(title='Choose download folder')\n"
            "import sys; sys.stdout.write(path or '')\n"
        )
        argv = [sys.executable, "-c", code]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        # A non-zero exit means the picker itself failed to open (e.g. no
        # display / Tk unavailable) rather than the user cancelling, which
        # returns an empty path with a clean exit.
        if result.returncode != 0:
            return jsonify({"path": "", "error": result.stderr.strip() or "picker failed"})
        return jsonify({"path": result.stdout.strip()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"path": "", "error": str(exc)})


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    """Open a finished job's output folder in the OS file manager."""
    data = request.get_json(silent=True) or {}
    job = JOBS.get(data.get("job_id", ""))
    if job is None or not os.path.isdir(job.output_dir):
        return jsonify({"error": "Folder not found."}), 404
    path = job.output_dir
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - local, trusted path
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.route("/api/jobs", methods=["POST"])
def start_job():
    data = request.get_json(silent=True) or request.form
    start_url = (data.get("url") or "").strip()
    if not start_url:
        return jsonify({"error": "A URL is required."}), 400
    if not start_url.lower().startswith(("http://", "https://")):
        start_url = "https://" + start_url

    parts = urlsplit(start_url)
    if not parts.netloc:
        return jsonify({"error": "That doesn't look like a valid URL."}), 400

    max_pages = _parse_pages(data.get("max_pages"))
    max_depth = _parse_depth(data.get("max_depth"))
    same_domain_only = str(data.get("same_domain_only", "true")).lower() not in ("false", "0", "no")
    respect_robots = str(data.get("respect_robots", "true")).lower() not in ("false", "0", "no")

    # Resolve the destination folder chosen by the user (or fall back to a
    # default folder next to the app). The mirror is written under
    # <destination>/<site-host>/..., so all links between pages stay relative
    # and the whole folder can be moved or opened anywhere.
    dest = (data.get("dest_dir") or "").strip()
    if dest:
        dest = os.path.abspath(os.path.expanduser(dest))
    else:
        dest = DEFAULT_OUTPUT_ROOT
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"Can't use that folder: {exc}"}), 400
    if not os.access(dest, os.W_OK):
        return jsonify({"error": "That folder isn't writable."}), 400

    output_dir = dest
    job_id = new_job_id()
    job = DownloadJob(
        id=job_id,
        start_url=start_url,
        output_dir=output_dir,
        max_pages=max_pages,
        max_depth=max_depth,
        same_domain_only=same_domain_only,
        respect_robots=respect_robots,
    )
    with JOBS_LOCK:
        JOBS[job_id] = job

    downloader = SiteDownloader(job)
    thread = threading.Thread(target=downloader.run, name=f"job-{job_id}", daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(job.snapshot())


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    job.request_stop()
    return jsonify({"ok": True})


@app.route("/site/<job_id>/", defaults={"path": ""})
@app.route("/site/<job_id>/<path:path>")
def browse_site(job_id, path):
    """Serve the mirrored, offline copy of a job so it can be previewed in-browser."""
    job = JOBS.get(job_id)
    if job is None:
        abort(404)
    if not path:
        # Redirect to the entry file's real path so relative links inside it
        # (computed relative to that path) resolve correctly in the browser.
        # Percent-encode so a non-ASCII (e.g. Arabic) entry path stays a valid
        # URL that decodes back to the real on-disk file name.
        if not job.entry_path:
            abort(404)
        return redirect(f"/site/{job_id}/{quote(job.entry_path)}")
    full = os.path.abspath(os.path.join(job.output_dir, path))
    if not full.startswith(os.path.abspath(job.output_dir) + os.sep):
        abort(403)
    return send_from_directory(job.output_dir, path)


def _find_free_port(preferred: int = 5000) -> int:
    """Use the preferred port if available, otherwise let the OS pick one."""
    import socket

    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def main() -> None:
    port = _find_free_port(5000)
    url = f"http://127.0.0.1:{port}/"

    # Auto-open the browser shortly after the server starts, so double-clicking
    # the packaged .exe just opens the app. Skipped when NO_BROWSER is set.
    if os.environ.get("WD_NO_BROWSER") != "1":
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"WebDownloader is running at {url}")
    print("Close this window to stop the app.")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
