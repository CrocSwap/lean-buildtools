#include <lean/lean.h>
#include <stdint.h>
#if defined(__APPLE__)
#include <mach/mach.h>
#else
#include <stdio.h>
#include <unistd.h>
#endif

LEAN_EXPORT lean_obj_res lean_buildtools_rss(void) {
    uint64_t bytes = 0;
#if defined(__APPLE__)
    mach_task_basic_info_data_t info;
    mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                  (task_info_t)&info, &count) == KERN_SUCCESS)
        bytes = info.resident_size;
#else
    unsigned long pages = 0;
    FILE *f = fopen("/proc/self/statm", "r");
    if (f) {
        if (fscanf(f, "%*s %lu", &pages) == 1)
            bytes = (uint64_t)pages * (uint64_t)sysconf(_SC_PAGESIZE);
        fclose(f);
    }
#endif
    return lean_io_result_mk_ok(lean_box_uint64(bytes));
}
