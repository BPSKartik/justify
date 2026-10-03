# Justify hosted service — web page, JSON API and public MCP endpoint in one container.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 justify

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY justify ./justify
RUN pip install --no-cache-dir ".[hosted]"

USER justify
ENV JUSTIFY_DATA_DIR=/tmp/justify-hosted \
    PYTHONUNBUFFERED=1 \
    GIT_TERMINAL_PROMPT=0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["justify", "serve", "--host", "0.0.0.0", "--port", "8000"]
