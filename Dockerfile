FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/app
WORKDIR ${APP_HOME}

# Install ffmpeg and remove package manager caches for a smaller image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy only needed files/directories to keep the image minimal.
# Copy JSON files (settings and profiles), templates directory, the main app, and requirements.
COPY *.json ./
COPY templates/ ./templates/
COPY igor.py ./igor.py
COPY requirements.txt ./

EXPOSE 5000

CMD ["python", "igor.py"]
