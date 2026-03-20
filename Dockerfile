FROM python:3.11-slim

# Install Firefox/browser system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libx11-6 libxrandr2 libxcomposite1 libxcursor1 libxdamage1 \
    libxext6 libxi6 libxtst6 libnss3 libnspr4 libdbus-1-3 \
    libexpat1 libcups2 libdrm2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 libasound2 \
    libglib2.0-0 libgdk-pixbuf-xlib-2.0-0 libxfixes3 libxrender1 \
    libxinerama1 libxkbcommon0 wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Camoufox Firefox binary
RUN python -m camoufox fetch

COPY server.py .

ENV CAMOUFOX_PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
