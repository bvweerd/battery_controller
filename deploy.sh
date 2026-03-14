#!/bin/bash
set -e

SRC="$(dirname "$0")/custom_components/battery_controller"
DEST="/media/data/homeassistant/config/custom_components/battery_controller"

echo "Deploying battery_controller to HA..."
rsync -av --delete "$SRC/" "$DEST/"
echo "Done."
