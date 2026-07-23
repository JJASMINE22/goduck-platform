# syntax=docker/dockerfile:1
# 前端：多阶段构建 —— node 编译 → nginx 提供静态服务（含 SPA 回退）。
# API 基址由 .env.production 的 VITE_API_BASE=/api 在构建期注入，
# 与边缘 nginx 的 /api 剥前缀转发配合（同源，无 CORS）。

FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.default.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
