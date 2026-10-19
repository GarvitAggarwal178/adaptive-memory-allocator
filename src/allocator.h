/*
 * Contiguous dynamic-partition allocator over a simulated region of M units
 * (one unit = 1 KiB). Supports First, Best, Worst and Next Fit, an adaptive
 * FF/BF selector, and a random FF/BF control used as a baseline.
 */
#ifndef ALLOCATOR_H
#define ALLOCATOR_H

#include "check.h"

typedef enum { FF, BF, WF, NF, ADAPTIVE, RANDOM_SWITCH, POLICY_COUNT } Policy;

typedef enum { ALLOC_OK = 0, FAIL_CAPACITY = 1, FAIL_FRAGMENTATION = 2 } AllocStatus;

/* A free hole [start, start + size). The hole array is kept sorted by start. */
typedef struct {
    int start;
    int size;
} Hole;

/* One slot per process id; active is 1 while the block is allocated. */
typedef struct {
    int start;
    int size;
    int active;
} Block;

typedef struct {
    AllocStatus status;
    int start;            /* address of the block, or -1 on failure */
    int search_probes;    /* holes examined by the fit search */
    int selector_probes;  /* holes examined by the adaptive statistics scan */
    Policy mode;          /* fit policy that served this attempt */
} AllocResult;

typedef struct {
    int region_size;      /* M */
    int free_total;       /* T, kept up to date on every split and merge */
    int max_pid;

    Hole *holes;
    int hole_count;
    Block *blocks;

    Policy policy;        /* what the user asked for */
    Policy mode;          /* fit policy currently in use (differs for ADAPTIVE / RANDOM_SWITCH) */
    int rover;            /* Next Fit: end address of the last allocation */

    /* Adaptive and random-control parameters. */
    int attempt;          /* attempts so far, failures included */
    int window;           /* W: re-decide the mode every W attempts */
    double theta;         /* choose BF when dispersion F >= theta ... */
    double phi;           /* ... or when free fraction T/M < phi */
    int hysteresis;       /* if set, lower theta by 0.1 while already in BF */
    double p_bf;          /* RANDOM_SWITCH: probability of choosing BF per window */
} Allocator;

extern const char *const policy_names[POLICY_COUNT];

void allocator_init(Allocator *a, int region_size, int max_pid, Policy policy);
void allocator_destroy(Allocator *a);

/* Index of the chosen hole for a request, or -1 if none fits. Sets *probes. */
int fit_search(const Hole *holes, int hole_count, int size, Policy mode, int rover, int *probes);

/* random_u is a uniform [0,1) draw, used only by RANDOM_SWITCH at window starts. */
AllocResult allocate_block(Allocator *a, int pid, int size, double random_u);

/* Returns 0 on success, -1 if pid is not currently allocated. */
int release_block(Allocator *a, int pid);

int largest_hole(const Allocator *a);

/* Aborts unless free holes and live blocks exactly tile [0, M). */
void check_invariants(const Allocator *a);

#endif
