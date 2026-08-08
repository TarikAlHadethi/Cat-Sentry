FROM python:3.12-slim

# Run as a non-root user. Combined with read_only + cap_drop in compose,
# a compromise of this container has very little to work with.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin sentry

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt || \
    pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ ./templates/

RUN mkdir -p /state && chown -R sentry:sentry /app /state
USER sentry

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "app.py"]
