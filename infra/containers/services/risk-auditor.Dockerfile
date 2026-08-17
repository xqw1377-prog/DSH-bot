FROM python:3.14-slim

WORKDIR /app
COPY packages/domain-contracts ./packages/domain-contracts
COPY packages/dsh-runtime ./packages/dsh-runtime
COPY plugins/dsh-risk-auditor ./plugins/dsh-risk-auditor

RUN pip install --no-cache-dir \
    -e packages/domain-contracts \
    -e packages/dsh-runtime \
    -e plugins/dsh-risk-auditor

EXPOSE 8005

CMD ["uvicorn", "dsh_risk_auditor.service:app", "--host", "0.0.0.0", "--port", "8005"]
