#!/bin/bash

# Deploy script for schedparse application

echo "🚀 Starting deployment of schedparse application..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ .env file not found! Please copy .env.example to .env and configure it."
    echo "Run: cp .env.example .env"
    exit 1
fi

# Check if Docker and Docker Compose are installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker first."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose is not installed. Please install Docker Compose first."
    exit 1
fi

# Create uploads directory structure
echo "📁 Creating uploads directory structure..."
mkdir -p uploads/eblans
mkdir -p uploads/lectures

# Stop existing containers
echo "🛑 Stopping existing containers..."
docker-compose down

# Build and start containers
echo "🔨 Building and starting containers..."
docker-compose up -d --build

# Wait for services to be ready
echo "⏳ Waiting for services to start..."
sleep 10

# Check if services are healthy
echo "🔍 Checking service health..."

# Check app health
if curl -f http://localhost:3434/api/cacheStats > /dev/null 2>&1; then
    echo "✅ Application is healthy"
else
    echo "❌ Application health check failed"
    docker-compose logs schedparse
fi

# Check database
if docker-compose exec -T postgres pg_isready -U schedparse_user -d schedparse_db > /dev/null 2>&1; then
    echo "✅ Database is healthy"
else
    echo "❌ Database health check failed"
    docker-compose logs postgres
fi

# Check Redis
if docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; then
    echo "✅ Redis is healthy"
else
    echo "❌ Redis health check failed"
    docker-compose logs redis
fi

echo ""
echo "🎉 Deployment completed!"
echo "📍 Application is available at: http://localhost:3434"
echo "📊 Cache stats: http://localhost:3434/api/cacheStats"
echo ""
echo "📝 Useful commands:"
echo "  View logs: docker-compose logs -f"
echo "  Stop app: docker-compose down"
echo "  Restart: docker-compose restart"
echo "  Clean data: docker-compose down -v"