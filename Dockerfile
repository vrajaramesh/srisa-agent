FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Non-root user
RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

USER appuser

# Cloud Run injects PORT (default 8080). Shell form so ${PORT} is expanded.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
