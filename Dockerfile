FROM python:3.12-slim

WORKDIR /app

# system deps minimal
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app
COPY server.py .

# Zeabur 預設會給一個 PORT 環境變數,MCP SSE server 監聽這個 port
ENV MCP_TRANSPORT=sse
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-u", "server.py"]
