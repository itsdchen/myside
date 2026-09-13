#!/usr/bin/env bash

# Instructions:
# Add your user to the docker group so you can run docker without sudo:
# sudo usermod -aG docker <user>
# Log out and back in for it to take effect, or run this:
# newgrp docker
# Then run noblebuild like you would compileit:
# ./noblebuild.sh -c pktrade
# Note: building with sudo works but will not generate the git commit and branch in version.h
# When you scp over, please remember to scp the noblebuilt version in noble.bin/
#
# Mirror of jammybuild.sh — but builds for Ubuntu 24.04 (noble). Use this when
# your dev box is 22.04 (jammy) and you need to deploy to a 24.04 host like n1.

set -euo pipefail

IMAGE=pktrade-noble-build:latest

# Build the Ubuntu 24.04 image (Conan 1.59.0 inside)
docker build -f Dockerfile.noble -t "$IMAGE" .

# Forward ALL args to compileit.sh; detect -d / -c without removing them
FORWARD_ARGS=( "$@" )
BUILD_TYPE=Release
CLEAN_CACHE=false
for arg in "${FORWARD_ARGS[@]}"; do
  [[ "$arg" == "-d" ]] && BUILD_TYPE=Debug
  [[ "$arg" == "-c" ]] && CLEAN_CACHE=true
done

UIDGID="$(id -u):$(id -g)"
mkdir -p "$HOME/.conan" "$HOME/.ccache"

# Quote-forward args for bash -lc. Pass -n so compileit.sh routes binaries to
# noble.bin/ (and noble.bin.debug/ for debug).
forward_escaped=" -n $(printf " %q" "${FORWARD_ARGS[@]}") "

echo "forwarding args: "$forward_escaped

# TTY-aware: jammybuild uses -it but that fails when called from a
# non-interactive shell (e.g. CI / automated runs). Use -t only if stdin is a TTY.
TTY_FLAG="-i"
if [ -t 0 ]; then TTY_FLAG="-it"; fi

docker run --rm $TTY_FLAG \
  -u "$UIDGID" \
  -e CCACHE_DIR=/ccache \
  -e CONAN_USER_HOME=/conan \
  -e BUILDDIR_OVERRIDE="/src/build.noble" \
  -v "$PWD":/src \
  -v "$HOME/.conan":/conan \
  -v "$HOME/.ccache":/ccache \
  -w /src \
  "$IMAGE" bash -lc '
    set -euo pipefail

    # Choose your compile script (compileit.sh preferred)
    COMPILE_SCRIPT="./compileit.sh"
    if [ ! -x "$COMPILE_SCRIPT" ]; then
      COMPILE_SCRIPT="./compileit.sh"
    fi
    test -x "$COMPILE_SCRIPT" || chmod +x "$COMPILE_SCRIPT"

    echo "== Conan profile =="
    conan remote add conancenter https://center.conan.io --force
    conan profile new default --detect --force
    conan profile update settings.compiler.libcxx=libstdc++11 default

    if '"$CLEAN_CACHE"'; then
      echo "== Clean requested (-c): removing CMake cache in build.noble =="
      rm -rf build.noble/CMakeCache.txt build.noble/CMakeFiles || true
    fi

    echo "== Conan install ('"$BUILD_TYPE"') -> build.noble/ =="
    if [ "'"$BUILD_TYPE"'" = "Debug" ]; then
      conan install . -if build.noble -s build_type=Debug \
        -g cmake_find_package -g cmake_paths \
        --build=missing
    else
      conan install . -if build.noble -s build_type=Release \
        -g cmake_find_package -g cmake_paths \
        --build=missing
    fi

    echo "== Tooling check (mold optional) =="
    if ! command -v mold >/dev/null 2>&1; then
      export LDFLAGS="${LDFLAGS:-}"
      LDFLAGS="${LDFLAGS/-fuse-ld=mold/}"
    fi

    echo "== Build via $COMPILE_SCRIPT with forwarded args =="
    set -x
    "$COMPILE_SCRIPT"'"$forward_escaped"'
    set +x

    echo "== Outputs =="
    ls -al noble.bin 2>/dev/null || true
    ls -al noble.bin.debug 2>/dev/null || true
  '
