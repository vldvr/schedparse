FROM python:3.13.1-slim

WORKDIR /app

# Install system dependencies for PostgreSQL and image processing
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY backup_manager.py .

# Create uploads directory
RUN mkdir -p uploads/eblans uploads/lectures backups

# Expose the port the app runs on
EXPOSE 5000

# Command to run the application
CMD ["python", "app.py"]