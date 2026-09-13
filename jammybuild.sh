#!/usr/bin/env bash

# Instructions:
# Add your user to the docker group so you can run docker without sudo:
# sudo usermod -aG docker <user>
# Log out and back in for it to take effect, or run this:
# newgrp docker
# Then run jammybuild like you would compileit:
# ./jammybuild.sh -c pktrade
# Note: building with sudo works but will not generate the git commit and branch in version.h
# When you scp over, please remember to scp the jammybuilt version in jammy.bin/

set -euo pipefail

IMAGE=pktrade-jammy-build:latest

# Build the Ubuntu 22.04 image (Conan 1.59.0 inside)
docker build -f Dockerfile.jammy -t "$IMAGE" .

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

# Quote-forward args for bash -lc
forward_escaped=" -j $(printf " %q" "${FORWARD_ARGS[@]}") "

echo "forwarding args: "$forward_escaped

docker run --rm -it \
  -u "$UIDGID" \
  -e CCACHE_DIR=/ccache \
  -e CONAN_USER_HOME=/conan \
  -e BUILDDIR_OVERRIDE="/src/build.jammy" \
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
      echo "== Clean requested (-c): removing CMake cache in build.jammy =="
      rm -rf build.jammy/CMakeCache.txt build.jammy/CMakeFiles || true
    fi

    echo "== Conan install ('"$BUILD_TYPE"') -> build.jammy/ =="
    if [ "'"$BUILD_TYPE"'" = "Debug" ]; then
      conan install . -if build.jammy -s build_type=Debug \
        -g cmake_find_package -g cmake_paths \
        --build=missing
    else
      conan install . -if build.jammy -s build_type=Release \
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
    ls -al bin 2>/dev/null || true
    ls -al bin.debug 2>/dev/null || true
  '
