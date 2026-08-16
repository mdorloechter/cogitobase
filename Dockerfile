FROM python:3.12-slim
# Kept in step with config.__version__ by a test — an image whose label lies about
# its version is worse than one carrying no label.
LABEL org.opencontainers.image.title="cogitobase" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/mdorloechter/cogitobase"
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser
CMD ["python", "server.py"]
