FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir flask flask-cors pywebview numpy pandas requests yfinance python-dotenv cryptography fpdf2 alpaca-trade-api

EXPOSE 5050

CMD ["python", "app.py"]
