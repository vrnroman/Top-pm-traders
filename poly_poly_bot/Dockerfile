FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m botuser
USER botuser

RUN mkdir -p data cache results logs

CMD ["python", "main.py"]
