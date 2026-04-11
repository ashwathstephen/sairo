# Stage 1: Build React frontend (always amd64 — output is static JS/CSS/HTML)
FROM --platform=linux/amd64 node:20-alpine AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# Stage 2: Python backend + serve built frontend
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY --from=frontend-build /app/dist ./static
RUN mkdir -p /data && \
    addgroup --system --gid 1000 appgroup && \
    adduser --system --uid 1000 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app /data
ENV DB_DIR=/data
EXPOSE 8000
USER appuser
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
