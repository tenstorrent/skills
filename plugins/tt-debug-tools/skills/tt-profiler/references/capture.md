# Capture paths and post-processing

Three ways in, and they are not alternatives so much as layers.

## 1. The environment variable alone

```bash
TT_METAL_DEVICE_PROFILER=1 ./your_program
```

Device zones only, written to the CSV at device close. No host-side timeline, no
GUI, nothing to install. This is the one to reach for when you want numbers
rather than a picture.

## 2. `python -m tracy`

The Python wrapper. It sets `TT_METAL_DEVICE_PROFILER=1` itself and captures host
and device together, so you get one timeline rather than two artifacts.

```bash
python -m tracy your_script.py
python -m tracy -m pytest path/to/test.py::test_name
python -m tracy -p -l -m pytest path/to/test.py::test_name
```

After a default capture run it starts a Tracy WASM web viewer in the background.
Viewing that from another machine needs **both** ports forwarded — 8080 for HTTP
and 8081 for the WebSocket — to the same ports on the remote loopback interface.
Forwarding only 8080 gives a page that loads and never connects.

## 3. `tracy-capture`

The client that writes a `.tracy` file for later, rather than serving a viewer.
Built with Tracy enabled, and it must be started **before** the application.

```bash
./build/tools/profiler/bin/tracy-capture -o run.tracy
```

The output compresses well; compress before copying it anywhere.

## Post-processing knobs

All read at runtime, all optional:

| Variable | Effect |
|---|---|
| `TT_METAL_DEVICE_PROFILER_DISPATCH` | Include the dispatch cores, whose zones are otherwise absent |
| `TT_METAL_PROFILER_MID_RUN_DUMP` | Dump during the run instead of only at close |
| `TT_METAL_PROFILER_SUM` | Summed output |
| `TT_METAL_PROFILER_ACCUMULATE` | Accumulate across runs |
| `TT_METAL_PROFILER_SYNC` | Host/device clock sync, needed for a meaningful joint timeline |
| `TT_METAL_PROFILER_CPP_POST_PROCESS` | Post-process in C++ rather than Python |
| `TT_METAL_PROFILER_DISABLE_DUMP_TO_FILES` | Skip the CSV |
| `TT_METAL_PROFILER_DISABLE_PUSH_TO_TRACY` | Skip the Tracy push |
| `TT_METAL_PROFILER_TRACE_TRACKING` | Track traced execution, which populates the `trace id` columns |
| `TT_METAL_PROFILER_PROGRAM_SUPPORT_COUNT` | How many programs the buffer is sized for |

`_DISABLE_DUMP_TO_FILES` and `_DISABLE_PUSH_TO_TRACY` together leave the
instrumentation running and no output anywhere — a combination worth
double-checking if a profiled run produces nothing.

## Reading results early

Results are collected when the device closes. Past roughly a thousand kernel
runs the on-device buffer needs draining sooner:

```c++
tt::tt_metal::detail::ReadDeviceProfilerResults(device);
```

Place it after the program you care about. It signals the device to sync results,
which then reach both the CSV and the Tracy client.

`TT_METAL_PROFILER_MID_RUN_DUMP` is the environment-variable equivalent for a
long-lived process — a serving job that never closes its device otherwise never
writes a profile.
