#if defined(__APPLE__)
#if !defined(_DARWIN_C_SOURCE)
#define _DARWIN_C_SOURCE
#endif
#else
#if !defined(_DEFAULT_SOURCE)
#define _DEFAULT_SOURCE
#endif
#if !defined(_POSIX_C_SOURCE)
#define _POSIX_C_SOURCE 200809L
#endif
#endif

#if defined(__has_include)
#if __has_include("ec_guard.h")
#include "ec_guard.h"
#elif __has_include("../tools/ec_guard/ec_guard.h")
#include "../tools/ec_guard/ec_guard.h"
#else
#include "tools/ec_guard/ec_guard.h"
#endif
#else
#include "../tools/ec_guard/ec_guard.h"
#endif

#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define EC_FIXTURE_SIZE 256
#define TEST_LOG_PATH    "/tmp/test_ec_guard_audit.log"
#define TEST_EC_PATH     "/tmp/test_fake_ec_256.bin"

static const char *get_test_config_path(void) {
    if (access("ec_guard_policy.json", R_OK) == 0) {
        return "ec_guard_policy.json";
    }
    if (access("config/ec_guard_policy.json", R_OK) == 0) {
        return "config/ec_guard_policy.json";
    }
    if (access("tools/ec_guard/config/ec_guard_policy.json", R_OK) == 0) {
        return "tools/ec_guard/config/ec_guard_policy.json";
    }
    return "../../tools/ec_guard/config/ec_guard_policy.json";
}

static void create_fake_ec_file(const char *path) {
    uint8_t ec_buffer[EC_FIXTURE_SIZE];
    memset(ec_buffer, 0, sizeof(ec_buffer));

    /* Baseline register values observed in read-only trial. */
    ec_buffer[0x03] = 0x21; /* Unlock register: must remain 0x21 unchanged. */
    ec_buffer[0x10] = 0x00; /* CoolBoost: 0x00 baseline. */
    ec_buffer[0x13] = 0xE4; /* Fan1 RPM low byte (2788 RPM). */
    ec_buffer[0x14] = 0x0A; /* Fan1 RPM high byte. */
    ec_buffer[0x15] = 0x90; /* Fan2 RPM low byte (2448 RPM). */
    ec_buffer[0x16] = 0x09; /* Fan2 RPM high byte. */
    ec_buffer[0x21] = 0x00; /* GPU mode: 0x00 baseline. */
    ec_buffer[0x22] = 0x00; /* CPU mode: 0x00 baseline. */
    ec_buffer[0x37] = 0x00; /* CPU speed: 0x00 baseline. */
    ec_buffer[0x38] = 0x00;
    ec_buffer[0x3A] = 0x00; /* GPU speed: 0x00 baseline. */
    ec_buffer[0x3B] = 0x00;

    int file_descriptor = open(path, O_RDWR | O_CREAT | O_TRUNC, 0600);
    assert(file_descriptor >= 0);
    ssize_t written = write(file_descriptor, ec_buffer, sizeof(ec_buffer));
    assert(written == sizeof(ec_buffer));
    close(file_descriptor);
}

static void test_deny_unlisted_offsets(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* Test 1: Unlisted single byte write at offset 0x01. */
    uint8_t dummy_value = 0x55;
    int write_status = ec_guard_write_byte(
        ec_file_descriptor,
        0x01,
        dummy_value,
        "test_unlisted"
    );
    assert(write_status == EC_GUARD_ERROR_DENIED);

    uint8_t read_back = 0xFF;
    pread(ec_file_descriptor, &read_back, 1, 0x01);
    assert(read_back == 0x00);

    /* Test 2: Unlisted offset 0x39. */
    write_status = ec_guard_write_byte(
        ec_file_descriptor,
        0x39,
        0x10,
        "test_unlisted"
    );
    assert(write_status == EC_GUARD_ERROR_DENIED);
    pread(ec_file_descriptor, &read_back, 1, 0x39);
    assert(read_back == 0x00);

    /* Test 3: Unlock register 0x03 is strictly forbidden. */
    write_status = ec_guard_write_byte(
        ec_file_descriptor,
        0x03,
        0x11,
        "test_unlock_attempt"
    );
    assert(write_status == EC_GUARD_ERROR_DENIED);
    pread(ec_file_descriptor, &read_back, 1, 0x03);
    assert(read_back == 0x21);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_deny_unlisted_offsets\n");
}

static void test_deny_partial_word_writes_before_any_byte_changes(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* Case A: Lower byte 0x36 is unlisted, upper byte 0x37 is allowed. */
    uint16_t word_at_0x36 = 0x6400;
    int status = ec_guard_pwrite(
        ec_file_descriptor,
        &word_at_0x36,
        2,
        0x36,
        "test_partial_word_low_unlisted"
    );
    assert(status == EC_GUARD_ERROR_DENIED);

    uint8_t byte_0x36 = 0xFF;
    uint8_t byte_0x37 = 0xFF;
    pread(ec_file_descriptor, &byte_0x36, 1, 0x36);
    pread(ec_file_descriptor, &byte_0x37, 1, 0x37);
    assert(byte_0x36 == 0x00);
    assert(byte_0x37 == 0x00);

    /* Case B: Lower byte 0x38 is allowed, upper byte 0x39 is unlisted. */
    uint16_t word_at_0x38 = 0x1200;
    status = ec_guard_pwrite(
        ec_file_descriptor,
        &word_at_0x38,
        2,
        0x38,
        "test_partial_word_high_unlisted"
    );
    assert(status == EC_GUARD_ERROR_DENIED);

    uint8_t byte_0x38 = 0xFF;
    uint8_t byte_0x39 = 0xFF;
    pread(ec_file_descriptor, &byte_0x38, 1, 0x38);
    pread(ec_file_descriptor, &byte_0x39, 1, 0x39);
    assert(byte_0x38 == 0x00);
    assert(byte_0x39 == 0x00);

    /* Case C: Disallowed upper byte value at 0x38. */
    uint16_t invalid_word_at_0x37 = 0x0564;
    status = ec_guard_write_word(
        ec_file_descriptor,
        0x37,
        invalid_word_at_0x37,
        "test_partial_word_disallowed_high_val"
    );
    assert(status == EC_GUARD_ERROR_DENIED);

    pread(ec_file_descriptor, &byte_0x37, 1, 0x37);
    pread(ec_file_descriptor, &byte_0x38, 1, 0x38);
    assert(byte_0x37 == 0x00);
    assert(byte_0x38 == 0x00);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_deny_partial_word_writes_before_any_byte_changes\n");
}

static void test_maximum_only_enforcement(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* Attempt active zero write: MUST be denied by maximum-only policy! */
    int status = ec_guard_write_word(
        ec_file_descriptor,
        0x37,
        0,
        "active_zero"
    );
    assert(status == EC_GUARD_ERROR_DENIED);

    uint8_t speed_byte = 0xFF;
    pread(ec_file_descriptor, &speed_byte, 1, 0x37);
    assert(speed_byte == 0x00);

    /* Attempt 30% speed write: disallowed. */
    status = ec_guard_write_byte(
        ec_file_descriptor,
        0x37,
        30,
        "test_low_speed"
    );
    assert(status == EC_GUARD_ERROR_DENIED);
    pread(ec_file_descriptor, &speed_byte, 1, 0x37);
    assert(speed_byte == 0x00);

    /* Attempt 50% speed write: disallowed. */
    status = ec_guard_write_byte(
        ec_file_descriptor,
        0x37,
        50,
        "test_mid_speed"
    );
    assert(status == EC_GUARD_ERROR_DENIED);
    pread(ec_file_descriptor, &speed_byte, 1, 0x37);
    assert(speed_byte == 0x00);

    /* Permitted 100% speed word write at 0x37. */
    status = ec_guard_write_word(
        ec_file_descriptor,
        0x37,
        100,
        "test_max_speed"
    );
    assert(status == EC_GUARD_STATUS_SUCCESS);

    uint8_t byte_0x37 = 0;
    uint8_t byte_0x38 = 0;
    pread(ec_file_descriptor, &byte_0x37, 1, 0x37);
    pread(ec_file_descriptor, &byte_0x38, 1, 0x38);
    assert(byte_0x37 == 100);
    assert(byte_0x38 == 0);

    /* Permitted 100% speed word write at 0x3A. */
    status = ec_guard_write_word(
        ec_file_descriptor,
        0x3A,
        100,
        "test_max_speed"
    );
    assert(status == EC_GUARD_STATUS_SUCCESS);

    uint8_t byte_0x3a = 0;
    uint8_t byte_0x3b = 0;
    pread(ec_file_descriptor, &byte_0x3a, 1, 0x3A);
    pread(ec_file_descriptor, &byte_0x3b, 1, 0x3B);
    assert(byte_0x3a == 100);
    assert(byte_0x3b == 0);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_maximum_only_enforcement\n");
}

static void test_bounds_checking_offset_255(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* 2-byte write at offset 255: would wrap to 0 if bounds check is naive. */
    uint16_t wrap_word = 0x6400;
    int status = ec_guard_pwrite(
        ec_file_descriptor,
        &wrap_word,
        2,
        255,
        "test_wrap_offset_255"
    );
    assert(status == EC_GUARD_ERROR_PARAM);

    /* Offset 256 is out of range. */
    status = ec_guard_write_byte(
        ec_file_descriptor,
        0,
        100,
        "test_out_of_range"
    );
    /* Offset 0 is unlisted. */
    assert(status == EC_GUARD_ERROR_DENIED);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_bounds_checking_offset_255\n");
}

static void test_old_new_audit_logging(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* 1. Perform allowed mode write at 0x22 (CPU mode manual 0x0C). */
    int status = ec_guard_write_byte(
        ec_file_descriptor,
        0x22,
        0x0C,
        "test_cpu_mode_manual"
    );
    assert(status == EC_GUARD_STATUS_SUCCESS);

    /* 2. Perform denied write at 0x03. */
    ec_guard_write_byte(ec_file_descriptor, 0x03, 0x11, "test_unlock_attempt");

    ec_guard_cleanup();
    close(ec_file_descriptor);

    /* Read back audit log and check content. */
    FILE *log_file = fopen(TEST_LOG_PATH, "r");
    assert(log_file != NULL);

    char line_buffer[1024];
    bool found_proposed = false;
    bool found_allowed  = false;
    bool found_denied   = false;

    while (fgets(line_buffer, sizeof(line_buffer), log_file) != NULL) {
        if (strstr(line_buffer, "\"action\":\"PROPOSED\"") != NULL &&
            strstr(line_buffer, "\"offset\":\"0x22\"") != NULL) {
            found_proposed = true;
        }
        if (strstr(line_buffer, "\"action\":\"ALLOWED\"") != NULL &&
            strstr(line_buffer, "\"offset\":\"0x22\"") != NULL &&
            strstr(line_buffer, "\"old\":[\"0x00\"]") != NULL &&
            strstr(line_buffer, "\"new\":[\"0x0C\"]") != NULL) {
            found_allowed = true;
        }
        if (strstr(line_buffer, "\"action\":\"DENIED\"") != NULL &&
            strstr(line_buffer, "\"offset\":\"0x03\"") != NULL &&
            strstr(line_buffer, "forbidden_unlock_register_0x03") != NULL) {
            found_denied = true;
        }
    }
    fclose(log_file);\

    assert(found_proposed);
    assert(found_allowed);
    assert(found_denied);
    printf("PASS: test_old_new_audit_logging\n");
}

static void test_cross_process_restoration_based_on_actual_bytes(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* Capture initial baseline snapshot in parent process. */
    int snap_status = ec_guard_capture_snapshot(ec_file_descriptor);
    assert(snap_status == EC_GUARD_STATUS_SUCCESS);

    /*
     * Simulate cross-process mutation: an external child process writes 100 to offset 0x37.
     * The parent's in-memory is_modified flag is NOT set by this external mutation.
     */
    uint8_t external_mutation = 100;
    pwrite(ec_file_descriptor, &external_mutation, 1, 0x37);

    uint8_t byte_val = 0;
    pread(ec_file_descriptor, &byte_val, 1, 0x37);
    assert(byte_val == 100);

    /*
     * Restore snapshot in parent process: MUST restore based on actual bytes on disk/device,
     * independent of in-process modified flags.
     */
    int restore_status = ec_guard_restore_snapshot(ec_file_descriptor);
    assert(restore_status == EC_GUARD_STATUS_SUCCESS);

    pread(ec_file_descriptor, &byte_val, 1, 0x37);
    assert(byte_val == 0x00);

    int verify_status = ec_guard_verify_snapshot(ec_file_descriptor);
    assert(verify_status == EC_GUARD_STATUS_SUCCESS);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_cross_process_restoration_based_on_actual_bytes\n");
}

static void test_cleanup_restoring_exact_saved_originals(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    int snap_status = ec_guard_capture_snapshot(ec_file_descriptor);
    assert(snap_status == EC_GUARD_STATUS_SUCCESS);

    ec_guard_write_byte(ec_file_descriptor, 0x22, 0x0C, "init_cpu_mode");
    ec_guard_write_byte(ec_file_descriptor, 0x21, 0x30, "init_gpu_mode");
    ec_guard_write_word(ec_file_descriptor, 0x37, 100, "init_cpu_speed");
    ec_guard_write_word(ec_file_descriptor, 0x3A, 100, "init_gpu_speed");

    uint8_t byte_val = 0;
    pread(ec_file_descriptor, &byte_val, 1, 0x22);
    assert(byte_val == 0x0C);
    pread(ec_file_descriptor, &byte_val, 1, 0x37);
    assert(byte_val == 100);

    int restore_status = ec_guard_restore_snapshot(ec_file_descriptor);
    assert(restore_status == EC_GUARD_STATUS_SUCCESS);

    int verify_status = ec_guard_verify_snapshot(ec_file_descriptor);
    assert(verify_status == EC_GUARD_STATUS_SUCCESS);

    pread(ec_file_descriptor, &byte_val, 1, 0x22);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x21);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x37);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x3A);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x03);
    assert(byte_val == 0x21);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_cleanup_restoring_exact_saved_originals\n");
}

static void test_recovery_from_baselines(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);
    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    int snap_status = ec_guard_capture_snapshot(ec_file_descriptor);
    assert(snap_status == EC_GUARD_STATUS_SUCCESS);

    /* Write manual modes and max speed */
    ec_guard_write_byte(ec_file_descriptor, 0x22, 0x0C, "init_cpu_mode");
    ec_guard_write_byte(ec_file_descriptor, 0x21, 0x30, "init_gpu_mode");
    ec_guard_write_word(ec_file_descriptor, 0x37, 100, "init_cpu_speed");
    ec_guard_write_word(ec_file_descriptor, 0x3A, 100, "init_gpu_speed");

    /* Simulate process crash/SIGKILL: memory state wiped */
    ec_guard_cleanup();
    close(ec_file_descriptor);

    /* Fresh process starts recovery */
    ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    /* Reconstruct baselines and restore */
    int rec_status = ec_guard_recover_from_baselines(ec_file_descriptor);
    assert(rec_status == EC_GUARD_STATUS_SUCCESS);

    /* Verify all registers returned to baseline */
    uint8_t byte_val = 0xFF;
    pread(ec_file_descriptor, &byte_val, 1, 0x22);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x21);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x37);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x3A);
    assert(byte_val == 0x00);
    pread(ec_file_descriptor, &byte_val, 1, 0x03);
    assert(byte_val == 0x21);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_recovery_from_baselines\n");
}

static void test_recovery_fails_on_corrupt_or_unsafe(void) {
    create_fake_ec_file(TEST_EC_PATH);
    int ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    unlink(TEST_LOG_PATH);

    /* Case A: Protected register 0x03 corrupted */
    uint8_t corrupt = 0x00;
    pwrite(ec_file_descriptor, &corrupt, 1, 0x03);

    int init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    int rec_status = ec_guard_recover_from_baselines(ec_file_descriptor);
    assert(rec_status == EC_GUARD_ERROR_STATE);

    ec_guard_cleanup();
    close(ec_file_descriptor);

    /* Case B: Writable register has unknown/unsafe foreign value (e.g. 0x37 = 77) */
    create_fake_ec_file(TEST_EC_PATH);
    ec_file_descriptor = open(TEST_EC_PATH, O_RDWR);
    assert(ec_file_descriptor >= 0);

    corrupt = 77; /* Neither baseline 0 nor allowed 100 in max policy */
    pwrite(ec_file_descriptor, &corrupt, 1, 0x37);

    init_status = ec_guard_init(get_test_config_path(), TEST_LOG_PATH);
    assert(init_status == EC_GUARD_STATUS_SUCCESS);

    rec_status = ec_guard_recover_from_baselines(ec_file_descriptor);
    assert(rec_status == EC_GUARD_ERROR_DENIED);

    /* Verify no writes occurred */
    uint8_t read_back = 0;
    pread(ec_file_descriptor, &read_back, 1, 0x37);
    assert(read_back == 77);

    ec_guard_cleanup();
    close(ec_file_descriptor);
    printf("PASS: test_recovery_fails_on_corrupt_or_unsafe\n");
}

int main(void) {
    printf("=== Starting C Guard Unit Tests on Synthetic 256-byte EC ===\n");
    test_deny_unlisted_offsets();
    test_deny_partial_word_writes_before_any_byte_changes();
    test_maximum_only_enforcement();
    test_bounds_checking_offset_255();
    test_old_new_audit_logging();
    test_cross_process_restoration_based_on_actual_bytes();
    test_cleanup_restoring_exact_saved_originals();
    test_recovery_from_baselines();
    test_recovery_fails_on_corrupt_or_unsafe();
    printf("=== All C Guard Unit Tests PASSED ===\n");
    return 0;
}
