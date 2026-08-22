FROM python:3.12-slim

WORKDIR /app

# Install deps first so code changes do not bust the layer cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY tools/ ./tools/
COPY run.py config.json ./

# watchlist.json and state/ are mounted as volumes so you can edit the
# watchlist without rebuilding, and state survives container restarts.
VOLUME ["/app/state", "/app/cache"]

ENV PYTHONUNBUFFERED=1
CMD ["python", "run.py"]
