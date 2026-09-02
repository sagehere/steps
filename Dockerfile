FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/sagehere/steps" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app app && mkdir -p /data && chown app:app /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["python", "app.py"]
