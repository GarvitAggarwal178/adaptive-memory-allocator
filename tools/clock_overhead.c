/* Measures the cost of an empty timed interval (two clock_gettime calls). */
#define _POSIX_C_SOURCE 200809L

#include "../src/check.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static uint64_t now_ns(void)
{
    struct timespec t;
    CHECK(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
    return (uint64_t)t.tv_sec * 1000000000ULL + (uint64_t)t.tv_nsec;
}

static int compare_u64(const void *x, const void *y)
{
    uint64_t p = *(const uint64_t *)x, q = *(const uint64_t *)y;
    return (p > q) - (p < q);
}

int main(void)
{
    enum { SAMPLES = 100000 };
    uint64_t *v = malloc(SAMPLES * sizeof *v);
    CHECK(v != NULL);

    for (int i = 0; i < SAMPLES; ++i) {
        uint64_t t0 = now_ns();
        v[i] = now_ns() - t0;
    }
    qsort(v, SAMPLES, sizeof *v, compare_u64);

    printf("{\"empty_interval_median_ns\":%.1f,\"empty_interval_p99_ns\":%llu,\"samples\":%d}\n",
           0.5 * (double)(v[SAMPLES / 2 - 1] + v[SAMPLES / 2]),
           (unsigned long long)v[SAMPLES * 99 / 100 - 1], SAMPLES);
    free(v);
    return 0;
}
