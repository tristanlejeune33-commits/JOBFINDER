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

# Wrapper pour avoir un log lisible avant gunicorn (si quelque chose plante
# avant gunicorn, on saura que le container a au moins démarré)
RUN echo '#!/bin/sh\n\
echo "[boot] starting jobfinder on port ${PORT:-8080}"\n\
echo "[boot] DATABASE_URL=$(echo $DATABASE_URL | sed "s/:[^@]*@/:****@/")"\n\
echo "[boot] python: $(python --version)"\n\
exec gunicorn jobfinder:app \\\n\
  --bind "0.0.0.0:${PORT:-8080}" \\\n\
  --workers 1 \\\n\
  --timeout 120 \\\n\
  --graceful-timeout 30 \\\n\
  --log-level info \\\n\
  --access-logfile - \\\n\
  --error-logfile -' > /app/start.sh && chmod +x /app/start.sh

CMD ["/app/start.sh"]
