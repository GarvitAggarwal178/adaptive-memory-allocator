#ifndef CHECK_H
#define CHECK_H

#include <stdio.h>
#include <stdlib.h>

/*
 * Like assert(), but never compiled out by NDEBUG. Use it for checks whose
 * expression has side effects (I/O, releases) or must hold in timed builds.
 */
#define CHECK(expr)                                                        \
    do {                                                                   \
        if (!(expr)) {                                                     \
            fprintf(stderr, "check failed: %s (%s:%d)\n", #expr, __FILE__, \
                    __LINE__);                                             \
            abort();                                                       \
        }                                                                  \
    } while (0)

#endif
