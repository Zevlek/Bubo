FROM python:3.12-slim

ARG INSTALL_AI_DEPS=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Paris

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libfreetype6 \
    libpng16-16 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-ai.txt ./

RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    if [ "$INSTALL_AI_DEPS" = "1" ]; then pip install -r requirements-ai.txt; fi

COPY . .

RUN mkdir -p data charts

CMD ["python", "bubo_engine.py"]
