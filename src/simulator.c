/*
 * Trace-driven simulator. Replays one workload trace against one policy.
 *
 * Each tick k: apply every release due at time <= k (ties by pid), then try to
 * allocate request k. A successful request is released at k + lifetime; a
 * failed one is dropped. The run stops after the last arrival. The first
 * `warmup` attempts update memory but are excluded from the summary.
 *
 * Prints one JSON summary line to stdout; optionally writes a per-attempt CSV.
 */
#define _POSIX_C_SOURCE 200809L

#include "allocator.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    int pid, arrival, size, lifetime;
} Request;

typedef struct {
    const char *trace_path;
    const char *uniforms_path;   /* one U[0,1) draw per window, for RANDOM_SWITCH */
    const char *raw_path;        /* NULL: no per-attempt log */
    Policy policy;
    int region_size;
    int requests;
    int warmup;
    int window;
    double theta, phi, p_bf;
    int hysteresis;
    int check;                   /* 1: verify invariants after every event, no timing */
} Options;

/* ---- release queue: binary min-heap ordered by (time, pid) ---- */

typedef struct {
    int time, pid;
} Release;

typedef struct {
    Release *items;
    int count;
} ReleaseQueue;

static int due_before(Release x, Release y)
{
    return x.time < y.time || (x.time == y.time && x.pid < y.pid);
}

static void queue_push(ReleaseQueue *q, Release item)
{
    int i = q->count++;
    while (i > 0 && due_before(item, q->items[(i - 1) / 2])) {
        q->items[i] = q->items[(i - 1) / 2];
        i = (i - 1) / 2;
    }
    q->items[i] = item;
}

static Release queue_pop(ReleaseQueue *q)
{
    Release top = q->items[0];
    Release last = q->items[--q->count];
    int i = 0;
    while (2 * i + 1 < q->count) {
        int child = 2 * i + 1;
        if (child + 1 < q->count && due_before(q->items[child + 1], q->items[child]))
            ++child;
        if (!due_before(q->items[child], last))
            break;
        q->items[i] = q->items[child];
        i = child;
    }
    if (q->count > 0)
        q->items[i] = last;
    return top;
}

/* ---- helpers ---- */

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

static Policy parse_policy(const char *name)
{
    for (int p = 0; p < POLICY_COUNT; ++p)
        if (strcmp(name, policy_names[p]) == 0)
            return (Policy)p;
    fprintf(stderr, "unknown policy '%s'\n", name);
    exit(2);
}

static void usage(void)
{
    fprintf(stderr,
            "usage: simulator --trace FILE --uniforms FILE --policy FF|BF|WF|NF|Adaptive|RandomSwitch\n"
            "                 --memory M --requests N [--warmup K] [--window W]\n"
            "                 [--theta X] [--phi X] [--hysteresis 0|1] [--p-bf X]\n"
            "                 [--raw FILE] [--check]\n");
    exit(2);
}

static Options parse_options(int argc, char **argv)
{
    Options o = {0};
    o.policy = POLICY_COUNT;
    o.window = 50;
    o.theta = 0.5;
    o.phi = 0.1;

    for (int i = 1; i < argc; ++i) {
        const char *flag = argv[i];
        if (strcmp(flag, "--check") == 0) {
            o.check = 1;
            continue;
        }
        if (i + 1 >= argc)
            usage();
        const char *value = argv[++i];

        if (strcmp(flag, "--trace") == 0)            o.trace_path = value;
        else if (strcmp(flag, "--uniforms") == 0)    o.uniforms_path = value;
        else if (strcmp(flag, "--raw") == 0)         o.raw_path = value;
        else if (strcmp(flag, "--policy") == 0)      o.policy = parse_policy(value);
        else if (strcmp(flag, "--memory") == 0)      o.region_size = atoi(value);
        else if (strcmp(flag, "--requests") == 0)    o.requests = atoi(value);
        else if (strcmp(flag, "--warmup") == 0)      o.warmup = atoi(value);
        else if (strcmp(flag, "--window") == 0)      o.window = atoi(value);
        else if (strcmp(flag, "--theta") == 0)       o.theta = atof(value);
        else if (strcmp(flag, "--phi") == 0)         o.phi = atof(value);
        else if (strcmp(flag, "--hysteresis") == 0)  o.hysteresis = atoi(value);
        else if (strcmp(flag, "--p-bf") == 0)        o.p_bf = atof(value);
        else usage();
    }
    if (!o.trace_path || !o.uniforms_path || o.policy == POLICY_COUNT ||
        o.region_size <= 0 || o.requests <= o.warmup || o.warmup < 0 || o.window <= 0)
        usage();
    return o;
}

/* Trace format: header line, then "pid,arrival,size,lifetime,..." with pid == arrival == row index. */
static Request *load_trace(const char *path, int n)
{
    FILE *f = fopen(path, "r");
    CHECK(f != NULL);
    Request *req = calloc((size_t)n, sizeof(Request));
    CHECK(req != NULL);

    char line[512];
    CHECK(fgets(line, sizeof line, f) != NULL);    /* header */
    for (int k = 0; k < n; ++k) {
        Request *r = &req[k];
        CHECK(fgets(line, sizeof line, f) != NULL);
        CHECK(sscanf(line, "%d,%d,%d,%d", &r->pid, &r->arrival, &r->size, &r->lifetime) == 4);
        CHECK(r->pid == k && r->arrival == k && r->size > 0 && r->lifetime >= 1);
    }
    CHECK(fgets(line, sizeof line, f) == NULL);    /* no extra rows */
    fclose(f);
    return req;
}

/* ---- summary accumulators (measured attempts only) ---- */

typedef struct {
    double frag_failures, cap_failures;
    double dispersion_sum;        /* sum of F = 1 - L/T over states with T > 0 */
    double dispersion_undefined;  /* states with T == 0 */
    double utilization_sum, holes_sum, live_blocks_sum;
    double search_probes, selector_probes, bf_attempts;
    int switches, last_mode;
    uint64_t *alloc_ns;
    int timed;
    uint64_t release_ns_total;
    int releases_timed;
} Stats;

static void print_summary(const Stats *s, int measured)
{
    uint64_t *t = s->alloc_ns;
    int n = s->timed;
    double median = (n % 2) ? (double)t[n / 2] : 0.5 * ((double)t[n / 2 - 1] + (double)t[n / 2]);
    int p99_index = (99 * n + 99) / 100 - 1;   /* inverted-CDF 99th percentile */
    double defined = measured - s->dispersion_undefined;

    printf("{\"frag_fail_rate\":%.12g,\"cap_fail_rate\":%.12g,\"mean_F\":%.12g,"
           "\"frac_F_undefined\":%.12g,\"mean_util\":%.12g,\"mean_holes\":%.12g,"
           "\"mean_search_probes\":%.12g,\"mean_selector_probes\":%.12g,\"mean_total_probes\":%.12g,"
           "\"median_ns\":%.12g,\"p99_ns\":%llu,\"bf_share\":%.12g,\"switches\":%d,"
           "\"mean_live_blocks\":%.12g,\"mean_release_ns\":%.12g,\"attempts\":%d}\n",
           s->frag_failures / measured, s->cap_failures / measured,
           defined > 0 ? s->dispersion_sum / defined : 0, s->dispersion_undefined / measured,
           s->utilization_sum / measured, s->holes_sum / measured,
           s->search_probes / measured, s->selector_probes / measured,
           (s->search_probes + s->selector_probes) / measured,
           median, (unsigned long long)t[p99_index], s->bf_attempts / measured, s->switches,
           s->live_blocks_sum / measured,
           s->releases_timed ? (double)s->release_ns_total / s->releases_timed : 0, measured);
}

static const char *status_name(AllocStatus status)
{
    return status == FAIL_CAPACITY ? "capacity" : status == FAIL_FRAGMENTATION ? "fragmentation" : "ok";
}

int main(int argc, char **argv)
{
    Options opt = parse_options(argc, argv);
    int n = opt.requests;
    Request *req = load_trace(opt.trace_path, n);

    FILE *uniforms = fopen(opt.uniforms_path, "r");
    CHECK(uniforms != NULL);
    FILE *raw = NULL;
    if (opt.raw_path) {
        raw = fopen(opt.raw_path, "w");
        CHECK(raw != NULL);
        fprintf(raw, "attempt,time,pid,size,success,reason,start,search_probes,selector_probes,"
                     "mode,elapsed_ns,warmup,free_total,largest_free,n_holes,live_units\n");
    }

    Allocator a;
    allocator_init(&a, opt.region_size, n, opt.policy);
    a.theta = opt.theta;
    a.phi = opt.phi;
    a.hysteresis = opt.hysteresis;
    a.window = opt.window;
    a.p_bf = opt.p_bf;

    ReleaseQueue queue = {calloc((size_t)n, sizeof(Release)), 0};
    Stats st = {0};
    st.last_mode = -1;
    st.alloc_ns = calloc((size_t)n, sizeof(uint64_t));
    CHECK(queue.items != NULL && st.alloc_ns != NULL);

    int live_blocks = 0;
    double random_u = 0;

    for (int k = 0; k < n; ++k) {
        int measured = k >= opt.warmup;

        /* 1. Releases due at or before this tick. */
        while (queue.count > 0 && queue.items[0].time <= req[k].arrival) {
            Release due = queue_pop(&queue);
            uint64_t t0 = opt.check ? 0 : now_ns();
            CHECK(release_block(&a, due.pid) == 0);
            if (!opt.check && measured) {
                st.release_ns_total += now_ns() - t0;
                ++st.releases_timed;
            }
            --live_blocks;
            if (opt.check)
                check_invariants(&a);
        }

        /* 2. The arrival. Only allocate_block() is inside the timed interval. */
        if (k % opt.window == 0)
            CHECK(fscanf(uniforms, "%lf", &random_u) == 1);

        uint64_t t0 = opt.check ? 0 : now_ns();
        AllocResult r = allocate_block(&a, k, req[k].size, random_u);
        uint64_t elapsed = opt.check ? 0 : now_ns() - t0;

        if (r.status == ALLOC_OK) {
            queue_push(&queue, (Release){k + req[k].lifetime, k});
            ++live_blocks;
        }
        if (opt.check) {
            check_invariants(&a);
            if (r.status == ALLOC_OK)
                CHECK(a.blocks[k].size == req[k].size);
        }

        /* 3. Observe the state after the attempt. */
        int largest = largest_hole(&a);
        if (measured) {
            st.frag_failures += r.status == FAIL_FRAGMENTATION;
            st.cap_failures += r.status == FAIL_CAPACITY;
            if (a.free_total > 0)
                st.dispersion_sum += 1.0 - (double)largest / a.free_total;
            else
                ++st.dispersion_undefined;
            st.utilization_sum += (double)(a.region_size - a.free_total) / a.region_size;
            st.holes_sum += a.hole_count;
            st.search_probes += r.search_probes;
            st.selector_probes += r.selector_probes;
            st.bf_attempts += r.mode == BF;
            st.live_blocks_sum += live_blocks;
            if (st.last_mode >= 0 && st.last_mode != (int)r.mode)
                ++st.switches;
            st.last_mode = (int)r.mode;
            st.alloc_ns[st.timed++] = elapsed;
        }

        if (raw) {
            fprintf(raw, "%d,%d,%d,%d,%d,%s,%d,%d,%d,%s,", k, k, k, req[k].size,
                    r.status == ALLOC_OK, status_name(r.status), r.start,
                    r.search_probes, r.selector_probes, policy_names[r.mode]);
            if (!opt.check)
                fprintf(raw, "%llu", (unsigned long long)elapsed);
            fprintf(raw, ",%d,%d,%d,%d,%d\n", !measured, a.free_total, largest,
                    a.hole_count, a.region_size - a.free_total);
        }
    }

    qsort(st.alloc_ns, (size_t)st.timed, sizeof(uint64_t), compare_u64);
    print_summary(&st, n - opt.warmup);

    if (raw) {
        CHECK(!ferror(raw));
        CHECK(fclose(raw) == 0);
    }
    fclose(uniforms);
    allocator_destroy(&a);
    free(req);
    free(queue.items);
    free(st.alloc_ns);
    return 0;
}
