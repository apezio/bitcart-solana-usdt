# The stock backend-plugin image build needs bitcart/bitcart:stable, which a pinned
# BITCART_VERSION deployment never has, so the coin registration module is mounted instead.
# Only our package is mounted, read-only, next to the image's own /app/modules/__init__.py
# (same layout the stock backend-plugins.Dockerfile produces with COPY plugins/backend modules).
MODULES_MOUNT = "./plugins/docker/solana/backend/forked:/app/modules/forked:ro"


def rule(services, settings):
    for name in ("backend", "worker"):
        if name in services:
            services[name].setdefault("volumes", []).append(MODULES_MOUNT)
