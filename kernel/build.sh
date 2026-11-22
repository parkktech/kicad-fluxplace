#!/bin/sh
# Build the kernel and drop the shared library where fluxplace/kernel.py loads it.
set -e
cd "$(dirname "$0")"
cargo build --release
cp target/release/libfpkernel.so ../fluxplace/_fpkernel.so
