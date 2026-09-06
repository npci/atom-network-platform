FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Deployment-supplied UI labels. LABEL_PROFILE names a JSON file in the build
# context (e.g. labels.mydomain.json); unset -> the ecosystem-neutral defaults in
# src/strings.js, which is what the public core ships. Read from a FILE rather
# than passed as a JSON string because the profile outgrew what a YAML scalar
# can hold unescaped -- one placeholder contains ": ", which silently ends a
# plain scalar. Build-time because Vite inlines import.meta.env, and because
# runtime labels would flash the default before swapping.
ARG LABEL_PROFILE=""
RUN if [ -n "$LABEL_PROFILE" ]; then \
      [ -f "$LABEL_PROFILE" ] || { echo "LABEL_PROFILE $LABEL_PROFILE not found"; exit 1; }; \
      VITE_LABEL_OVERRIDES="$(cat "$LABEL_PROFILE")" npm run build; \
    else npm run build; fi

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
# This checkout carries a default POSIX ACL with `other::---`, so git writes
# source files without the world-read bit and COPY preserves it all the way
# into the image — nginx runs as `nginx`, not root, and 403s on its own assets.
RUN chmod -R a+rX /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/conf.d/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/docker-entrypoint.sh"]
