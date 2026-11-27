FROM node:24-bookworm-slim AS engine

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY install-linux.sh ./
RUN bash install-linux.sh --engine-only \
    && rm -rf app/.git app/src app/gui app/tampermonkey app/scripts

FROM node:24-bookworm-slim
ENV REWARDS_CONTAINER=1 REWARDS_DATA_DIR=/opt/rewards/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/rewards/browser TZ=Asia/Shanghai

WORKDIR /opt/rewards
COPY --from=engine /build/app ./app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 tzdata xvfb x11vnc \
    && node app/node_modules/patchright/cli.js install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o node -g node -m 0700 data \
    && install -o node -g node -m 0600 /dev/null data/.keep \
    && ln -s ../data/engine-logs app/logs \
    && ln -s ../data/diagnostics app/diagnostics \
    && ln -s data/panel-logs logs \
    && chmod -R a+rX browser
COPY VERSION LICENSE THIRD-PARTY-NOTICES.md launch-linux.sh *.py *.js *.cjs ./
COPY panel ./panel
COPY assets ./assets

USER node
EXPOSE 8765 5900
CMD ["bash", "launch-linux.sh"]
