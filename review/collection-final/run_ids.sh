#!/bin/zsh
# usage: run_ids.sh <checkout-dir> <label>   runs each failing test id in its own pytest process
cd "$1" || exit 2
PY=/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python
out=/tmp/res_$2.txt
: > "$out"
while IFS= read -r id; do
  r=$(env -u TTAKKARI_TEST_DB PYTHONPATH=src $PY -m pytest -q --no-header -p no:cacheprovider --timeout=300 "$id" 2>&1 | tail -n 1)
  case "$r" in
    *passed*) s=PASS;;
    *failed*) s=FAIL;;
    *error*) s=ERROR;;
    *) s="OTHER:$r";;
  esac
  echo "$s $id" >> "$out"
done < /tmp/failed_ids.txt
echo done >> "$out"
