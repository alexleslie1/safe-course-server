FROM python:3.12-slim

# Install system dependencies (ffmpeg for video composition, fonts for Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-dejavu \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency file first for Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Railway provides PORT env var at runtime
EXPOSE 8080

# Start Gunicorn
CMD gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --timeout 600
