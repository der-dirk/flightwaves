# Entwicklungsumgebung: Python 3.11 wie auf Raspberry Pi OS (Bookworm).
# Der Anwendungscode selbst braucht nichts hiervon – nur Tests und Linter.
FROM python:3.11-slim
RUN pip install --no-cache-dir pytest ruff
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
