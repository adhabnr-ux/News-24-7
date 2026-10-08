FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WEB_HOST=0.0.0.0 DATA_DIR=/data/data TZ=America/New_York
WORKDIR /app

COPY pyproject.toml README.md ./
COPY news247 ./news247
RUN pip install . && useradd --create-home --uid 1000 news247 && mkdir -p /data/data && chown -R news247 /data

USER news247
WORKDIR /data
EXPOSE 8247
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8247'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status == 200 else 1)"

# Configure with environment variables (IMESSAGE_TO, SENDBLUE_*, ...); optionally mount
# /data/config.yaml for more control and use: news247 -c /data/config.yaml run
ENTRYPOINT ["news247"]
CMD ["run"]
