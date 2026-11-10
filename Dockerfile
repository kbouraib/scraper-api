# Image tag MUST match the playwright version in requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_BREAK_SYSTEM_PACKAGES=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py scraper.py ./

USER pwuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# One worker: the in-memory cache and browser are per-process
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]