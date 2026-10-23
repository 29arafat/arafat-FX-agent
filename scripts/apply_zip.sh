#!/usr/bin/env bash
# ব্যবহার: bash scripts/apply_zip.sh <zip-ফাইল> "<কমিট মেসেজ>"
# কাজ: git pull → unzip → zip মুছে ফেলা → git add/commit/push
set -euo pipefail
ZIP="${1:?zip ফাইলের নাম দিন}"
MSG="${2:?কমিট মেসেজ দিন}"
[ -f "$ZIP" ] || { echo "ফাইল পাওয়া যায়নি: $ZIP"; exit 1; }
git pull --rebase
unzip -o "$ZIP"
rm "$ZIP"
git add -A
git commit -m "$MSG"
git push
echo "সম্পন্ন।"
