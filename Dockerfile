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

# Le start.sh est COPY via le COPY . . plus haut (vrai fichier dans le repo,
# évite les pièges d'echo avec dash)
RUN chmod +x /app/start.sh && head -3 /app/start.sh

CMD ["/app/start.sh"]
