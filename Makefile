CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Werror -pedantic
SAN     := -std=c11 -O1 -g -Wall -Wextra -Werror -pedantic -fsanitize=address,undefined -fno-omit-frame-pointer

LIB_SRC := src/allocator.c
HEADERS := src/allocator.h src/check.h

all: build/simulator build/test_allocator build/demo build/clock_overhead

build:
	mkdir -p build

build/simulator: $(LIB_SRC) src/simulator.c $(HEADERS) | build
	$(CC) $(CFLAGS) $(LIB_SRC) src/simulator.c -o $@

build/test_allocator: $(LIB_SRC) tests/test_allocator.c $(HEADERS) | build
	$(CC) $(CFLAGS) -Isrc $(LIB_SRC) tests/test_allocator.c -o $@

build/demo: $(LIB_SRC) examples/demo.c $(HEADERS) | build
	$(CC) $(CFLAGS) -Isrc $(LIB_SRC) examples/demo.c -o $@

build/clock_overhead: tools/clock_overhead.c src/check.h | build
	$(CC) $(CFLAGS) tools/clock_overhead.c -o $@

test: build/test_allocator
	./build/test_allocator

demo: build/demo
	./build/demo

# Leak detection fails inside some containers; set ASAN_OPTIONS=detect_leaks=0 there.
sanitize: | build
	$(CC) $(SAN) -Isrc $(LIB_SRC) tests/test_allocator.c -o build/test_sanitize
	./build/test_sanitize

clean:
	rm -rf build

.PHONY: all test demo sanitize clean
