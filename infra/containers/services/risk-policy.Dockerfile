FROM python:3.14-slim

WORKDIR /app
COPY services/risk-policy ./services/risk-policy

RUN pip install --no-cache-dir -e services/risk-policy

EXPOSE 8003

CMD ["uvicorn", "risk_policy.main:app", "--host", "0.0.0.0", "--port", "8003"]
