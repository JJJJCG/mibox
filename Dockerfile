FROM python:3.12-slim

LABEL description="mibox - play local music on Xiaomi AI speakers, with DLNA renderer and Home Assistant bridge"

# ffmpeg 用于按需转码（部分型号不支持无损格式）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/conf /app/music

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    MIBOX_CONF_PATH=/app/conf \
    MIBOX_MUSIC_PATH=/app/music \
    MIBOX_HOSTNAME=192.168.31.145 \
    MIBOX_WEB_PORT=8080 \
    MIBOX_DLNA_PORT=8200

# TCP 8080: Web/API/媒体服务   TCP 8200: DLNA   UDP 1900: SSDP 组播
EXPOSE 8080 8200

VOLUME ["/app/conf", "/app/music"]

CMD ["python", "-u", "app.py"]
