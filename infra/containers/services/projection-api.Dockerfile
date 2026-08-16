FROM python:3.14-slim

WORKDIR /app
COPY services/projection-api ./services/projection-api

RUN pip install --no-cache-dir -e services/projection-api

EXPOSE 8004

CMD ["uvicorn", "projection_api.main:app", "--host", "0.0.0.0", "--port", "8004"]
