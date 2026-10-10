FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Il database vive in /data: su Railway/Render collega qui un volume persistente
RUN mkdir -p /data
ENV DATABASE_PATH=/data/leads.db
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
