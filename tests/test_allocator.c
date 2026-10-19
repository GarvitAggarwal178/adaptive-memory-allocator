/* Unit tests for the allocator. Run with `make test`. */
#include "allocator.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/*
 * Region of 352 units with holes of 120, 70 and 160 at addresses 0, 121, 192,
 * separated by two one-unit live blocks (pids 18 and 19).
 */
static void three_hole_fixture(Allocator *a, Policy policy)
{
    allocator_init(a, 352, 20, policy);
    a->holes[0] = (Hole){0, 120};
    a->holes[1] = (Hole){121, 70};
    a->holes[2] = (Hole){192, 160};
    a->hole_count = 3;
    a->free_total = 350;
    a->blocks[18] = (Block){120, 1, 1};
    a->blocks[19] = (Block){191, 1, 1};
    check_invariants(a);
}

/* The worked example in the report: requests 60, 110, 130. */
static void test_hand_example(void)
{
    Allocator a;
    AllocResult r;

    three_hole_fixture(&a, FF);
    r = allocate_block(&a, 0, 60, 0);
    CHECK(r.start == 0 && r.search_probes == 1);
    r = allocate_block(&a, 1, 110, 0);
    CHECK(r.start == 192 && r.search_probes == 3);
    r = allocate_block(&a, 2, 130, 0);          /* 180 units free, largest hole 70 */
    CHECK(r.status == FAIL_FRAGMENTATION && r.search_probes == 3);
    check_invariants(&a);
    allocator_destroy(&a);

    three_hole_fixture(&a, BF);
    r = allocate_block(&a, 0, 60, 0);
    CHECK(r.start == 121 && r.search_probes == 3);
    r = allocate_block(&a, 1, 110, 0);
    CHECK(r.start == 0 && r.search_probes == 3);
    r = allocate_block(&a, 2, 130, 0);
    CHECK(r.start == 192 && r.search_probes == 3);
    check_invariants(&a);
    allocator_destroy(&a);

    puts("PASS hand example (FF fails the third request, BF serves it)");
}

/* Three adjacent blocks released in every order must merge back into one hole. */
static void test_coalescing(void)
{
    const int orders[6][3] = {{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}};

    for (int o = 0; o < 6; ++o) {
        Allocator a;
        allocator_init(&a, 90, 3, FF);
        for (int pid = 0; pid < 3; ++pid)
            CHECK(allocate_block(&a, pid, 30, 0).status == ALLOC_OK);
        for (int j = 0; j < 3; ++j) {
            CHECK(release_block(&a, orders[o][j]) == 0);
            check_invariants(&a);
        }
        CHECK(a.hole_count == 1 && a.holes[0].size == 90 && a.free_total == 90);
        CHECK(release_block(&a, 0) == -1);   /* double release is rejected */
        allocator_destroy(&a);
    }
    puts("PASS coalescing in all six release orders, double release rejected");
}

static void test_fit_rules(void)
{
    Hole h[3] = {{0, 50}, {70, 50}, {150, 90}};
    int probes;

    CHECK(fit_search(h, 3, 50, BF, 0, &probes) == 0 && probes == 1);    /* exact fit stops early */
    CHECK(fit_search(h, 3, 40, BF, 0, &probes) == 0 && probes == 3);    /* tie -> lowest address */
    CHECK(fit_search(h, 3, 40, WF, 0, &probes) == 2 && probes == 3);
    CHECK(fit_search(h, 3, 40, NF, 80, &probes) == 1 && probes == 1);   /* rover inside hole 1 */
    CHECK(fit_search(h, 3, 40, NF, 240, &probes) == 0 && probes == 1);  /* rover past the end wraps */
    CHECK(fit_search(h, 3, 40, NF, 50, &probes) == 1 && probes == 1);   /* rover on a boundary */
    CHECK(fit_search(h, 3, 100, NF, 80, &probes) == -1 && probes == 3); /* each hole once */

    /* A hole merged across the rover is examined whole, from its low end. */
    Allocator a;
    allocator_init(&a, 100, 4, NF);
    for (int pid = 0; pid < 3; ++pid)
        CHECK(allocate_block(&a, pid, 20, 0).status == ALLOC_OK);
    CHECK(release_block(&a, 1) == 0);
    CHECK(release_block(&a, 2) == 0);
    CHECK(a.rover == 60 && a.holes[0].start == 20 && a.holes[0].size == 80);
    CHECK(allocate_block(&a, 3, 30, 0).start == 20);
    check_invariants(&a);
    allocator_destroy(&a);

    puts("PASS fit rules, tie-breaks and Next Fit rover boundaries");
}

static void test_failure_classes(void)
{
    for (int p = 0; p < POLICY_COUNT; ++p) {
        Allocator a;
        three_hole_fixture(&a, (Policy)p);
        AllocResult r = allocate_block(&a, 0, 351, 0);     /* more than T = 350 */
        CHECK(r.status == FAIL_CAPACITY && r.search_probes == 0);
        r = allocate_block(&a, 1, 200, 0);                 /* T is enough, no hole is */
        CHECK(r.status == FAIL_FRAGMENTATION && r.search_probes == 3);
        check_invariants(&a);
        allocator_destroy(&a);
    }

    /* Adaptive with memory completely full picks BF and scans zero holes. */
    Allocator a;
    allocator_init(&a, 10, 3, ADAPTIVE);
    a.window = 1;
    CHECK(allocate_block(&a, 0, 10, 0).status == ALLOC_OK);
    AllocResult r = allocate_block(&a, 1, 1, 0);
    CHECK(r.status == FAIL_CAPACITY && r.mode == BF && r.selector_probes == 0);
    check_invariants(&a);
    allocator_destroy(&a);

    puts("PASS capacity vs fragmentation classification");
}

static void test_adaptive_window(void)
{
    Allocator a;
    AllocResult r;

    /* Fixture F = 1 - 160/350 = 0.543. Decisions happen only at attempts 0, 3, 6, ... */
    three_hole_fixture(&a, ADAPTIVE);
    a.theta = 0.6;
    a.phi = 0;
    a.window = 3;
    r = allocate_block(&a, 0, 1, 0);
    CHECK(r.mode == FF && r.selector_probes == 3);
    a.theta = 0.1;                               /* would now choose BF ... */
    r = allocate_block(&a, 1, 1, 0);
    CHECK(r.mode == FF && r.selector_probes == 0);   /* ... but not mid-window */
    r = allocate_block(&a, 2, 1, 0);
    CHECK(r.mode == FF && r.selector_probes == 0);
    r = allocate_block(&a, 3, 1, 0);
    CHECK(r.mode == BF && r.selector_probes == 3);
    allocator_destroy(&a);

    /* Hysteresis: already in BF, threshold drops to 0.5, so F = 0.543 keeps BF. */
    three_hole_fixture(&a, ADAPTIVE);
    a.window = 1;
    a.theta = 0.6;
    a.phi = 0;
    a.hysteresis = 1;
    a.mode = BF;
    CHECK(allocate_block(&a, 0, 1, 0).mode == BF);
    allocator_destroy(&a);

    puts("PASS adaptive window timing, scan cost and hysteresis");
}

/*
 * Differential test against a per-unit ownership map that knows nothing about
 * the free list. Random allocations and releases; First Fit placement must agree
 * with "lowest run of `size` free units" on every attempt.
 */
static uint32_t lcg_state = 7;
static unsigned lcg_next(void)
{
    lcg_state = 1664525U * lcg_state + 1013904223U;
    return lcg_state >> 16;   /* the low bits of an LCG are poor */
}

static void test_against_unit_map(void)
{
    enum { UNITS = 256, STEPS = 1000 };
    Allocator a;
    allocator_init(&a, UNITS, STEPS, FF);

    int owner[UNITS];
    int ref_start[STEPS], ref_size[STEPS];
    int successes = 0, releases = 0, live = 0;
    for (int x = 0; x < UNITS; ++x)
        owner[x] = -1;
    for (int k = 0; k < STEPS; ++k)
        ref_start[k] = -1;

    for (int k = 0; k < STEPS; ++k) {
        /* With probability 0.75, release a random live block. */
        if (live > 0 && lcg_next() % 100 < 75) {
            int rank = (int)(lcg_next() % (unsigned)live), pid = -1;
            for (int j = 0; j < k; ++j)
                if (ref_start[j] >= 0 && rank-- == 0) {
                    pid = j;
                    break;
                }
            CHECK(pid >= 0 && release_block(&a, pid) == 0);
            for (int x = ref_start[pid]; x < ref_start[pid] + ref_size[pid]; ++x) {
                CHECK(owner[x] == pid);
                owner[x] = -1;
            }
            ref_start[pid] = -1;
            --live;
            ++releases;
        }

        int size = 1 + (int)(lcg_next() % 32);
        int expected = -1, run = 0;
        for (int x = 0; x < UNITS; ++x) {
            run = owner[x] == -1 ? run + 1 : 0;
            if (run == size) {
                expected = x - size + 1;
                break;
            }
        }

        AllocResult r = allocate_block(&a, k, size, 0);
        CHECK((r.status == ALLOC_OK) == (expected >= 0));
        CHECK(r.start == expected);
        if (expected >= 0) {
            for (int x = expected; x < expected + size; ++x)
                owner[x] = k;
            ref_start[k] = expected;
            ref_size[k] = size;
            ++successes;
            ++live;
        }
        check_invariants(&a);
    }
    CHECK(successes >= 600 && releases >= 600);
    printf("PASS %d random decisions match a unit map (%d successes, %d releases)\n",
           STEPS, successes, releases);
    allocator_destroy(&a);
}

int main(void)
{
    test_hand_example();
    test_coalescing();
    test_fit_rules();
    test_failure_classes();
    test_adaptive_window();
    test_against_unit_map();
    puts("ALL TESTS PASSED");
    return 0;
}
