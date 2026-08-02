# A container somebody can run without installing Python, and without giving it
# more than it needs.
#
# Two stages. The builder compiles wheels; the runtime carries none of the build
# toolchain, which is both smaller and one less thing to keep patched.
#
# **It does not run as root.** A forensics tool reads untrusted data all day ---
# label files from strangers, JSON from explorers, HTML it renders. Running that
# as uid 0 in a container that mounts your case directory is a bad trade for
# nothing.
#
# **There is no network in the image's own tests.** The suite blocks outbound
# sockets at the fixture level, so `docker build` proves the build works rather
# than proving the internet was up.

FROM python:3.12-slim AS builder

# gcc for the few wheels without a musl/manylinux build on every arch. Dropped
# entirely in the runtime stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip build \
    && pip wheel --no-cache-dir --wheel-dir /wheels ".[all]"


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="chainscope" \
      org.opencontainers.image.description="Blockchain forensics with provenance in the type system" \
      org.opencontainers.image.source="https://github.com/ZzyzxLabs/chainscope" \
      org.opencontainers.image.licenses="MIT"

# git is genuinely used --- case bundles are versioned with it, and `doctor`
# reports its absence for that reason.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels chainscope[all] \
    && rm -rf /wheels

# A non-root user with a stable uid, so a bind-mounted case directory has
# predictable ownership on the host.
RUN useradd --create-home --uid 1000 analyst
USER analyst
WORKDIR /case

# Where a bind mount is expected to land. Declared so `docker run` without one
# still works --- on an anonymous volume rather than failing.
VOLUME ["/case"]

ENV CHAINSCOPE_CACHE_DIR=/case/.chainscope/cache \
    PYTHONUNBUFFERED=1

# `doctor` exits non-zero when ADDRESS_HISTORY is unreachable, which is exactly
# the condition that makes this container useless, so it is the right check.
HEALTHCHECK --interval=1m --timeout=15s --start-period=5s --retries=2 \
    CMD ["chainscope", "doctor"]

ENTRYPOINT ["chainscope"]
CMD ["doctor"]
