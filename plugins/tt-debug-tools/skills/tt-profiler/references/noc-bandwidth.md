# NoC, DRAM and Ethernet bandwidth columns (tt-npe)

`NOC UTIL (%)`, `MULTICAST NOC UTIL (%)`, `DRAM BW UTIL (%)`,
`DRAM BW UTIL PER CTRL (%)`, `ETH BW UTIL (%)` and `NPE CONG IMPACT (%)` in the
ops-perf CSV come from tt-npe, a separate NoC performance estimator that
replays recorded NoC events. A default profile does not fill them.

Blank means not measured, not idle.

## Fill them

```bash
# once: build tt-npe
git clone https://github.com/tenstorrent/tt-npe && (cd tt-npe && ./build-npe.sh)

# each shell: tt-metal venv first, then tt-npe's environment
source <tt-metal venv>/bin/activate
source <tt-npe>/ENV_SETUP

python -m tracy -p -r --collect-noc-traces -m pytest <test> -k <filter>
```

`--collect-noc-traces` has the device record per-op NoC event traces, and
`-r` post-processing then runs tt-npe over them itself and fills the columns:
no second step is needed. If post-processing warns `Could not import tt-npe
module`, `ENV_SETUP` was not sourced in that shell. To redo only the tt-npe
step later on existing logs, source `ENV_SETUP` and run
`python tools/tracy/process_ops_logs.py -o <output-dir> --analyze-noc-traces`.

## Cost

NoC tracing recompiles kernels with event instrumentation and adds capture
overhead. Turn it on for a bandwidth question, not for every profile.
