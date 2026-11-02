#!/usr/bin/env bash
# tools/nbfc_readonly_check.sh
#
# Bounded read-only evaluation helper for NoteBook FanControl (NBFC) on Acer Nitro AN515-54.
# Requires root privileges (invoked via sudo).
# Enforces kernel write_support=N and strictly passes --read-only and --embedded-controller ec_sys.

set -euo pipefail

BASE_DIR="${NBFC_BASE_DIR:-/home/luke/nbfc-evaluation}"
PKG_DIR="${BASE_DIR}/package"
DEP_DIR="${BASE_DIR}/dependencies"
LIB_DIR="${DEP_DIR}/usr/lib/x86_64-linux-gnu"
SERVICE_BIN="${PKG_DIR}/usr/bin/nbfc_service"
NBFC_BIN="${PKG_DIR}/usr/bin/nbfc"
PROFILE_FILE="${PKG_DIR}/usr/share/nbfc/configs/Acer Nitro AN515-54.json"
SERVICE_CONFIG="${BASE_DIR}/service.json"

REAL_DURATION="${NBFC_TRIAL_DURATION:-45}"
DUMMY_DURATION="${NBFC_DUMMY_DURATION:-3}"

SYS_DMI_PRODUCT="${NBFC_SYSFS_DMI_PRODUCT:-/sys/class/dmi/id/product_name}"
SYS_DMI_VENDOR="${NBFC_SYSFS_DMI_VENDOR:-/sys/class/dmi/id/sys_vendor}"
SYS_WRITE_SUPPORT="${NBFC_SYSFS_WRITE_SUPPORT:-/sys/module/ec_sys/parameters/write_support}"
SYS_EC_IO="${NBFC_SYSFS_EC_IO:-/sys/kernel/debug/ec/ec0/io}"

echo "=== NBFC Read-Only Evaluation Preflight ==="

# 1. Privileges check
if [ "${EUID:-$(id -u)}" -ne 0 ] && [ "${NBFC_ALLOW_NON_ROOT:-0}" -ne 1 ]; then
    echo "ERROR: nbfc_service requires root privileges. Please run via sudo: sudo $0" >&2
    exit 1
fi

for setting in REAL_DURATION DUMMY_DURATION; do
    duration="${!setting}"
    if [[ ! "$duration" =~ ^[0-9]{1,3}$ ]] || (( 10#$duration < 1 || 10#$duration > 120 )); then
        echo "ERROR: $setting must be a whole number from 1 to 120 seconds." >&2
        exit 1
    fi
done

# 2. Hardware DMI check
if [ ! -r "$SYS_DMI_PRODUCT" ] || [ ! -r "$SYS_DMI_VENDOR" ]; then
    echo "ERROR: Cannot read DMI hardware identifiers ($SYS_DMI_PRODUCT or $SYS_DMI_VENDOR)" >&2
    exit 1
fi

PRODUCT=$(cat "$SYS_DMI_PRODUCT")
VENDOR=$(cat "$SYS_DMI_VENDOR")
if [[ "$PRODUCT" != "Nitro AN515-54" ]] || [[ "$VENDOR" != *"Acer"* ]]; then
    echo "ERROR: DMI mismatch (Vendor: '$VENDOR', Product: '$PRODUCT'). Expected Acer Nitro AN515-54." >&2
    exit 1
fi
echo "Verified target hardware: $VENDOR $PRODUCT"

# 3. Kernel write protection check
if [ ! -f "$SYS_WRITE_SUPPORT" ]; then
    echo "ERROR: ec_sys parameter not found at $SYS_WRITE_SUPPORT. Module must be loaded." >&2
    exit 1
fi

WS_VAL=$(cat "$SYS_WRITE_SUPPORT")
if [ "$WS_VAL" != "N" ] && [ "$WS_VAL" != "0" ]; then
    echo "ERROR: ec_sys write_support is '$WS_VAL', expected 'N' or '0'. Refusing to proceed with write-enabled module." >&2
    exit 1
fi
echo "Verified ec_sys write_support: $WS_VAL (kernel write protection active)"

# 4. EC debugfs node check
if [ ! -e "$SYS_EC_IO" ]; then
    echo "ERROR: EC debugfs io node not found at $SYS_EC_IO" >&2
    exit 1
fi
echo "Verified EC node exists: $SYS_EC_IO"

# 5. Binaries, profile, and library checks
for file in "$SERVICE_BIN" "$NBFC_BIN" "$PROFILE_FILE"; do
    if [ ! -f "$file" ]; then
        echo "ERROR: Required file missing: $file" >&2
        exit 1
    fi
done

if [ ! -x "$SERVICE_BIN" ] || [ ! -x "$NBFC_BIN" ]; then
    echo "ERROR: NBFC binaries are not executable ($SERVICE_BIN, $NBFC_BIN)" >&2
    exit 1
fi

if [ ! -f "${LIB_DIR}/liblua5.4.so.0" ]; then
    echo "ERROR: Missing dependency liblua5.4.so.0 in $LIB_DIR" >&2
    exit 1
fi
echo "Verified NBFC binaries, libraries, and model profile."

# 6. Service configuration verification
if [ ! -f "$SERVICE_CONFIG" ]; then
    echo "Creating initial service configuration: $SERVICE_CONFIG"
    cat << EOF > "$SERVICE_CONFIG"
{
  "SelectedConfigId": "$PROFILE_FILE",
  "EmbeddedControllerType": "ec_sys"
}
EOF
fi

# 7. Competing instance check (non-destructive)
if pgrep -x nbfc_service >/dev/null 2>&1; then
    echo "ERROR: An nbfc_service process is already running. Please terminate it before running this trial." >&2
    exit 1
fi

PID_FILE="/run/nbfc_service.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "ERROR: Active nbfc_service process (PID $OLD_PID) found via $PID_FILE." >&2
        exit 1
    fi
fi
echo "Preflight checks passed."

TRIAL_PID=""
SNAPSHOT_PID=""
cleanup() {
    if [ -n "$SNAPSHOT_PID" ]; then
        kill "$SNAPSHOT_PID" 2>/dev/null || true
        wait "$SNAPSHOT_PID" 2>/dev/null || true
        SNAPSHOT_PID=""
    fi
    if [ -n "$TRIAL_PID" ]; then
        # GNU timeout forwards TERM to its child and bounds cleanup with -k.
        kill -TERM "$TRIAL_PID" 2>/dev/null || true
        wait "$TRIAL_PID" 2>/dev/null || true
        TRIAL_PID=""
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_trial() {
    local duration="$1" backend="$2" result
    timeout -s TERM -k 5s "${duration}s" env LD_LIBRARY_PATH="$LIB_DIR" \
        "$SERVICE_BIN" --read-only --debug --config-file "$SERVICE_CONFIG" --embedded-controller "$backend" &
    TRIAL_PID=$!
    if wait "$TRIAL_PID"; then result=0; else result=$?; fi
    TRIAL_PID=""
    if [ "$result" -ne 124 ]; then
        echo "ERROR: $backend trial ended before its full duration (exit $result)." >&2
        if [ "$result" -eq 0 ]; then return 1; fi
        return "$result"
    fi
    echo "$backend trial reached its ${duration}s duration and was stopped by timeout."
}

# Phase 1: Dummy validation
echo ""
echo "=== Phase 1: Validating config and profile with dummy controller (${DUMMY_DURATION}s) ==="
run_trial "$DUMMY_DURATION" dummy
echo "Phase 1 dummy backend validation completed."
sleep 1

# Phase 2: Real read-only EC trial
echo ""
echo "=== Phase 2: Bounded real EC read-only trial (${REAL_DURATION}s) ==="
echo "Executing: nbfc_service --read-only --debug --config-file $SERVICE_CONFIG --embedded-controller ec_sys"

# Status snapshot background worker
(
    # Reap the worker's own delayed jobs if the outer script is interrupted.
    trap 'for child in $(jobs -pr); do kill "$child" 2>/dev/null || true; done; wait || true' EXIT
    trap 'exit 0' TERM INT
    sleep 5 & wait "$!"
    if [ -S /run/nbfc_service.socket ]; then
        echo ""
        echo "--- [t+5s] Native NBFC Status Snapshot ---"
        timeout -k 1s 5s env LD_LIBRARY_PATH="$LIB_DIR" "$NBFC_BIN" status --json || true
        echo "------------------------------------------"
    fi
    sleep 20 & wait "$!"
    if [ -S /run/nbfc_service.socket ]; then
        echo ""
        echo "--- [t+25s] Native NBFC Status Snapshot ---"
        timeout -k 1s 5s env LD_LIBRARY_PATH="$LIB_DIR" "$NBFC_BIN" status --json || true
        echo "-------------------------------------------"
    fi
) &
SNAPSHOT_PID=$!

run_trial "$REAL_DURATION" ec_sys
cleanup
echo "Phase 2 trial completed."

# Post-trial verification
echo ""
echo "=== Post-Trial Verification ==="
POST_WS=$(cat "$SYS_WRITE_SUPPORT" 2>/dev/null || echo "UNKNOWN")
echo "ec_sys write_support: $POST_WS"
if [ "$POST_WS" != "N" ] && [ "$POST_WS" != "0" ]; then
    echo "ERROR: write_support changed! (Now: $POST_WS)" >&2
    exit 1
fi

if pgrep -x nbfc_service >/dev/null 2>&1; then
    echo "ERROR: nbfc_service process is still running." >&2
    exit 1
else
    echo "Confirmed: nbfc_service process terminated cleanly."
fi
echo "Read-only trial finished. Review output above for EC register read traces (0x13, 0x15)."
