#!/bin/bash
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# 
# Copyright (C) 2025 Aless Microsystems
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
set -euo pipefail
#set -x

dir="/tmp/null"
rm -rf "$dir"
mkdir "$dir"
cd "$dir"

# Add all arguments as the initial core packages
printf '%s\n' "$@" > keep
# Packages required for a shell environment
cat >>keep <<EOF
bash
coreutils
filesystem
crypto-policies
libpython3_13-1_0
system-user-root
libbrotlicommon1
libbrotlidec1
libbz2-1
libcom_err2
libexpat1
libffi7
libgcrypt20
libgpg-error0
libjitterentropy3
liblzma5
libreadline7
libnghttp2-14
libssh4
libpsl5
libsasl2-3
libuuid1
libverto1
libzstd1
libpcre2-8-0
libkeyutils1
libldap-data
libltdl7
libstdc++6
terminfo-base
EOF



# Disallow list to block certain packages and their dependencies
cat >disallow <<EOF
alsa-lib
cups-libs
gawk
p11-kit
EOF

sort -u keep -o keep

echo "==> Installing packages into chroot" >&2

set -x

# Check if findutils and diffutils are installed, install if not.
if ! rpm -q findutils &>/dev/null; then
 echo "findutils not found. Attempting to install."
 if zypper install -y findutils; then
  echo "findutils installed successfully."
 else
  echo "Error installing findutils. Exiting."
  exit 1
 fi
fi

if ! rpm -q diffutils &>/dev/null; then
 echo "diffutils not found. Attempting to install."
 if zypper install -y diffutils; then
  echo "diffutils installed successfully."
 else
  echo "Error installing diffutils. Exiting."
  exit 1
 fi
fi

rootfs="$(realpath rootfs)"
mkdir -p "$rootfs"

# Refresh repositories before installing to chroot
# in a chroot older keys are not available so we import them manually
rpm -r "$rootfs" --import https://download.opensuse.org/ports/aarch64/tumbleweed/repo/oss/gpg-pubkey-39db7c82-5f68629b.asc
zypper --installroot "$rootfs" -n  --gpg-auto-import-keys refresh --force
<keep xargs zypper --installroot "$rootfs" -n --gpg-auto-import-keys in --no-recommends
zypper --installroot "$rootfs" clean -a
rm -rf "$rootfs"/var/cache/zypp/* "$rootfs"/var/log/zypp/*
{ set +x; } 2>/dev/null

echo "==> Building dependency tree" >&2

# 1. Get requirement names (not quite the same as package names)
# 2. Filter out any install-time requirements
# 3. Query which packages are being used to satisfy the requirements
# 4. Keep just their package names
# 5. Remove packages that are on the disallow list
# 6. Store result as an allowlist
# This was done for a different reason but it is kept. all the transient deps are actually needed to
# have "xxx.so not found needed for xxxx.so" errors. the deps are already minimum.
<keep xargs rpm -r "$rootfs" -q --requires | sort -Vu | cut -d ' ' -f1 \
  | grep -v -e '^rpmlib(' \
  | xargs -d $'\n' rpm -r "$rootfs" -q --whatprovides \
  | grep -v -e '^no package provides' \
  | sed -r 's/^(.*)-.*-.*$/\1/' \
  | grep -vxF -f disallow \
  > new || true

mv keep old
cat old new > keep
sort -u keep -o keep

rpm -r "$rootfs" -qa | sed -r 's/^(.*)-.*-.*$/\1/' | sort -u > all
grep -vxF -f keep all > remove

echo "==> $(wc -l remove | cut -d ' ' -f1) packages to erase:" >&2
cat remove
echo "==> $(wc -l keep | cut -d ' ' -f1) packages to keep:" >&2
cat keep
echo "" >&2

echo "==> Erasing packages" >&2
 set -x
 <remove xargs rpm -r "$rootfs" --erase --allmatches
 { set +x; } 2>/dev/null

echo "" >&2
echo "==> Packages erased ok!" >&2
