FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 5015
CMD ["bitrix-mcp", "--http", "--host", "0.0.0.0", "--port", "5015"]
