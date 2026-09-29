FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY README.md ARCHITECTURE.md DATA-MODEL.md JUDGING.md LICENSE ./
COPY fixtures.json run.py ./
RUN mkdir -p /app/data

ENV PORT=8000
ENV DB_PATH=/app/data/hackathon.db
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]