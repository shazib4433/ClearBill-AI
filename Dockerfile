FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Seed Read-Only Mock HIS DB and clean Audit DB with immutability triggers,
# and ensure non-root runtime users can write SQLite WAL/SHM files in /app/database
RUN python database/seed_his_db.py --reset-audit && chmod -R 777 /app/database

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
