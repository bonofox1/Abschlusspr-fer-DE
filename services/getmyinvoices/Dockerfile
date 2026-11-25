FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --create-home --uid 10001 mcp
WORKDIR /app
COPY --chown=mcp:mcp common /app/common
COPY --chown=mcp:mcp services/getmyinvoices /app/services/getmyinvoices
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "services.getmyinvoices.server"]

