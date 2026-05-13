"""
Birdnest: aggregates multiple birdcams into a single view.

Runs status + clip-index pollers per cam, serves a unified API for the
React client, and (in proxy mode) forwards stream/clip/thumbnail requests
upstream to the cams with the right auth header attached.
"""
import logging
import sqlite3
import threading
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from time import sleep, time

import requests
from flask import Flask, Response, abort, jsonify, request, stream_with_context

logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger("birdnest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

# --- config ---
CONFIG_PATHS = [
    Path("/etc/birdnest/config.toml"),
    Path.home() / ".config/birdnest/config.toml",
    Path("birdnest.toml"),
]
STATUS_POLL_SECONDS = 5
CLIPS_POLL_SECONDS = 10
OFFLINE_AFTER_SECONDS = 30  # status older than this -> cam is "offline"
HTTP_TIMEOUT = 4  # short so a dead cam doesn't wedge the poller
VERSION = "0.1.0"


def load_config():
    for path in CONFIG_PATHS:
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            log.info(f"loaded config from {path}")
            return data, path
    raise SystemExit(
        "No birdnest config found. Create /etc/birdnest/config.toml "
        "or ./birdnest.toml (see birdnest.example.toml)."
    )


CONFIG, CONFIG_PATH = load_config()
CAMS = {c["name"]: c for c in CONFIG.get("cams", [])}
if not CAMS:
    raise SystemExit(f"No cams defined in {CONFIG_PATH}")

SERVER_CFG = CONFIG.get("server", {})
PROXY_MODE = SERVER_CFG.get("proxy_mode", "proxy")
if PROXY_MODE not in ("proxy", "direct"):
    raise SystemExit(f"proxy_mode must be 'proxy' or 'direct', got {PROXY_MODE!r}")
DB_PATH = Path(SERVER_CFG.get("db_path", "birdnest.db"))
CLIP_STORE = Path(SERVER_CFG.get("clip_store", "clip_store"))
PORT = SERVER_CFG.get("port", 8000)
CLIP_STORE.mkdir(parents=True, exist_ok=True)

log.info(f"proxy_mode={PROXY_MODE} cams={list(CAMS)} db={DB_PATH}")


# --- database ---
def init_db():
    """Initialize SQLite schema. Safe to call repeatedly."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clips (
                cam_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                offloaded_path TEXT,
                offloaded_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_on_cam_at TEXT,
                PRIMARY KEY (cam_name, filename)
            );
            CREATE INDEX IF NOT EXISTS idx_clips_recorded_at
                ON clips(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_clips_cam_recorded
                ON clips(cam_name, recorded_at DESC);
        """)
        conn.commit()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


init_db()


# --- per-cam status cache (in-memory, replaced atomically) ---
status_lock = threading.Lock()
cam_status = {
    name: {
        "name": name,
        "online": False,
        "last_polled_at": None,
        "last_success_at": None,
        "status": None,  # raw payload from /api/status
        "error": None,
    }
    for name in CAMS
}


def get_cam_status(name):
    with status_lock:
        return dict(cam_status[name])


def get_all_cam_statuses():
    with status_lock:
        return [dict(s) for s in cam_status.values()]


# --- HTTP helpers ---
def cam_base_url(cam):
    scheme = cam.get("scheme", "http")
    return f"{scheme}://{cam['host']}:{cam.get('port', 5000)}"


def cam_auth_headers(cam):
    return {"Authorization": f"Bearer {cam['token']}"}


def cam_get(cam, path, **kwargs):
    """GET against a cam with auth. Returns the response or raises."""
    url = cam_base_url(cam) + path
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    kwargs.setdefault("headers", {}).update(cam_auth_headers(cam))
    return requests.get(url, **kwargs)


# --- pollers ---
def poll_status(cam):
    """One iteration of the status poller for a single cam."""
    name = cam["name"]
    now_iso = datetime.utcnow().isoformat()
    try:
        r = cam_get(cam, "/api/status")
        r.raise_for_status()
        payload = r.json()
        with status_lock:
            cam_status[name].update({
                "online": True,
                "last_polled_at": now_iso,
                "last_success_at": now_iso,
                "status": payload,
                "error": None,
            })
    except Exception as e:
        with status_lock:
            cam_status[name].update({
                "last_polled_at": now_iso,
                "error": str(e),
            })
            # mark offline if last success was too long ago (or never)
            last = cam_status[name]["last_success_at"]
            if last is None:
                cam_status[name]["online"] = False
            else:
                age = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
                if age > OFFLINE_AFTER_SECONDS:
                    cam_status[name]["online"] = False


def status_poller_loop(cam):
    log.info(f"[{cam['name']}] status poller started")
    while True:
        poll_status(cam)
        sleep(STATUS_POLL_SECONDS)


def poll_clips(cam):
    """Fetch the clip list from a cam and upsert into the DB."""
    name = cam["name"]
    try:
        # paginate through all pages so we get everything
        page = 1
        all_clips = []
        while True:
            r = cam_get(cam, "/api/clips", params={"page": page})
            r.raise_for_status()
            data = r.json()
            all_clips.extend(data["clips"])
            if page >= data["total_pages"] or data["total_pages"] == 0:
                break
            page += 1
    except Exception as e:
        log.warning(f"[{name}] clip poll failed: {e}")
        return

    now_iso = datetime.utcnow().isoformat()
    with db() as conn:
        for c in all_clips:
            # parse the timestamp birdcam gives us; it's local-tz isoformat
            recorded_at = c["timestamp"]
            size_bytes = int(c["size_mb"] * 1024 * 1024)
            # upsert: insert if new, otherwise just update last_seen
            conn.execute("""
                INSERT INTO clips (cam_name, filename, recorded_at, size_bytes,
                                   first_seen_at, last_seen_on_cam_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cam_name, filename) DO UPDATE SET
                    last_seen_on_cam_at = excluded.last_seen_on_cam_at,
                    size_bytes = excluded.size_bytes
            """, (name, c["name"], recorded_at, size_bytes, now_iso, now_iso))


def clips_poller_loop(cam):
    log.info(f"[{cam['name']}] clips poller started")
    while True:
        poll_clips(cam)
        sleep(CLIPS_POLL_SECONDS)


def start_pollers():
    for cam in CAMS.values():
        threading.Thread(target=status_poller_loop, args=(cam,), daemon=True).start()
        threading.Thread(target=clips_poller_loop, args=(cam,), daemon=True).start()


# --- Flask app ---
app = Flask(__name__)


def url_for_cam_resource(cam, path, with_token_qs=False):
    """Build a URL for a cam resource. In direct mode, includes ?token= for
    things like <img> and <video> that can't send headers."""
    base = cam_base_url(cam) + path
    if with_token_qs:
        sep = "&" if "?" in path else "?"
        return f"{base}{sep}token={cam['token']}"
    return base


@app.route("/api/cams")
def api_cams():
    """One call for the dashboard: every cam's current status + how the client
    should reach its stream/clips/thumbnails given the current proxy mode."""
    out = []
    for name, cam in CAMS.items():
        s = get_cam_status(name)
        if PROXY_MODE == "proxy":
            urls = {
                "stream": f"/api/cams/{name}/stream.mjpg",
                "clip_template": f"/api/cams/{name}/clips/{{filename}}",
                "thumb_template": f"/api/cams/{name}/clips/{{filename}}/thumbnail",
            }
        else:
            urls = {
                "stream": url_for_cam_resource(cam, "/stream.mjpg", with_token_qs=True),
                "clip_template": url_for_cam_resource(
                    cam, "/clips/{filename}", with_token_qs=True
                ),
                "thumb_template": url_for_cam_resource(
                    cam, "/api/clips/{filename}/thumbnail", with_token_qs=True
                ),
            }
        out.append({
            "name": name,
            "host": cam["host"],
            "online": s["online"],
            "last_polled_at": s["last_polled_at"],
            "last_success_at": s["last_success_at"],
            "error": s["error"],
            "status": s["status"],
            "urls": urls,
        })
    return jsonify({
        "proxy_mode": PROXY_MODE,
        "cams": out,
    })


@app.route("/api/clips")
def api_clips():
    """Unified timeline across all cams.

    Query params:
      cam: filter to one cam name (optional)
      tag: filter to clips containing this tag (optional, substring match on json)
      page: 1-indexed, default 1
      per_page: default 30
    """
    page = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(200, int(request.args.get("per_page", 30))))
    cam_filter = request.args.get("cam")
    tag_filter = request.args.get("tag")

    where = []
    params = []
    if cam_filter:
        where.append("cam_name = ?")
        params.append(cam_filter)
    if tag_filter:
        # naive but fine for small N: tags are stored as a JSON array of strings
        where.append("tags_json LIKE ?")
        params.append(f'%"{tag_filter}"%')
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM clips {where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(f"""
            SELECT cam_name, filename, recorded_at, size_bytes, tags_json,
                   offloaded_path, offloaded_at, last_seen_on_cam_at
            FROM clips
            {where_sql}
            ORDER BY recorded_at DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, (page - 1) * per_page]).fetchall()

    out = []
    for row in rows:
        cam = CAMS.get(row["cam_name"])
        on_cam = row["last_seen_on_cam_at"] is not None
        if cam and on_cam:
            if PROXY_MODE == "proxy":
                clip_url = f"/api/cams/{row['cam_name']}/clips/{row['filename']}"
                thumb_url = f"/api/cams/{row['cam_name']}/clips/{row['filename']}/thumbnail"
            else:
                clip_url = url_for_cam_resource(
                    cam, f"/clips/{row['filename']}", with_token_qs=True
                )
                thumb_url = url_for_cam_resource(
                    cam, f"/api/clips/{row['filename']}/thumbnail", with_token_qs=True
                )
        else:
            clip_url = thumb_url = None
        out.append({
            "cam_name": row["cam_name"],
            "filename": row["filename"],
            "recorded_at": row["recorded_at"],
            "size_bytes": row["size_bytes"],
            "size_mb": round(row["size_bytes"] / (1024 * 1024), 1),
            "tags": _parse_tags(row["tags_json"]),
            "offloaded": row["offloaded_path"] is not None,
            "offloaded_at": row["offloaded_at"],
            "on_cam": on_cam,
            "clip_url": clip_url,
            "thumb_url": thumb_url,
        })

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
        "clips": out,
    })


def _parse_tags(s):
    import json
    try:
        return json.loads(s or "[]")
    except json.JSONDecodeError:
        return []


@app.route("/api/clips/<cam_name>/<filename>/tags", methods=["PUT"])
def api_set_tags(cam_name, filename):
    """Replace the tag list for a clip. Body: {"tags": ["favorite", "blue-jay"]}"""
    import json
    body = request.get_json(silent=True) or {}
    tags = body.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return jsonify({"error": "tags must be a list of strings"}), 400
    tags_json = json.dumps(tags)
    with db() as conn:
        cur = conn.execute(
            "UPDATE clips SET tags_json = ? WHERE cam_name = ? AND filename = ?",
            (tags_json, cam_name, filename),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "clip not found"}), 404
    return jsonify({"tags": tags})


# --- proxy endpoints (only active in proxy mode, but defined either way) ---
@app.route("/api/cams/<cam_name>/stream.mjpg")
def proxy_stream(cam_name):
    if PROXY_MODE != "proxy":
        abort(404)
    cam = CAMS.get(cam_name)
    if cam is None:
        abort(404)
    upstream = requests.get(
        cam_base_url(cam) + "/stream.mjpg",
        headers=cam_auth_headers(cam),
        stream=True,
        timeout=HTTP_TIMEOUT,
    )
    if upstream.status_code != 200:
        return Response(f"upstream returned {upstream.status_code}", status=502)

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        mimetype=upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame"),
    )


@app.route("/api/cams/<cam_name>/clips/<filename>")
def proxy_clip(cam_name, filename):
    if PROXY_MODE != "proxy":
        abort(404)
    cam = CAMS.get(cam_name)
    if cam is None:
        abort(404)
    return _proxy_passthrough(cam, f"/clips/{filename}", forward_range=True)


@app.route("/api/cams/<cam_name>/clips/<filename>/thumbnail")
def proxy_thumbnail(cam_name, filename):
    if PROXY_MODE != "proxy":
        abort(404)
    cam = CAMS.get(cam_name)
    if cam is None:
        abort(404)
    return _proxy_passthrough(cam, f"/api/clips/{filename}/thumbnail")


def _proxy_passthrough(cam, path, forward_range=False):
    """Generic upstream passthrough for non-streaming resources."""
    headers = cam_auth_headers(cam)
    if forward_range:
        # forward Range header so seekable <video> works
        rng = request.headers.get("Range")
        if rng:
            headers["Range"] = rng
    upstream = requests.get(
        cam_base_url(cam) + path,
        headers=headers,
        stream=True,
        timeout=HTTP_TIMEOUT,
    )

    # pass through useful response headers
    passthrough_headers = {}
    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=passthrough_headers,
    )


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "version": VERSION,
        "cams": [{"name": s["name"], "online": s["online"]} for s in get_all_cam_statuses()],
    })


if __name__ == "__main__":
    start_pollers()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)