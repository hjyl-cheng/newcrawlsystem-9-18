FROM node:24.21.0-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json ./
RUN npm ci
COPY services ./services
RUN npm run build

FROM build AS production-dependencies
RUN npm prune --omit=dev

FROM node:24.21.0-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
ARG REVISION
ARG VERSION
LABEL org.opencontainers.image.source="https://github.com/hjyl-cheng/newcrawlsystem-9-18" \
      org.opencontainers.image.revision=$REVISION
ENV NODE_ENV=production APP_REVISION=$REVISION APP_VERSION=$VERSION PORT=8080
WORKDIR /app
COPY --from=build --chown=node:node /app/dist ./dist
COPY --from=production-dependencies --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/package.json ./package.json
RUN node --input-type=module -e "import pg from 'pg'; import sdk from '@confluentinc/kafka-javascript'; if (!pg.Pool || !sdk.KafkaJS.Kafka) process.exit(1)"
USER node
EXPOSE 8080
CMD ["node", "dist/services/runtime-smoke/src/main.js"]
