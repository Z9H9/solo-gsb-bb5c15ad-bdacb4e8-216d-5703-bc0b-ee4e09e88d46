FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/parser.db \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parser.py db.py schemas.py main.py ./

# 数据卷（SQLite 文件），使用命名卷避免宿主权限问题
RUN mkdir -p /data && useradd -r -u 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser
VOLUME ["/data"]

EXPOSE 8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
