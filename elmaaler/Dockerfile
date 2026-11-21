FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd -u 1000 -m elmaaler && mkdir -p /data && chown elmaaler /data
COPY app ./app
USER elmaaler
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/sundhed', timeout=4)" || exit 1
CMD ["python", "-m", "app.main"]
