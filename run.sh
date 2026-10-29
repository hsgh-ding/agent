#!/usr/bin/env bash
# run.sh <input_dir> <output_dir>
#
#   <input_dir>/prompt.txt      task statement
#   <input_dir>/interface.txt   not provided by the evaluation task set -- the
#                               module interface is in prompt.txt
#
#   <output_dir>/solution.v   generated Verilog/SystemVerilog (empty file if we failed)
#   <output_dir>/trace.jsonl    one JSON object per tool / model call
#
# The contract is: never fail loudly. "Could not solve it" and "the service is
# broken" must stay distinguishable at the protocol level, so a failed run still
# produces an empty solution.v and exits 0.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN="${1:?usage: run.sh <input_dir> <output_dir>}"
OUT="${2:?usage: run.sh <input_dir> <output_dir>}"

mkdir -p "$OUT"

# Vivado / Vitis licence. The platform image does not preset it, and ~/.bashrc
# is reset whenever the instance restarts, so default it here; a value already
# in the environment wins.
export XILINXD_LICENSE_FILE="${XILINXD_LICENSE_FILE:-2100@vivado-lic.fpgachina.svc.cluster.local}"
export FLEXLM_TIMEOUT="${FLEXLM_TIMEOUT:-300000000}"

cd "$HERE"
python3 -m agent.main --input "$IN" --output "$OUT"
rc=$?

# Safety net: the harness expects these two files to exist no matter what.
[ -f "$OUT/solution.v" ] || : > "$OUT/solution.v"
[ -f "$OUT/trace.jsonl" ]  || : > "$OUT/trace.jsonl"

if [ $rc -ne 0 ]; then
    echo "run.sh: agent exited with $rc, emitted empty solution" >&2
fi
exit 0
