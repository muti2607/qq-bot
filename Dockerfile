FROM python:3.11-slim

# ---- 国内加速：默认走阿里云 PyPI 镜像，可用 --build-arg 覆盖 ----
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY . .

RUN chmod +x start.sh

EXPOSE 8080 8001

CMD ["sh", "start.sh"]
