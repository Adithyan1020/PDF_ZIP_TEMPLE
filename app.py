"""PDF -> temple folders zip.  Run:  python app.py   then open http://127.0.0.1:5000"""
from __future__ import annotations

import io
import os
import re
import threading
import time
import uuid
import webbrowser

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import builder
import extractor

HERE = os.path.dirname(os.path.abspath(__file__))
JOB_TTL = 2 * 60 * 60  # keep uploaded jobs for 2 hours

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def _cleanup():
    now = time.time()
    with LOCK:
        for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
            JOBS.pop(jid, None)


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.post("/api/extract")
def api_extract():
    _cleanup()
    f = request.files.get("pdf")
    if not f or not f.filename:
        return jsonify(error="No PDF uploaded."), 400
    data = f.read()
    if not data.startswith(b"%PDF"):
        return jsonify(error="That file is not a PDF."), 400
    try:
        result = extractor.extract(data)
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Could not read the PDF: {exc}"), 400

    articles = result["articles"]
    if not articles:
        return jsonify(error="No articles found. Each article needs a large bold title "
                             "followed by a location line."), 400

    stem = os.path.splitext(os.path.basename(f.filename))[0]
    jid = uuid.uuid4().hex
    with LOCK:
        JOBS[jid] = {
            "created": time.time(),
            "name": stem,
            "images": {i: a.images for i, a in enumerate(articles)},
        }

    return jsonify(
        job=jid,
        zip_name=f"{stem}.zip",
        articles=[{
            "index": i,
            "number": a.number,
            "title": a.title,
            "location": a.location,
            "paragraphs": a.paragraphs,
            "highlights": a.highlights,
            "links": a.links,
            "pages": a.pages,
            "images": [{"ext": img["ext"], "size": len(img["data"])} for img in a.images],
        } for i, a in enumerate(articles)],
    )


@app.get("/api/job/<jid>/image/<int:art>/<int:idx>")
def api_image(jid, art, idx):
    job = JOBS.get(jid) or abort(404)
    imgs = job["images"].get(art) or abort(404)
    if not 0 <= idx < len(imgs):
        abort(404)
    img = imgs[idx]
    mime = "image/jpeg" if img["ext"] == "jpg" else "image/png"
    resp = send_file(io.BytesIO(img["data"]), mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=7200"
    return resp


@app.post("/api/job/<jid>/zip")
def api_zip(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="This upload has expired. Please upload the PDF again."), 410
    payload = request.get_json(silent=True) or {}
    articles = [a for a in payload.get("articles", []) if a.get("include", True)]
    if not articles:
        return jsonify(error="Nothing selected to export."), 400
    name = re.sub(r'[<>:"/\\|?*]', "", payload.get("zip_name") or f"{job['name']}.zip")
    if not name.lower().endswith(".zip"):
        name += ".zip"
    data = builder.build_zip(articles, job["images"])
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")   # HOST=0.0.0.0 to allow other devices
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host=host, port=port, debug=False)
