FROM python:3.12-slim

# Dependencies first, so editing the exporter does not invalidate this layer.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ollama_exporter.py .

# Run as a non-root user: the exporter only needs outbound HTTP.
RUN useradd --create-home --uid 10001 exporter
USER 10001

EXPOSE 9105

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9105/healthz')"

CMD ["python", "-u", "ollama_exporter.py"]
