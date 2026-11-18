# Moon (CPQC) — image dùng chung cho web + scheduler (khác nhau ở command trong compose)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Ho_Chi_Minh

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /home/admin1/cpqc

COPY requirements.txt requirements.runtime.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -r requirements.runtime.txt || true

COPY . .

EXPOSE 5060
CMD ["gunicorn", "--bind", "0.0.0.0:5060", "--workers", "1", "--worker-class", "gthread", "--threads", "4", "--timeout", "120", "web_app:app"]
