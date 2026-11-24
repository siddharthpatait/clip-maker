# Clip Maker

Cut a part of a YouTube video into an Instagram-ready MP4 (Reel 9:16 or original).
Web page with email + password sign-in.

Environment variables:

- `ALLOWED_EMAILS` (required): allowed emails, separated by commas
- `ACCESS_PASSWORD` (required): the shared password
- `WEB_WORKERS` (default 1 here): clips made at the same time
- `YTCROP_THREADS` (default 2 here): ffmpeg threads, keeps memory low
- `YTCROP_COOKIES` / `YTCROP_PROXY` (optional): only if YouTube blocks the server
