# PRISM: the portal and the report chats. The local model runs in the ollama service (compose.yaml).
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONIOENCODING=utf-8 OLLAMA_HOST=http://ollama:11434
# the portal, then one port per report chat
EXPOSE 8080 8101-8120
CMD ["python", "-m", "prism", "--host", "0.0.0.0", "--port", "8080"]
