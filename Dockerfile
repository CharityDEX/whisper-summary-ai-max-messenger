FROM python:3.10-slim

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    ffmpeg \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

# Копирование файлов зависимостей
COPY requirements.txt .

# Установка Python зависимостей
# requirements.txt is a complete pip freeze — use --no-deps to skip resolution
# (avoids conflicts between maxapi/aiogram aiohttp pins, jiter version pins, etc.)
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt --no-deps && \
    pip install --no-cache-dir maxapi==0.9.13 --no-deps && \
    pip install --no-cache-dir puremagic

# Копирование исходного кода
COPY . .

# Entrypoint: default is Telegram bot; override with max_main.py for Max bot
ARG ENTRYPOINT_SCRIPT=main.py
ENV ENTRYPOINT_SCRIPT=${ENTRYPOINT_SCRIPT}

CMD ["sh", "-c", "python ${ENTRYPOINT_SCRIPT}"]
