# One-shot acceptance image: Python (pytest, real API) + Node (Playwright).
FROM node:20-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /accept

# Backend + pytest deps
COPY backend/requirements.txt /accept/backend/requirements.txt
COPY backend/requirements-dev.txt /accept/backend/requirements-dev.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir \
        -r /accept/backend/requirements.txt \
        -r /accept/backend/requirements-dev.txt

# Frontend deps (install Chromium for Playwright)
COPY frontend/package.json frontend/package-lock.json* /accept/frontend/
RUN cd /accept/frontend && npm ci --no-audit --no-fund \
    && npx playwright install --with-deps chromium

# Sources
COPY backend /accept/backend
COPY frontend /accept/frontend
COPY verify/run.sh /accept/run.sh
RUN chmod +x /accept/run.sh

CMD ["/accept/run.sh"]
