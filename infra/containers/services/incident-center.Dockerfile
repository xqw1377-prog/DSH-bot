FROM python:3.12-slim
WORKDIR /app
COPY packages/domain-contracts /app/packages/domain-contracts
COPY services/incident-center /app/services/incident-center
RUN pip install --no-cache-dir -e /app/packages/domain-contracts -e /app/services/incident-center
EXPOSE 8006
CMD ["uvicorn", "incident_center.main:app", "--host", "0.0.0.0", "--port", "8006"]
