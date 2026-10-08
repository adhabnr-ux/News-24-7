FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY news247 ./news247
RUN pip install . && useradd --create-home --uid 1000 news247 && mkdir -p /data/data && chown -R news247 /data

USER news247
WORKDIR /data
EXPOSE 8247
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8247/health', timeout=4).status == 200 else 1)"

# Mount your config at /data/config.yaml (and optionally /data/.env)
ENTRYPOINT ["news247"]
CMD ["run", "--host", "0.0.0.0"]
