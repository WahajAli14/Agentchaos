FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENTCHAOS_DATA_DIR=/app/data

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "agentchaos.services.asgi:orders", "--factory", "--host", "0.0.0.0", "--port", "8000"]
