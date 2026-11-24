#!/usr/bin/env python3
"""ytcrop - cut a clip out of a YouTube video and save it as an Instagram-ready MP4.

Usage:
    python ytcrop.py                      # interactive
    python ytcrop.py --batch clips.txt    # batch (Reel 9:16 by default)
    python ytcrop.py --batch clips.txt --original
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
FONTS_DIR = SCRIPT_DIR / "fonts"

PAD_SECONDS = 2.0          # extra seconds downloaded on each side of the cut
REELS_MAX_SECONDS = 90
REEL_W, REEL_H = 1080, 1920


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class UserError(Exception):
    """A normal mistake or problem: print the message, no traceback."""


class CutError(UserError):
    pass


class LinkError(UserError):
    pass


# --------------------------------------------------------------------------- #
# Link parsing
# --------------------------------------------------------------------------- #
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(link: str) -> str:
    """Return the 11-char video ID from any common YouTube link (or a bare ID)."""
    link = link.strip().strip("<>\"'")
    if VIDEO_ID_RE.match(link):
        return link
    if not re.match(r"^[a-z]+://", link, re.I):
        link = "https://" + link
    u = urlparse(link)
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    candidate = None
    if host == "youtu.be":
        candidate = u.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if u.path == "/watch":
            candidate = (parse_qs(u.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v|e)/([^/?#]+)", u.path)
            if m:
                candidate = m.group(1)
    if candidate and VIDEO_ID_RE.match(candidate):
        return candidate
    raise LinkError("That doesn't look like a YouTube video link. "
                    "Try something like https://www.youtube.com/watch?v=XXXXXXXXXXX")


# --------------------------------------------------------------------------- #
# Cut parsing
# --------------------------------------------------------------------------- #
_TIME = r"\d+(?::\d+){0,2}(?:\.\d+)?"
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_RANGE_RE = re.compile(
    rf"({_TIME})(?:\s*(?:[{_DASHES}]|\bto\b)\s*|\s+)({_TIME})", re.I)


def parse_time(text: str) -> float:
    """'52' -> 52, '0:52.5' -> 52.5, '1:02:03' -> 3723."""
    parts = text.strip().split(":")
    if len(parts) > 3 or any(p == "" for p in parts):
        raise CutError(f"'{text}' is not a valid time.")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise CutError(f"'{text}' is not a valid time.") from None
    if any("." in p for p in parts[:-1]):
        raise CutError(f"'{text}': only the seconds part can have decimals.")
    if len(nums) > 1 and any(n >= 60 for n in nums[1:]):
        raise CutError(f"'{text}': minutes and seconds must be below 60.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def parse_cut(text: str) -> tuple[float, float] | None:
    """Parse a cut like '0:52–1:12 (20 sec)'. Returns (start, end), or None for
    an empty input (= whole video). Extra text after the first range is ignored."""
    text = (text or "").strip()
    if not text:
        return None
    m = _RANGE_RE.search(text)
    if not m:
        raise CutError("Couldn't find a time range. Use e.g. 0:52-1:12, 52-72 or 1:02:03-1:03:10.")
    start, end = parse_time(m.group(1)), parse_time(m.group(2))
    if end <= start:
        raise CutError(f"The end ({fmt_clock(end)}) must be after the start ({fmt_clock(start)}).")
    return start, end


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _num(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def fmt_clock(seconds: float) -> str:
    """3723 -> '1:02:03', 128 -> '2:08', 52.5 -> '0:52.5'."""
    whole = int(seconds)
    frac = seconds - whole
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:02d}" + (_num(frac)[1:] if frac >= 0.005 else "")
    return f"{h}:{m:02d}:{s_txt}" if h else f"{m}:{s_txt}"


def fmt_label(seconds: float) -> str:
    """Filename label: 52 -> '0m52s', 3723 -> '1h02m03s', 52.5 -> '0m52.5s'."""
    whole = int(seconds)
    frac = seconds - whole
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:02d}" + (_num(frac)[1:] if frac >= 0.005 else "")
    return f"{h}h{m:02d}m{s_txt}s" if h else f"{m}m{s_txt}s"


_SLUG_FILLER = {"scene", "clip", "clips", "official", "video", "hd", "4k", "full", "trailer",
                "movieclips", "remastered", "lyrics", "audio"}


def short_slug(title: str, max_words: int = 6, max_len: int = 40) -> str:
    """'Planes, Trains and Automobiles - Those Aren't Pillows' -> 'those-arent-pillows'."""
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title)          # drop (Official Video), [HD]
    t = re.split(r"\s+[|•]\s+", t)[0]                        # 'Clip | Channel' -> 'Clip'
    segments = [s for s in re.split(r"\s+[-\u2013\u2014]\s+", t) if s.strip()]
    if segments:
        t = segments[-1]                                     # 'Movie - Scene' -> 'Scene'
    t = re.sub(r"['\u2019`]", "", t.lower())
    words = re.findall(r"[a-z0-9]+", t)
    while len(words) > 1 and words[-1] in _SLUG_FILLER:     # '... Pillows Scene' -> '... pillows'
        words.pop()
    words = words[:max_words]
    slug = "-".join(words)[:max_len].strip("-")
    return slug or "clip"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# --------------------------------------------------------------------------- #
# Dependency check
# --------------------------------------------------------------------------- #
def install_hints() -> dict[str, str]:
    system = platform.system()
    if system == "Windows":
        return {"ffmpeg": "winget install --id Gyan.FFmpeg -e   (then open a new terminal)",
                "yt-dlp": "python -m pip install -U -r requirements.txt"}
    if system == "Darwin":
        return {"ffmpeg": "brew install ffmpeg",
                "yt-dlp": "python3 -m pip install -U -r requirements.txt"}
    if shutil.which("apt-get"):
        ff = "sudo apt-get update && sudo apt-get install -y ffmpeg"
    elif shutil.which("dnf"):
        ff = "sudo dnf install -y ffmpeg"
    elif shutil.which("pacman"):
        ff = "sudo pacman -S ffmpeg"
    else:
        ff = "install ffmpeg with your distro's package manager"
    return {"ffmpeg": ff, "yt-dlp": "python3 -m pip install -U -r requirements.txt"}


def check_dependencies() -> bool:
    hints = install_hints()
    missing = []
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        missing.append(("ffmpeg", hints["ffmpeg"]))
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append(("yt-dlp", hints["yt-dlp"]))
    if not missing:
        return True
    os_name = {"Darwin": "macOS"}.get(platform.system(), platform.system())
    print(f"Missing requirements (detected OS: {os_name}):")
    for name, cmd in missing:
        print(f"  - {name}:  {cmd}")
    print("Install them and run ytcrop again.")
    return False


# --------------------------------------------------------------------------- #
# YouTube (yt-dlp)
# --------------------------------------------------------------------------- #
UPDATE_HINT = "Your yt-dlp is probably out of date. Update it with:  python -m pip install -U yt-dlp"


# YTCROP_MAX_HEIGHT / YTCROP_PRESET let a slow server trade a little quality for speed.
MAX_HEIGHT = int(os.environ.get("YTCROP_MAX_HEIGHT") or 1080)
X264_PRESET = os.environ.get("YTCROP_PRESET") or "veryfast"
FORMAT_SELECTOR = (f"bv*[height<={MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]/"
                   f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/bv*+ba/b")


class _Logger:
    """Collect yt-dlp warnings/errors quietly; we report our own messages."""

    def __init__(self):
        self.messages: list[str] = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.messages.append(msg)

    def error(self, msg):
        self.messages.append(msg)


def _ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _Logger(),
        # YouTube needs a JS runtime for its challenges; use whichever is installed.
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}},
        "retries": 3,
        "socket_timeout": 30,
    }
    # Optional, mainly for cloud servers that YouTube blocks:
    #   YTCROP_PROXY   = http://user:pass@host:port
    #   YTCROP_COOKIES = contents of a Netscape-format cookies.txt (or YTCROP_COOKIES_FILE = path)
    if os.environ.get("YTCROP_PROXY"):
        opts["proxy"] = os.environ["YTCROP_PROXY"].strip()
    cookie_file = _cookie_file()
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    opts.update(extra)
    return opts


_cookie_path: Path | None = None


def _cookie_file() -> Path | None:
    global _cookie_path
    if os.environ.get("YTCROP_COOKIES_FILE"):
        return Path(os.environ["YTCROP_COOKIES_FILE"])
    text = os.environ.get("YTCROP_COOKIES", "").strip()
    if not text:
        return None
    if _cookie_path is None:
        # yt-dlp rewrites the cookie file, so give it its own copy.
        _cookie_path = Path(tempfile.gettempdir()) / "ytcrop_cookies.txt"
        _cookie_path.write_text(text.replace("\\n", "\n") + "\n", encoding="utf-8")
    return _cookie_path


def explain_ytdlp_error(err: Exception) -> str:
    msg = str(err)
    low = msg.lower()
    if "private video" in low:
        return "This video is private."
    if "age" in low and ("confirm your age" in low or "age-restricted" in low or "inappropriate" in low):
        return "This video is age-restricted and can't be downloaded without signing in."
    if "removed" in low or "terminated" in low or "no longer available" in low:
        return "This video has been removed or is no longer available."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "premiere" in low or "live event will begin" in low or "is not available yet" in low:
        return "This video hasn't been released yet (premiere / upcoming live)."
    if any(s in low for s in ("getaddrinfo", "name or service not known", "nodename nor servname",
                              "failed to resolve", "network is unreachable", "no route to host",
                              "timed out", "connection refused", "temporary failure in name resolution",
                              "connection reset", "errno 11001", "unable to connect")):
        return "Couldn't reach YouTube. Check your internet connection and try again."
    if any(s in low for s in ("signature", "nsig", "requested format is not available",
                              "unable to extract", "only images are available",
                              "sign in to confirm you", "http error 403", "player response")):
        return f"YouTube download failed ({_first_line(msg)}).\n{UPDATE_HINT}"
    if "video unavailable" in low or "unavailable" in low:
        return "This video is unavailable (it may be removed, private or blocked in your country)."
    if "is not a valid url" in low or "unsupported url" in low:
        return "That link isn't a valid YouTube video link."
    return f"Download failed: {_first_line(msg)}\nIf this keeps happening: {UPDATE_HINT}"


def _first_line(msg: str) -> str:
    msg = re.sub(r"^ERROR:\s*", "", msg.strip())
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)
    return msg.splitlines()[0][:200] if msg else "unknown error"


@dataclass
class VideoInfo:
    id: str
    title: str
    duration: float | None
    url: str
    raw: dict | None = None      # full yt-dlp info, reused so the download doesn't ask YouTube again


# Asking YouTube for a video's details is the slowest step (~5 s), so remember the
# answer for a while. Stream links in it stay valid for hours; 30 min is safe.
INFO_CACHE_SECONDS = 30 * 60
_info_cache: dict[str, tuple[float, VideoInfo]] = {}
_info_lock = threading.Lock()


def fetch_info(video_id: str) -> VideoInfo:
    import yt_dlp
    with _info_lock:
        hit = _info_cache.get(video_id)
        if hit and time.time() - hit[0] < INFO_CACHE_SECONDS:
            return hit[1]
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True, format=FORMAT_SELECTOR)) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=False))
    except yt_dlp.utils.DownloadError as e:
        raise UserError(explain_ytdlp_error(e)) from None
    if info.get("is_live"):
        raise UserError("This is a live stream that's still running; wait until it has ended.")
    result = VideoInfo(id=video_id, title=info.get("title") or video_id,
                       duration=info.get("duration"), url=url, raw=info)
    with _info_lock:
        _info_cache[video_id] = (time.time(), result)
    return result


def _clear_partial(workdir: Path) -> None:
    for p in workdir.iterdir():
        if p.name.startswith("source"):
            p.unlink(missing_ok=True)


def download_section(info: VideoInfo, start: float, end: float, workdir: Path,
                     whole: bool) -> tuple[Path, float]:
    """Download [start-PAD, end+PAD] (or the whole video). Returns (file, offset)
    where offset is where `start` sits inside the downloaded file."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    sec_start = 0.0 if whole else max(0.0, start - PAD_SECONDS)
    sec_end = end + PAD_SECONDS
    if info.duration:
        sec_end = min(sec_end, info.duration)
    opts = _ydl_opts(
        format=FORMAT_SELECTOR,
        merge_output_format="mp4",
        outtmpl=str(workdir / "source.%(ext)s"),
        paths={"home": str(workdir), "temp": str(workdir)},
    )
    if not whole:
        opts["download_ranges"] = download_range_func(None, [(sec_start, sec_end)])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if info.raw:
                try:      # reuse the details we already have (saves ~5 s)
                    ydl.process_ie_result(copy.deepcopy(info.raw), download=True)
                except yt_dlp.utils.DownloadError:
                    # cached stream links may have expired: ask YouTube again
                    _clear_partial(workdir)
                    with _info_lock:
                        _info_cache.pop(info.id, None)
                    ydl.download([info.url])
            else:
                ydl.download([info.url])
    except yt_dlp.utils.DownloadError as e:
        raise UserError(explain_ytdlp_error(e)) from None
    files = [p for p in workdir.iterdir()
             if p.name.startswith("source") and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".m4v")]
    if not files:
        raise UserError(f"Download finished but no video file was produced.\n{UPDATE_HINT}")
    return max(files, key=lambda p: p.stat().st_size), start - sec_start


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #
def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise UserError(f"Couldn't read the downloaded video: {out.stderr.strip()[:200]}")
    data = json.loads(out.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        raise UserError("The downloaded file has no video stream.")
    fps = 30.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (video.get(key) or "0/0").partition("/")
        try:
            if float(den or 1) and float(num) > 0:
                fps = float(num) / float(den or 1)
                break
        except ValueError:
            pass
    return {"width": int(video["width"]), "height": int(video["height"]), "fps": fps,
            "has_audio": audio is not None,
            "duration": float(data.get("format", {}).get("duration") or 0)}


def find_font() -> Path | None:
    """A bold font: anything in ./fonts first, then common system fonts."""
    if FONTS_DIR.is_dir():
        for p in sorted(FONTS_DIR.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf"):
                return p
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        windir / "arialbd.ttf", windir / "segoeuib.ttf", windir / "calibrib.ttf", windir / "verdanab.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Verdana Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ]
    return next((p for p in candidates if p.is_file()), None)


def wrap_hook(text: str, width_px: int) -> tuple[list[str], int]:
    """Wrap text into at most 2 lines; returns (lines, font_size)."""
    text = " ".join(text.split())
    usable = width_px * 0.84                    # ~8% safe margin each side
    max_size = round(width_px * 0.075)          # 81px on a 1080-wide frame
    min_size = round(width_px * 0.045)
    for size in range(max_size, min_size - 1, -2):
        chars = max(8, int(usable / (size * 0.5)))    # rough bold-sans char width
        lines = textwrap.wrap(text, chars)
        if len(lines) <= 2:
            return lines, size
    chars = max(8, int(usable / (min_size * 0.5)))
    lines = textwrap.wrap(text, chars)
    second = " ".join(lines[1:])
    if len(second) > chars:
        second = second[: chars - 1].rstrip() + "\u2026"
    return [lines[0], second], min_size


def build_filter(src: dict, reel: bool, hook_lines: list[str], font_size: int,
                 font_name: str | None) -> str:
    chains = []
    if reel:
        chains.append(
            "[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"gblur=sigma=12,eq=brightness=-0.08,scale={REEL_W}:{REEL_H},setsar=1[bg];"
            f"[fgsrc]scale={REEL_W}:{REEL_H}:force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2")
        out_w, out_h = REEL_W, REEL_H
    else:
        chains.append("[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1")
        out_w, out_h = src["width"] // 2 * 2, src["height"] // 2 * 2
    if src["fps"] > 30.5:
        chains.append("fps=30")
    if hook_lines:
        # Reels: top ~13% is covered by Instagram's UI; keep text below it.
        top = round(out_h * 0.13) if reel else round(out_h * 0.06)
        line_h = round(font_size * 1.22)
        border = max(3, round(font_size * 0.09))
        font_opt = f"fontfile={font_name}:" if font_name else ""
        for i, _ in enumerate(hook_lines):
            chains.append(
                f"drawtext={font_opt}textfile=hook{i}.txt:fontcolor=white:fontsize={font_size}:"
                f"borderw={border}:bordercolor=black:x=(w-text_w)/2:y={top + i * line_h}")
    chains.append("format=yuv420p")
    return ",".join(chains) + "[v]"


# --------------------------------------------------------------------------- #
# "White" layout: 9:16 white page, caption above the video (meme style)
# --------------------------------------------------------------------------- #
EMOJI_URL = "https://cdn.jsdelivr.net/npm/emoji-datasource-apple@15.1.2/img/apple/64/{}.png"
EMOJI_DIR = Path(tempfile.gettempdir()) / "ytcrop_emoji"


def find_regular_font() -> Path | None:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        windir / "arial.ttf", windir / "segoeui.ttf",
        Path("/System/Library/Fonts/Helvetica.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    return next((p for p in candidates if p.is_file()), None)


CAPTION_FONT = Path(__file__).resolve().parent / "fonts" / "caption" / "TikTokSans.ttf"
CAPTION_WEIGHT = 400                                  # Regular (user asked for not bold)


def caption_font(size: int):
    """TikTok Sans SemiBold (bundled, OFL licence); falls back to a system sans."""
    from PIL import ImageFont
    if CAPTION_FONT.is_file():
        try:
            font = ImageFont.truetype(str(CAPTION_FONT), size)
            axes = font.get_variation_axes()
            vals = []
            for a in axes:
                name = a["name"].decode() if isinstance(a["name"], bytes) else a["name"]
                vals.append(CAPTION_WEIGHT if name == "Weight" else a["default"])
            font.set_variation_by_axes(vals)
            return font
        except Exception:
            pass
    fpath = find_regular_font()
    return ImageFont.truetype(str(fpath), size) if fpath else ImageFont.load_default(size)


def detect_bars(source: Path, offset: float, duration: float, workdir: Path,
                width: int, height: int) -> tuple[int, int, int, int] | None:
    """Find black bars baked into the video. Returns crop (w, h, x, y) or None."""
    run = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", f"{offset:.3f}", "-t", f"{min(duration, 30):.3f}",
         "-i", source.name, "-vf", "fps=2,cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"],
        cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", run.stderr)
    if not found:
        return None
    w, h, x, y = map(int, found[-1])
    # ignore tiny trims and nonsense (e.g. an all-black clip)
    if w < width * 0.4 or h < height * 0.4 or (width - w) + (height - h) < max(width, height) * 0.02:
        return None
    return w, h, x, y


def _emoji_start(cp: int, nxt: int) -> bool:
    if 0x1F000 <= cp <= 0x1FAFF:
        return True
    if 0x2300 <= cp <= 0x23FF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF:
        return True
    # text symbols (© ® ™ ↔ 1 # ...) only when the emoji form is asked for
    return nxt in (0xFE0F, 0x20E3)


def split_emoji(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_emoji, piece) runs, keeping ZWJ/skin-tone/flag sequences whole."""
    out: list[tuple[bool, str]] = []
    i, n = 0, len(text)
    while i < n:
        cp = ord(text[i])
        nxt = ord(text[i + 1]) if i + 1 < n else 0
        if not _emoji_start(cp, nxt):
            if out and not out[-1][0]:
                out[-1] = (False, out[-1][1] + text[i])
            else:
                out.append((False, text[i]))
            i += 1
            continue
        j = i + 1
        if 0x1F1E6 <= cp <= 0x1F1FF and 0x1F1E6 <= nxt <= 0x1F1FF:
            j += 1                                    # flag = two regional letters
        while j < n:
            c = ord(text[j])
            if c in (0xFE0F, 0xFE0E, 0x20E3) or 0x1F3FB <= c <= 0x1F3FF or 0xE0020 <= c <= 0xE007F:
                j += 1
            elif c == 0x200D and j + 1 < n:
                j += 2                                # joined emoji (family, profession, ...)
            else:
                break
        out.append((True, text[i:j]))
        i = j
    return out


def emoji_image(seq: str):
    """The iPhone-style picture for one emoji (cached), or None."""
    from PIL import Image
    cps = [f"{ord(c):x}" for c in seq]
    plain = [c for c in cps if c != "fe0f"]
    names = ["-".join(cps), "-".join(plain), "-".join(plain[:1] + ["fe0f"] + plain[1:])]
    EMOJI_DIR.mkdir(exist_ok=True)
    for name in dict.fromkeys(names):
        path = EMOJI_DIR / f"{name}.png"
        if not path.is_file():
            try:
                with urllib.request.urlopen(EMOJI_URL.format(name), timeout=10) as r:
                    data = r.read()
            except Exception:
                continue
            path.write_bytes(data)
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            path.unlink(missing_ok=True)
    return None


def white_layout(src_w: int, src_h: int, hook: str, workdir: Path) -> tuple[int, int, dict]:
    """Plan the white 9:16 page and draw the caption to caption.png.
    Returns (canvas_w, canvas_h, video box {w,h,x,y})."""
    W = 1080 if MAX_HEIGHT >= 1080 else 720
    H = W * 16 // 9
    margin = round(W * 0.074)
    size = round(W * 0.055)
    line_h = round(size * 1.38)
    gap = round(W * 0.035)
    top_min = round(H * 0.11)                         # keep clear of Instagram's top bar

    lines: list[list[tuple[bool, str, object]]] = []
    font = None
    if hook.strip():
        font = caption_font(size)
        e_size = round(size * 0.95)
        e_adv = e_size + round(size * 0.1)

        def word_items(word: str):
            items = []
            for is_e, piece in split_emoji(word):
                img = emoji_image(piece) if is_e else None
                items.append((True, piece, img) if img is not None else (False, piece, None))
            return items

        def width(items) -> float:
            return sum(e_adv if is_e else font.getlength(t) for is_e, t, _ in items)

        max_w = W - 2 * margin
        space = font.getlength(" ")
        cur, cur_w = [], 0.0
        for word in hook.split():
            w_items = word_items(word)
            w = width(w_items)
            if cur and cur_w + space + w > max_w:
                lines.append(cur)
                cur, cur_w = [], 0.0
            if cur:
                cur.append((False, " ", None))
                cur_w += space
            cur += w_items
            cur_w += w
        if cur:
            lines.append(cur)

    text_h = len(lines) * line_h
    # video: full width, centred; moved down / shrunk if the caption needs room
    vw, vh = W, round(W * src_h / src_w)
    need_top = top_min + text_h + gap if lines else 0
    bottom_room = round(H * 0.06)
    if vh > H - need_top - (bottom_room if lines else 0):
        vh = H - need_top - (bottom_room if lines else 0)
        vw = round(vh * src_w / src_h)
    vw, vh = vw // 2 * 2, vh // 2 * 2
    vy = max((H - vh) // 2, need_top) // 2 * 2
    vx = (W - vw) // 2 // 2 * 2

    if lines:
        from PIL import Image, ImageDraw
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        ascent = font.getmetrics()[0]
        top = vy - gap - text_h
        for i, line in enumerate(lines):
            x = float(margin)
            base = top + i * line_h + round((line_h - size) / 2) + ascent
            for is_e, piece, img in line:
                if is_e:
                    em = img.resize((e_size, e_size), Image.LANCZOS)
                    canvas.alpha_composite(em, (round(x), round(base - e_size * 0.86)))
                    x += e_adv
                else:
                    draw.text((x, base), piece, font=font, fill=(0, 0, 0, 255), anchor="ls")
                    x += font.getlength(piece)
        canvas.save(workdir / "caption.png")
    return W, H, {"w": vw, "h": vh, "x": vx, "y": vy, "caption": bool(lines)}


def build_white_filter(src: dict, W: int, H: int, box: dict, crop=None) -> str:
    chain = "[0:v]" + (f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else "")
    chain += (f"scale={box['w']}:{box['h']},setsar=1,"
             f"pad={W}:{H}:{box['x']}:{box['y']}:color=white")
    if src["fps"] > 30.5:
        chain += ",fps=30"
    if box["caption"]:
        chain += "[base];[base][1:v]overlay=0:0"
    return chain + ",format=yuv420p[v]"


def render(source: Path, offset: float, duration: float, out_path: Path, workdir: Path,
           reel, hook: str) -> None:
    """reel: True = blurred 9:16 Reel, False = original shape, "white" = white 9:16 page."""
    src = probe(source)
    hook_lines, font_size, font_name = [], 0, None
    white = reel == "white"
    if white:
        reel = False
        crop = detect_bars(source, offset, duration, workdir, src["width"], src["height"])
        vid_w, vid_h = (crop[0], crop[1]) if crop else (src["width"], src["height"])
        try:
            W, H, box = white_layout(vid_w, vid_h, hook, workdir)
        except Exception as e:
            print(f"  (Couldn't draw the text: {e}; saving without it.)")
            W, H, box = white_layout(vid_w, vid_h, "", workdir)
    elif hook.strip():
        hook_lines, font_size = wrap_hook(hook, REEL_W if reel else src["width"])
        for i, line in enumerate(hook_lines):
            (workdir / f"hook{i}.txt").write_text(line, encoding="utf-8")
        font = find_font()
        if font:
            font_name = "hookfont" + font.suffix.lower()
            shutil.copy(font, workdir / font_name)   # relative path: no filter escaping issues
        else:
            print("  (No bold font found; using ffmpeg's default font. "
                  "Drop a .ttf into the 'fonts' folder to choose one.)")

    def cmd(filter_graph: str) -> list[str]:
        c = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}", "-i", source.name]
        if white and box["caption"]:
            c += ["-i", "caption.png"]
        c += ["-filter_complex", filter_graph, "-map", "[v]"]
        if src["has_audio"]:
            c += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        c += ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", "18", "-pix_fmt", "yuv420p",
              "-profile:v", "high", "-level", "4.1", "-movflags", "+faststart"]
        if os.environ.get("YTCROP_THREADS"):        # small cloud servers: limit memory use
            c[1:1] = ["-filter_complex_threads", os.environ["YTCROP_THREADS"]]
            c += ["-threads", os.environ["YTCROP_THREADS"]]
        c.append(str(out_path.resolve()))
        return c

    graph = (build_white_filter(src, W, H, box, crop) if white
             else build_filter(src, reel, hook_lines, font_size, font_name))
    run = subprocess.run(cmd(graph),
                         cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if run.returncode != 0 and hook_lines:
        print("  (Couldn't draw the hook text with this ffmpeg build; saving without it.)")
        run = subprocess.run(cmd(build_filter(src, reel, [], 0, None)),
                             cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if run.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise UserError(f"ffmpeg failed: {run.stderr.strip()[-400:]}")


# --------------------------------------------------------------------------- #
# One clip, end to end
# --------------------------------------------------------------------------- #
def resolve_cut(cut: tuple[float, float] | None, duration: float | None) -> tuple[float, float, bool, list[str]]:
    """Apply video length rules. Returns (start, end, whole, notes)."""
    notes = []
    if cut is None:
        if not duration:
            raise CutError("This video has no known length; please enter a cut.")
        start, end, whole = 0.0, float(duration), True
    else:
        (start, end), whole = cut, False
        start, end = _check_bounds(start, end, duration, notes)
    if end - start > REELS_MAX_SECONDS:
        notes.append(f"Heads up: this clip is {_num(end - start)}s, longer than the "
                     f"{REELS_MAX_SECONDS}s Reels limit.")
    return start, end, whole, notes


def _check_bounds(start: float, end: float, duration: float | None, notes: list[str]) -> tuple[float, float]:
    if duration:
        if start >= duration:
            raise CutError(f"The cut starts at {fmt_clock(start)}, but the video is only "
                           f"{fmt_clock(duration)} long.")
        if end > duration:
            notes.append(f"End {fmt_clock(end)} is past the end of the video; "
                         f"using {fmt_clock(duration)} instead.")
            end = float(duration)
    return start, end


_name_lock = threading.Lock()


def make_clip(info: VideoInfo, start: float, end: float, whole: bool, reel: bool, hook: str,
              log=print) -> tuple[Path, float]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    name = f"{short_slug(info.title)}_{fmt_label(start)}-{fmt_label(end)}.mp4"
    with _name_lock:                     # several clips can be made at the same time
        out_path = unique_path(OUTPUT_DIR / name)
        out_path.touch()                 # reserve the name
    workdir = Path(tempfile.mkdtemp(prefix="ytcrop_"))
    try:
        log("  Downloading section..." if not whole else "  Downloading video...")
        source, offset = download_section(info, start, end, workdir, whole)
        log("  Cutting and encoding...")
        render(source, offset, end - start, out_path, workdir, reel, hook)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out_path, probe(out_path)["duration"]


# --------------------------------------------------------------------------- #
# Interactive mode
# --------------------------------------------------------------------------- #
def ask(prompt: str) -> str:
    return input(prompt).strip()


def interactive() -> None:
    print("ytcrop - cut YouTube clips for Instagram Reels. Type q to quit.\n")
    while True:
        link = ask("Paste YouTube link: ")
        if link.lower() in ("q", "quit", "exit"):
            return
        if not link:
            continue
        try:
            info = fetch_info(extract_video_id(link))
        except UserError as e:
            print(f"  {e}\n")
            continue
        dur_txt = f" ({fmt_clock(info.duration)})" if info.duration else ""
        print(f"  {info.title}{dur_txt}")

        while True:
            raw = ask("Cut (start-end): ")
            if raw.lower() in ("q", "quit"):
                return
            try:
                start, end, whole, notes = resolve_cut(parse_cut(raw), info.duration)
                break
            except CutError as e:
                print(f"  {e}")
        for n in notes:
            print(f"  {n}")

        fmt = ask("Format? [1] Original  [2] Reel 9:16  [3] White page 9:16 (default: 2): ")
        while fmt not in ("", "1", "2", "3"):
            fmt = ask("  Please type 1, 2 or 3 (Enter = 2): ")
        reel = "white" if fmt == "3" else fmt != "1"
        hook = ask("Hook text on top? (Enter to skip): ")

        try:
            out, length = make_clip(info, start, end, whole, reel, hook)
            print(f"  Saved: {out}\n  Clip length: {_num(length)}s\n")
        except UserError as e:
            print(f"  {e}\n")


# --------------------------------------------------------------------------- #
# Batch mode
# --------------------------------------------------------------------------- #
def parse_batch_line(line: str) -> tuple[str, str, str]:
    """'<link> <cut>' / '<link>\\t<cut>' / '<link> | <cut> | <hook>' -> (link, cut, hook)."""
    parts = [p.strip() for p in line.split("|")]
    first = parts[0]
    if re.search(r"\s", first):
        link, cut = first.split(None, 1)
        hook = "|".join(parts[1:]).strip()
    else:
        link = first
        cut = parts[1] if len(parts) > 1 else ""
        hook = "|".join(parts[2:]).strip()
    return link.strip(), cut.strip(), hook


def batch(path: Path, reel: bool) -> int:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as e:
        print(f"Couldn't read {path}: {e.strerror}")
        return 1
    jobs = [(n, l.strip()) for n, l in enumerate(lines, 1) if l.strip() and not l.strip().startswith("#")]
    if not jobs:
        print(f"No clips found in {path}.")
        return 1
    print(f"Batch: {len(jobs)} clip(s), format: {'Reel 9:16' if reel else 'Original'}\n")
    done, failed = [], []
    for i, (lineno, line) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] line {lineno}: {line[:90]}")
        try:
            link, cut_text, hook = parse_batch_line(line)
            info = fetch_info(extract_video_id(link))
            print(f"  {info.title}" + (f" ({fmt_clock(info.duration)})" if info.duration else ""))
            start, end, whole, notes = resolve_cut(parse_cut(cut_text), info.duration)
            for n in notes:
                print(f"  {n}")
            out, length = make_clip(info, start, end, whole, reel, hook)
            print(f"  Saved: {out} ({_num(length)}s)\n")
            done.append((lineno, out))
        except UserError as e:
            print(f"  FAILED: {e}\n")
            failed.append((lineno, str(e).splitlines()[0]))
        except Exception as e:  # keep going no matter what
            print(f"  FAILED: unexpected error: {e}\n")
            failed.append((lineno, f"unexpected error: {e}"))
    print("=" * 60)
    print(f"Summary: {len(done)} done, {len(failed)} failed")
    for lineno, out in done:
        print(f"  done    line {lineno}: {out}")
    for lineno, reason in failed:
        print(f"  failed  line {lineno}: {reason}")
    return 0 if not failed else 2


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut YouTube clips into Instagram-ready MP4s.")
    parser.add_argument("--batch", metavar="FILE", help="process a file of '<link> <cut> [| hook]' lines")
    parser.add_argument("--original", action="store_true", help="batch: keep original format instead of Reel 9:16")
    args = parser.parse_args(argv)

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if not check_dependencies():
        return 1
    try:
        if args.batch:
            return batch(Path(args.batch), reel=not args.original)
        interactive()
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
