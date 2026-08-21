FROM python:3.14-slim

WORKDIR /app
COPY packages/domain-contracts /app/packages/domain-contracts
COPY services/risk-policy ./services/risk-policy

RUN pip install --no-cache-dir -e /app/packages/domain-contracts -e services/risk-policy

EXPOSE 8003

CMD ["uvicorn", "risk_policy.main:app", "--host", "0.0.0.0", "--port", "8003"]
