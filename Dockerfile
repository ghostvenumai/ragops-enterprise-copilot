# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder
WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip==26.2 setuptools==83.0.0 wheel==0.47.0 \
    && /opt/venv/bin/pip install . -c constraints.txt

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAGOPS_LLM_PROVIDER=deterministic
WORKDIR /app
RUN groupadd --system appuser \
    && useradd --system --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser
COPY --from=builder /opt/venv /opt/venv
COPY apps ./apps
COPY src ./src
COPY data ./data
COPY scripts ./scripts
COPY README.md SPEC.md AGENTS.md ./
RUN mkdir -p /app/evidence /tmp/ragops \
    && chown -R appuser:appuser /app /tmp/ragops
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
