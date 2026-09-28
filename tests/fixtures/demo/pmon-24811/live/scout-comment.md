## SONiC Scout: PR-CI coverage advisory

> Advisory only: Scout never blocks a merge. **Facts** are computed from the tree and the pipeline definition. **Judgements** are a local model's reading of the evidence, checked in code but not facts.

`sonic-net/sonic-buildimage` `1e7ebe495..3589b565d` (range) · detector D6, CI coverage gap · run **complete** · model `qwen2.5:7b-instruct` · 1 of 1 question(s) put, 1 answered

### 1. Change materially affects 14 platform(s) PR CI never builds

**Fact** (proven from the tree; severity high): Of 38 platform(s) this change reaches, 24 declare an ASIC family one of the 5 PR-CI job groups builds and 14 declare only families none of them builds (barefoot, broadcom-dnx).

`device/arista/x86_64-arista_common/pmon_daemon_control.json` line(s) 3-6 at head (cause):

```text
    "enable_xcvrd_sff_mgr": true,
    "xcvrd": {
        "dom_temperature_poll_interval": 10
    }
```

**Never built by PR CI:** 14 platform(s), declaring `barefoot`, `broadcom-dnx`: `arista/x86_64-arista_7170_32c`, `arista/x86_64-arista_7170_32cd`, `arista/x86_64-arista_7170_64c`, `arista/x86_64-arista_7170b_64c`, `arista/x86_64-arista_7280cr3_32d4`, and 9 more.

**Judgement** (`qwen2.5:7b-instruct` output, not a fact; confidence high, score 0.685): **material**. Complete evidence over 14 platform(s).
- Group A, 4 platform(s): **material**, answered. "Added xcvrd.dom_temperature_poll_interval for barefoot platforms." (cites E1, E4)
- Group B, 10 platform(s): **material**, answered. "Added xcvrd.dom_temperature_poll_interval for broadcom-dnx platforms." (cites E7, E6)

<sub>Brief `b00f6c499d51` · prompt `4ab3b6c390fd` · 1 model call(s), 1706 tokens in, 117 out · checks fired: citation 0, cited job group 0, entity closure 0, question binding 0, rule consistency 0.</sub>
