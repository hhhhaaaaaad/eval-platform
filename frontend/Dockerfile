# 评测平台前端镜像（React + Vite 构建，nginx 托管静态产物）
#
# 构建阶段：npm ci 依据 package-lock.json 精确安装（不用 npm install，避免 lock 漂移），
# tsc -b 做类型检查，vite build 产出 dist/。
FROM node:20-alpine AS build

WORKDIR /app

# 先拷依赖清单再 npm ci：只要 package*.json 不变，改源码不会重装 node_modules。
COPY package.json package-lock.json ./
RUN npm ci

# 源码（node_modules / dist 由 .dockerignore 排除）
COPY . .

RUN npm run build

# 运行阶段：nginx 托管 dist，SPA fallback 与 /api 反代见 nginx.conf。
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
