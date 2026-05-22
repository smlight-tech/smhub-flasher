#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 SMLIGHT
set -e

# Make sure flatpak and flatpak-builder are installed
if ! command -v flatpak-builder >/dev/null 2>&1; then
    echo "!! Please install flatpak and flatpak-builder first:"
    echo "   sudo apt-get install -y flatpak flatpak-builder"
    exit 1
fi

if [ ! -f "flatpak/tech.smlight.SMHUBFlasher.yml" ]; then
    echo "!! Please run this script from the repository root: ./flatpak/build-flatpak.sh"
    exit 1
fi

echo ">> Setting up Flathub remote..."
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo

echo ">> Installing GNOME SDK and Platform..."
flatpak install --user -y flathub org.gnome.Platform//50 org.gnome.Sdk//50

echo ">> Copying source to /tmp to avoid Flatpak /usr mount restrictions..."
rm -rf /tmp/smhub-flatpak-build
mkdir -p /tmp/smhub-flatpak-build
cp -a . /tmp/smhub-flatpak-build/

# Move flatpak specific files to the root of the build environment
mv /tmp/smhub-flatpak-build/flatpak/tech.smlight.SMHUBFlasher.* /tmp/smhub-flatpak-build/
mv /tmp/smhub-flatpak-build/flatpak/run.sh /tmp/smhub-flatpak-build/

cd /tmp/smhub-flatpak-build

echo ">> Building the Flatpak..."
flatpak-builder --repo=repo --force-clean --disable-rofiles-fuse build-dir tech.smlight.SMHUBFlasher.yml

echo ">> Installing locally..."
flatpak-builder --user --install --force-clean --disable-rofiles-fuse build-dir tech.smlight.SMHUBFlasher.yml

echo ">> Generating Flatpak bundle..."
flatpak build-bundle repo SMHUB-Flasher.flatpak tech.smlight.SMHUBFlasher
cp SMHUB-Flasher.flatpak "$OLDPWD/"

echo ">> Success! You can now run the Flatpak with:"
echo "   flatpak run tech.smlight.SMHUBFlasher"
