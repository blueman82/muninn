# Experimental Codex learning qualification

Automatic learning retains its accepted ongoing operating authority. The audit
has not revoked that authority. Recall modes remain independent from learning.
The four accepted uncertainties in `CodexExperiment` remain applicable.

## Current dispositions

| Accepted uncertainty | Supported evidence | Remaining acceptance gap |
| --- | --- | --- |
| `posthoc-actions` | The streaming regression sends each reported action type through a real owned worker. The monitor terminates it before provider completion and discards output. A synthetic side effect before the report remains observable. | Detection and containment do not prove that an action was prevented. |
| `unknown-requests-and-billing` | Decoder and durable budget tests preserve unknown usage and distinguish reservations from reported cost. The local probe counts requests received by its synthetic endpoint. | Local request counts do not establish internal provider requests or attributable billing. No new provider spend grant is inferred. |
| `incomplete-tool-source-isolation` | The native probe inspects both top-level tools and `additional_tools` in the actual request produced by the selected CLI. It tests disposable config, rules and instruction canaries. | Missing canaries do not prove complete source isolation. Advertised deferred tools remain a qualification failure even when top-level tools are empty. Authentication and managed configuration outside the disposable scope remain unverified. |
| `deferred-C6-assessment` | The assessment contracts retain frozen settings, independent labels and judgments, required hosts, finite shared trial accounting, and explicit unknown results. | A fresh authorized assessment, independent labels, untouched heldout families, approved tolerances/sample sizes, required native hosts and independent final results remain required. Existing ongoing production authority does not create a new finite trial authorization. |

## Evidence boundaries

The no-inference probe uses an explicitly selected local executable, disposable
homes, synthetic content and a loopback fake Responses endpoint. That endpoint
rejects requests rather than invoking a model. This measures request construction
on that host and executable. The direct CLI probe does not exercise production
capability admission. Production refuses a nonempty CLI working directory. A
working-directory instruction canary is a diagnostic control outside that
admitted state. It does not establish behavior of a remote provider,
model compliance, billable request counts or universal absence of tool actions.

Run the local probe with an explicit Codex executable and a new report path:

```sh
.venv/bin/python3.13 -m tools.learning_eval.codex_tool_probe \
  --codex /absolute/path/to/codex --output /private/tmp/new-tool-probe.json
```

The output retains executable and request digests, observed tool names,
configuration variants and limits. A new executable requires fresh measurement.
A signature check establishes provenance, not isolation. Accepted ongoing policy
may permit a signed upgrade while qualification of its behavior remains unknown.

Initial characterization of the signed macOS arm64 Codex CLI `0.164.0` found
`functions.exec` and collaboration tools in `additional_tools`. Disabling selected
tool-related feature flags did not remove those advertised tools. A broader
restriction set also produced a Code Mode error. These observations do not justify
changing the production recipe to claim tool prevention. Raw reports belong in
the campaign's durable evidence, bound to their exact executable and candidate.

The empty-working-directory auth canary also appeared in the request as an
`AGENTS.md` instruction. `--ignore-user-config` suppressed the distinct config
canary. Removing the ignore flags exposed that config canary as a positive
control. The tested `project_doc_max_bytes=0` override did not suppress the
auth-home instruction canary. A rules-file comment canary was not observed, but
that is insufficient evidence that execution rules were excluded. These results
leave source isolation unverified. The probe provides no ambient credentials
and forces file-based credential storage. It does not instrument all credential
access or external network activity.

Process cleanup attempts a group signal before reaping the owned leader. A
successful signal and leader exit do not prove that every descendant or in-flight
write has stopped. The report leaves descendant quiescence unverified. It does
not use that cleanup sequence as proof of isolation or complete termination.

OpenAI documents that read-only automation can still read files and execute
commands. It documents `--ignore-user-config`, `--ignore-rules` and `--ephemeral`
as separate controls. Those flags do not by themselves establish complete tool
or source isolation. See [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[security](https://learn.chatgpt.com/docs/agent-approvals-security) and
[configuration](https://learn.chatgpt.com/docs/config-file/config-sample).

## Qualification decision

Status remains **unverified**. The executable probe and regression tests reduce
uncertainty about the tested failure paths. They do not complete C6 or authorize a
new trial. Keep every unresolved requirement visible in the release and final
audit. A passing repository gate, hosted CI or signer check cannot replace native
qualification. No accepted uncertainty should be deleted merely to clear a
warning or produce an acceptance claim.
