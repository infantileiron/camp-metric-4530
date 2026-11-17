FROM python:3.11-slim
WORKDIR /app
COPY server.py store.py verify.py ./
RUN mkdir -p /data && chown nobody:nogroup /data
ENV RECEIVER_DB=/data/receiver.db RECEIVER_HOST=0.0.0.0 RECEIVER_PORT=8000 PYTHONDONTWRITEBYTECODE=1
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python","server.py"]
