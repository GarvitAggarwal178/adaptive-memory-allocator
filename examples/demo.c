/*
 * Prints the report's worked example for every fit policy and Adaptive:
 * holes of 120, 70 and 160 KiB, then requests of 60, 110 and 130 KiB.
 */
#include "allocator.h"

#include <stdio.h>

int main(void)
{
    const int layout[5] = {120, 1, 70, 1, 160};   /* hole, used, hole, used, hole */
    const int requests[3] = {60, 110, 130};

    for (int p = FF; p <= ADAPTIVE; ++p) {
        Allocator a;
        allocator_init(&a, 352, 20, FF);
        for (int i = 0; i < 5; ++i)
            CHECK(allocate_block(&a, i, layout[i], 0).status == ALLOC_OK);
        CHECK(release_block(&a, 0) == 0);
        CHECK(release_block(&a, 2) == 0);
        CHECK(release_block(&a, 4) == 0);

        a.policy = (Policy)p;
        a.mode = (p == ADAPTIVE) ? FF : (Policy)p;
        a.attempt = 0;
        a.theta = 0.7;
        a.phi = 0.2;

        printf("\n%s: holes [120, 70, 160]\n", policy_names[p]);
        for (int i = 0; i < 3; ++i) {
            AllocResult r = allocate_block(&a, 5 + i, requests[i], 0);
            const char *outcome = r.status == ALLOC_OK ? "placed at"
                                : r.status == FAIL_CAPACITY ? "capacity fail"
                                : "fragmentation fail";
            printf("  request %3d: %-18s %4d  (mode %s, probes %d + %d)  holes [",
                   requests[i], outcome, r.start, policy_names[r.mode],
                   r.search_probes, r.selector_probes);
            for (int j = 0; j < a.hole_count; ++j)
                printf("%s%d", j ? ", " : "", a.holes[j].size);
            puts("]");
            check_invariants(&a);
        }
        allocator_destroy(&a);
    }
    return 0;
}
