# Clip Maker for cloud hosts (Render, Hugging Face, any Docker host)
FROM python:3.12-slim

# ffmpeg for cutting, a bold font for hook text
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-liberation ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 PORT=10000 \
    WEB_WORKERS=1 YTCROP_THREADS=2 \
    YTCROP_MAX_HEIGHT=720 YTCROP_PRESET=ultrafast
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
# deno = the JavaScript runtime yt-dlp needs for YouTube
RUN pip install --no-cache-dir --user -r requirements.txt deno

COPY --chown=user ytcrop.py web.py web_page.html login_page.html ./
COPY --chown=user fonts ./fonts

EXPOSE 10000
# Get the newest yt-dlp on each start (YouTube changes often), then run.
CMD pip install --no-cache-dir --user -q -U "yt-dlp[default]" ; exec python web.py --cloud
