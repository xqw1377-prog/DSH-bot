FROM python:3.14-slim

WORKDIR /app
COPY services/intelligence-ingest ./services/intelligence-ingest

RUN pip install --no-cache-dir -e services/intelligence-ingest

# 非特权用户运行;情报库与快照挂 /data 卷
RUN useradd --system --no-create-home --uid 10001 dsh
USER dsh

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8007/healthz', timeout=3).status == 200 else 1)"

EXPOSE 8007

CMD ["uvicorn", "intelligence_ingest.main:app", "--host", "0.0.0.0", "--port", "8007"]
