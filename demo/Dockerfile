# Multi-platform upstream digest; update deliberately with registry verification.
FROM nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
COPY default.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY --chmod=0755 40-demo-generation.sh /docker-entrypoint.d/40-demo-generation.sh
STOPSIGNAL SIGQUIT
HEALTHCHECK --interval=15s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1/up || exit 1
