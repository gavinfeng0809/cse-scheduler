FROM node:22-bookworm-slim AS build

WORKDIR /app

COPY package.json ./

# 服务器网络环境限制，使用国内 npm 镜像
RUN npm config set registry https://registry.npmmirror.com && npm install

COPY . .

RUN npm run build


FROM docker.m.daocloud.io/library/nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
