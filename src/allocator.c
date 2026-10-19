#include "allocator.h"

#include <stdlib.h>
#include <string.h>

const char *const policy_names[POLICY_COUNT] = {
    "FF", "BF", "WF", "NF", "Adaptive", "RandomSwitch",
};

void allocator_init(Allocator *a, int region_size, int max_pid, Policy policy)
{
    CHECK(region_size > 0 && max_pid > 0);
    memset(a, 0, sizeof(*a));

    a->region_size = region_size;
    a->free_total = region_size;
    a->max_pid = max_pid;
    a->policy = policy;
    /* The two switching policies start in First Fit until their first decision. */
    a->mode = (policy == ADAPTIVE || policy == RANDOM_SWITCH) ? FF : policy;

    a->window = 50;
    a->theta = 0.5;
    a->phi = 0.1;

    /* There can never be more than region_size holes of size >= 1. */
    a->holes = calloc((size_t)region_size + 1, sizeof(Hole));
    a->blocks = calloc((size_t)max_pid, sizeof(Block));
    CHECK(a->holes != NULL && a->blocks != NULL);

    a->holes[0] = (Hole){0, region_size};
    a->hole_count = 1;
}

void allocator_destroy(Allocator *a)
{
    free(a->holes);
    free(a->blocks);
}

/* First hole whose end lies beyond the rover; wraps to 0 if there is none. */
static int next_fit_start(const Hole *holes, int hole_count, int rover)
{
    int lo = 0, hi = hole_count;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        if (holes[mid].start + holes[mid].size <= rover)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo == hole_count ? 0 : lo;
}

/*
 * A probe is one read of a candidate hole's size. Locating the Next Fit start
 * hole is not counted: a real allocator would keep a direct pointer to it.
 */
int fit_search(const Hole *holes, int hole_count, int size, Policy mode, int rover, int *probes)
{
    int first = (mode == NF) ? next_fit_start(holes, hole_count, rover) : 0;
    int best = -1;
    int best_size = 0;

    *probes = 0;
    for (int step = 0; step < hole_count; ++step) {
        int i = (first + step) % hole_count;
        int candidate = holes[i].size;
        ++*probes;

        if (candidate < size)
            continue;
        if (mode == FF || mode == NF)
            return i;
        if (mode == BF && candidate == size)
            return i;   /* an exact fit cannot be beaten */

        /* Strict comparisons keep the lowest address on ties. */
        if (best < 0 ||
            (mode == BF && candidate < best_size) ||
            (mode == WF && candidate > best_size)) {
            best = i;
            best_size = candidate;
        }
    }
    return best;
}

int largest_hole(const Allocator *a)
{
    int largest = 0;
    for (int i = 0; i < a->hole_count; ++i)
        if (a->holes[i].size > largest)
            largest = a->holes[i].size;
    return largest;
}

/*
 * Adaptive rule, evaluated once per window. F = 1 - L/T measures how scattered
 * the free space is. Best Fit is chosen when free space is scattered (F high)
 * or scarce (T/M low); otherwise the cheaper First Fit is used.
 * The scan that finds L is charged to the attempt as selector probes.
 */
static Policy adaptive_decision(const Allocator *a, int *selector_probes)
{
    int largest = largest_hole(a);
    *selector_probes = a->hole_count;

    if (a->free_total == 0)
        return BF;

    double dispersion = 1.0 - (double)largest / a->free_total;
    double free_fraction = (double)a->free_total / a->region_size;
    double threshold = a->theta;
    if (a->hysteresis && a->mode == BF)
        threshold -= 0.1;

    return (dispersion >= threshold || free_fraction < a->phi) ? BF : FF;
}

/* Remove holes[i], shifting the tail left. */
static void remove_hole(Allocator *a, int i)
{
    memmove(&a->holes[i], &a->holes[i + 1], (size_t)(a->hole_count - i - 1) * sizeof(Hole));
    --a->hole_count;
}

AllocResult allocate_block(Allocator *a, int pid, int size, double random_u)
{
    CHECK(pid >= 0 && pid < a->max_pid && size > 0 && !a->blocks[pid].active);

    AllocResult r = {ALLOC_OK, -1, 0, 0, FF};

    if (a->attempt % a->window == 0) {
        if (a->policy == ADAPTIVE)
            a->mode = adaptive_decision(a, &r.selector_probes);
        else if (a->policy == RANDOM_SWITCH)
            a->mode = (random_u < a->p_bf) ? BF : FF;
    }
    ++a->attempt;
    r.mode = a->mode;

    /* O(1) pre-check, identical for every policy, so it costs no probes. */
    if (a->free_total < size) {
        r.status = FAIL_CAPACITY;
        return r;
    }

    int i = fit_search(a->holes, a->hole_count, size, a->mode, a->rover, &r.search_probes);
    if (i < 0) {
        r.status = FAIL_FRAGMENTATION;
        return r;
    }

    /* Take the low end of the hole; the remainder stays free. */
    r.start = a->holes[i].start;
    a->blocks[pid] = (Block){r.start, size, 1};
    a->free_total -= size;
    a->holes[i].start += size;
    a->holes[i].size -= size;
    if (a->holes[i].size == 0)
        remove_hole(a, i);

    if (a->mode == NF)
        a->rover = r.start + size;
    return r;
}

int release_block(Allocator *a, int pid)
{
    if (pid < 0 || pid >= a->max_pid || !a->blocks[pid].active)
        return -1;

    Block b = a->blocks[pid];
    a->blocks[pid].active = 0;
    a->free_total += b.size;

    /* Find the insertion point that keeps holes sorted by address. */
    int lo = 0, hi = a->hole_count;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        if (a->holes[mid].start < b.start)
            lo = mid + 1;
        else
            hi = mid;
    }
    int i = lo;
    memmove(&a->holes[i + 1], &a->holes[i], (size_t)(a->hole_count - i) * sizeof(Hole));
    a->holes[i] = (Hole){b.start, b.size};
    ++a->hole_count;

    /* Coalesce with the left neighbour, then with the right one. */
    if (i > 0 && a->holes[i - 1].start + a->holes[i - 1].size == a->holes[i].start) {
        a->holes[i - 1].size += a->holes[i].size;
        remove_hole(a, i);
        --i;
    }
    if (i + 1 < a->hole_count && a->holes[i].start + a->holes[i].size == a->holes[i + 1].start) {
        a->holes[i].size += a->holes[i + 1].size;
        remove_hole(a, i + 1);
    }
    return 0;
}

static int compare_start(const void *x, const void *y)
{
    const Hole *p = x, *q = y;
    return (p->start > q->start) - (p->start < q->start);
}

void check_invariants(const Allocator *a)
{
    /* Holes: positive size, sorted, never touching (otherwise they should have merged). */
    int free_sum = 0;
    for (int i = 0; i < a->hole_count; ++i) {
        CHECK(a->holes[i].size > 0 && a->holes[i].start >= 0);
        if (i > 0)
            CHECK(a->holes[i - 1].start + a->holes[i - 1].size < a->holes[i].start);
        free_sum += a->holes[i].size;
    }
    CHECK(free_sum == a->free_total);

    /* Holes and live blocks together must tile [0, M) with no gap or overlap. */
    int parts_count = a->hole_count;
    for (int pid = 0; pid < a->max_pid; ++pid)
        parts_count += a->blocks[pid].active;

    Hole *parts = malloc((size_t)(parts_count + 1) * sizeof(Hole));
    CHECK(parts != NULL);
    memcpy(parts, a->holes, (size_t)a->hole_count * sizeof(Hole));
    int n = a->hole_count;
    for (int pid = 0; pid < a->max_pid; ++pid) {
        if (!a->blocks[pid].active)
            continue;
        CHECK(a->blocks[pid].size > 0);
        parts[n++] = (Hole){a->blocks[pid].start, a->blocks[pid].size};
    }
    qsort(parts, (size_t)n, sizeof(Hole), compare_start);

    int expected_start = 0;
    for (int i = 0; i < n; ++i) {
        CHECK(parts[i].start == expected_start);
        expected_start += parts[i].size;
    }
    CHECK(expected_start == a->region_size);
    free(parts);
}
