FROM python:3.14-slim

WORKDIR /app
COPY packages/domain-contracts ./packages/domain-contracts
COPY services/quant-gateway ./services/quant-gateway

RUN pip install --no-cache-dir -e packages/domain-contracts -e services/quant-gateway

EXPOSE 8001

# 非特权用户运行（写入仅限 /data 卷）
RUN useradd --system --no-create-home --uid 10001 dsh
USER dsh

# 探针:容器编排依赖 /healthz 判定存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "quant_gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
