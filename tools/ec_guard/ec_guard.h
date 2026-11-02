#ifndef NITROFAN_EC_GUARD_H
#define NITROFAN_EC_GUARD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#define EC_GUARD_MAX_ALLOWED_OFFSETS     32
#define EC_GUARD_MAX_VALUES_PER_OFFSET   16
#define EC_GUARD_MAX_SNAPSHOT_ENTRIES    32
#define EC_GUARD_MAX_CONTEXT_STRING_SIZE 64
#define EC_GUARD_MAX_RESTORATION_ORDER   32
#define EC_GUARD_FORBIDDEN_REGISTER      0x03

/* Return codes for guard operations. */
#define EC_GUARD_STATUS_SUCCESS 0
#define EC_GUARD_ERROR_DENIED   (-1)
#define EC_GUARD_ERROR_PARAM    (-2)
#define EC_GUARD_ERROR_IO       (-3)
#define EC_GUARD_ERROR_STATE    (-4)

/* Single register rule in the active write allowlist. */
typedef struct {
    uint8_t offset;
    size_t  value_count;
    uint8_t allowed_values[EC_GUARD_MAX_VALUES_PER_OFFSET];
    char    description[EC_GUARD_MAX_CONTEXT_STRING_SIZE];
} ECGuardRegisterRule;

/* Pre-write snapshot entry preserving the exact original byte. */
typedef struct {
    uint8_t offset;
    uint8_t original_value;
    bool    is_captured;
    bool    is_writable;
    bool    has_expected_baseline;
    uint8_t expected_baseline;
} ECGuardSnapshotEntry;

/* Complete active guard policy and runtime state. */
typedef struct {
    bool                 is_initialized;
    bool                 enforce_maximum_only;
    size_t               rule_count;
    ECGuardRegisterRule  rules[EC_GUARD_MAX_ALLOWED_OFFSETS];
    size_t               monitored_offset_count;
    uint8_t              monitored_offsets[EC_GUARD_MAX_ALLOWED_OFFSETS];
    size_t               snapshot_count;
    ECGuardSnapshotEntry snapshot_entries[EC_GUARD_MAX_SNAPSHOT_ENTRIES];
    size_t               restoration_order_count;
    uint8_t              restoration_order[EC_GUARD_MAX_RESTORATION_ORDER];
    int                  log_file_descriptor;
} ECGuardState;

/*
 * Initialize the guard state with an explicit JSON policy configuration.
 * Returns EC_GUARD_STATUS_SUCCESS on success, or a negative error code on failure.
 */
int ec_guard_init(const char *config_path, const char *log_path);

/*
 * Capture baseline bytes for all monitored and writable offsets.
 * Pre-reads bytes from descriptor and verifies them against expected safe baselines.
 */
int ec_guard_capture_snapshot(int file_descriptor);

/*
 * The single low-level pwrite entry point through which every EC write must pass.
 * Validates all bytes before writing, logs old/new values, and performs the write.
 */
int ec_guard_pwrite(
    int         file_descriptor,
    const void *buffer,
    size_t      byte_count,
    off_t       register_offset,
    const char *context_string
);

/*
 * Convenience wrapper for writing a single byte through the guarded pwrite path.
 */
int ec_guard_write_byte(
    int         file_descriptor,
    uint8_t     register_offset,
    uint8_t     value,
    const char *context_string
);

/*
 * Convenience wrapper for writing a 16-bit word through the guarded pwrite path.
 * Converts to little-endian format and ensures both bytes are verified before writing.
 */
int ec_guard_write_word(
    int         file_descriptor,
    uint8_t     register_offset,
    uint16_t    value,
    const char *context_string
);

/*
 * Restore all writable registers to their exact original captured baseline values.
 * Does not depend on in-process modified flags so cross-process writes are restored.
 * Follows configured restoration order (mode registers before speed registers).
 */
int ec_guard_restore_snapshot(int file_descriptor);

/*
 * Verify that all captured registers match their original baseline values.
 * Returns EC_GUARD_STATUS_SUCCESS if all match, or the number of mismatches as a negative int.
 */
int ec_guard_verify_snapshot(int file_descriptor);

/*
 * Flush write audit logs, close descriptors, and reset guard state.
 */
void ec_guard_cleanup(void);

/*
 * External recovery: reconstruct saved originals from expected_baselines
 * after verifying protected monitored offsets and allowed current register values.
 * Restores mode registers first, then targets, through the guarded writer.
 */
int ec_guard_recover_from_baselines(int file_descriptor);

int ec_guard_validate_synthetic_target(const char *path, const char *root);

#endif /* NITROFAN_EC_GUARD_H */
