"""WebDownloader - a simple offline website downloader/scraper with a web UI.

Run with:  python app.py
Then open http://127.0.0.1:5000 in your browser.
"""
from __future__ import annotations

import os
import threading
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, abort

from webdownloader.scraper import DownloadJob, SiteDownloader, new_job_id

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = Flask(__name__)
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

    job_id = new_job_id()
    output_dir = os.path.join(JOBS_DIR, job_id, "site")
    os.makedirs(output_dir, exist_ok=True)

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


@app.route("/api/jobs/<job_id>/download")
def download_zip(job_id):
    job = JOBS.get(job_id)
    if job is None or not job.zip_path or not os.path.exists(job.zip_path):
        abort(404)
    return send_file(job.zip_path, as_attachment=True, download_name=f"{job_id}-offline-site.zip")


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
        if not job.entry_path:
            abort(404)
        return redirect(f"/site/{job_id}/{job.entry_path}")
    full = os.path.join(job.output_dir, path)
    if not os.path.abspath(full).startswith(os.path.abspath(job.output_dir)):
        abort(403)
    return send_from_directory(job.output_dir, path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
