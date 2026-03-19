FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server.py .

# Create logs directory
RUN mkdir -p /var/log/sentinel

# Expose port (default for SSE might vary, assuming 8000 for standard FastAPI/uvicorn underneath)
EXPOSE 8000

# Run the server
CMD ["python", "server.py"]
