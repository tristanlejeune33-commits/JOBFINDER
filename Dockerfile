# Image Playwright officielle Microsoft : Python + Chromium + toutes les libs système
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Default PORT (Railway override via $PORT)
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

# Copie requirements en premier pour tirer parti du cache Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Re-vérifie chromium (l'image l'a déjà mais on s'assure)
RUN playwright install chromium

# Copie le reste du code
COPY . .

# Crée les dossiers persistants
RUN mkdir -p /app/data /app/cv_adaptes

# Test que le module Python s'importe correctement (échec ici = build fail visible)
RUN SECRET_KEY=test python -c "import jobfinder; print('jobfinder import OK')"

EXPOSE 8080

# Form shell pour permettre l'expansion de $PORT à runtime
# --preload : import l'app dans le master AVANT de fork les workers (erreurs visibles)
# --log-level=info : pour voir le démarrage dans les logs
CMD gunicorn jobfinder:app \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --timeout 120 \
    --preload \
    --log-level info \
    --access-logfile - \
    --error-logfile -
