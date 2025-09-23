FROM python:3.13.1-slim

WORKDIR /app

# Install system dependencies for PostgreSQL, image processing, and CIFS mounting
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    cifs-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY backup_manager.py .

# Create uploads directory and NAS mount point
RUN mkdir -p uploads/eblans uploads/lectures backups /mnt/nas_backup

# Expose the port the app runs on
EXPOSE 5000

# Command to run the application
CMD ["python", "app.py"]