# Acceptance environment: one image, every stack the tasks may use.
# Every acceptance run happens here with networking disabled (docker run --network none).
#
# Candidates are expected to mirror this image exactly for their own local test
# runs, so a local pass predicts an acceptance pass. Before distributing, replace
# the tag with a digest pin:
#   docker pull ubuntu:24.04 && docker inspect --format '{{index .RepoDigests 0}}' ubuntu:24.04
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Toolchains from the Ubuntu 24.04 archive (versions are fixed by the base image):
#   python3 3.12, nodejs 18, golang-go 1.22, rustc/cargo 1.75, openjdk 21, gcc/g++ 13, git 2.43
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash coreutils ca-certificates git make build-essential \
      python3 \
      nodejs npm \
      golang-go \
      rustc cargo \
      openjdk-21-jdk-headless \
 && npm install -g typescript@5.6.3 \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/* \
 && for t in bash git make gcc g++ python3 node npm tsc go rustc cargo javac java; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done \
 && git --version && python3 --version && node --version && go version && rustc --version && javac -version

# Tests run as an unprivileged user with the repository mounted at /work.
# Build caches (Go, Cargo, npm) are pointed at /tmp, which the harness mounts as a writable tmpfs.
ENV HOME=/tmp \
    GOCACHE=/tmp/gocache \
    GOPATH=/tmp/gopath \
    GOFLAGS=-mod=mod \
    CARGO_HOME=/tmp/cargo \
    npm_config_cache=/tmp/npm
WORKDIR /work
USER 65534:65534

CMD ["bash"]
