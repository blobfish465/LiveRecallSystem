# Team Contributions

> Drafted from git history, `git blame` and `HANDOFF.md` (2 Nov), updated 3 Nov. Responsibilities and percentages are confirmed on the team call.

| Member | Main responsibilities | Experiments/report sections | Agreed contribution % |
|---|---|---|---:|
| Tay Cole Lin (A0210841U, `lihloway`) | Server core: revision state, bounded history, HISTORY/resume/late join, per-client channel skeleton. Test suite + `check.py` gate. Hidden-condition testing (`hidden_sim.py`, `custom_source.py`: 25 scenarios) and the fixes it led to: history cap + HISTORY byte cap, lag memory across reconnects, slow-reader reclassification on slow links, TCP segment cap. Python 3.8–3.14 support. Environment/timer-drift diagnosis. Team process (`CLAUDE.md`, relay plan, handoff format). README read-through and design notes; this statement; submission | Naive-baseline and Day 1 numbers; hidden-variation matrix (WSL2, median of 3); report: architecture & protocol, revision/recovery semantics | 25* |
| Muhammad Bin Hamzah (A0272938N, `muhdbhz`) | Slow-reader backpressure (overload policy v1): provisional coalescing, pong-based lag detection, finals-only mode, recovery with backoff, hopeless-client close. Plain-FIFO baseline switch for before/after; `[stats]` totals; channel tests; T3 gate | Day 2 T3 experiments; report figures (Figures 1–3 diagrams, Figures 4–6 charts via `report/make_charts.py`); report: overload policy, failures & trade-offs; README overload notes | 25* |
| Ong Qi An (A0281202W, `QiAn`) | Browser client (in-place revisions, provisional/final badges, local recall, reconnect/resume, restart detection) + browser e2e tests. Experiment tooling: `bandwidth_test.py`, `convergence_test.py`, `check.py --stress`, RSS/`[stats]` capture, tool tests | Constrained-bandwidth finding (Day 2 policy lost to plain FIFO), stress and convergence studies, Day 3 VM medians; demo video (run sheet, recording, editing); report: client/usability, limitations, future work incl. the hidden-test table; README browser and demo sections | 25* |
| Nicholas Kee (A0308575W, `Frankwotfurters`) | Overload policy v2 for slow links: ping budget, no stop-and-wait, slow-link vs slow-reader classification from the kernel's TCP RTT, jittered recovery, small send buffers. Two fixes found by the reference-VM runs (T3 tail-final loss, T5 false congestion detection); channel tests. Reference-environment decision; first draft of this statement | All final experiments on the reference VM (T1–T5 ×3, bandwidth, convergence, stress, 5-min memory, T3 before/after on the final code); curated results data (`report/data/`); README test commands; report: experiments & results, final assembly and PDF export | 25* |

Total: 100%.

\* Provisional equal split, to be confirmed on the team call.

In the rest of this document members are referred to by GitHub handle; "Frankfurters" is Nicholas Kee (`Frankwotfurters`).

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
| Policy fixes from hidden-condition tests: lag memory across reconnects, slow-reader reclassification on slow links, TCP segment cap (large HISTORY on a shaped link), history cap + HISTORY byte cap | lihloway | builds on v1/v2 | 2–3 Nov, `HANDOFF.md` "Update 2 Nov (2)/(3)" |
| Python 3.8 compatibility (server path, per-version `requirements.txt`) | lihloway | – | 2–3 Nov |
| Before/after baseline switch (`LIVERECALL_NO_COALESCE`) | muhdbhz | – | Day 2 |
| Browser client (`client/`) | QiAn | – | Day 3 |
| Unit and server tests (`test_state.py`, `test_server.py`) | lihloway | muhdbhz | Days 1–2 |
| Channel tests (`test_channel.py`) | muhdbhz | Frankfurters, lihloway | Days 1, 2, 4 |
| Browser e2e tests (`test_client.py`) | QiAn | – | Day 3 |
| `check.py` regression gate (T1–T4, late join) | lihloway | QiAn (stress, RSS/`[stats]` capture), muhdbhz (T3 gate) | Days 1–3 |
| Bandwidth, convergence and stress tools | QiAn | – | Day 3 |
| Hidden-variation matrix (`hidden_sim.py`, `custom_source.py`) and its results | lihloway | – | 2–3 Nov |
| Final experiments on the reference VM, curated data | Frankfurters | – | Day 4 part 2, `report/data/` |
| Earlier experiments (naive baseline, Day 2 T3, Day 3 bandwidth/stress/convergence) | lihloway, muhdbhz, QiAn | – | `HANDOFF.md` Days 1–3 |
| Timer-drift check / environment diagnosis | lihloway | – | Day 1 |
| Report figures and charts (`report/figures/`, `report/make_charts.py`) | muhdbhz | Frankfurters (results data) | 3 Nov |
| Team process docs (`CLAUDE.md`, relay plan, `HANDOFF.md` format) | lihloway | everyone (own sections) | Day 1 |
| README | lihloway | Frankfurters (test commands), muhdbhz (overload notes), QiAn (browser, demo) | per allocation |
| `AI_USAGE.md` | everyone (own entries) | lihloway drafted the final team sections | per allocation |
| Report | split by section | Frankfurters assembles | per allocation |
| Demo video | QiAn | everyone reviews before submission | per allocation |
| Submission (zip, fresh-unzip test, Canvas upload) | lihloway | – | per allocation |

## Notes on shared work

- **The overload policy is layered work.** muhdbhz built v1 for slow readers. QiAn's bandwidth experiment showed it lost to a plain FIFO on a slow link. Frankfurters redesigned it (v2) and fixed it after the reference-VM runs. lihloway's hidden-condition tests then found three remaining failure modes (a slow reader re-flooded after reconnecting, a slow reader on a slow link, a large HISTORY stalling on the shaped link) and fixed them.
- **Everyone wrote their own `HANDOFF.md` section and `AI_USAGE.md` entry.**
- **Git history is supporting evidence only.** Line counts measure code volume, not effort: experiment runs, debugging and write-ups barely show up in them.
