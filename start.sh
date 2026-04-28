#!/bin/sh
# Boot wrapper : log clair de l'état du container avant de lancer gunicorn.
# Si quelque chose plante avant gunicorn, on le verra dans les logs Railway.

# Resolve PORT en utilisant python plutôt que l'expansion shell, qui peut être
# inhibée selon comment Railway exec le startCommand.
RESOLVED_PORT=$(python -c "import os; print(os.environ.get('PORT','8080'))")

echo "[boot] starting jobfinder on port ${RESOLVED_PORT}"
echo "[boot] raw \$PORT env=${PORT:-(unset)}"
echo "[boot] DATABASE_URL=$(echo $DATABASE_URL | sed 's/:[^@]*@/:****@/')"
echo "[boot] python: $(python --version)"
echo "[boot] cwd: $(pwd)"
echo "[boot] gunicorn: $(gunicorn --version)"
echo "[boot] files: $(ls /app | head -10 | tr '\n' ' ')"

exec gunicorn jobfinder:app \
  --bind "0.0.0.0:${RESOLVED_PORT}" \
  --workers 1 \
  --timeout 120 \
  --graceful-timeout 30 \
  --log-level info \
  --access-logfile - \
  --error-logfile -
