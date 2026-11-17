FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENTCHAOS_DATA_DIR=/app/data AGENTCHAOS_WEB_DIST=/app/web/dist

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=web /web/dist ./web/dist

EXPOSE 8000
CMD ["uvicorn", "agentchaos.api:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
