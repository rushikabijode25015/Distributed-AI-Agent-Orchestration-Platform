FROM python:3.11-slim

# Set build-time settings
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Create shared data folder for sandbox outputs
RUN mkdir -p /app/shared && chmod 777 /app/shared

# Copy the rest of the application code
COPY . /app/

EXPOSE 8000
