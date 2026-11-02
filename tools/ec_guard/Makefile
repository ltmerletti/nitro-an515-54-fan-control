CC ?= cc
CFLAGS ?= -Wall -Wextra -Werror -pedantic -std=c99 -fPIC -O2
LDFLAGS ?=

UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
    SHARED_LIB = libnbfc_ec_guard.dylib
    SHARED_FLAGS = -dynamiclib
else
    SHARED_LIB = libnbfc_ec_guard.so
    SHARED_FLAGS = -shared
endif

all: $(SHARED_LIB) ec_guard.o test_ec_guard

ec_guard.o: ec_guard.c ec_guard.h
	$(CC) $(CFLAGS) -c ec_guard.c -o ec_guard.o

$(SHARED_LIB): ec_guard.c ec_guard.h
	$(CC) $(CFLAGS) $(SHARED_FLAGS) ec_guard.c -o $(SHARED_LIB)

test_ec_guard: ../../tests/test_ec_guard.c ec_guard.o
	$(CC) $(CFLAGS) ../../tests/test_ec_guard.c ec_guard.o -o test_ec_guard

test: all
	./test_ec_guard

clean:
	rm -f *.o *.so *.dylib test_ec_guard
