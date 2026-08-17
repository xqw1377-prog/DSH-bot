FROM python:3.12-slim
WORKDIR /app
COPY packages/domain-contracts /app/packages/domain-contracts
COPY services/risk-auditor /app/services/risk-auditor
RUN pip install --no-cache-dir -e /app/packages/domain-contracts -e /app/services/risk-auditor
EXPOSE 8005
CMD ["uvicorn", "risk_auditor.main:app", "--host", "0.0.0.0", "--port", "8005"]
