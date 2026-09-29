/* Minimal native packaging glue; model logic stays in the portable backend. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], core[PATH_MAX], helper[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, resolved)) {
        fputs("Pair launcher path unavailable\n", stderr);
        return 1;
    }
    char *directory = dirname(resolved);
    if (snprintf(core, sizeof(core), "%s/../Resources/Core/PairCore", directory) >= sizeof(core) ||
        snprintf(helper, sizeof(helper), "%s/pair-keychain", directory) >= sizeof(helper)) {
        fputs("Pair bundle path too long\n", stderr);
        return 1;
    }
    setenv("PAIR_KEYCHAIN_HELPER", helper, 1);
    char **arguments = calloc((size_t)argc + 1, sizeof(char *));
    if (!arguments) return 1;
    arguments[0] = core;
    for (int i = 1; i < argc; i++) arguments[i] = argv[i];
    execv(core, arguments);
    fputs("Pair backend unavailable\n", stderr);
    free(arguments);
    return 1;
}
