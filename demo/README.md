# Verification image

Published by `.github/workflows/demo.yml` for linux/amd64 and linux/arm64 as
`ghcr.io/pocketcontext/pocketdeploy-demo`. It serves port 80 and `/up`, as required
by pinned ONCE. `/generation` exposes the deliberately public synthetic
`POCKETDEPLOY_TEST` value so an environment update can be verified over HTTP.
Never put secrets in that variable. Other environment variables are not exposed.

The upstream NGINX base is pinned by multi-platform digest. SIGQUIT provides a
clean NGINX shutdown for stop-first verification. ONCE supplies its normal named
storage volumes; removal must retain them unless explicitly authorized otherwise.
