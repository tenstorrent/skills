# NoC, DRAM and Ethernet bandwidth columns (tt-npe)

`NOC UTIL (%)`, `MULTICAST NOC UTIL (%)`, `DRAM BW UTIL (%)`,
`DRAM BW UTIL PER CTRL (%)`, `ETH BW UTIL (%)` and `NPE CONG IMPACT (%)` in the
ops-perf CSV come from tt-npe, a separate NoC performance estimator that
replays recorded NoC events. A default profile does not fill them.

Blank means not measured, not idle. Do not read a blank column as "no
traffic".

## Fill them

```bash
# once: build tt-npe
git clone https://github.com/tenstorrent/tt-npe && (cd tt-npe && ./build-npe.sh)

# each shell: tt-metal venv first, then tt-npe's environment
source <tt-metal venv>/bin/activate
source <tt-npe>/ENV_SETUP

python -m tracy -p -r --collect-noc-traces -m pytest <test> -k <filter>
python tools/tracy/process_ops_logs.py --analyze-noc-traces
```

`--collect-noc-traces` has the device record per-op NoC event traces.
`--analyze-noc-traces` runs tt-npe over them and fills the columns. If
post-processing warns `Could not import tt-npe module`, `ENV_SETUP` was not
sourced in that shell.

## Cost

NoC tracing recompiles kernels with event instrumentation and adds capture
overhead. Turn it on for a bandwidth question, not for every profile.

## Which column answers what

| Question | Column | Kind |
|---|---|---|
| Was the matrix or vector engine busy? | `Avg FPU / SFPU / Math util on full grid (%)` | measured counters (`perf-counters.md`) |
| Which RISC bounds the op? | per-RISC kernel durations | measured (`op-report.md`) |
| Is the NoC, DRAM or Ethernet saturated? | the columns above | tt-npe, from recorded NoC traffic |
| What did the op's perf model predict? | `PM FPU UTIL (%)` and other `PM` columns | estimate; never decide a bound on it |
