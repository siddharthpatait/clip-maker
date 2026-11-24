#!/usr/bin/env python3
"""ytcrop web page: a simple page to make clips.

On your computer:
    python web.py            # start the page and (if cloudflared is installed) a public link
    python web.py --local    # only on this computer, no public link

In the cloud (e.g. Hugging Face Spaces):
    python web.py --cloud    # listens on $PORT (default 7860), no tunnel

Access:
- If ALLOWED_EMAILS and ACCESS_PASSWORD are set (environment variables / Space secrets),
  people sign in with an allowed email and that password.
- Otherwise the printed link carries a secret ?key=... (local only: cloud mode will not
  open the page until email sign-in is set up).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file, session

import ytcrop

PORT = 8765
WORKERS = max(1, int(os.environ.get("WEB_WORKERS", "3")))   # clips made at the same time
ZIP_DIR = Path(tempfile.gettempdir()) / "ytcrop_zips"
MAX_WEB_CLIP_SECONDS = 10 * 60          # keep strangers from making hour-long clips
KEEP_CLIPS_HOURS = 6                    # cloud mode: delete finished clips after this
COOKIE = "ytcrop_key"

app = Flask(__name__)
app.permanent_session_lifetime = datetime.timedelta(days=30)
ACCESS_KEY = secrets.token_urlsafe(9)
CLOUD = False                           # set in main()

jobs: dict[str, dict] = {}
job_queue: "queue.Queue[str]" = queue.Queue()
jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Access control
#   email mode: ALLOWED_EMAILS + ACCESS_PASSWORD set -> sign-in page
#   key mode (local only): the share link carries ?key=..., kept in a cookie
# --------------------------------------------------------------------------- #
def allowed_emails() -> set[str]:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in re.split(r"[,;\s]+", raw) if "@" in e}


def access_password() -> str:
    return os.environ.get("ACCESS_PASSWORD", "").strip()


def email_mode() -> bool:
    return bool(allowed_emails() and access_password())


def _pw_tag() -> str:
    """Changes when ACCESS_PASSWORD changes, which signs everyone out."""
    return hashlib.sha256(("ytcrop-pw:" + access_password()).encode()).hexdigest()[:16]


def setup_secret_key():
    # Stable across restarts (so people stay signed in) without needing another secret.
    seed = os.environ.get("SECRET_KEY") or ("ytcrop-session:" + access_password() + ":" +
                                            ",".join(sorted(allowed_emails())))
    app.secret_key = hashlib.sha256(seed.encode()).digest() if email_mode() else secrets.token_bytes(32)


def signed_in_email() -> str | None:
    email = session.get("email")
    if email and email in allowed_emails() and session.get("pw") == _pw_tag():
        return email          # removing someone from ALLOWED_EMAILS locks them out right away
    return None


_failed_logins: dict[str, list[float]] = {}
_failed_lock = threading.Lock()


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if (CLOUD and fwd) else (request.remote_addr or "?")


def too_many_failures(ip: str) -> bool:
    now = time.time()
    with _failed_lock:
        recent = [t for t in _failed_logins.get(ip, []) if now - t < 15 * 60]
        _failed_logins[ip] = recent
        return len(recent) >= 10


def _simple_page(title: str, body_html: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><div style='font:17px/1.5 system-ui,sans-serif;max-width:480px;"
            f"margin:40px auto;padding:0 16px'><h2>{title}</h2><p>{body_html}</p></div>")


@app.before_request
def check_access():
    if email_mode():
        if request.path in ("/login", "/logout") or signed_in_email():
            return None
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="You were signed out. Reload the page and sign in again."), 401
        return redirect("/login")
    if CLOUD:
        return (_simple_page("Not set up yet",
                             "Add the secrets <b>ALLOWED_EMAILS</b> and <b>ACCESS_PASSWORD</b> in the "
                             "Space settings, then restart the Space."), 503)
    if secrets.compare_digest(request.cookies.get(COOKIE, ""), ACCESS_KEY) or \
            secrets.compare_digest(request.args.get("key", ""), ACCESS_KEY):
        return None
    return _simple_page("Missing key", "This link is missing its key. Ask for the full link again."), 403


@app.after_request
def remember_key(resp):
    if not email_mode() and request.args.get("key") == ACCESS_KEY:
        resp.set_cookie(COOKIE, ACCESS_KEY, max_age=30 * 86400, httponly=True, samesite="Lax")
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if not email_mode():
        return redirect("/")
    error, email = "", ""
    if request.method == "POST":
        ip = client_ip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        if too_many_failures(ip):
            error = "Too many wrong tries. Wait 15 minutes and try again."
        elif email in allowed_emails() and secrets.compare_digest(password.encode(), access_password().encode()):
            session.clear()
            session.permanent = True
            session["email"] = email
            session["pw"] = _pw_tag()
            print(f"[web] signed in: {email}")
            return redirect("/")
        else:
            with _failed_lock:
                _failed_logins.setdefault(ip, []).append(time.time())
            time.sleep(1)
            error = "That email isn't on the list, or the password is wrong."
    elif signed_in_email():
        return redirect("/")
    page = Path(__file__).with_name("login_page.html").read_text(encoding="utf-8")
    return page.replace("{{error}}", html.escape(error)).replace("{{email}}", html.escape(email))


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


# --------------------------------------------------------------------------- #
# Background workers: WORKERS clips at the same time, others wait in line
# --------------------------------------------------------------------------- #
def set_job(job_id: str, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def worker():
    while True:
        job_id = job_queue.get()
        job = jobs[job_id]
        try:
            set_job(job_id, state="working", status="Getting video info...")
            info = ytcrop.fetch_info(ytcrop.extract_video_id(job["link"]))
            start, end, whole, notes = ytcrop.resolve_cut(ytcrop.parse_cut(job["cut"]), info.duration)
            if end - start > MAX_WEB_CLIP_SECONDS:
                raise ytcrop.UserError(f"Clips made on the web page can be at most "
                                       f"{MAX_WEB_CLIP_SECONDS // 60} minutes long.")
            set_job(job_id, title=info.title, notes=notes)

            def log(msg):
                text = msg.strip()
                if "Downloading" in text:
                    text = "Downloading from YouTube..."
                elif "encoding" in text:
                    text = "Cutting and making the video..."
                set_job(job_id, status=text)

            out, length = ytcrop.make_clip(info, start, end, whole, job["reel"], job["hook"], log=log)
            set_job(job_id, state="done", status="Done!", file=str(out), filename=out.name,
                    length=round(length, 1))
            print(f"[web] made {out.name}")
        except ytcrop.UserError as e:
            set_job(job_id, state="error", status=str(e))
        except Exception as e:  # never kill the worker
            set_job(job_id, state="error", status=f"Something went wrong: {e}")
        finally:
            job_queue.task_done()


def queue_position(job_id: str) -> int:
    """How many queued clips are ahead of this one."""
    with jobs_lock:
        waiting = sorted((j for j in jobs.values() if j["state"] == "queued"), key=lambda j: j["created"])
        ids = [j["id"] for j in waiting]
    return ids.index(job_id) if job_id in ids else 0


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    page = Path(__file__).with_name("web_page.html").read_text(encoding="utf-8")
    who = signed_in_email()
    if who:   # add "Sign out" to the top bar
        link = (f'<a class="btn ghost" href="/logout" title="Signed in as {html.escape(who)}">'
                f'Sign out</a>')
        page = page.replace('<div class="actions">', '<div class="actions">' + link, 1)
    resp = app.response_class(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def cleanup_loop():
    """Cloud mode: the disk is small and temporary, so delete clips after KEEP_CLIPS_HOURS."""
    while True:
        cutoff = time.time() - KEEP_CLIPS_HOURS * 3600
        for f in list(ytcrop.OUTPUT_DIR.glob("*.mp4")) if ytcrop.OUTPUT_DIR.is_dir() else []:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        with jobs_lock:
            for jid in [j for j, v in jobs.items() if v["created"] < cutoff and v["state"] in ("done", "error")]:
                del jobs[jid]
        time.sleep(600)


@app.post("/api/info")
def api_info():
    link = (request.json or {}).get("link", "")
    try:
        info = ytcrop.fetch_info(ytcrop.extract_video_id(link))
    except ytcrop.UserError as e:
        return jsonify(ok=False, error=str(e))
    dur = ytcrop.fmt_clock(info.duration) if info.duration else None
    return jsonify(ok=True, title=info.title, duration=dur)


@app.post("/api/clip")
def api_clip():
    data = request.json or {}
    link = str(data.get("link", "")).strip()
    cut = str(data.get("cut", "")).strip()
    hook = str(data.get("hook", "")).strip()[:150]
    try:                                    # fail fast on obvious mistakes
        ytcrop.extract_video_id(link)
        ytcrop.parse_cut(cut)
    except ytcrop.UserError as e:
        return jsonify(ok=False, error=str(e))
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = dict(id=job_id, link=link, cut=cut, hook=hook, reel=data.get("format") == "reel",
                            state="queued", status="Waiting...", created=time.time(), notes=[])
    job_queue.put(job_id)
    return jsonify(ok=True, job=job_id)


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404)
    out = {k: job.get(k) for k in ("state", "status", "title", "notes", "filename", "length")}
    if job["state"] == "queued":
        pos = queue_position(job_id)
        out["status"] = (f"Waiting in line ({pos} ahead)..." if pos
                         else "Waiting for a free slot...")
    return jsonify(out)


@app.get("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job.get("state") != "done" or not Path(job["file"]).is_file():
        abort(404)
    return send_file(job["file"], mimetype="video/mp4", as_attachment=request.args.get("inline") != "1",
                     download_name=job["filename"])


@app.get("/download-zip")
def download_zip():
    ids = [i for i in request.args.get("jobs", "").split(",") if i][:50]
    with jobs_lock:
        files = [Path(jobs[i]["file"]) for i in ids
                 if i in jobs and jobs[i].get("state") == "done" and Path(jobs[i]["file"]).is_file()]
    if not files:
        abort(404)
    ZIP_DIR.mkdir(exist_ok=True)
    for old in ZIP_DIR.glob("*.zip"):        # remove zips from earlier downloads (Windows locks
        try:                                 # a file while it's being sent, so not right away)
            if time.time() - old.stat().st_mtime > 600:
                old.unlink()
        except OSError:
            pass
    zip_path = ZIP_DIR / f"{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:   # mp4 doesn't compress further
        for f in files:
            zf.write(f, f.name)
    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name=f"clips_{time.strftime('%Y-%m-%d_%H%M')}.zip")


# --------------------------------------------------------------------------- #
# Public link via Cloudflare quick tunnel (free, no account)
# --------------------------------------------------------------------------- #
def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for p in (Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
              Path(r"C:\Program Files\cloudflared\cloudflared.exe")):
        if p.is_file():
            return str(p)
    return None


def start_tunnel(exe: str, port: int = PORT) -> tuple[subprocess.Popen, str | None]:
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    url_holder: list[str] = []
    found, registered = threading.Event(), threading.Event()

    def read():
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not url_holder:
                url_holder.append(m.group(0))
                found.set()
            if "Registered tunnel connection" in line:
                registered.set()
    threading.Thread(target=read, daemon=True).start()
    found.wait(timeout=40)
    # Opening the link before it's live makes some internet providers remember
    # "doesn't exist" for a while, so only show it once it's ready.
    if url_holder:
        registered.wait(timeout=30)
        time.sleep(8)
    return proc, (url_holder[0] if url_holder else None)


def main() -> int:
    parser = argparse.ArgumentParser(description="ytcrop web page")
    parser.add_argument("--local", action="store_true", help="no public link, only this computer")
    parser.add_argument("--cloud", action="store_true",
                        help="run on a cloud host: listen on $PORT (default 7860), email sign-in required")
    parser.add_argument("--port", type=int, help="port to listen on")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    global CLOUD
    CLOUD = args.cloud or bool(os.environ.get("SPACE_ID"))    # SPACE_ID is set on Hugging Face
    setup_secret_key()
    if CLOUD:
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")

    if not ytcrop.check_dependencies():
        return 1
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()

    from waitress import create_server
    if CLOUD:
        port = args.port or int(os.environ.get("PORT", 7860))
        threading.Thread(target=cleanup_loop, daemon=True).start()
        server = create_server(app, host="0.0.0.0", port=port, threads=8)
        print(f"Clip Maker (cloud) listening on port {port}")
        if email_mode():
            print(f"Email sign-in is on for {len(allowed_emails())} address(es).")
        else:
            print("WARNING: ALLOWED_EMAILS / ACCESS_PASSWORD not set; the page shows 'Not set up yet'.")
        server.run()
        return 0

    port = args.port or PORT
    server = create_server(app, host="127.0.0.1", port=port, threads=8)

    print("=" * 64)
    print(" ytcrop web page is running. Keep this window open.")
    if email_mode():
        print(f"\n Email sign-in is on ({len(allowed_emails())} allowed address(es)).")
        print(f"\n Open on this computer:\n   http://localhost:{port}/")
    else:
        print(f"\n Open on this computer:\n   http://localhost:{port}/?key={ACCESS_KEY}")

    tunnel = None
    if not args.local:
        exe = find_cloudflared()
        if not exe:
            print("\n (No public link: cloudflared isn't installed. Install it with:\n"
                  "   winget install --id Cloudflare.cloudflared -e\n  then start this again.)")
        else:
            print("\n Making a public link for your friend...")
            tunnel, url = start_tunnel(exe, port)
            if url:
                share = f"{url}/" if email_mode() else f"{url}/?key={ACCESS_KEY}"
                print(f"\n SEND THIS LINK TO YOUR FRIEND:\n   {share}")
                print("\n (A new link is made each time you start this.)")
            else:
                print("\n Couldn't make a public link right now; the page still works on this computer.")
    print("\n To stop: close this window (or press Ctrl+C).")
    print("=" * 64)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel:
            tunnel.terminate()
        shutil.rmtree(ZIP_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
