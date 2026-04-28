# Image Playwright officielle Microsoft : Python + Chromium + toutes les libs système
# Garanti de marcher sur Railway, n'importe quel container Docker, etc.
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Copie requirements en premier pour tirer parti du cache Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Re-vérifie/installe chromium (l'image l'a déjà mais on s'assure de la version)
RUN playwright install chromium

# Copie le reste du code
COPY . .

# Crée les dossiers persistants si pas déjà présents (volumes Railway les écraseront)
RUN mkdir -p /app/data /app/cv_adaptes

# Port exposé (Railway l'override via $PORT)
EXPOSE 8080

# Lance gunicorn — single worker (rate limit en mémoire) — timeout 120s pour les
# générations PDF longues
CMD gunicorn jobfinder:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
