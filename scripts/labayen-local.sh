#!/usr/bin/env bash
# Reduces the Labayen 2020 captures to a flow table, at home: Mendeley
# refuses Kaggle's machines. Reads only the archives fetch-data.sh has
# finished, so it can be run while the large ones are still coming, and
# again when they are done.
set -euo pipefail
cd "$(dirname "$0")/.."
src=data/ext/labayen
stage=$src/stage
mkdir -p "$stage" data/public/labayen
find "$stage" -maxdepth 1 -type l -delete
for done in "$src"/*.tar.gz.done; do
    [[ -e $done ]] || continue
    archive=${done%.done}
    ln -s "$(realpath "$archive")" "$stage/$(basename "$archive")"
done
echo "archives ready: $(ls "$stage" | tr '\n' ' ')"
.venv/bin/python kaggle/pcap_flow_tables.py --local "$stage" --out data/public/labayen --source labayen-2020 | tail -n 4
