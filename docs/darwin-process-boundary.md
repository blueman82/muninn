# Darwin process cleanup: qualification boundary

Draft decision for independent review, 26 October 2026. Production source basis:
`ab736316cfb9cfbbb8a9a6df169a348c75fd2801`. No production behavior changes are
proposed here. One approved Stage A direct-child experiment completed on this
host. Stage B has not been executed.

## Decision under current evidence

Retain the explicitly unqualified Darwin boundary: the current observation covers
the owned process group's current membership and its unreaped terminal leader.
It does not prove closed-session quiescence or exclude a later same-session
process joining that group. No supported primitive establishing those stronger
properties was identified among the public APIs examined. This is a bounded
research result, not a claim that every possible Darwin mechanism is unsuitable.

## What the implementation observes

[`sole_zombie`](../muninn/review_process_posix.py) requests the owned PGID's
members through `proc_listpids`, using two PID slots. It requires exactly one
returned PID equal to the leader. It then requires an exact-child terminal
`waitid` observation with `WEXITED | WNOHANG | WNOWAIT`. The wait does not reap.
Membership and terminal status are separate observations; they are not a single
atomic session snapshot.

[`group_quiescent`](../muninn/review_process_quiescence.py) uses that Darwin
predicate. [`contain`](../muninn/review_process.py) and the test runner's
[`_contain`](../tools/test_process.py) signal the original owned PGID and retain
the leader through observation before waiting to reap. A late actual wait cannot
be undone even when the runner subsequently refuses cleanup success. These
contracts do not authorize signaling another PGID or an individual escaped PID.

The Linux branch is different. It checks visible procfs, terminal membership
across the leader's whole SID, two membership reads, and an unchanged global fork
counter bracket ordered with non-consuming terminal-leader observations. Its
proof conditions must not be attributed to the Darwin branch.

## Public API evidence and its limits

Research examined Apple's public XNU commit
`ac9718fb1af618d5ce8678d0dc6e8a58f252216f` (`xnu-12377.121.6`). Recorded host
metadata instead names `xnu-13432.1.9~1` on macOS 27.0.1, build 26A434, arm64,
with SDK 27.0. The older public implementation is not an attestation of the
running kernel.

In that public source, group enumeration traverses live and zombie lists under
the process-list lock. A retained zombie remains a member until reap. The
same-session `setpgid` checks motivate a possible later join by a member outside
the observed group. They do not establish that the proposed transition succeeds
on this host. See Apple's pinned [group enumeration](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/proc_info.c#L401-L475),
[reap path](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/kern_exit.c#L2967-L2970),
and [same-session join checks](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/kern_prot.c#L574-L640).

The examined public libproc selectors expose PGID, not SID. A header constant
for `KERN_PROC_SESSION` does not supply a working handler: the examined sysctl
dispatch has no SESSION case. PID listing followed by later metadata reads is
not an atomic SID-bearing observation. See [public selectors](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/sys/proc_info.h#L51-L99)
and [sysctl dispatch](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/kern_sysctl.c#L881-L908).

`kern.num_tasks` refers to the configured `task_max = CONFIG_TASK_MAX` ceiling,
not a cumulative process-birth counter. Current task/process populations can
decrease. The research established no supported public global birth-counter
read with the required publication ordering. Repeated equal finite lists or
population values therefore supply no closed-session proof. See
[sysctl binding](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/kern_sysctl.c#L2479-L2481)
and [configured ceiling](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/kern/task.c#L522).

## Stage A: observed exit and reap of one direct child

The experiment registered a plain per-PID `EVFILT_PROC/NOTE_EXIT` watch while
a known direct child was held alive, released it to exit cooperatively, observed
its matching native exit event, and separately waited to reap it. Apple's
[kevent manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kevent.2.html)
describes the interface; the pinned implementation [rejects fork-tracking flags](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/bsd/kern/kern_event.c#L1095-L1150).
An exit event for one known process is not descendant tracking, orphan cleanup,
or session closure. An ACK, EOF, timeout, or registration error cannot replace it.

Historical source reviews refused the initial drafts for fallback identity,
cooperative fallback/reap budgets, and post-operation deadline acceptance.
Session 03 verified those corrections in v3 and retained source-review GO for
the v4 manifest; the v4 change only made a mocked test portable. The historical
NO-GOs remain preserved. Root then authorized one unchanged, exact Stage A run.

That invocation exited 0. The log records verified PID=PGID=SID 30688,
registration while live, one matching native event (`EVFILT_PROC`, `NOTE_EXIT`),
and a separate actual wait returning status 0. Readiness, registration, event
and normal reap were all recorded on time. The result reports cleanup complete,
no fallback signal, no ownership abandonment and zero evidence errors, in
0.034610125 seconds. Source SHA256 before and after was
`4ba894ac859c38843dfa9f34996abc6e348c9917f94c3790e891c84046b4b2f8`.
This supplies positive evidence for one known directly owned child on this host;
it does not qualify production callers, orphan handling, Stage B or whole-SID
closure. It does not remove the native-stall risk documented in the source review.

Durable evidence locators are relative to the campaign coordination directory's
`followup-wave-2026-10-26/`:

- `02-approved-darwin-stage-a-run.json`: single-run authorization.
- `03-peer-02-stage-a-v3-source-review.md` and
  `03-peer-02-stage-a-v4-source-review.md`: correction and exact-source reviews.
- `02/stage-a-source-manifest-v4.json`: reviewed frozen inputs.
- `02/stage-a-approved-exit.json`: actual executor exit and unchanged source binding.
- `02/stage-a-native.log` and `02/stage-a-result.json`: native event, wait and
  cleanup facts. These are retained evidence, not an instruction to repeat the run.

## Stage B: proposed late-rejoin falsifier

This later experiment is not execution-ready:

1. Create an owned session leader L and a cooperative peer P. Verify identities,
   then park P in PGID=P while retaining SID=L behind bounded pipe barriers.
2. Allow L to exit and retain it unreaped. Signal only original PGID=L if needed.
   Require the actual unmodified observer to return true while P remains live
   outside that group.
3. Release P to call `setpgid(0, L)`. Verify unchanged identity, SID=L, PGID=L and
   a fresh nonce-bound PING response. Require the actual observer to return false
   while L is still unreaped. False alone is insufficient: observation errors
   also refuse.
4. Require positive peer termination evidence before final leader reap. Preserve
   bounded cooperative EXIT, EOF and self-expiry paths; do not broaden signals.

A successful verified rejoin would falsify the inference that a sole-current-
group zombie observation excludes later same-session joins on this host.
An unsuccessful or incomplete experiment would not establish closure.

P becomes orphaned when L exits, so the external parent cannot directly reap P.
Stage A does not qualify event attachment/delivery across orphaning or safe
failure cleanup. A parked, unresponsive P cannot be forcibly signaled under the
original-PGID-only boundary. A genuine exit event could establish termination
without establishing reaping or complete orphan cleanup. Those ownership and
authority questions need a separate reviewed design before any Stage B launch.

Changing this qualification requires a supported primitive with an explicit
complete-session or ordered-publication contract and independently reviewed
evidence. Neither a proposed experiment nor this draft widens production
authority or closes the Darwin qualification item.
