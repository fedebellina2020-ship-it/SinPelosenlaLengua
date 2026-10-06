FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV DB_PATH=/data/data.db PORT=8000
VOLUME /data
CMD ["python","server.py"]
