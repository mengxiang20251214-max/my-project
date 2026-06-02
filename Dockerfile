FROM python:3.11-slim

WORKDIR /app

# ffmpeg：视频封面自动提取依赖它；postgresql-client：数据库备份用的 pg_dump
# （若服务端 PG 版本更高导致 pg_dump 版本不匹配，备份脚本会自动回退到逻辑导出）
RUN apt-get update && apt-get install -y gcc curl ffmpeg postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p static/uploads/videos static/uploads/covers static/uploads/avatars
RUN cp .env.example .env 2>/dev/null || true

EXPOSE 8000

# 从项目根目录运行，使用包路径
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
