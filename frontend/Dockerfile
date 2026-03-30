FROM node:20-alpine AS builder

ARG USE_CN_MIRROR=true

WORKDIR /app
COPY package*.json /app/
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
      npm config set registry https://registry.npmmirror.com; \
    fi && \
    npm ci --no-audit --no-fund
COPY . /app
RUN npm run build

FROM nginx:1.27-alpine
ENV TZ=Asia/Shanghai
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
