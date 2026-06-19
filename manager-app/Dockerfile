FROM python:3.12-slim

WORKDIR /app
COPY app.py feature_extension.py live_metrics_extension.py quota_sync.py startup.py manager-entrypoint.sh /app/
RUN chmod +x /app/manager-entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

EXPOSE 8080
CMD ["/app/manager-entrypoint.sh"]
