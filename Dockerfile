FROM python:3.11-slim

WORKDIR /app

# ffmpeg：视频封面自动提取依赖它（缺了的话封面会静默回退到占位图）
RUN apt-get update && apt-get install -y gcc curl ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p static/uploads/videos static/uploads/covers static/uploads/avatars
RUN cp .env.example .env 2>/dev/null || true

EXPOSE 8000

# 从项目根目录运行，使用包路径
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
