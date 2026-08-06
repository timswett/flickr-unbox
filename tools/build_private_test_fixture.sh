#!/bin/bash
set -uo pipefail

# Usage: ./testfixture_build.sh <photo_zip> <info_zips_dir> <work_dir>
#
# Extracts one Flickr photo-batch zip + all JSON info zips found in
# <info_zips_dir>, matches each photo to its photo_<id>.json sidecar
# (same rules as build_rename_plan.pl), copies matches into
# <work_dir>/matched_json/, and zips that into a single mini info-zip.
#
# LOCAL / PRIVATE USE ONLY — output contains your real photo/EXIF data.
# Do not commit the contents of <work_dir> into the public repo.

PHOTO_ZIP="${1:?Usage: $0 <photo_zip> <info_zips_dir> <work_dir>}"
INFO_ZIPS_DIR="${2:?Usage: $0 <photo_zip> <info_zips_dir> <work_dir>}"
WORK_DIR="${3:?Usage: $0 <photo_zip> <info_zips_dir> <work_dir>}"

echo "Started: $(date)"

mkdir -p "$WORK_DIR/photos" "$WORK_DIR/info_extracted" "$WORK_DIR/matched_json"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

echo "Extracting photo zip..."
unzip -oq "$PHOTO_ZIP" -d "$WORK_DIR/photos"

echo "Cleaning junk files from photos..."
find "$WORK_DIR/photos" -type f \( -name "._*" -o -name ".DS_Store" \) -delete

echo "Extracting info zips from $INFO_ZIPS_DIR..."
n=0
for z in "$INFO_ZIPS_DIR"/*.zip; do
  [ -e "$z" ] || continue
  n=$((n+1))
  echo "  -> part$n: $(basename "$z")"
  unzip -oq "$z" -d "$WORK_DIR/info_extracted/part$n"
done

if [ "$n" -eq 0 ]; then
  echo "ERROR: no .zip files found in $INFO_ZIPS_DIR"
  exit 1
fi

echo "Cleaning junk files from info extraction..."
find "$WORK_DIR/info_extracted" -type f \( -name "._*" -o -name ".DS_Store" \) -delete

echo "Matching photos to JSON sidecars..."
search_dirs=("$WORK_DIR"/info_extracted/part*)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
perl "$SCRIPT_DIR/match_photos_to_json.pl" \
  "$WORK_DIR/photos" \
  "$WORK_DIR/matched_json" \
  "$WORK_DIR/match_report.tsv" \
  "${search_dirs[@]}"

echo "Zipping matched JSON into mini info-zip..."
( cd "$WORK_DIR/matched_json" && zip -qr "$WORK_DIR/mini_infozip.zip" . )

echo "Finished: $(date)"
echo ""
echo "Output:"
echo "  Photos:          $WORK_DIR/photos/"
echo "  Matched JSON:    $WORK_DIR/matched_json/"
echo "  Mini info-zip:   $WORK_DIR/mini_infozip.zip"
echo "  Match report:    $WORK_DIR/match_report.tsv"
echo ""
echo "REMINDER: this folder contains real personal photo/EXIF data."
echo "Do not commit it to the public repo — local testing only."
