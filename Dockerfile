# Usa uma imagem oficial do Python super leve e rápida
FROM python:3.10-slim

# Define a pasta de trabalho lá dentro
WORKDIR /app

# Instala as ferramentas do sistema necessárias para o PostgreSQL e o Pandas rodarem lisos
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Copia as bibliotecas e instala tudo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do teu código
COPY . .

# Garante que sempre exista um número de porta válido
ENV PORT=5000

# O PULO DO GATO: Timeout aumentado de 120 para 600 segundos (10 minutos)!
CMD gunicorn app:app -b 0.0.0.0:$PORT --timeout 600 --workers 1 --preload
