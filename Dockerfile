ARG PYTHON_VERSION=3.14-alpine

# -----------------------------------------------------------------------------
# Stage 1: builder – install deps, collectstatic
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Create venv and install deps (no cache)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-prod.txt /tmp/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm -rf /root/.cache/

COPY app/ /app/
RUN python manage.py collectstatic --noinput

# -----------------------------------------------------------------------------
# Stage 2: final – minimal runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PATH="/opt/venv/bin:$PATH"

RUN adduser --disabled-password --gecos '' pingtrail
RUN mkdir -p /app /data

# Copy venv and app from builder (no pip cache, no build artifacts)
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

WORKDIR /app

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

RUN chown -R pingtrail:pingtrail /app /data

USER pingtrail

EXPOSE 8030

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn --no-control-socket --bind 0.0.0.0:${PORT:-8030} --workers 2 config.wsgi"]
