FROM python:3.11-slim

WORKDIR /app

# Abhängigkeiten zuerst – schnelleres Caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot-Code
COPY . .

# Logs sofort durchreichen
ENV PYTHONUNBUFFERED=1

# Startkommando (Token kommt per ENV, nicht aus .env)
CMD ["python", "bot.py"]
