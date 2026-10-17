FROM python:3.11-slim

WORKDIR /app

# Ensure logs output immediately without buffering
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "main.py"]
