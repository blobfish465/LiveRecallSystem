# Team Contributions

> **Draft for the team call** (2 Nov, drafted from git history, `git blame` and `HANDOFF.md`). Percentages are agreed on the call; edit freely.

| Member | Main responsibilities | Experiments/report sections | Agreed contribution % |
|---|---|---|---:|
| lihloway | Server core: revision state, bounded history, HISTORY/resume/late join, per-client channel skeleton; test suite + `check.py` gate; Python 3.10 support; environment/timer-drift diagnosis; team process (`CLAUDE.md`, relay plan, handoff format); README structure and design notes; submission | Naive-baseline and Day 1 numbers; report: architecture & protocol, revision/recovery semantics | TBD |
| muhdbhz | Slow-reader backpressure (overload policy v1): provisional coalescing, pong-based lag detection, finals-only mode, recovery with backoff, hopeless-client close; plain-FIFO baseline switch for before/after; `[stats]` totals; channel tests; T3 gate | Day 2 T3 experiments; charts; report: overload policy, failures & trade-offs; README overload notes; team AI_USAGE sections | TBD |
| QiAn | Browser client (in-place revisions, local recall, reconnect/resume, restart detection) + browser e2e tests; experiment tooling: `bandwidth_test.py`, `convergence_test.py`, `check.py --stress`, RSS/`[stats]` capture, tool tests | Constrained-bandwidth finding (Day 2 policy lost to plain FIFO), stress and convergence studies, Day 3 VM medians; demo video; report: client/usability, limitations, future work; README demo section | TBD |
| Frankfurters | Overload policy v2 for slow links: ping budget, no stop-and-wait, slow-link vs slow-reader classification from the kernel's TCP RTT, jittered recovery, small send buffers; two fixes found by the reference-VM runs (T3 tail-final loss, T5 false congestion detection); channel tests | All final experiments on the reference VM (T1–T5 ×3, bandwidth, convergence, stress, 5-min memory, T3 before/after); curated results data (`report/data/`); README test commands; report: experiments & results, final assembly | TBD |

Total contribution should be 100%.

## Attribution by component

Main author first; supporting evidence in brackets. Lines in `solution_server.py` and `client/` from the initial commit are the instructor's starter code and are not counted for anyone.

| Part of the project | Main author | Contributors | Evidence |
|---|---|---|---|
| Caption state: revision rules, durable finals, duplicate/stale rejection (`liverecall/state.py`) | lihloway | – | all lines; Day 1 |
| Bounded history + HISTORY: resume, late join, atomic snapshot + register | lihloway | – | `state.py`, `solution_server.py`; Day 1 |
| Server wiring (`solution_server.py`) | lihloway | muhdbhz (baseline flag, `[stats]` totals, `write_limit`), Frankfurters (kernel TCP RTT, small send buffers) | Days 1, 2, 4 |
| Per-client channel skeleton: bounded queue, sender task, never drops finals | lihloway | – | `channel.py`; Day 1 |
| Overload policy v1 (slow readers) | muhdbhz | – | Day 2 commits |
| Overload policy v2 (slow links) + VM-run fixes | Frankfurters | builds on muhdbhz's v1 | Day 4 parts 1–2 |
| Before/after baseline switch (`LIVERECALL_NO_COALESCE`) | muhdbhz | – | Day 2 |
| Browser client (`client/`) | QiAn | – | Day 3 |
| Unit and server tests (`test_state.py`, `test_server.py`) | lihloway | muhdbhz | Days 1–2 |
| Channel tests (`test_channel.py`) | muhdbhz | Frankfurters, lihloway | Days 1, 2, 4 |
| Browser e2e tests (`test_client.py`) | QiAn | – | Day 3 |
| `check.py` regression gate (T1–T4, late join) | lihloway | QiAn (stress, RSS/`[stats]` capture), muhdbhz (T3 gate) | Days 1–3 |
| Bandwidth, convergence and stress tools | QiAn | – | Day 3 |
| Final experiments on the reference VM, curated data | Frankfurters | – | Day 4 part 2, `report/data/` |
| Earlier experiments (naive baseline, Day 2 T3, Day 3 bandwidth/stress/convergence) | lihloway, muhdbhz, QiAn | – | `HANDOFF.md` Days 1–3 |
| Timer-drift check / environment diagnosis | lihloway | – | Day 1 |
| Team process docs (`CLAUDE.md`, relay plan, `HANDOFF.md` format) | lihloway | everyone (own sections) | Day 1 |
| README | lihloway | Frankfurters (test commands), muhdbhz (overload notes), QiAn (browser, demo) | per allocation |
| Report | split by section | Frankfurters assembles | per allocation |
| Demo video / submission | QiAn / lihloway | – | per allocation |

## Notes on shared work

- **The overload policy is layered work.** muhdbhz built v1 for slow readers. QiAn's bandwidth experiment showed it lost to a plain FIFO on a slow link. Frankfurters redesigned it (v2) and fixed it after the reference-VM runs.
- **Everyone wrote their own `HANDOFF.md` section and `AI_USAGE.md` entry.**
- **Git history is supporting evidence only.** Line counts measure code volume, not effort: experiment runs, debugging and write-ups barely show up in them.
