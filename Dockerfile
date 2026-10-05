FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-noto-core fonts-noto-extra ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml /app/
COPY reelbot /app/reelbot
RUN pip install --no-cache-dir .
RUN useradd --system --uid 10001 --create-home reelbot && mkdir /data && chown reelbot:reelbot /data
USER reelbot
CMD ["reelbot"]

