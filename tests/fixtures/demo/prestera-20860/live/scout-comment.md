## SONiC Scout: PR-CI coverage advisory

> Advisory only: Scout never blocks a merge. **Facts** are computed from the tree and the pipeline definition. **Judgements** are a local model's reading of the evidence, checked in code but not facts.

`sonic-net/sonic-buildimage` `46eb26ee1..d93686ede` (range) · detector D6, CI coverage gap · run **complete** · model `qwen2.5:7b-instruct` · 1 of 1 question(s) put, 1 answered

### 1. 5 of 10 platform(s) declaring marvell-prestera are never built for their CPU architecture

**Fact** (proven from the tree; severity high): No PR-CI job group builds marvell-prestera by name: marvell-prestera-arm64 builds marvell-prestera only for arm64; marvell-prestera-armhf builds marvell-prestera only for armhf. Of the 10 platform(s) this change reaches that declare it, BI-R3 and BI-R4, which rest on a naming convention rather than a declaration, find 5 built for their own CPU architecture and 5 not.

`azure-pipelines.yml` line(s) 81-85 at head (contract):

```text
      - name: marvell-prestera-arm64
        pool: sonicbld-arm64
        variables:
           PLATFORM_NAME: marvell-prestera
           PLATFORM_ARCH: arm64
```

**Never built by PR CI:** 5 platform(s), declaring `marvell-prestera`: `marvell/x86_64-marvell_db98cx8514_10cc-r0`, `marvell/x86_64-marvell_db98cx8522_10cc-r0`, `marvell/x86_64-marvell_db98cx8540_16cd-r0`, `marvell/x86_64-marvell_db98cx8580_32cd-r0`, `marvell/x86_64-marvell_rd98DX35xx-r0`.

**Rule answer** (BI-R3 and BI-R4, a naming convention, not a declaration; it stands unless contested with evidence): 5 built for their own architecture, 5 not.

**Judgement** (`qwen2.5:7b-instruct` output, not a fact; confidence high, score 0.508): **the model confirms the rule's answer for 3 of 3 group(s)**, and the rule's answer stands. Complete evidence over 10 platform(s).

| Group | Platforms | Rule answer | Model answer | Outcome |
| --- | --- | --- | --- | --- |
| A | 5 | not covered | not covered: "No job group builds marvell-prestera for amd64." | confirmed |
| B | 4 | covered by `marvell-prestera-arm64` | covered by `marvell-prestera-arm64`: "marvell-prestera-arm64 builds arm64 platforms." | confirmed; its job group checks out for family and architecture |
| C | 1 | covered by `marvell-prestera-armhf` | covered by `marvell-prestera-armhf`: "marvell-prestera-armhf builds armhf platforms." | confirmed; its job group checks out for family and architecture |

<sub>Brief `029b4284a8f1` · prompt `4ab3b6c390fd` · 1 model call(s), 1932 tokens in, 208 out · checks fired: citation 0, cited job group 0, entity closure 0, question binding 0, rule consistency 0.</sub>
