# Playwright's official image ships with Chromium and all system deps pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HEADLESS=true
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# 1 worker — scraper is CPU/memory heavy; threading handles concurrency internally
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "180", "app:app"]
