# =============================================================================
# smart-erp/web —— 前端静态产物（Vite 构建 → nginx 1.27-alpine 运行）
# -----------------------------------------------------------------------------
# 依据 §1A.2：node:22-alpine 构建 → nginx:1.27-alpine 运行；只拷 dist/ 与 nginx 配置；
# 目标 < 60MB。子路径部署 base=/demo/smart-erp/（§1A.4 路由）。
#
# 构建（context 必须是仓库根）：
#   docker build -f projects/smart-ERP/docker/web.Dockerfile \
#                --build-arg ERP_WEB_BASE=/demo/smart-erp/ \
#                -t smart-erp/web:$(git rev-parse HEAD) .
#
# 未实测说明：仓库尚无 web/package.json 与 web 源码，构建需等 S6 落地；
#   本文件保证「镜像结构、非 root、healthcheck 路径 /healthz」三件事是确定的。
# =============================================================================
ARG NODE_IMAGE=node:22-alpine
ARG NGINX_IMAGE=nginx:1.27-alpine

# ---- 阶段 1：前端构建 ----
FROM ${NODE_IMAGE} AS build

WORKDIR /src
# 先拷锁文件，依赖不变时不重跑 npm ci
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./

# 子路径部署：与香港反代 /demo/smart-erp/ 对齐；本机预览可传 ./
ARG ERP_WEB_BASE=/demo/smart-erp/
ENV ERP_WEB_BASE=${ERP_WEB_BASE}
RUN npm run build

# ---- 阶段 2：运行（非 root，8080 非特权端口）----
FROM ${NGINX_IMAGE} AS runtime

ENV TZ=Asia/Shanghai

COPY --from=build /src/dist /usr/share/nginx/html
COPY projects/smart-ERP/docker/web-nginx-main.conf /etc/nginx/nginx.conf
COPY projects/smart-ERP/docker/web-nginx.conf /etc/nginx/conf.d/default.conf

# nginx 默认以 root 起 master；这里改为 10001：pid 与各类临时目录都落到 /tmp，
# 因此无需对 /var/cache/nginx 提权。
RUN mkdir -p /tmp/nginx \
 && chown -R 10001:10001 /usr/share/nginx/html /tmp/nginx /etc/nginx/conf.d

USER 10001

EXPOSE 8080

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]
