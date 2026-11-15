FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SITEMAP_DATA_DIR=/data
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sitemap2proto.py ./
COPY app ./app

RUN useradd --system --uid 10001 --home-dir /srv sitemap && mkdir /data && chown sitemap /data
USER sitemap
VOLUME /data
EXPOSE 8000

# FORWARDED_ALLOW_IPS should be the proxy's address so login rate limiting sees real client IPs.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
