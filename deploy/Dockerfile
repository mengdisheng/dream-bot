# A verified official base archive can be docker-loaded when the registry is
# unreachable from the deployment host. The resolved digest is in the receipt.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE} AS runtime
ARG DEBIAN_MIRROR=https://deb.debian.org
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browser \
    PERSONAL_BOT_GATEWAY_PUBLIC_KEY=/run/keys/gateway-public.pem \
    PERSONAL_BOT_FILES_ROOT=/data/files PERSONAL_BOT_OUTPUT_ROOT=/scratch/outputs \
    PERSONAL_BOT_HOST_BRIDGE=/run/personal-bot/host/bridge.sock
RUN python -c "from pathlib import Path; import os; p=Path('/etc/apt/sources.list.d/debian.sources'); s=p.read_text(); mirror=os.environ['DEBIAN_MIRROR']; assert mirror.startswith('https://'); p.write_text(s.replace('http://deb.debian.org',mirror))" \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get install -y --no-install-recommends \
    xvfb x11vnc s6 util-linux ca-certificates fonts-noto-cjk fonts-liberation \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libx11-6 libxcb1 libxext6 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 31000 personal-bot-ipc \
    && useradd -u 31001 -g 31000 -M -s /usr/sbin/nologin bot-api \
    && useradd -u 31002 -g 31000 -M -s /usr/sbin/nologin bot-worker \
    && useradd -u 31003 -g 31000 -M -s /usr/sbin/nologin bot-browser \
    && groupadd -g 31004 bot-executor \
    && useradd -u 31004 -g 31004 -M -s /usr/sbin/nologin bot-executor
WORKDIR /opt/personal-bot/runtime
COPY runtime/pyproject.toml ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -c "import tomllib,subprocess,sys; p=tomllib.load(open('pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir',*p['dependencies'],*p['optional-dependencies']['test']])"
RUN python -m playwright install chromium --no-shell \
    && chmod -R a+rX /opt/browser
COPY runtime/personal_bot ./personal_bot
COPY runtime/tests ./tests
COPY deploy/container-init.py /opt/personal-bot/container-init.py
COPY deploy/services /opt/personal-bot/services
RUN chmod -R a+rX /opt/personal-bot/services \
    && chmod 0555 /opt/personal-bot/services/*/run /opt/personal-bot/services/finish
EXPOSE 8091
ENTRYPOINT ["python", "/opt/personal-bot/container-init.py"]

FROM ${BASE_IMAGE} AS sqlite-build
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN python -c "from pathlib import Path; import os; p=Path('/etc/apt/sources.list.d/debian.sources'); s=p.read_text(); mirror=os.environ['DEBIAN_MIRROR']; assert mirror.startswith('https://'); p.write_text(s.replace('http://deb.debian.org',mirror))" \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get install -y --no-install-recommends gcc libc6-dev ca-certificates
COPY deploy/build-sqlite.py /tmp/build-sqlite.py
RUN python /tmp/build-sqlite.py

FROM runtime AS final
COPY --from=sqlite-build /opt/sqlite/lib/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
COPY --from=sqlite-build /opt/sqlite/lib/SOURCE.txt /opt/personal-bot/sqlite-source.txt
COPY deploy/sqlite_override.py /usr/local/lib/python3.12/site-packages/sitecustomize.py
RUN ldconfig \
    && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3,51,3), sqlite3.sqlite_version; db=sqlite3.connect(':memory:'); db.execute('CREATE VIRTUAL TABLE proof USING fts5(text)')" \
    && python -m compileall -q personal_bot
