#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#define _DARWIN_C_SOURCE

#include "ec_guard.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define REGISTER_COUNT 256
#define POLICY_CAPACITY 8192
#define TEXT_CAPACITY 256
#define AUDIT_CAPACITY 1024

static struct {
    bool initialized;
    bool captured;
    bool allowed[REGISTER_COUNT];
    bool values[REGISTER_COUNT][REGISTER_COUNT];
    bool expected_set[REGISTER_COUNT];
    bool monitored[REGISTER_COUNT];
    uint8_t expected[REGISTER_COUNT];
    uint8_t original[REGISTER_COUNT];
    uint8_t order[REGISTER_COUNT];
    size_t order_count;
    uint8_t handoff[REGISTER_COUNT];
    size_t handoff_count;
    dev_t device;
    ino_t inode;
    int log_descriptor;
} state = {.log_descriptor = -1};

/* This parser accepts the policy's bounded ASCII JSON subset and rejects unknown fields. */
static void whitespace(const char **cursor) {
    while (**cursor == ' ' || **cursor == '\n' || **cursor == '\r' || **cursor == '\t')
        ++*cursor;
}

static bool token(const char **cursor, char value) {
    whitespace(cursor);
    if (**cursor != value)
        return false;
    ++*cursor;
    return true;
}

static bool string_value(const char **cursor, char *output, size_t capacity) {
    if (!token(cursor, '"'))
        return false;
    size_t length = 0;
    while (**cursor && **cursor != '"') {
        unsigned char value = (unsigned char)*(*cursor)++;
        if (value < ' ' || value > '~')
            return false;
        if (value == '\\') {
            value = (unsigned char)*(*cursor)++;
            if (value != '"' && value != '\\' && value != '/')
                return false;
        }
        if (length + 1 >= capacity)
            return false;
        output[length++] = (char)value;
    }
    output[length] = '\0';
    return token(cursor, '"');
}

static bool byte_value(const char **cursor, uint8_t *output) {
    whitespace(cursor);
    if (!isdigit((unsigned char)**cursor))
        return false;
    if (**cursor == '0' && isdigit((unsigned char)(*cursor)[1]))
        return false;
    unsigned value = 0;
    do {
        value = value * 10 + (unsigned)(*(*cursor)++ - '0');
        if (value >= REGISTER_COUNT)
            return false;
    } while (isdigit((unsigned char)**cursor));
    *output = (uint8_t)value;
    return true;
}

static bool byte_array(const char **cursor, uint8_t *output, size_t *count) {
    bool seen[REGISTER_COUNT] = {false};
    *count = 0;
    if (!token(cursor, '['))
        return false;
    if (token(cursor, ']'))
        return true;
    do {
        uint8_t value;
        if (!byte_value(cursor, &value) || seen[value])
            return false;
        seen[value] = true;
        output[(*count)++] = value;
        if (token(cursor, ']'))
            return true;
    } while (token(cursor, ','));
    return false;
}

static bool baselines(const char **cursor) {
    if (!token(cursor, '{') || token(cursor, '}'))
        return false;
    do {
        char key[TEXT_CAPACITY];
        char *end;
        uint8_t value;
        if (!string_value(cursor, key, sizeof(key)) || !token(cursor, ':'))
            return false;
        errno = 0;
        unsigned long offset = strtoul(key, &end, 0);
        if (errno || end == key || *end || offset >= REGISTER_COUNT ||
            state.expected_set[offset] || !byte_value(cursor, &value))
            return false;
        state.expected_set[offset] = true;
        state.monitored[offset] = true;
        state.expected[offset] = value;
        if (token(cursor, '}'))
            return true;
    } while (token(cursor, ','));
    return false;
}

static bool rules(const char **cursor) {
    if (!token(cursor, '[') || token(cursor, ']'))
        return false;
    do {
        uint8_t offset = 0, values[REGISTER_COUNT];
        size_t value_count = 0;
        unsigned seen = 0;
        if (!token(cursor, '{'))
            return false;
        do {
            char key[TEXT_CAPACITY], description[TEXT_CAPACITY];
            unsigned field;
            if (!string_value(cursor, key, sizeof(key)) || !token(cursor, ':'))
                return false;
            if (!strcmp(key, "offset"))
                field = 1;
            else if (!strcmp(key, "allowed_values"))
                field = 2;
            else if (!strcmp(key, "description"))
                field = 4;
            else
                return false;
            if (seen & field)
                return false;
            seen |= field;
            if ((field == 1 && !byte_value(cursor, &offset)) ||
                (field == 2 && !byte_array(cursor, values, &value_count)) ||
                (field == 4 && !string_value(cursor, description, sizeof(description))))
                return false;
            if (token(cursor, '}'))
                break;
            if (!token(cursor, ','))
                return false;
        } while (true);
        if ((seen & 3) != 3 || !value_count || state.allowed[offset] ||
            offset == EC_GUARD_FORBIDDEN_REGISTER)
            return false;
        state.allowed[offset] = true;
        state.monitored[offset] = true;
        for (size_t index = 0; index < value_count; ++index)
            state.values[offset][values[index]] = true;
        if (token(cursor, ']'))
            return true;
    } while (token(cursor, ','));
    return false;
}

static bool parse_policy(const char *cursor) {
    unsigned seen = 0;
    if (!token(&cursor, '{'))
        return false;
    do {
        char key[TEXT_CAPACITY], description[TEXT_CAPACITY];
        unsigned field;
        uint8_t entries[REGISTER_COUNT], version;
        size_t count;
        if (!string_value(&cursor, key, sizeof(key)) || !token(&cursor, ':'))
            return false;
        if (!strcmp(key, "description")) field = 1;
        else if (!strcmp(key, "version")) field = 2;
        else if (!strcmp(key, "expected_baselines")) field = 4;
        else if (!strcmp(key, "protected_monitored_offsets")) field = 8;
        else if (!strcmp(key, "restoration_order")) field = 16;
        else if (!strcmp(key, "allowed_writes")) field = 32;
        else if (!strcmp(key, "handoff_offsets")) field = 64;
        else return false;
        if (seen & field)
            return false;
        seen |= field;
        switch (field) {
        case 1:
            if (!string_value(&cursor, description, sizeof(description))) return false;
            break;
        case 2:
            if (!byte_value(&cursor, &version) || version != 1) return false;
            break;
        case 4:
            if (!baselines(&cursor)) return false;
            break;
        case 8:
            if (!byte_array(&cursor, entries, &count) || !count) return false;
            for (size_t index = 0; index < count; ++index)
                state.monitored[entries[index]] = true;
            break;
        case 16:
            if (!byte_array(&cursor, state.order, &state.order_count)) return false;
            break;
        case 32:
            if (!rules(&cursor)) return false;
            break;
        case 64:
            if (!byte_array(&cursor, state.handoff, &state.handoff_count)) return false;
            break;
        }
        if (token(&cursor, '}'))
            break;
        if (!token(&cursor, ','))
            return false;
    } while (true);
    whitespace(&cursor);
    if (*cursor || (seen & 126) != 126 || !state.handoff_count ||
        !state.monitored[EC_GUARD_FORBIDDEN_REGISTER])
        return false;
    bool ordered[REGISTER_COUNT] = {false};
    for (size_t index = 0; index < state.order_count; ++index)
        ordered[state.order[index]] = true;
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        if (state.allowed[offset] != ordered[offset] ||
            (state.monitored[offset] && !state.expected_set[offset]))
            return false;
    }
    if (state.handoff_count > state.order_count)
        return false;
    for (size_t index = 0; index < state.handoff_count; ++index)
        if (state.handoff[index] != state.order[index])
            return false;
    return true;
}

static int audit(const char *action, off_t offset, size_t count,
                 const uint8_t *old, const uint8_t *proposed, const char *context) {
    char line[AUDIT_CAPACITY], escaped[TEXT_CAPACITY], old_text[32], new_text[32];
    size_t position = 0;
    if (!context)
        context = "unknown";
    for (size_t index = 0; context[index] && position + 3 < sizeof(escaped); ++index) {
        unsigned char value = (unsigned char)context[index];
        if (value < ' ' || value > '~') value = '?';
        if (value == '"' || value == '\\') escaped[position++] = '\\';
        escaped[position++] = (char)value;
    }
    escaped[position] = '\0';
    if (count == 2) {
        snprintf(old_text, sizeof(old_text), "[\"0x%02X\",\"0x%02X\"]",
                 old ? old[0] : 0, old ? old[1] : 0);
        snprintf(new_text, sizeof(new_text), "[\"0x%02X\",\"0x%02X\"]",
                 proposed ? proposed[0] : 0, proposed ? proposed[1] : 0);
    } else {
        snprintf(old_text, sizeof(old_text), "[\"0x%02X\"]", old ? old[0] : 0);
        snprintf(new_text, sizeof(new_text), "[\"0x%02X\"]", proposed ? proposed[0] : 0);
    }
    int length = snprintf(line, sizeof(line),
        "{\"timestamp\":%lld,\"action\":\"%s\",\"offset\":\"0x%02lX\","
        "\"count\":%zu,\"old\":%s,\"new\":%s,\"reason\":\"%s\",\"context\":\"%s\"}\n",
        (long long)time(NULL), action, (unsigned long)offset, count,
        old_text, new_text, !strcmp(action, "DENIED") ? escaped : "", escaped);
    if (state.log_descriptor < 0 || length <= 0 || (size_t)length >= sizeof(line))
        return EC_GUARD_ERROR_IO;
    size_t written = 0;
    while (written < (size_t)length) {
        ssize_t result = write(state.log_descriptor, line + written, (size_t)length - written);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0) return EC_GUARD_ERROR_IO;
        written += (size_t)result;
    }
    int result;
    do { result = fsync(state.log_descriptor); } while (result < 0 && errno == EINTR);
    return result == 0 ? EC_GUARD_STATUS_SUCCESS : EC_GUARD_ERROR_IO;
}

int ec_guard_init(const char *config_path, const char *log_path) {
    ec_guard_cleanup();
    if (!config_path || !log_path) return EC_GUARD_ERROR_PARAM;
    int descriptor = open(config_path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return EC_GUARD_ERROR_IO;
    char content[POLICY_CAPACITY];
    ssize_t count = read(descriptor, content, sizeof(content));
    close(descriptor);
    if (count <= 0 || count >= (ssize_t)sizeof(content) || memchr(content, '\0', (size_t)count))
        return EC_GUARD_ERROR_PARAM;
    content[count] = '\0';
    if (!parse_policy(content)) {
        ec_guard_cleanup();
        return EC_GUARD_ERROR_PARAM;
    }
    state.log_descriptor = open(log_path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (state.log_descriptor < 0) {
        ec_guard_cleanup();
        return EC_GUARD_ERROR_IO;
    }
    struct stat information;
    if (fstat(state.log_descriptor, &information) || !S_ISREG(information.st_mode)) {
        ec_guard_cleanup();
        return EC_GUARD_ERROR_IO;
    }
    state.initialized = true;
    return EC_GUARD_STATUS_SUCCESS;
}

int ec_guard_capture_snapshot(int descriptor) {
    if (!state.initialized || descriptor < 0) return EC_GUARD_ERROR_STATE;
    if (state.captured) return EC_GUARD_STATUS_SUCCESS;
    struct stat information;
    if (fstat(descriptor, &information)) return EC_GUARD_ERROR_IO;
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        if (!state.monitored[offset]) continue;
        if (pread(descriptor, &state.original[offset], 1, (off_t)offset) != 1)
            return EC_GUARD_ERROR_IO;
        if (state.original[offset] != state.expected[offset])
            return EC_GUARD_ERROR_STATE;
    }
    state.device = information.st_dev;
    state.inode = information.st_ino;
    state.captured = true;
    return EC_GUARD_STATUS_SUCCESS;
}

/* Both active writes and restoration reach the only pwrite call here. */
static int checked_write(int descriptor, const void *buffer, size_t count,
                         off_t offset, const char *context, bool restoring) {
    uint8_t old[2] = {0};
    const uint8_t *proposed = buffer;
    if (!state.initialized || descriptor < 0 || !buffer || count < 1 || count > 2 ||
        offset < 0 || offset >= REGISTER_COUNT || count > REGISTER_COUNT - (size_t)offset)
        return EC_GUARD_ERROR_PARAM;
    for (size_t index = 0; index < count; ++index) {
        size_t target = (size_t)offset + index;
        if (!state.allowed[target] || target == EC_GUARD_FORBIDDEN_REGISTER ||
            (!restoring && !state.values[target][proposed[index]]) ||
            (restoring && (!state.captured || proposed[index] != state.original[target]))) {
            audit("DENIED", offset, count, NULL, proposed,
                  target == EC_GUARD_FORBIDDEN_REGISTER
                      ? "forbidden_unlock_register_0x03" : "offset_or_value_not_allowed");
            return EC_GUARD_ERROR_DENIED;
        }
    }
    if (!state.captured && ec_guard_capture_snapshot(descriptor) != EC_GUARD_STATUS_SUCCESS)
        return EC_GUARD_ERROR_STATE;
    struct stat information;
    if (fstat(descriptor, &information) || information.st_dev != state.device ||
        information.st_ino != state.inode)
        return EC_GUARD_ERROR_STATE;
    if (pread(descriptor, old, count, offset) != (ssize_t)count && !restoring)
        return EC_GUARD_ERROR_IO;
    int log_status = audit("PROPOSED", offset, count, old, proposed, context);
    if (log_status != EC_GUARD_STATUS_SUCCESS && !restoring)
        return log_status;
    if (log_status != EC_GUARD_STATUS_SUCCESS)
        fprintf(stderr, "ec_guard: audit failed; restoring baseline at 0x%02lX\n", (unsigned long)offset);
    ssize_t written = pwrite(descriptor, buffer, count, offset);
    int result_log = audit(written == (ssize_t)count ? (restoring ? "RESTORED" : "ALLOWED") : "IO_ERROR",
                           offset, count, old, proposed, context);
    if (written != (ssize_t)count || log_status || result_log)
        return EC_GUARD_ERROR_IO;
    return EC_GUARD_STATUS_SUCCESS;
}

int ec_guard_pwrite(int descriptor, const void *buffer, size_t count, off_t offset, const char *context) {
    return checked_write(descriptor, buffer, count, offset, context, false);
}

int ec_guard_write_byte(int descriptor, uint8_t offset, uint8_t value, const char *context) {
    return ec_guard_pwrite(descriptor, &value, 1, offset, context);
}

int ec_guard_write_word(int descriptor, uint8_t offset, uint16_t value, const char *context) {
    uint8_t bytes[2] = {(uint8_t)(value & 255), (uint8_t)(value >> 8)};
    return ec_guard_pwrite(descriptor, bytes, 2, offset, context);
}

int ec_guard_restore_snapshot(int descriptor) {
    if (!state.initialized || !state.captured) return EC_GUARD_ERROR_STATE;
    bool errors = false;
    for (size_t index = 0; index < state.order_count; ++index) {
        /* If mode restoration fails, leave maximum targets in place. */
        if (index == state.handoff_count) {
            for (size_t handoff = 0; handoff < state.handoff_count; ++handoff) {
                uint8_t offset = state.handoff[handoff], current;
                if (pread(descriptor, &current, 1, offset) != 1 || current != state.original[offset])
                    return EC_GUARD_ERROR_IO;
            }
        }
        uint8_t offset = state.order[index], current;
        if (pread(descriptor, &current, 1, offset) == 1 && current == state.original[offset])
            continue;
        if (checked_write(descriptor, &state.original[offset], 1, offset, "restore_snapshot", true))
            errors = true;
    }
    return errors ? EC_GUARD_ERROR_IO : EC_GUARD_STATUS_SUCCESS;
}

int ec_guard_verify_snapshot(int descriptor) {
    if (!state.initialized || !state.captured) return EC_GUARD_ERROR_STATE;
    int mismatches = 0;
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        uint8_t current;
        if (state.monitored[offset] &&
            (pread(descriptor, &current, 1, (off_t)offset) != 1 || current != state.original[offset]))
            ++mismatches;
    }
    return -mismatches;
}

void ec_guard_cleanup(void) {
    if (state.log_descriptor >= 0) close(state.log_descriptor);
    memset(&state, 0, sizeof(state));
    state.log_descriptor = -1;
}

int ec_guard_recover_from_baselines(int descriptor) {
    if (!state.initialized || descriptor < 0)
        return EC_GUARD_ERROR_STATE;
    struct stat information;
    if (fstat(descriptor, &information))
        return EC_GUARD_ERROR_IO;

    /* 1. Verify protected monitored offsets (unwritable) match expected baseline exactly */
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        if (!state.monitored[offset] || state.allowed[offset])
            continue;
        uint8_t current = 0;
        if (pread(descriptor, &current, 1, (off_t)offset) != 1)
            return EC_GUARD_ERROR_IO;
        if (current != state.expected[offset]) {
            audit("DENIED", (off_t)offset, 1, &current, NULL, "recovery_protected_offset_mismatch");
            return EC_GUARD_ERROR_STATE;
        }
    }

    /* 2. Verify all allowed writable registers hold either expected baseline or allowed write value */
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        if (!state.allowed[offset])
            continue;
        uint8_t current = 0;
        if (pread(descriptor, &current, 1, (off_t)offset) != 1)
            return EC_GUARD_ERROR_IO;
        bool is_baseline = (state.expected_set[offset] && current == state.expected[offset]);
        bool is_allowed = state.values[offset][current];
        if (!is_baseline && !is_allowed) {
            audit("DENIED", (off_t)offset, 1, &current, NULL, "recovery_register_value_unsafe");
            return EC_GUARD_ERROR_DENIED;
        }
    }

    /* 3. Reconstruct saved originals strictly from expected_baselines */
    for (size_t offset = 0; offset < REGISTER_COUNT; ++offset) {
        if (state.expected_set[offset]) {
            state.original[offset] = state.expected[offset];
        }
    }
    state.device = information.st_dev;
    state.inode = information.st_ino;
    state.captured = true;

    /* 4. Restore snapshot: restores modes first, then speed targets, via guarded writer */
    int restore_status = ec_guard_restore_snapshot(descriptor);
    if (restore_status != EC_GUARD_STATUS_SUCCESS)
        return restore_status;

    /* 5. Verify snapshot matches */
    int verify_status = ec_guard_verify_snapshot(descriptor);
    return verify_status == 0 ? EC_GUARD_STATUS_SUCCESS : EC_GUARD_ERROR_IO;
}

int ec_guard_validate_synthetic_target(const char *path, const char *root) {
    char resolved_path[PATH_MAX], resolved_root[PATH_MAX];
    struct stat information;
    if (!path || !root || !realpath(path, resolved_path) || !realpath(root, resolved_root) ||
        lstat(path, &information) || !S_ISREG(information.st_mode) || information.st_size != REGISTER_COUNT)
        return EC_GUARD_ERROR_PARAM;
    size_t length = strlen(resolved_root);
    if (length < 2 || strncmp(resolved_path, resolved_root, length) || resolved_path[length] != '/')
        return EC_GUARD_ERROR_DENIED;
    return EC_GUARD_STATUS_SUCCESS;
}
