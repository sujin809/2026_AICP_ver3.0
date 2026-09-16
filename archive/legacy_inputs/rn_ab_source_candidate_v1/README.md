# RN 100-agent source candidate v1

This directory was generated locally from the read-only source
`outputs/sys_100.db`. It is not copied from `data/sys_100.db` (that file is
zero bytes) and it does not use a runtime/checkpoint database.

The source database SHA-256 is
`f3c3756f4892f0824776804c3e2167dfc6ca6b250e1856671534d7cee05414ae`.
The generator verified that this digest was unchanged before and after the
snapshot build.

The sealed candidate contains exactly 100 agents with the frozen information
depth distribution D0/D1/D2 = 30/55/15. Only `persona_prompt` is rerendered
from structured fields. The repair manifest proves that no structured field or
depth assignment changed and that all 100 prompts round-trip through the
canonical renderer.

This is usable as a preflight input only when the StudySpec directly pins the
snapshot manifest and depth manifest hashes below. The snapshot manifest, in
turn, pins the ordered prompt map and snapshot database hashes:

- persona snapshot manifest:
  `7de05577a86536d72a565e9df63575e0ec5911eb81c311438e7b4b431adc4a8f`
- persona depth manifest:
  `7932fa2e610a43d7ce7624521b52045a69fb43bcca2ec246caff39196d9ff161`
- ordered persona prompt map:
  `58eee35098e28fee89e760453bc619ebbc018c948a4496fd2f77dcdbd09c7738`
- repaired snapshot database:
  `72f1814c5cf3a42eb1a6d840c1d04a5ce7812e67cffc272071b320a11ae22939`

No human approval, network request, or paid API call is claimed. Identity and
provenance are established by the source hash, deterministic renderer, exact
schema validation, and StudySpec hash pinning.

Revalidate locally:

```bash
PYTHONPATH=. python3.12 scripts/10_prepare_rn_ab_source_personas.py validate \
  --snapshot-dir preparation/rn_ab_source_candidate_v1/persona_snapshot
```
