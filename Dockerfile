FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source after requirements to avoid cache invalidation on dep changes.
# ADD a cache-busting arg so source is always fresh when needed.
ARG CACHE_BUST=1
COPY . .

CMD ["python", "main.py"]
