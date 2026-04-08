FROM node:22-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src/ src/
RUN npm run build

FROM node:22-alpine

# Patch Alpine packages (busybox, zlib) and npm's picomatch CVE
RUN apk upgrade --no-cache && npm install -g npm@latest 2>/dev/null; true

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=builder /app/dist/ dist/

# Runtime data and logs directories — owned by node user
RUN mkdir -p data logs && chown -R node:node data logs

USER node
CMD ["node", "dist/index.js"]
