FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    aiosqlite \
    pydantic \
    pydantic-settings \
    loguru

COPY src/ ./src/

ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
