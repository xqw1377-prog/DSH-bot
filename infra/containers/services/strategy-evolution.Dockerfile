FROM python:3.14-slim

WORKDIR /app
COPY packages/domain-contracts ./packages/domain-contracts
COPY services/strategy-evolution ./services/strategy-evolution

RUN pip install --no-cache-dir -e packages/domain-contracts -e services/strategy-evolution

EXPOSE 8002

CMD ["uvicorn", "strategy_evolution.main:app", "--host", "0.0.0.0", "--port", "8002"]
