#!/bin/sh
set -e

export DJANGO_BOOTSTAMP=$(date +%Y%m%d%H%M%S)

echo "Running migrations..."
python manage.py migrate

echo "Collecting static files..."
python manage.py collectstatic --noinput

# Run PING_TRAIL_CMD if set (env override), else exec CMD args
if [ -n "${PING_TRAIL_CMD}" ]; then
  echo "Running PING_TRAIL_CMD: $PING_TRAIL_CMD"
  exec /bin/sh -c "$PING_TRAIL_CMD"
else
  echo "Running $*"
  exec "$@"
fi
