FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy application code
COPY server.py .

# Create logs directory
RUN mkdir -p /var/log/sentinel

# Expose port (default for SSE might vary, assuming 8000 for standard FastAPI/uvicorn underneath)
EXPOSE 8000

# Run the server
CMD ["uv", "run", "python", "server.py"]
