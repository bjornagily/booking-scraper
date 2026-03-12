# Playwright's official image ships with Chromium and all system deps pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Xvfb for a virtual display (allows headless=False to avoid bot detection)
RUN apt-get update && apt-get install -y xvfb && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run headed on virtual display — looks like a real browser to Booking.com
ENV HEADLESS=false
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# Start Xvfb virtual display, then launch gunicorn
CMD ["sh", "-c", "Xvfb :99 -screen 0 1440x900x24 -ac +extension GLX +render -noreset & sleep 2 && gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --timeout 180 app:app"]
