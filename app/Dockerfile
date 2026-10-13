FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p media staticfiles && DJANGO_SECRET_KEY=build python manage.py collectstatic --noinput

RUN useradd -m crm && chown -R crm /app
USER crm
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000", "-k", "gthread", "-w", "4", "--threads", "8", "--timeout", "60"]
