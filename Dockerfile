FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p static/uploads/image static/uploads/video instance

EXPOSE 5000
CMD ["sh", "-c", "gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app"]
