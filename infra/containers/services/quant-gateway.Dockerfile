FROM python:3.14-slim

WORKDIR /app
COPY packages/domain-contracts ./packages/domain-contracts
COPY services/quant-gateway ./services/quant-gateway

RUN pip install --no-cache-dir -e packages/domain-contracts -e services/quant-gateway

EXPOSE 8001

CMD ["uvicorn", "quant_gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
