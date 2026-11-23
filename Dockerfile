FROM python:3.12-slim

WORKDIR /app

COPY bot.py admin.py core.py store.py tgapi.py /app/
RUN useradd --system --uid 10001 --home /app taxi \
    && mkdir -p /data \
    && chown -R taxi:taxi /app /data

USER taxi
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "/app/bot.py"]
