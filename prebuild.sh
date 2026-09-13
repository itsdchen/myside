#! /usr/bin/env bash

conan config install .conan/

os=$(uname -s)
arch=$(uname -m)

if [[ $os = Linux ]]; then
    os=linux
elif [[ $os = Darwin ]]; then
    os=mac
else
    echo "Unsupported OS ($os)"
    exit 1
fi

if [[ $arch = arm64 ]]; then
    arch=a64
elif [[ $arch = x86_64 ]]; then
    arch=x64
else
    echo "Unsupported architecture ($arch)"
    exit 1
fi

profile=pk-$os-$arch

# --build=missing will build stuff without prebuilt images. It'll store
# things in ~/.conan/data
conan install -if build/ -pr $profile . --build=missing
echo "Finished prebuild"
