FROM python:3.12-slim

WORKDIR /app

# Зависимости ставим отдельным слоем — кэшируются при пересборке кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Папка для SQLite (монтируется как volume)
RUN mkdir -p /app/data
ENV DATA_DIR=/app/data

CMD ["python", "bot.py"]
